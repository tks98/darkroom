"""Mosaics: subs taken at many pointings (the Seestar's mosaic mode moves the mount
between frames), stacked onto one canvas.

Registration against one reference frame drops every frame that does not overlap
it, so a mosaic is plate-solved frame by frame instead. This module

* reads the commanded pointing (RA, DEC) of each sub from its header, to tell a
  mosaic from an ordinary stack and to predict the star catalogues Siril's plate
  solver will ask for;
* prefills Siril's catalogue cache with one Gaia query for the whole field, split
  locally into the per-pointing files Siril reads (``cat_02_RA_DEC_RADIUS_MAG.csv``),
  so solving a hundred frames needs one network request, not a hundred;
* crops the stacked canvas to the largest rectangle that has data everywhere (turning
  the image when that keeps clearly more) and keeps its WCS right.

Only RA, DEC and the image size are read from the headers. Site coordinates are not.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import numpy as np

from . import catalog, tools

LIMIT_MAG = 13.0            # -limitmag of the plate solve; 13 solved 139 of 151 Rosette frames
SPREAD_FRACTION = 0.25      # pointings spread over more than this fraction of the field -> mosaic
MAX_SAMPLE = 2000           # headers read when detecting a mosaic
ARCSEC_PER_RAD = 206264.806


# ---------------------------------------------------------------- pointings

@dataclass(frozen=True)
class Frame:
    ra: float               # degrees, commanded pointing
    dec: float
    width: int              # pixels
    height: int


def read_frames(files: list[Path], limit: int | None = None) -> list[Frame]:
    """Pointing and size of each sub (frames without RA/DEC in the header are left out).
    ``limit`` reads only that many, evenly spaced."""
    from astropy.io import fits
    files = list(files)
    if limit and len(files) > limit:
        step = (len(files) - 1) / (limit - 1)
        files = [files[round(i * step)] for i in range(limit)]
    out = []
    for f in files:
        try:
            h = fits.getheader(f, 0)
            ra, dec = float(h["RA"]), float(h["DEC"])
            w, hh = int(h["NAXIS1"]), int(h["NAXIS2"])
        except Exception:       # unreadable file, missing or non-numeric keyword
            continue
        if math.isfinite(ra) and math.isfinite(dec):
            out.append(Frame(ra, dec, w, hh))
    return out


def separation(ra1, dec1, ra2, dec2):
    """Angular distance in degrees (numpy-broadcasting; haversine, fine at small angles)."""
    r1, d1, r2, d2 = (np.radians(np.asarray(x, float)) for x in (ra1, dec1, ra2, dec2))
    a = np.sin((d2 - d1) / 2) ** 2 + np.cos(d1) * np.cos(d2) * np.sin((r2 - r1) / 2) ** 2
    return np.degrees(2 * np.arcsin(np.sqrt(np.clip(a, 0, 1))))


def scale_arcsec(focal_mm: float, pixel_um: float) -> float:
    """Image scale in arcseconds per pixel."""
    return ARCSEC_PER_RAD * pixel_um * 1e-3 / focal_mm


def frame_radius_arcmin(width: int, height: int, focal_mm: float, pixel_um: float) -> float:
    """Half the frame diagonal in arcminutes: the radius of the catalogue cone Siril asks for
    (73.21' for the Seestar S30 at 150 mm, 2.9 um, 1080 x 1920)."""
    return 0.5 * math.hypot(width, height) * scale_arcsec(focal_mm, pixel_um) / 60


def mean_pointing(ras, decs) -> tuple[float, float]:
    """Centre of a set of pointings (vector mean, so it works across RA 0/360)."""
    r, d = np.radians(np.asarray(ras, float)), np.radians(np.asarray(decs, float))
    x, y, z = (np.cos(d) * np.cos(r)).mean(), (np.cos(d) * np.sin(r)).mean(), np.sin(d).mean()
    return float(np.degrees(np.arctan2(y, x)) % 360), float(np.degrees(np.arctan2(z, math.hypot(x, y))))


@dataclass(frozen=True)
class Spread:
    pointings: int          # distinct pointings
    spread_deg: float       # largest distance of a pointing from the centre of them all
    field_deg: float        # short side of one frame
    is_mosaic: bool

    def describe(self) -> str:
        return (f"{self.pointings} pointings, up to {self.spread_deg:.2f} deg from their centre, "
                f"{self.field_deg:.2f} deg field")


def spread(frames: list[Frame], focal_mm: float, pixel_um: float,
           fraction: float = SPREAD_FRACTION) -> Spread | None:
    """How far the pointings spread compared with the field of view; None without frames."""
    if not frames or not focal_mm or not pixel_um:
        return None
    pts = sorted({(round(f.ra, 4), round(f.dec, 4)) for f in frames})
    ras, decs = [p[0] for p in pts], [p[1] for p in pts]
    c = mean_pointing(ras, decs)
    far = float(separation(ras, decs, *c).max())
    short = min(frames[0].width, frames[0].height) * scale_arcsec(focal_mm, pixel_um) / 3600
    return Spread(len(pts), far, short, far > fraction * short)


def detect(files: list[Path], focal_mm: float, pixel_um: float) -> Spread | None:
    return spread(read_frames(files, MAX_SAMPLE), focal_mm, pixel_um)


# ------------------------------------------------------- catalogue prefill

def siril_cache_dir() -> Path:
    """Where Siril keeps downloaded catalogues (DARKROOM_SIRIL_CACHE overrides it)."""
    override = os.environ.get("DARKROOM_SIRIL_CACHE")
    if override:
        return Path(override).expanduser()
    if tools.is_macos():
        return Path("~/Library/Application Support/org.siril.Siril/siril/download_cache").expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or "~/.cache"
    return Path(base).expanduser() / "siril" / "download_cache"


def cache_name(ra: float, dec: float, radius_arcmin: float, mag: float) -> str:
    """The file Siril's solver reads for one frame (-nocache: one per frame's header RA/DEC)."""
    return f"cat_{catalog.GAIA_DR3:02d}_{ra:08.4f}_{dec:+08.4f}_{radius_arcmin:.2f}_{mag:.1f}.csv"


def requests(frames: list[Frame], focal_mm: float, pixel_um: float, mag: float) -> dict[str, tuple]:
    """Cache file name -> (ra, dec, radius_arcmin) for every distinct pointing."""
    out = {}
    for f in frames:
        ra, dec = round(f.ra, 4), round(f.dec, 4)
        radius = round(frame_radius_arcmin(f.width, f.height, focal_mm, pixel_um), 2)
        out.setdefault(cache_name(ra, dec, radius, mag), (ra, dec, radius))
    return out


def split_catalog(text: str, cones: dict[str, tuple]) -> dict[str, tuple[str, int]]:
    """Cut one catalogue (Siril's CSV format) into the cones named in ``cones``
    (name -> (ra, dec, radius_arcmin)). Returns name -> (csv text, number of stars)."""
    lines = [l for l in text.replace("\r\n", "\n").split("\n") if l.strip()]
    if not lines or lines[0].strip() != catalog.HEADER:
        raise ValueError("not a catalogue in Siril's format")
    rows = lines[1:]
    sra = np.array([float(r.split(",", 2)[0]) for r in rows])
    sdec = np.array([float(r.split(",", 2)[1]) for r in rows])
    out = {}
    for name, (ra, dec, radius) in cones.items():
        keep = np.flatnonzero(separation(sra, sdec, ra, dec) <= radius / 60) if rows else []
        out[name] = (catalog.HEADER + "\n" + "".join(rows[i] + "\n" for i in keep), len(keep))
    return out


def prefill(files: list[Path], focal_mm: float, pixel_um: float, mag: float = LIMIT_MAG,
            cache: Path | None = None, fetcher=None, log=print) -> list[Path]:
    """Write Siril's per-pointing catalogue files for these subs from one Gaia query.
    Files already in the cache that pass validation are kept. Returns the files written."""
    frames = read_frames(files)
    if not frames:
        log("mosaic: no RA/DEC in the subs' headers; Siril will download each frame's catalogue itself")
        return []
    cache = cache or siril_cache_dir()
    cones = requests(frames, focal_mm, pixel_um, mag)
    todo = {}
    for name, cone in cones.items():
        try:
            catalog.validate_csv((cache / name).read_text(), min_stars=0)
        except (OSError, ValueError):
            todo[name] = cone
    if not todo:
        log(f"mosaic: star catalogues for all {len(cones)} pointings are already in {cache}")
        return []
    ras, decs = [c[0] for c in todo.values()], [c[1] for c in todo.values()]
    c = mean_pointing(ras, decs)
    radius = max(c[2] for c in todo.values()) / 60
    big = float(separation(ras, decs, *c).max()) + radius + 0.01
    log(f"mosaic: fetching Gaia DR3 for {len(todo)} pointings in one query from the ESA archive "
        f"(cone of {big:.2f} deg, G <= {mag:g})")
    text = (fetcher or catalog.fetch)(c[0], c[1], big, mag)
    written = []
    total = 0
    for name, (csv, n) in split_catalog(text, todo).items():
        catalog.validate_csv(csv, min_stars=0)
        catalog.write_atomic(cache / name, csv)
        written.append(cache / name)
        total += n
    log(f"mosaic: wrote {len(written)} catalogue files ({total} stars in all) to {cache}")
    return written


# ---------------------------------------------------------------- cropping

def _largest_rectangle(mask: np.ndarray) -> tuple[int, int, int, int]:
    """(y, x, h, w) of the largest all-True axis-aligned rectangle (histogram method)."""
    rows, cols = mask.shape
    best = (0, 0, 0, 0, 0)
    heights = np.zeros(cols, int)
    for r in range(rows):
        heights = np.where(mask[r], heights + 1, 0)
        stack = []                              # (start column, height), heights increasing
        for c in range(cols + 1):
            h = int(heights[c]) if c < cols else 0
            start = c
            while stack and stack[-1][1] >= h:
                s0, h0 = stack.pop()
                if h0 * (c - s0) > best[0]:
                    best = (h0 * (c - s0), r - h0 + 1, s0, h0, c - s0)
                start = s0
            stack.append((start, h))
    return best[1:]


def valid_box(valid: np.ndarray, coarse: int = 400) -> tuple[int, int, int, int] | None:
    """(x, y, w, h) in numpy indices of a large rectangle where ``valid`` is True everywhere:
    the largest one on a grid of blocks (at most ``coarse`` blocks on the long side), then
    grown pixel by pixel. None if nothing is valid."""
    H, W = valid.shape
    f = max(1, math.ceil(max(H, W) / coarse))
    hb, wb = H // f, W // f
    blocks = valid[:hb * f, :wb * f].reshape(hb, f, wb, f).all(axis=(1, 3))
    y, x, h, w = _largest_rectangle(blocks)
    if h == 0:
        if not valid.any():
            return None
        y, x = map(int, np.argwhere(valid)[0])
        y0, x0, y1, x1 = y, x, y + 1, x + 1
    else:
        y0, x0, y1, x1 = y * f, x * f, (y + h) * f, (x + w) * f
    grew = True
    while grew:
        grew = False
        if y0 > 0 and valid[y0 - 1, x0:x1].all():
            y0 -= 1; grew = True
        if y1 < H and valid[y1, x0:x1].all():
            y1 += 1; grew = True
        if x0 > 0 and valid[y0:y1, x0 - 1].all():
            x0 -= 1; grew = True
        if x1 < W and valid[y0:y1, x1].all():
            x1 += 1; grew = True
    return x0, y0, x1 - x0, y1 - y0


def valid_mask(data: np.ndarray) -> np.ndarray:
    """Pixels with data in every channel: Siril leaves the parts of a -maximize canvas no
    frame covered at exactly 0. ``data`` is (H, W) or (C, H, W) as astropy reads it."""
    d = data if data.ndim == 3 else data[None]
    return (np.isfinite(d) & (d != 0)).all(axis=0)


def shift_wcs(header, x0: int, y0: int) -> None:
    """Move the WCS reference pixel for a crop that starts at numpy column x0, row y0.
    astropy's array row 0 is FITS row 1 (the first row in the file), and Siril writes its
    images in that order, so numpy offsets are FITS offsets: no flip."""
    if "CRPIX1" in header:
        header["CRPIX1"] = float(header["CRPIX1"]) - x0
    if "CRPIX2" in header:
        header["CRPIX2"] = float(header["CRPIX2"]) - y0


# ---------------------------------------------------------- turned crop

MIN_ROTATE_GAIN = 1.10      # turn the image only when that keeps at least 10% more pixels than an upright crop
EDGE_TRIM = 3               # pixels trimmed off the data's edge before turning: the interpolation reaches past it
SEARCH_BLOCKS = 150         # blocks on the long side for the 1-degree angle search
REFINE_BLOCKS = 400         # and for the 0.25-degree refinement
SPLINE_ORDER = 3            # cubic spline resampling when the image is turned


def _run_lengths(mask: np.ndarray, axis: int) -> np.ndarray:
    """Length of the run of True that ends at each element, along ``axis``."""
    c = np.cumsum(mask, axis=axis, dtype=np.int32)
    return c - np.maximum.accumulate(np.where(mask, 0, c), axis=axis)


def largest_rect(mask: np.ndarray) -> tuple[int, int, int, int]:
    """(y, x, h, w) of the largest all-True axis-aligned rectangle, exactly and without a Python loop
    over pixels: for each height k, the widest run of columns whose valid run up to one row is >= k."""
    heights = _run_lengths(mask, 0)
    cols = mask.shape[1]
    best = (0, 0, 0, 0, 0)
    for k in np.unique(heights)[::-1]:              # tallest first; stop when no width can beat the best
        k = int(k)
        if k == 0 or k * cols <= best[0]:
            break
        runs = _run_lengths(heights >= k, 1)
        r, c = divmod(int(runs.argmax()), cols)
        w = int(runs[r, c])
        if k * w > best[0]:
            best = (k * w, r - k + 1, c - w + 1, k, w)
    return best[1:]


def _blocks(mask: np.ndarray, n: int) -> np.ndarray:
    """``mask`` shrunk to at most ``n`` blocks on its long side (a block is True if all its pixels are)."""
    H, W = mask.shape
    f = max(1, math.ceil(max(H, W) / n))
    return mask[:H // f * f, :W // f * f].reshape(H // f, f, W // f, f).all(axis=(1, 3))


def rotation(shape: tuple[int, int], angle: float) -> tuple[np.ndarray, np.ndarray, tuple[int, int]]:
    """(M, offset, out_shape) for turning an image of ``shape`` (rows, cols) by ``angle`` degrees onto
    a canvas just big enough to hold it: output pixel (row, col) comes from input M @ (row, col) + offset.
    A positive angle turns the picture clockwise as Siril shows it (FITS row 1 at the bottom)."""
    H, W = shape
    t = math.radians(angle)
    c, s = math.cos(t), math.sin(t)
    out = (math.ceil(abs(W * s) + abs(H * c) - 1e-6), math.ceil(abs(W * c) + abs(H * s) - 1e-6))
    M = np.array([[c, s], [-s, c]])
    off = np.array([(H - 1) / 2, (W - 1) / 2]) - M @ np.array([(out[0] - 1) / 2, (out[1] - 1) / 2])
    return M, off, out


def rotate_mask(mask: np.ndarray, angle: float) -> np.ndarray:
    """``mask`` turned by ``angle`` degrees (nearest neighbour; outside the input is False)."""
    if angle == 0:
        return mask
    from scipy import ndimage
    M, off, out = rotation(mask.shape, angle)
    return ndimage.affine_transform(mask.astype(np.uint8), M, off, out, order=0, cval=0) > 0


def best_angle(valid: np.ndarray, step: float = 1.0, fine: float = 0.25) -> float:
    """The angle in degrees (about -45..45) that gives the largest rectangle with ``valid`` True
    everywhere: every ``step`` degrees on a coarse grid, then every ``fine`` degrees around the two
    best on a finer one. Upright (0) wins ties."""
    def area(mask, a):
        _, _, h, w = largest_rect(rotate_mask(mask, a))
        return h * w

    small = _blocks(valid, SEARCH_BLOCKS)
    coarse = {float(a): area(small, a) for a in np.arange(-45, 45, step)}
    top = sorted(coarse, key=lambda a: (-coarse[a], abs(a)))[:2]
    big = _blocks(valid, REFINE_BLOCKS)
    n = round(step / fine)
    cands = {0.0} | {round(a + d * fine, 3) for a in top for d in range(-n, n + 1)}
    score = {a: area(big, a) for a in cands}
    return max(score, key=lambda a: (score[a], -abs(a)))


def _has_sip(header) -> bool:
    return "A_ORDER" in header or str(header.get("CTYPE1", "")).endswith("-SIP")


def _pc(header) -> np.ndarray:
    """The PC matrix of a header, from PCi_j or CROTA2 (identity when neither is there)."""
    if any(f"PC{i}_{j}" in header for i in (1, 2) for j in (1, 2)) or "CROTA2" not in header:
        return np.array([[float(header.get(f"PC{i}_{j}", 1.0 if i == j else 0.0)) for j in (1, 2)]
                         for i in (1, 2)])
    t = math.radians(float(header["CROTA2"]))
    r = float(header.get("CDELT2", 1.0)) / float(header.get("CDELT1", 1.0))
    return np.array([[math.cos(t), -math.sin(t) * r], [math.sin(t) / r, math.cos(t)]])


def rotate_wcs(header, M: np.ndarray, offset: np.ndarray) -> None:
    """Update the WCS for an image resampled so that output pixel (row, col) comes from input pixel
    M @ (row, col) + offset (numpy indices, 0-based). Linear WCS only (no SIP)."""
    if "CRPIX1" not in header or "CRPIX2" not in header:
        return
    A = np.array([[M[1, 1], M[1, 0]], [M[0, 1], M[0, 0]]])        # the same map in (x, y) = (col, row)
    b = np.array([offset[1], offset[0]])
    # FITS pixel p = q + 1, so p_in = A p_out + (b + 1 - A 1): CRPIX_out = A^-1 (CRPIX_in - 1 - b) + 1
    crpix = np.linalg.solve(A, np.array([float(header["CRPIX1"]), float(header["CRPIX2"])]) - 1 - b) + 1
    header["CRPIX1"], header["CRPIX2"] = float(crpix[0]), float(crpix[1])
    if any(k in header for k in ("CD1_1", "CD1_2", "CD2_1", "CD2_2")):
        cd = np.array([[float(header.get(f"CD{i}_{j}", 0.0)) for j in (1, 2)] for i in (1, 2)]) @ A
        for i in (1, 2):
            for j in (1, 2):
                header[f"CD{i}_{j}"] = float(cd[i - 1, j - 1])
    else:
        pc = _pc(header) @ A
        for k in ("CROTA1", "CROTA2"):
            header.remove(k, ignore_missing=True)
        for i in (1, 2):
            for j in (1, 2):
                header[f"PC{i}_{j}"] = float(pc[i - 1, j - 1])


def resample(data: np.ndarray, valid: np.ndarray, M: np.ndarray, offset: np.ndarray,
             shape: tuple[int, int], order: int = SPLINE_ORDER) -> np.ndarray:
    """``data`` ((H, W) or (C, H, W)) resampled onto ``shape`` through M and offset (see rotation),
    as float32. Pixels without data are set to the channel's median first, so the spline does not
    ring against the zeros, and the result is clipped to the range of the data."""
    from scipy import ndimage
    d = data if data.ndim == 3 else data[None]
    out = np.empty((d.shape[0],) + tuple(shape), np.float32)
    for i, ch in enumerate(d):
        v = ch[valid]
        filled = np.where(valid, ch, np.median(v)).astype(np.float64)
        ndimage.affine_transform(filled, M, offset, shape, output=out[i], order=order, mode="nearest")
        np.clip(out[i], v.min(), v.max(), out=out[i])
    return out if data.ndim == 3 else out[0]


class Crop(NamedTuple):
    """Where the crop is: a box in the image turned by ``angle`` degrees (0: in the canvas itself)."""
    x: int
    y: int
    w: int
    h: int
    angle: float = 0.0


def crop_to_data(path: Path, full: Path, mode: str = "rotate", log=print) -> Crop:
    """Crop the stacked mosaic at ``path`` to the largest rectangle with data everywhere, after
    moving the uncropped file to ``full``. ``mode`` "rect" keeps the canvas upright; "rotate" also
    tries every angle and turns the image when that keeps clearly more (MIN_ROTATE_GAIN)."""
    from astropy.io import fits
    if mode not in ("rotate", "rect"):
        raise ValueError(f"crop mode {mode!r}")
    with fits.open(path) as hdul:
        data = hdul[0].data
        if data is None or data.ndim not in (2, 3):
            raise tools.StageError(f"{path.name}: expected a 2-D or 3-D image")
        H, W = data.shape[-2:]
        valid = valid_mask(data)
        box = valid_box(valid)
        if box is None:
            raise tools.StageError(f"{path.name} has no pixels with data")
        x, y, w, h = box
        if (w, h) == (W, H):
            log(f"mosaic: {path.name} ({W}x{H}) has data everywhere; not cropped")
            return Crop(*box)
        n_valid = int(valid.sum())
        header = hdul[0].header.copy()
        turn = None
        if mode == "rotate" and _has_sip(header):
            log("mosaic: the WCS has SIP distortion terms, which a turned crop cannot carry; cropping upright")
        elif mode == "rotate":
            from scipy import ndimage
            trimmed = ndimage.binary_erosion(valid, np.ones((3, 3), bool), iterations=EDGE_TRIM)
            a = best_angle(trimmed)
            if a != 0:
                rb = valid_box(rotate_mask(trimmed, a))
                if rb and rb[2] * rb[3] >= MIN_ROTATE_GAIN * w * h:
                    turn = (a, rb)
                else:
                    log(f"mosaic: turning the image by {a:g} deg would keep less than "
                        f"{100 * (MIN_ROTATE_GAIN - 1):.0f}% more; cropping upright")
        if turn:
            a, (x, y, w, h) = turn
            M, off, _ = rotation(valid.shape, a)
            off = off + M @ np.array([y, x], float)
            pixels = resample(data, valid, M, off, (h, w))
            rotate_wcs(header, M, off)
            header.add_history(f"darkroom: turned {a:g} deg and cropped to {w}x{h} of {W}x{H} (data everywhere)")
            crop = Crop(x, y, w, h, a)
        else:
            shift_wcs(header, x, y)
            header.add_history(f"darkroom: cropped to x={x} y={y} w={w} h={h} of {W}x{H} (data everywhere)")
            pixels = np.ascontiguousarray(data[..., y:y + h, x:x + w])
            crop = Crop(x, y, w, h)
        cropped = fits.HDUList([fits.PrimaryHDU(pixels, header=header)] + list(hdul[1:]))
        tmp = path.with_name(path.stem + ".crop-tmp.fit")
        cropped.writeto(tmp, overwrite=True)
    os.replace(path, full)
    os.replace(tmp, path)
    kept = f"{100 * w * h / n_valid:.0f}% of the pixels with data, {100 * w * h / (W * H):.0f}% of the canvas"
    if crop.angle:
        log(f"mosaic: turned the {W}x{H} canvas {abs(crop.angle):g} deg "
            f"{'clockwise' if crop.angle > 0 else 'anticlockwise'} and cropped it to {w}x{h}, the largest "
            f"rectangle with data in every pixel at any angle ({kept}; the largest upright one, "
            f"{box[2]}x{box[3]}, keeps {100 * box[2] * box[3] / n_valid:.0f}%). North is no longer up; "
            f"[stack] mosaic_crop = \"rect\" keeps the canvas upright. Uncropped stack kept as {full.name}")
    else:
        log(f"mosaic: cropped {W}x{H} to {w}x{h} at x={x}, y={y} (the largest rectangle with data in every "
            f"pixel; {kept}); uncropped stack kept as {full.name}")
    return crop


