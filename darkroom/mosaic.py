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
* crops the stacked canvas to the largest rectangle that has data everywhere and
  keeps its WCS right.

Only RA, DEC and the image size are read from the headers. Site coordinates are not.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

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
    """Where Siril keeps downloaded catalogues (DARKROOM_SIRIL_CACHE overrides it).

    Siril puts them in its config folder, not a cache folder: on Linux that is
    $XDG_CONFIG_HOME/siril/download_cache, by default ~/.config/siril/download_cache
    (seen in Siril 1.4.5's AppImage, and in the Docker image, where HOME is /cache)."""
    override = os.environ.get("DARKROOM_SIRIL_CACHE")
    if override:
        return Path(override).expanduser()
    if tools.is_macos():
        return Path("~/Library/Application Support/org.siril.Siril/siril/download_cache").expanduser()
    return Path(tools._xdg("XDG_CONFIG_HOME", "~/.config")).expanduser() / "siril" / "download_cache"


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


def crop_to_data(path: Path, full: Path, log=print) -> tuple[int, int, int, int] | None:
    """Crop the stacked mosaic at ``path`` to the largest rectangle with data everywhere,
    after moving the uncropped file to ``full``. Returns the box (x, y, w, h)."""
    from astropy.io import fits
    with fits.open(path) as hdul:
        data = hdul[0].data
        if data is None or data.ndim not in (2, 3):
            raise tools.StageError(f"{path.name}: expected a 2-D or 3-D image")
        H, W = data.shape[-2:]
        box = valid_box(valid_mask(data))
        if box is None:
            raise tools.StageError(f"{path.name} has no pixels with data")
        x, y, w, h = box
        if (w, h) == (W, H):
            log(f"mosaic: {path.name} ({W}x{H}) has data everywhere; not cropped")
            return box
        header = hdul[0].header.copy()
        shift_wcs(header, x, y)
        header.add_history(f"darkroom: cropped to x={x} y={y} w={w} h={h} of {W}x{H} (data everywhere)")
        cropped = fits.HDUList([fits.PrimaryHDU(np.ascontiguousarray(data[..., y:y + h, x:x + w]),
                                                header=header)] + list(hdul[1:]))
        tmp = path.with_name(path.stem + ".crop-tmp.fit")
        cropped.writeto(tmp, overwrite=True)
    os.replace(path, full)
    os.replace(tmp, path)
    log(f"mosaic: cropped {W}x{H} to {w}x{h} at x={x}, y={y} (the largest rectangle with data in every "
        f"pixel; {100 * w * h / (W * H):.0f}% of the canvas); uncropped stack kept as {full.name}")
    return box
