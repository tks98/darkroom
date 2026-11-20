"""Recombine a StarNet starless image with reduced stars, plus final polish.

stars    = unscreen(original, starless)      # so screen(starless, stars) == original
galaxy   = starless -> black point, midtone lift, saturation
           -> core compression (core)               [bright core: lower, keep detail]
           -> yellow/blue contrast inside the galaxy (yb)   [core warmer, arms bluer]
           -> masked unsharp mask on luminance (sharpen)
stars'   = stars ** gamma (optionally eroded) * star_strength
result   = screen(galaxy, stars') -> sky neutralization (neutral) -> crop

Port of the hand-run recombine.py; the maths is unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import tifffile
from PIL import Image
from scipy import ndimage as ndi


@dataclass
class FinishParams:
    gamma: float = 1.6
    star_strength: float = 0.95
    mid: float = 0.45
    sat: float = 1.2
    yb: float = 1.8
    core: float = 0.3                               # 0 = off; compresses the brightest part of the target
    sharpen: float = 0.8
    sharpen_radius: float = 2.0
    black: float = 0.02
    erode: int = 0
    neutral: bool = True
    crop: tuple[int, int, int, int] | None = None   # x, y, w, h
    crop_aspect: tuple[int, int] | None = None      # W:H; box chosen around the target (see aspect_box)
    crop_center: tuple[int, int] | None = None      # x, y; overrides the detected target centre
    crop_scale: float = 1.0                         # aspect crop: fraction of the largest box (1 = largest)
    stars: str = "reduce"                           # "reduce": starless + shrunk stars; "keep": no star removal


def load(p):
    try:
        x = tifffile.imread(p)
    except ValueError as e:      # tifffile needs imagecodecs for the LZW TIFF that StarNet2 writes
        if "imagecodecs" in str(e).lower():
            raise ValueError(f"cannot read {p}: {e} (pip install imagecodecs)") from None
        raise
    x = x.astype(np.float32)
    if x.shape[0] == 3 and x.ndim == 3:
        x = np.moveaxis(x, 0, -1)
    return x / (65535.0 if x.max() > 1.5 else 1.0)


def mtf(m, x):  # midtones transfer: maps m -> 0.5
    return ((m - 1) * x) / ((2 * m - 1) * x - m)


def screen(a, b):
    return 1 - (1 - a) * (1 - b)


def unscreen(orig, sl):
    """Star layer s such that screen(sl, s) == orig (clipped to 0..1)."""
    return np.clip((orig - sl) / np.maximum(1 - sl, 1e-4), 0, 1)


def saturate(img, k):
    L = img.mean(axis=-1, keepdims=True)
    return np.clip(L + (img - L) * k, 0, 1)


def galaxy_mask(L, sky, sky_sigma):
    """0 on sky, ramps to 1 on the galaxy (smooth, from the starless luminance)."""
    m = (ndi.gaussian_filter(L, 4) - sky - 2 * sky_sigma) / (6 * sky_sigma)
    return np.clip(m, 0, 1)[..., None]


CORE_KNEE = 0.55     # starless brightness (large scale) where core compression starts
CORE_SIGMA = 12.0    # px; scale that counts as "large" (the bulge, not its detail)


def compress_core(g, k):
    """Two-scale compression of the brightest part of the starless layer, for a galaxy core
    that the stretch has pushed close to white. The luminance is split into a smooth base
    (Gaussian, CORE_SIGMA) and the detail on top of it (L / base). Where the base is above
    CORE_KNEE, its excess is reduced by up to a factor (1 - k) at white, and the detail there is
    raised to the power 1 + k so it is not flattened with it. Colour ratios are kept (every
    channel is scaled by the same factor). Everything below the knee is untouched, apart from a
    6 px blend at the edge of the compressed area. k = 0 returns g unchanged."""
    if k <= 0:
        return g
    L = g.mean(-1) + 1e-6
    base = ndi.gaussian_filter(L, CORE_SIGMA)
    detail = L / np.maximum(ndi.gaussian_filter(L, CORE_SIGMA), 1e-6)
    over = np.clip((base - CORE_KNEE) / (1 - CORE_KNEE), 0, 1)
    newbase = np.where(base > CORE_KNEE, CORE_KNEE + (base - CORE_KNEE) * (1 - k * over ** 0.7), base)
    ratio = newbase * detail ** (1 + k) / L
    w = ndi.gaussian_filter((base > CORE_KNEE - 0.1).astype(np.float32), 6)
    ratio = 1 + w * (ratio - 1)
    return np.clip(g * ratio[..., None], 0, 1)


def star_layer(stars, gamma, star_strength, erode=0):
    s = stars.copy()
    for c in range(3):
        for _ in range(erode):
            s[..., c] = ndi.grey_erosion(s[..., c], size=(3, 3))
    return s ** gamma * star_strength


SKY_FALLBACK_PCT = 5      # no sky pixel: the sky colour comes from the faintest 5% of the mask


def neutralize(out, gm, log=print):
    """Shift the channels so the sky is grey. The sky colour is the per-channel median of every
    pixel outside the target mask. (Do not pre-select the darker pixels: that picks, per pixel,
    the moments where the noisiest channel dipped, and reports a colour cast that is not there.)
    When no pixel is outside the mask (a nebula that fills the frame), the faintest
    SKY_FALLBACK_PCT percent of the (smooth) mask stand in for the sky; when the mask is 1
    everywhere, neutralisation is skipped."""
    m = gm[..., 0]
    skypx = m < 0.01
    if not skypx.any():
        lo = float(np.percentile(m, SKY_FALLBACK_PCT))
        if not lo < 1:
            log("neutral: no part of the image counts as sky; sky neutralisation skipped")
            return out
        skypx = m <= lo
        log(f"neutral: no pixel is outside the target mask; using the faintest {SKY_FALLBACK_PCT}% "
            f"of the mask ({int(skypx.sum())} pixels) as the sky")
    bg = np.median(out[skypx], axis=0)
    out = np.clip(out - (bg - bg.mean()), 0, 1)
    log(f"sky RGB before neutralization {bg.round(4)}")
    return out


def crop(img, box):
    x, y, w, h = box
    return img[y:y + h, x:x + w]


def parse_crop(s: str) -> tuple[int, int, int, int]:
    parts = [int(v) for v in s.split(",")]
    if len(parts) != 4:
        raise ValueError(f"--crop wants x,y,w,h, got {s!r}")
    return tuple(parts)


def parse_aspect(s: str) -> tuple[int, int]:
    """'2:3' -> (2, 3): width:height, positive integers."""
    parts = s.split(":")
    try:
        w, h = (int(v) for v in parts)
    except ValueError:
        raise ValueError(f"aspect ratio wants W:H with whole numbers, got {s!r}") from None
    if w <= 0 or h <= 0:
        raise ValueError(f"aspect ratio wants positive numbers, got {s!r}")
    return w, h


def parse_crop_spec(s: str | None):
    """A crop setting as written in the config or on the command line.

    Returns ("none", None), ("box", (x, y, w, h)) or ("aspect", (W, H)).
    """
    t = (s or "none").strip()
    if t.lower() == "none":
        return "none", None
    if ":" in t:
        return "aspect", parse_aspect(t)
    try:
        box = parse_crop(t)
    except ValueError:
        raise ValueError(f'crop wants "none", "x,y,w,h" or an aspect ratio like "2:3", got {s!r}') from None
    if box[0] < 0 or box[1] < 0 or box[2] <= 0 or box[3] <= 0:
        raise ValueError(f"crop box needs x,y >= 0 and w,h > 0, got {s!r}")
    return "box", box


def parse_center(s: str) -> tuple[int, int]:
    try:
        x, y = (int(v) for v in s.split(","))
    except ValueError:
        raise ValueError(f'crop centre wants "x,y" in pixels, got {s!r}') from None
    return x, y


def target_center(starless) -> tuple[int, int]:
    """(x, y) of the brightest point of the starless image's mean-RGB luminance, blurred with a
    20 px Gaussian: the core of the galaxy or nebula rather than a leftover star."""
    L = ndi.gaussian_filter(starless.mean(-1), 20)
    y, x = np.unravel_index(int(np.argmax(L)), L.shape)
    return int(x), int(y)


def aspect_box(width: int, height: int, aspect: tuple[int, int], center: tuple[int, int], scale: float = 1.0):
    """Largest box of aspect W:H that fits in a width x height image, centred on ``center`` and
    clamped to the image. Integer maths: the limiting side is used in full, the other is
    floor(side * ratio); the origin is centre - size // 2. ``scale`` < 1 shrinks that box
    (for a target that is small in the frame)."""
    aw, ah = aspect
    if width * ah <= height * aw:          # width is the limiting side
        w, h = width, width * ah // aw
    else:
        w, h = height * aw // ah, height
    if scale != 1.0:
        w, h = max(1, int(w * scale)), max(1, int(h * scale))
    cx, cy = center
    x0 = min(max(cx - w // 2, 0), width - w)
    y0 = min(max(cy - h // 2, 0), height - h)
    return x0, y0, w, h


def sky_sigma(L, sky):
    """Noise of the sky: 1.4826 x the MAD of the fainter 60% of ``L`` around ``sky``, at least
    1e-3 (also when there is no fainter pixel, in an image of one level)."""
    faint = L[L < np.percentile(L, 60)]
    s = 1.4826 * np.median(np.abs(faint - sky)) if faint.size else 0.0
    return max(s, 1e-3) if np.isfinite(s) else 1e-3


def keep_stars(orig, p: FinishParams):
    """Finish without star removal, for targets that are made of stars (clusters): black point,
    midtone and saturation on the whole image. The star and galaxy settings do not apply."""
    g = np.clip((orig - p.black) / (1 - p.black), 0, 1)
    g = mtf(p.mid, g)
    g = saturate(g, p.sat)
    L = g.mean(-1)
    sky = np.median(L)
    return g, galaxy_mask(L, sky, sky_sigma(L, sky))


def recombine(orig, sl, p: FinishParams, log=print):
    if p.stars == "keep":
        out, gm = keep_stars(orig, p)
        if p.neutral:
            out = neutralize(out, gm, log)
        return _crop_out(out, orig, p, log, "stretched")
    stars = unscreen(orig, sl)

    g = np.clip((sl - p.black) / (1 - p.black), 0, 1)
    g = mtf(p.mid, g)
    g = saturate(g, p.sat)

    L = g.mean(-1)
    sky = np.median(L)                                     # most of the frame is sky
    gm = galaxy_mask(L, sky, sky_sigma(L, sky))

    g = compress_core(g, p.core)

    if p.yb != 1.0:
        # opponent axis: yb = (R+G)/2 - B ; push it away from its galaxy median
        yb = (g[..., 0] + g[..., 1]) / 2 - g[..., 2]
        w = gm[..., 0]
        inside = w > 0.5
        if inside.any():
            ref = np.median(yb[inside])
            d = (yb - ref) * (p.yb - 1) * w                 # extra yellow(+)/blue(-) shift
            g = np.clip(g + np.stack([d / 3, d / 3, -2 * d / 3], -1), 0, 1)
        else:
            log("yb: no pixel is inside the target mask; yellow/blue contrast skipped")

    if p.sharpen > 0:
        L = g.mean(-1, keepdims=True)
        hp = L - ndi.gaussian_filter(L, (p.sharpen_radius, p.sharpen_radius, 0))
        g = np.clip(g + p.sharpen * hp * gm, 0, 1)

    s = star_layer(stars, p.gamma, p.star_strength, p.erode)

    out = screen(g, s)

    if p.neutral:
        out = neutralize(out, gm, log)

    return _crop_out(out, sl, p, log, "starless")


def _crop_out(out, ref, p: FinishParams, log, ref_name: str):
    """Apply the crop setting; an aspect crop is centred on the brightest point of blurred ``ref``."""
    box = p.crop
    if box is None and p.crop_aspect:
        H, W = ref.shape[:2]
        center = p.crop_center or target_center(ref)
        box = aspect_box(W, H, p.crop_aspect, center, p.crop_scale)
        log(f"crop {p.crop_aspect[0]}:{p.crop_aspect[1]} around x={center[0]}, y={center[1]}"
            f"{' (given)' if p.crop_center else f' (brightest point of the blurred {ref_name} image)'}"
            + (f", scale {p.crop_scale:g}" if p.crop_scale != 1.0 else "")
            + f" -> {box[0]},{box[1]},{box[2]},{box[3]}")
    if box:
        H, W = out.shape[:2]
        x, y, w, h = box
        if x + w > W or y + h > H:
            raise ValueError(f"crop {x},{y},{w},{h} does not fit in the {W}x{H} image")
        out = crop(out, box)
    return out


def check_finite(out) -> None:
    """Refuse to save an image with NaN or infinite pixels (they would be written as garbage)."""
    bad = int(np.count_nonzero(~np.isfinite(out)))
    if bad:
        raise ValueError(f"the finished image has {bad} NaN or infinite value(s) "
                         f"({bad / out.size:.1%}); nothing was saved")


def save(out, base: Path, log=print):
    check_finite(out)
    tifffile.imwrite(str(base) + ".tif", (out * 65535 + 0.5).astype(np.uint16), photometric="rgb")
    Image.fromarray((out * 255 + 0.5).astype(np.uint8)).save(str(base) + ".jpg", quality=95)
    log(f"saved {base}.tif/.jpg {out.shape}")


def run(original: Path, starless: Path | None, base: Path, p: FinishParams, log=print):
    """``starless`` is not read when p.stars == "keep"."""
    out = recombine(load(original), None if p.stars == "keep" else load(starless), p, log)
    save(out, base, log)
