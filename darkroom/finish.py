"""Recombine a StarNet starless image with reduced stars, plus final polish.

stars    = unscreen(original, starless)      # so screen(starless, stars) == original
galaxy   = starless -> black point, midtone lift, saturation
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
    sharpen: float = 0.8
    sharpen_radius: float = 2.0
    black: float = 0.02
    erode: int = 0
    neutral: bool = True
    crop: tuple[int, int, int, int] | None = None   # x, y, w, h


def load(p):
    x = tifffile.imread(p).astype(np.float32)
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


def star_layer(stars, gamma, star_strength, erode=0):
    s = stars.copy()
    for c in range(3):
        for _ in range(erode):
            s[..., c] = ndi.grey_erosion(s[..., c], size=(3, 3))
    return s ** gamma * star_strength


def neutralize(out, gm, log=print):
    Lo = out.mean(-1)
    skypx = (gm[..., 0] < 0.01) & (Lo < np.percentile(Lo, 50))
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


def recombine(orig, sl, p: FinishParams, log=print):
    stars = unscreen(orig, sl)

    g = np.clip((sl - p.black) / (1 - p.black), 0, 1)
    g = mtf(p.mid, g)
    g = saturate(g, p.sat)

    L = g.mean(-1)
    sky = np.median(L)                                     # most of the frame is sky
    sky_sig = 1.4826 * np.median(np.abs(L[L < np.percentile(L, 60)] - sky))
    gm = galaxy_mask(L, sky, max(sky_sig, 1e-3))

    if p.yb != 1.0:
        # opponent axis: yb = (R+G)/2 - B ; push it away from its galaxy median
        yb = (g[..., 0] + g[..., 1]) / 2 - g[..., 2]
        w = gm[..., 0]
        ref = np.median(yb[w > 0.5])
        d = (yb - ref) * (p.yb - 1) * w                     # extra yellow(+)/blue(-) shift
        g = np.clip(g + np.stack([d / 3, d / 3, -2 * d / 3], -1), 0, 1)

    if p.sharpen > 0:
        L = g.mean(-1, keepdims=True)
        hp = L - ndi.gaussian_filter(L, (p.sharpen_radius, p.sharpen_radius, 0))
        g = np.clip(g + p.sharpen * hp * gm, 0, 1)

    s = star_layer(stars, p.gamma, p.star_strength, p.erode)

    out = screen(g, s)

    if p.neutral:
        out = neutralize(out, gm, log)

    if p.crop:
        out = crop(out, p.crop)
    return out


def save(out, base: Path, log=print):
    tifffile.imwrite(str(base) + ".tif", (out * 65535 + 0.5).astype(np.uint16), photometric="rgb")
    Image.fromarray((out * 255 + 0.5).astype(np.uint8)).save(str(base) + ".jpg", quality=95)
    log(f"saved {base}.tif/.jpg {out.shape}")


def run(original: Path, starless: Path, base: Path, p: FinishParams, log=print):
    out = recombine(load(original), load(starless), p, log)
    save(out, base, log)
