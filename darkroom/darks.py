"""Light-frame survey (FITS headers) and automatic master-dark selection.

Only the header keywords listed in ``_KEYS`` (plus a camera preset's match
keyword, for detection) are ever kept. Site coordinates and the
telescope/device name are deliberately not among them.
"""
from __future__ import annotations

import fnmatch
import os
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from . import tools

_KEYS = ("OBJECT", "EXPTIME", "EXPOSURE", "CCD-TEMP", "GAIN", "OFFSET", "FILTER", "FOCALLEN", "XPIXSZ",
         "BAYERPAT", "NAXIS1", "NAXIS2", "DATE-OBS")
MAX_SAMPLE = 500            # headers read per survey (evenly spaced over the night)
WARN_TEMP_DIFF = 5.0        # deg C
FITS_EXTENSIONS = (".fit", ".fits", ".fts")     # in any case: the files calib.FRAME_GLOBS finds


def _header(path: Path) -> dict:
    from astropy.io import fits
    try:
        return dict(fits.getheader(path, 0))
    except Exception as e:      # astropy raises several unrelated types for damaged files
        raise tools.StageError(f"cannot read the FITS header of {path}: {e}") from None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _text(v):
    return v.strip() if isinstance(v, str) and v.strip() else None


def _fmt(x) -> str:
    return format(float(x), "g")


def exposure_of(h: dict) -> float | None:
    """EXPTIME, else EXPOSURE (the older keyword some capture programs still write)."""
    v = _num(h.get("EXPTIME"))
    return v if v is not None else _num(h.get("EXPOSURE"))


def _size_of(h: dict) -> tuple[int, int] | None:
    w, hh = _num(h.get("NAXIS1")), _num(h.get("NAXIS2"))
    return (int(w), int(hh)) if w is not None and hh is not None else None


@dataclass
class Survey:
    """What the subs' headers say. ``None`` / empty where a keyword is absent."""
    count: int = 0
    sampled: int = 0
    object: str | None = None
    exposures: list[float] = field(default_factory=list)     # distinct values, sorted
    gains: list[float] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)
    temp_median: float | None = None
    temp_min: float | None = None
    temp_max: float | None = None
    focal: float | None = None
    pixel: float | None = None
    bayer: str | None = None
    offsets: list[float] = field(default_factory=list)
    sizes: list[tuple[int, int]] = field(default_factory=list)    # distinct (width, height)
    dates: list[str] = field(default_factory=list)                # distinct DATE-OBS days, sorted

    @property
    def exposure(self) -> float | None:
        return self.exposures[0] if len(self.exposures) == 1 else None

    @property
    def gain(self) -> float | None:
        return self.gains[0] if len(self.gains) == 1 else None

    @property
    def offset(self) -> float | None:
        return self.offsets[0] if len(self.offsets) == 1 else None

    @property
    def size(self) -> tuple[int, int] | None:
        return self.sizes[0] if len(self.sizes) == 1 else None

    @property
    def total_seconds(self) -> float | None:
        return self.count * self.exposure if self.exposure is not None else None


def is_fits(name) -> bool:
    return os.path.splitext(str(name))[1].lower() in FITS_EXTENSIONS


def matches(name: str, glob: str) -> bool:
    """Whether a file name matches a sub glob such as ``Light_*.fit``, ignoring case. A glob
    that ends in a FITS extension matches every FITS extension (.fit, .fits, .fts), so
    ``*.fit`` means every FITS file."""
    base, ext = os.path.splitext(glob)
    if ext.lower() in FITS_EXTENSIONS:
        stem, own = os.path.splitext(name)
        return own.lower() in FITS_EXTENSIONS and fnmatch.fnmatchcase(stem.lower(), base.lower())
    return fnmatch.fnmatchcase(name.lower(), glob.lower())


def list_subs(subs: Path, glob: str = "*.fit") -> list[Path]:
    """The files directly inside ``subs`` that match ``glob`` (see ``matches``); no hidden files."""
    try:
        entries = list(Path(subs).iterdir())
    except OSError:
        return []
    return sorted(p for p in entries if not p.name.startswith(".") and matches(p.name, glob) and p.is_file())


def survey(files: list[Path]) -> Survey:
    """Summarise the headers of up to MAX_SAMPLE evenly spaced frames."""
    s = Survey(count=len(files))
    if not files:
        return s
    if len(files) > MAX_SAMPLE:
        step = (len(files) - 1) / (MAX_SAMPLE - 1)
        sample = [files[round(i * step)] for i in range(MAX_SAMPLE)]
    else:
        sample = files
    heads = [{k: h.get(k) for k in _KEYS} for h in map(_header, sample)]
    s.sampled = len(heads)
    s.object = _text(heads[0]["OBJECT"])
    s.exposures = sorted({v for v in map(exposure_of, heads) if v is not None})
    s.gains = sorted({v for v in (_num(h["GAIN"]) for h in heads) if v is not None})
    s.filters = sorted({v for v in (_text(h["FILTER"]) for h in heads) if v})
    temps = [v for v in (_num(h["CCD-TEMP"]) for h in heads) if v is not None]
    if temps:
        s.temp_median, s.temp_min, s.temp_max = statistics.median(temps), min(temps), max(temps)
    s.focal = _num(heads[0]["FOCALLEN"])
    s.pixel = _num(heads[0]["XPIXSZ"])
    s.bayer = _text(heads[0]["BAYERPAT"])
    s.offsets = sorted({v for v in (_num(h["OFFSET"]) for h in heads) if v is not None})
    s.sizes = sorted({sz for sz in map(_size_of, heads) if sz is not None})
    s.dates = sorted({d[:10] for d in (_text(h["DATE-OBS"]) for h in heads) if d})
    return s


def detect_camera(first_sub: Path, presets: dict[str, dict]) -> str | None:
    """Name of the camera preset whose match text appears in its match keyword, if any.
    ``presets`` maps preset name -> its [camera] table. A preset whose text equals the
    keyword's value wins; otherwise the longest matching text does, so "ZWO Seestar S30"
    does not claim the subs of a "ZWO Seestar S30 Pro"."""
    h = _header(first_sub)
    hits = []
    for name, cam in sorted(presets.items()):
        key, text = cam.get("match_keyword"), cam.get("match")
        value = str(h.get(key, "")).strip().lower() if key else ""
        if key and text and text.strip().lower() in value:
            hits.append((value == text.strip().lower(), len(text.strip()), name))
    if not hits:
        return None
    best = max(hit[:2] for hit in hits)
    return next(n for exact, length, n in hits if (exact, length) == best)


# ------------------------------------------------------------- dark selection

@dataclass
class Dark:
    path: Path
    exposure: float | None
    temp: float | None
    gain: float | None
    stamp: str                  # sortable; newest wins a tie
    size: tuple[int, int] | None = None
    bayer: str | None = None


def filename_pattern(filename_re: str) -> re.Pattern:
    """The compiled [darks] filename regex; StageError (not re.error) when it is not valid."""
    try:
        return re.compile(filename_re)
    except re.error as e:
        raise tools.StageError(f"[darks] filename of the camera preset is not a valid regular expression "
                               f"({e}): {filename_re!r}") from None


def read_dark(path: Path, filename_re: str) -> Dark:
    """Exposure, temperature and gain from the header, falling back to the file name."""
    m = filename_pattern(filename_re).fullmatch(path.name)
    name = m.groupdict() if m else {}
    try:
        h = _header(path)
    except tools.StageError:
        h = {}
    exposure = exposure_of(h)
    temp = _num(h.get("CCD-TEMP"))
    if exposure is None:
        exposure = _num(name.get("exposure"))
    if temp is None:
        temp = _num(name.get("temp"))
    stamp = name.get("date") or ""
    if not stamp:
        date = _text(h.get("DATE-OBS"))
        stamp = re.sub(r"[-:T]", "", date)[:14] if date else ""
    return Dark(path, exposure, temp, _num(h.get("GAIN")), stamp, _size_of(h), _text(h.get("BAYERPAT")))


def scan_library(library: Path, meta: dict) -> list[Dark]:
    library = Path(library)
    if not library.is_dir():
        raise tools.StageError(f"dark library not found: {library} (is the drive connected?)")
    filename_pattern(meta["filename"])
    files = sorted(p for p in library.rglob("*")
                   if not p.name.startswith(".") and matches(p.name, meta["glob"]) and p.is_file())
    return [read_dark(p, meta["filename"]) for p in files]


def _size_text(size) -> str:
    return f"{size[0]}x{size[1]}"


def _misfit(d: Dark, lights: Survey) -> str | None:
    """Why the dark cannot calibrate these lights (size or Bayer pattern), or None."""
    if lights.size and d.size and d.size != lights.size:
        return (f"{_size_text(d.size)} pixels, the lights {_size_text(lights.size)} (another camera, binning "
                "or region of interest)")
    if lights.bayer and d.bayer and d.bayer != lights.bayer:
        return f"Bayer pattern {d.bayer}, the lights {lights.bayer}"
    return None


def select_dark(library: Path, meta: dict, exposure: float | None, gain: float | None,
                temp: float | None, log=print, lights: Survey | None = None) -> Path:
    """Pick the master dark for subs of the given exposure, gain and median temperature.
    With ``lights`` (the subs' survey), darks of another size or Bayer pattern are skipped."""
    if exposure is None:
        raise tools.StageError("cannot pick a dark automatically: the subs do not have one common exposure "
                               "time (EXPTIME or EXPOSURE)")
    darks = scan_library(library, meta)
    if not darks:
        raise tools.StageError(f"no master darks matching {meta['glob']} in {library}")
    same = [d for d in darks if d.exposure is not None and abs(d.exposure - exposure) < 0.01]
    if not same:
        have = sorted({d.exposure for d in darks if d.exposure is not None})
        raise tools.StageError(f"no master dark with the subs' exposure ({_fmt(exposure)} s) in {library}; "
                               "exposures available: " + (", ".join(f"{_fmt(e)} s" for e in have) or "none"))
    if lights is not None:
        misfits = [(d, why) for d in same if (why := _misfit(d, lights))]
        if misfits:
            same = [d for d in same if not _misfit(d, lights)]
            d, why = misfits[0]
            msg = (f"{len(misfits)} {_fmt(exposure)} s master dark(s) in {library} do not fit the lights "
                   f"(e.g. {d.path.name}: {why})")
            if not same:
                raise tools.StageError(f"no usable master dark: {msg}")
            log(f"dark: skipped {msg}")
    if gain is not None and any(d.gain is not None for d in same):
        ok = [d for d in same if d.gain is None or abs(d.gain - gain) < 0.01]
        if not ok:
            have = sorted({d.gain for d in same if d.gain is not None})
            raise tools.StageError(f"no {_fmt(exposure)} s master dark at the subs' gain ({_fmt(gain)}) in "
                                   f"{library}; gains available: " + ", ".join(_fmt(g) for g in have))
        same = ok
    elif gain is None and any(d.gain is not None for d in same):
        why = (f"the subs were taken at more than one gain ({', '.join(map(_fmt, lights.gains))})"
               if lights is not None and len(lights.gains) > 1 else "the subs do not record their GAIN")
        log(f"dark: gain not matched: {why}")
    if temp is None:
        best = max(same, key=lambda d: d.stamp)
        log(f"dark: {best.path} (newest of {len(same)}; the subs have no CCD-TEMP to match)")
        return best.path
    with_temp = [d for d in same if d.temp is not None]
    if not with_temp:
        best = max(same, key=lambda d: d.stamp)
        log(f"dark: {best.path} (newest of {len(same)}; none of the darks records a temperature)")
        return best.path
    closest = min(abs(d.temp - temp) for d in with_temp)
    best = max((d for d in with_temp if abs(d.temp - temp) <= closest + 1e-9), key=lambda d: d.stamp)
    diff = best.temp - temp
    log(f"dark: {best.path}")
    log(f"dark: {_fmt(best.exposure)} s, {best.temp:.1f} C"
        + (f", gain {_fmt(best.gain)}" if best.gain is not None else "")
        + f"; subs median {temp:.1f} C, difference {diff:+.1f} C; {len(same)} candidate(s)")
    if abs(diff) > WARN_TEMP_DIFF:
        log(f"WARNING: the nearest master dark is {abs(diff):.1f} C from the subs' median temperature "
            f"(more than {WARN_TEMP_DIFF:g} C); dark current and hot pixels may not match")
    return best.path


def format_duration(seconds: float) -> str:
    m = int(round(seconds / 60))
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"
