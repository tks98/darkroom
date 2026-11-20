"""Calibration frames: bias, darks and flats.

Each of [target] dark, bias and flats is a master file or a folder of raw frames
(dark can also be "auto" or "none", see cli._stack_table; bias and flats "none").
A folder is stacked into <work>/masters/master_<kind>.fit with Siril
(siril/masters.ssf), and rebuilt only when its frames or the master bias it was
calibrated with change: each master has a record in <work>/logs like the stages.

The lights are then calibrated with the dark (bias included) or, without a dark,
the bias, and with the flat. The flats are calibrated with the master bias.
"""
from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import darks, siril, stages, tools

MASTERS = "masters"
KINDS = ("bias", "dark", "flats")           # in build order: the flats need the master bias
MASTER = {"bias": "master_bias", "dark": "master_dark", "flats": "master_flat"}
WHAT = {"bias": "bias frames", "dark": "darks", "flats": "flats"}
FRAME_GLOBS = ("*.fit", "*.fits", "*.fts")
SCRIPT = "masters.ssf"
LOG = "masters.log"


@dataclass
class Calibration:
    """The master files the lights are calibrated with (None: not used)."""
    dark: Path | None = None
    bias: Path | None = None        # only without a dark: the dark already contains the bias
    flats: Path | None = None


def masters_dir(work: Path) -> Path:
    return Path(work).expanduser().resolve() / MASTERS


def master_path(work: Path, kind: str) -> Path:
    return masters_dir(work) / f"{MASTER[kind]}.fit"


def frame_files(folder: Path) -> list[Path]:
    """The FITS files directly inside ``folder`` (no hidden files, no subfolders)."""
    out = set()
    for g in FRAME_GLOBS:
        out.update(p for p in Path(folder).glob(g) if p.is_file() and not p.name.startswith("."))
    return sorted(out)


def _record_name(kind: str) -> str:
    return MASTER[kind]                     # logs/master_bias.params.json, ...


def master_params(kind: str, folder: Path, files: list[Path], bias: Path | None = None) -> dict:
    """What a master is built from: the folder, and the names and sizes of its frames (so adding,
    removing or replacing a frame rebuilds it); for the flat also the master bias."""
    digest = hashlib.sha1("\n".join(f"{p.name}\t{p.stat().st_size}" for p in files).encode()).hexdigest()
    out = {"frames": str(folder), "count": len(files), "digest": digest[:16]}
    if kind == "flats":
        out["bias"] = str(bias) if bias else "none"
    return out


def master_up_to_date(work: Path, kind: str, params: dict, inputs: list[Path]) -> tuple[bool, str]:
    out = master_path(work, kind)
    if not out.is_file():
        return False, f"{out.name} does not exist"
    old = stages.recorded_params(work, _record_name(kind))
    if old is None:
        return False, "no record of how it was built"
    if old != stages._plain(params):
        changed = sorted(k for k in set(old) | set(params) if old.get(k) != params.get(k))
        return False, "changed: " + ", ".join(changed)
    built = out.stat().st_mtime_ns
    for p in inputs:
        if p.exists() and p.stat().st_mtime_ns > built:
            return False, f"{p.name} is newer than {out.name}"
    return True, ""


# ------------------------------------------------------------------ checks

def _fmt(x) -> str:
    return format(float(x), "g")


def _size(s: darks.Survey) -> str:
    return " and ".join(f"{w}x{h}" for w, h in s.sizes)


def check(kind: str, lights: darks.Survey, frames: darks.Survey) -> tuple[list[str], list[str]]:
    """(errors, warnings) for calibration frames of ``kind`` against the lights. Only what both
    headers record is compared."""
    what = WHAT[kind]
    errors, warnings = [], []
    if len(frames.sizes) > 1:
        errors.append(f"the {what} do not all have the same size ({_size(frames)} pixels)")
    elif lights.size and frames.size and lights.size != frames.size:
        errors.append(f"the {what} are {_size(frames)} pixels and the lights {_size(lights)}: calibration "
                      "frames must come from the same camera, with the same binning and region of interest")
    if lights.bayer and frames.bayer and lights.bayer != frames.bayer:
        errors.append(f"the {what} have Bayer pattern {frames.bayer} and the lights {lights.bayer}: they "
                      "do not come from the same camera, or one of them was flipped or cropped")
    if lights.gain is not None and frames.gains and frames.gains != [lights.gain]:
        warnings.append(f"the {what} were taken at gain {', '.join(map(_fmt, frames.gains))} and the lights "
                        f"at gain {_fmt(lights.gain)}; the bias level and noise differ between gains")
    if lights.offset is not None and frames.offsets and frames.offsets != [lights.offset]:
        warnings.append(f"the {what} were taken at offset {', '.join(map(_fmt, frames.offsets))} and the "
                        f"lights at offset {_fmt(lights.offset)}; the bias level will not match")
    if kind == "dark":
        if lights.exposure is not None and frames.exposures and frames.exposures != [lights.exposure]:
            warnings.append(f"the darks are {', '.join(_fmt(e) + ' s' for e in frames.exposures)} and the "
                            f"lights {_fmt(lights.exposure)} s; dark current grows with exposure, so hot pixels "
                            "and amp glow will be over- or under-subtracted")
        if lights.temp_median is not None and frames.temp_median is not None:
            diff = frames.temp_median - lights.temp_median
            if abs(diff) > darks.WARN_TEMP_DIFF:
                warnings.append(f"the darks are {abs(diff):.1f} C from the lights' median temperature (more "
                                f"than {darks.WARN_TEMP_DIFF:g} C); dark current and hot pixels may not match")
    if kind == "flats" and lights.filters and frames.filters and frames.filters != lights.filters:
        warnings.append(f"the flats were taken through filter {', '.join(frames.filters)} and the lights "
                        f"through {', '.join(lights.filters)}; flats must be taken through the same filter, or "
                        "vignetting and dust shadows will not be corrected properly")
    if kind == "flats" and lights.dates and frames.dates:
        fdays = [d for d in map(_day, frames.dates) if d is not None]
        ldays = [d for d in map(_day, lights.dates) if d is not None]
        if fdays and ldays:
            gap = min(abs((f - l).days) for f in fdays for l in ldays)
            if gap > 1:
                warnings.append(f"the flats were taken {gap} days from the lights; dust moves and the camera "
                                "may have been turned in between, so dust shadows can be over- or "
                                "under-corrected")
        else:
            bad = next(d for d in frames.dates + lights.dates if _day(d) is None)
            warnings.append(f"cannot read the date DATE-OBS = {bad!r}, so the flats' date is not compared "
                            "with the lights'")
    return errors, warnings


_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d/%m/%y", "%Y.%m.%d", "%d.%m.%Y", "%Y%m%d")


def _day(text: str):
    """The date of a DATE-OBS value (ISO 8601, or the old FITS dd/mm/yy and a few common forms);
    None when it cannot be read."""
    import datetime
    t = str(text).strip()
    for cand in (t[:10], t):
        for fmt in _DATE_FORMATS:
            try:
                return datetime.datetime.strptime(cand, fmt).date()
            except ValueError:
                continue
    return None


def describe(src: Path, sv: darks.Survey, lights: darks.Survey | None = None) -> str:
    """One line for `darkroom init`: what the calibration frames are."""
    head = (f"{sv.count} frames in {src}" if src.is_dir() else f"master {src}")
    parts = []
    if sv.exposures:
        parts.append(", ".join(f"{_fmt(e * 1000)} ms" if e < 1 else f"{_fmt(e)} s" for e in sv.exposures))
    if sv.gains:
        parts.append("gain " + ", ".join(map(_fmt, sv.gains)))
    if sv.offsets:
        parts.append("offset " + ", ".join(map(_fmt, sv.offsets)))
    if sv.filters:
        parts.append("filter " + ", ".join(sv.filters))
    if sv.temp_median is not None:
        t = (f"{sv.temp_median:.1f} C" if sv.temp_min == sv.temp_max
             else f"{sv.temp_min:.1f} to {sv.temp_max:.1f} C")
        if lights is not None and lights.temp_median is not None:
            t += f" ({sv.temp_median - lights.temp_median:+.1f} C from the lights)"
        parts.append(t)
    if sv.dates:
        parts.append(sv.dates[0] if len(sv.dates) == 1 else f"{sv.dates[0]} to {sv.dates[-1]}")
    return head + (": " + ", ".join(parts) if parts else "")


def survey_of(src: Path) -> darks.Survey:
    files = frame_files(src) if src.is_dir() else [src]
    if src.is_dir() and not files:
        raise tools.StageError(f"no FITS files (*.fit, *.fits, *.fts) in {src}")
    return darks.survey(files)


# ---------------------------------------------------------------- building

def _stage(folder: Path, dest: Path, files: list[Path]) -> Path:
    """Links to the frames in a folder of darkroom's own: Siril cannot `cd` into a read-only
    folder, and so it never works inside the user's folder."""
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    elif dest.is_dir():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for f in files:
        (dest / f.name).symlink_to(f)
    return dest


def _scratch(work: Path, kind: str) -> tuple[Path, Path]:
    m = masters_dir(work)
    return m / f"{kind}_frames", m / f"{kind}_seq"


def _cleanup(work: Path) -> None:
    for kind in KINDS:
        for d in _scratch(work, kind):
            if d.is_symlink():
                d.unlink()
            elif d.is_dir():
                shutil.rmtree(d)


def _was_folder(work: Path | None, kind: str, src: Path) -> bool:
    """Whether ``src`` is a folder of raw frames, or was one when its master was built (an
    unplugged drive)."""
    if src.is_dir():
        return True
    if src.exists() or work is None:
        return False
    old = stages.recorded_params(work, _record_name(kind))
    return bool(old) and old.get("frames") == str(src)


def prepare(work: Path | None, lights: darks.Survey | None, dark: Path | None = None,
            bias: Path | None = None, flats: Path | None = None, log=print,
            dark_matched: bool = False) -> Calibration:
    """Check the calibration frames against the lights, build the masters that are missing or
    out of date, and return the master files to calibrate the lights with. ``dark``, ``bias`` and
    ``flats`` are each None, a master file or a folder of raw frames. ``dark_matched``: the dark
    was picked from a library for the lights' exposure and temperature, so only its size, Bayer
    pattern, gain and offset are checked here."""
    sources = {"bias": bias, "dark": dark, "flats": flats}
    resolved = {k: Path(v).expanduser().resolve() for k, v in sources.items() if v is not None}
    flat_folder = "flats" in resolved and _was_folder(work, "flats", resolved["flats"])
    bias_unused = "bias" in resolved and "dark" in resolved and not flat_folder
    errors, masters, todo = [], {}, {}
    for kind in KINDS:
        src = sources[kind]
        if src is None:
            continue
        if kind == "bias" and bias_unused:
            # the dark contains the bias, and there are no raw flats to calibrate with it
            log("bias: not used; the lights get the dark subtracted, which contains the bias, and there are "
                "no raw flats to calibrate with it")
            continue
        src = resolved[kind]
        if not src.exists():
            out = master_path(work, kind) if work is not None else None
            old = stages.recorded_params(work, _record_name(kind)) if work is not None else None
            if out is not None and out.is_file() and old and old.get("frames") == str(src):
                log(f"{kind}: {src} is not available; using {out}, built from it earlier")
                masters[kind] = out
                continue
            errors.append(f"{WHAT[kind]} not found: {src} (is the drive connected?)")
            continue
        try:
            sv = survey_of(src)
        except tools.StageError as e:
            if src.is_dir():
                errors.append(str(e))
                continue
            sv = None                           # an unreadable master: Siril will say more
            log(f"WARNING: cannot check the {WHAT[kind]} against the lights: {e}")
        if sv is not None and lights is not None:
            err, warn = check(kind, lights, sv)
            if kind == "dark" and dark_matched:      # exposure and temperature were matched already
                warn = [w for w in warn if not w.startswith("the darks are")]
            errors += err
            for w in warn:
                log(f"WARNING: {w}")
        if src.is_file():
            masters[kind] = src
            continue
        if work is None:
            raise tools.StageError(f"cannot build a master from {src} without a work directory")
        files = frame_files(src)
        flat_bias = masters.get("bias") if kind == "flats" else None
        params = master_params(kind, src, files, flat_bias)
        ok, why = master_up_to_date(work, kind, params, files + ([flat_bias] if flat_bias else []))
        if ok and kind == "flats" and "bias" in todo:
            ok, why = False, "the master bias is rebuilt"
        masters[kind] = master_path(work, kind)
        if ok:
            log(f"{kind}: {masters[kind]} is up to date ({len(files)} frames from {src})")
        else:
            todo[kind] = (src, files, params)
            log(f"{kind}: stacking {len(files)} frames from {src} into {masters[kind].name} ({why})")
    if errors:
        raise tools.StageError("calibration: " + "\ncalibration: ".join(errors))
    if "flats" in todo and "bias" not in masters:
        log("WARNING: the flats are stacked without a bias, so the master flat keeps the camera's bias "
            "level and corrects vignetting and dust too weakly; give a bias (--bias) as well")
    if todo:
        build(work, todo, masters.get("bias"), log)
    return Calibration(dark=masters.get("dark"), flats=masters.get("flats"),
                       bias=masters.get("bias") if "dark" not in masters else None)


def build(work: Path, todo: dict, bias: Path | None, log=print) -> None:
    """Stack the folders in ``todo`` (kind -> (folder, files, params)) into masters."""
    work = Path(work).expanduser().resolve()
    m = masters_dir(work)
    m.mkdir(parents=True, exist_ok=True)
    _cleanup(work)
    values = {"MASTERS": str(m)}
    for kind, (_src, files, _params) in todo.items():
        frames, seq = _scratch(work, kind)
        _stage(_src, frames, files)
        seq.mkdir()
        key = {"bias": "BIAS", "dark": "DARK", "flats": "FLAT"}[kind]
        values |= {f"{key}_FRAMES": str(frames), f"{key}_SEQ": str(seq)}
        if kind == "flats" and bias is not None:
            values["FLAT_BIAS"] = str(bias)
        stages.forget_params(work, _record_name(kind))
        master_path(work, kind).unlink(missing_ok=True)   # a failed run cannot leave an old one looking current
    script = siril.render_file(SCRIPT, work, values)
    logf = tools.logs_dir(work) / LOG
    siril.run_script(script, logf)
    tools.check_outputs([master_path(work, k) for k in todo], logf)
    for kind, (_src, _files, params) in todo.items():
        stages.record_params(work, _record_name(kind), params)
    _cleanup(work)
    log("masters: built " + ", ".join(master_path(work, k).name for k in todo) + f" (log: {logf})")


def script_values(cal: Calibration) -> dict[str, str]:
    """DARK, BIAS and FLAT for the calibrate line of stack.ssf and mosaic_solve.ssf."""
    if cal.dark is not None and cal.bias is not None:
        raise ValueError("the lights are calibrated with a dark or a bias, not both")
    return {"DARK": str(cal.dark) if cal.dark else "", "BIAS": str(cal.bias) if cal.bias else "",
            "FLAT": str(cal.flats) if cal.flats else ""}
