"""Pipeline stages. Each reads/writes fixed filenames inside the work directory."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from . import __version__, siril, tools
from .finish import FinishParams, parse_center, parse_crop_spec

STAGES = ["stack", "bge", "color", "denoise", "stretch", "starnet", "finish"]

# Fixed filenames inside the work directory.
STACK = "stack.fit"
BGE = "bge.fits"
BGE_BACKGROUND = "bge_background.fits"
SPCC = "spcc.fit"
DENOISED = "denoised.fits"
STRETCHED_FIT = "stretched.fit"
STRETCHED_TIF = "stretched.tif"
STARLESS = "starless.tif"
FINAL = "final"
PROCESS = "process"


def _abs(p) -> Path:
    return Path(p).expanduser().resolve()


# ------------------------------------------------------------------- stack

def process_dir(work: Path) -> Path:
    return _abs(work) / PROCESS


def _has_sequence(process: Path) -> bool:
    return process.is_dir() and (any(process.glob("*.seq")) or any(process.glob("*.fit")))


def remove_process(work: Path, log=print) -> None:
    """Delete <work>/process (darkroom's own intermediate frames)."""
    process = process_dir(work)
    if process.is_symlink():
        process.unlink()
    elif process.is_dir():
        shutil.rmtree(process)
    else:
        return
    log(f"removed {process}")


def stack(work: Path, subs: Path, dark: Path | None, framing: str = "min", keep_fwhm: float = 90,
          keep_round: float = 90, clean: bool = False, force: bool = False, reuse: bool = False,
          out: str = "stack", sub_glob: str = "*.fit") -> None:
    """dark=None stacks without dark subtraction. ``reuse`` re-frames and re-stacks the
    registered sequence already in <work>/process instead of starting from the subs."""
    work = _abs(work)
    if Path(out).name != out:
        raise tools.StageError("--out is a basename inside the work directory, not a path")
    process = work / PROCESS
    values = {"PROCESS": str(process), "STACK": str(work / out),
              "FRAMING": framing, "KEEP_FWHM": siril.fmt_num(keep_fwhm), "KEEP_ROUND": siril.fmt_num(keep_round),
              "MAXIMIZE": "1" if framing == "max" else ""}
    if reuse:
        if force:
            raise tools.StageError("--force and --reuse-registration contradict each other")
        if not (process / "pp_light_.seq").is_file():
            raise tools.StageError(f"nothing to reuse: no registered sequence (pp_light_.seq) in {process}")
        name = "restack.ssf"
    else:
        subs = _abs(subs)
        if not subs.is_dir():
            raise tools.StageError(f"subs directory not found: {subs}")
        fits_files = sorted(p.name for p in subs.glob("*.fit"))
        if not fits_files:
            raise tools.StageError(f"no *.fit files in {subs}")
        extra = set(fits_files) - {p.name for p in subs.glob(sub_glob)}
        if extra:
            print(f"WARNING: {len(extra)} .fit file(s) in {subs} do not match {sub_glob} "
                  f"(e.g. {sorted(extra)[0]}); Siril stacks every .fit in the folder", flush=True)
        if dark is not None:
            dark = _abs(dark)
            tools.require_file(dark, "master dark")
        if _has_sequence(process):
            if not force:
                raise tools.StageError(
                    f"{process} already holds frames from an earlier stack run, and stacking into it would "
                    "mix the two. Pass --force to delete that folder and stack from scratch, or "
                    "--reuse-registration to re-frame and re-stack the frames already there.")
            remove_process(work)
        process.mkdir(parents=True, exist_ok=True)
        values |= {"SUBS": str(subs), "DARK": str(dark) if dark is not None else ""}
        name = "stack.ssf"
    script = siril.render_file(name, work, values)
    log = tools.logs_dir(work) / "stack.log"
    siril.run_script(script, log)
    tools.check_outputs([work / f"{out}.fit"], log)
    kept = frames_kept(log)
    if kept:
        print(f"stack: {kept[0]} frames stacked" + (f" of {kept[1]}" if kept[1] else ""), flush=True)
    if clean:
        remove_process(work)


def frames_kept(log_path: Path) -> tuple[int, int | None] | None:
    """(stacked, frames in the sequence or None) from Siril's stack log, if it says."""
    try:
        text = Path(log_path).read_text(errors="replace")
    except OSError:
        return None
    done = re.findall(r"(\d+) images have been stacked", text)
    if not done:
        return None
    total = re.findall(r"\(\d+/(\d+) of the sequence\)|\(\d+ images on (\d+)\)", text)
    n = next((int(x) for t in reversed(total) for x in t if x), None)
    return int(done[-1]), n


# ------------------------------------------------------------ other stages

def bge(work: Path, smoothing: float = 0.1) -> None:
    work = _abs(work)
    src = tools.require_file(work / STACK, "stack (run `darkroom stack` first)")
    exe = tools.require_executable(tools.graxpert(), "DARKROOM_GRAXPERT")
    log = tools.logs_dir(work) / "bge.log"
    log.write_text("")
    # GraXpert appends .fits to -output
    tools.run_tool([str(exe), "-cli", "-cmd", "background-extraction", "-correction", "Subtraction",
                    "-smoothing", siril.fmt_num(smoothing), "-bg",
                    "-output", str(work / Path(BGE).stem), str(src)], log)
    tools.check_outputs([work / BGE, work / BGE_BACKGROUND], log)


def color(work: Path, sensor="Sony IMX585", filt="UV/IR Block", focal=160.0, pixel=2.9,
          catalog="gaia", whiteref="Average Spiral Galaxy", catalog_fallback=True) -> None:
    from . import catalog as cat
    work = _abs(work)
    tools.require_file(work / BGE, "background-extracted image (run `darkroom bge` first)")
    script = siril.render_file("color.ssf", work, {
        "WORK": str(work), "FOCAL": siril.fmt_num(focal), "PIXEL": siril.fmt_num(pixel),
        "CATALOG": catalog, "SENSOR": sensor, "FILTER": filt, "WHITEREF": whiteref,
    })
    log = tools.logs_dir(work) / "color.log"
    spcc = work / SPCC
    before = spcc.stat().st_mtime_ns if spcc.is_file() else None
    try:
        siril.run_script(script, log)
        if spcc.is_file() and spcc.stat().st_mtime_ns == before:
            raise tools.StageError(f"Siril did not write {spcc}; last lines of {log}:\n" + tools.tail(log))
    except tools.StageError as first:
        text = log.read_text(errors="replace") if log.is_file() else ""
        requests = cat.failed_requests(text)
        if not requests:
            raise
        if not catalog_fallback:
            raise tools.StageError(f"{first}\n(Siril could not download its star catalogue; the fallback "
                                   "that fetches it from the ESA Gaia archive is switched off)") from None
        first_log = log.with_name("color.attempt1.log")
        first_log.write_text(text)
        try:
            written = cat.recover(text)
        except tools.StageError as e:
            raise tools.StageError(f"{first}\ncatalogue fallback also failed: {e}") from None
        if not written:
            raise
        try:
            siril.run_script(script, log)
        except tools.StageError as second:
            raise tools.StageError(
                f"plate solve failed twice.\nFirst attempt (log kept as {first_log}):\n{first}\n"
                f"Second attempt, after fetching the catalogue from the ESA Gaia archive:\n{second}") from None
        with open(log, "a") as f:
            f.write("[darkroom] catalogue fallback used: " + ", ".join(str(p) for p in written)
                    + f"; first attempt's log is {first_log.name}\n")
        print("color: catalogue fallback was used (Siril's catalogue server could not be reached)", flush=True)
    tools.check_outputs([spcc], log)


def denoise(work: Path, strength: float = 0.6, cpu: bool = False) -> None:
    from . import denoise as dn
    work = _abs(work)
    src = tools.require_file(work / SPCC, "colour-calibrated image (run `darkroom color` first)")
    with tools.StageLog(work, "denoise") as log:
        dn.run(src, work / DENOISED, strength, cpu, log.path, log)
    tools.check_outputs([work / DENOISED], log.path)


def stretch(work: Path, asinh=25.0, shadows=-2.5, target_bg=0.10, sat=0.35) -> None:
    work = _abs(work)
    tools.require_file(work / DENOISED, "denoised image (run `darkroom denoise` first)")
    script = siril.render_file("stretch.ssf", work, {
        "WORK": str(work), "ASINH": siril.fmt_num(asinh), "SHADOWS": siril.fmt_num(shadows),
        "TARGET_BG": siril.fmt_num(target_bg), "SAT": siril.fmt_num(sat),
    })
    log = tools.logs_dir(work) / "stretch.log"
    siril.run_script(script, log)
    tools.check_outputs([work / STRETCHED_FIT, work / STRETCHED_TIF], log)


def starnet(work: Path, starnet_dir: str | None = None) -> None:
    work = _abs(work)
    src = tools.require_file(work / STRETCHED_TIF, "stretched TIFF (run `darkroom stretch` first)")
    d = tools.starnet_dir(starnet_dir)
    log = tools.logs_dir(work) / "starnet.log"
    log.write_text("")
    # StarNet finds its weights relative to the cwd, so it must run from its own folder.
    tools.run_tool(["./starnet++", str(src), str(work / STARLESS)], log, cwd=d)
    tools.check_outputs([work / STARLESS], log)


def finish(work: Path, params: FinishParams, out: str = FINAL) -> None:
    from . import finish as fin
    work = _abs(work)
    orig = tools.require_file(work / STRETCHED_TIF, "stretched TIFF (run `darkroom stretch` first)")
    sl = tools.require_file(work / STARLESS, "starless TIFF (run `darkroom starnet` first)")
    if Path(out).name != out:
        raise tools.StageError("--out is a basename inside the work directory, not a path")
    with tools.StageLog(work, "finish") as log:
        log(f"params: {params}")
        try:
            fin.run(orig, sl, work / out, params, log)
        except ValueError as e:
            raise tools.StageError(str(e)) from None
    tools.check_outputs([work / f"{out}.tif", work / f"{out}.jpg"], log.path)


def finish_params(f: dict) -> FinishParams:
    """FinishParams from a resolved [finish] table (crop as written in the config)."""
    kind, value = parse_crop_spec(f.get("crop"))
    center = parse_center(f["crop_center"]) if f.get("crop_center") else None
    return FinishParams(gamma=f["gamma"], star_strength=f["star_strength"], mid=f["mid"], sat=f["sat"],
                        yb=f["yb"], sharpen=f["sharpen"], sharpen_radius=f["sharpen_radius"],
                        black=f["black"], erode=f["erode"], neutral=f["neutral"],
                        crop=value if kind == "box" else None,
                        crop_aspect=value if kind == "aspect" else None,
                        crop_center=center if kind == "aspect" else None)


# ------------------------------------------- recorded parameters, skipping

_RESULT_KEYS = {
    "bge": ("smoothing",),
    "color": ("sensor", "filter", "focal", "pixel", "catalog", "whiteref"),
    "denoise": ("strength", "cpu"),
    "stretch": ("asinh", "shadows", "target_bg", "sat"),
    "starnet": (),
    "finish": ("gamma", "star_strength", "mid", "sat", "yb", "sharpen", "sharpen_radius", "black", "erode",
               "neutral", "crop", "crop_center"),
}


def result_params(stage: str, table: dict) -> dict:
    """The settings of a stage that change its output (what the skip logic compares).
    For ``stack`` pass a table that already has ``subs`` and the resolved ``dark``."""
    if stage == "stack":
        return {"subs": str(table["subs"]), "dark": str(table["dark"]) if table["dark"] else "none",
                "framing": table["framing"], "keep_fwhm": float(table["keep_fwhm"]),
                "keep_round": float(table["keep_round"])}
    out = {}
    for k in _RESULT_KEYS[stage]:
        v = table[k]
        out[k] = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and k != "erode" else v
    if stage == "finish" and parse_crop_spec(out["crop"])[0] != "aspect":
        out["crop_center"] = None           # only used by an aspect-ratio crop
    return out


def outputs(work: Path, stage: str) -> list[Path]:
    work = _abs(work)
    names = {"stack": [STACK], "bge": [BGE, BGE_BACKGROUND], "color": [SPCC], "denoise": [DENOISED],
             "stretch": [STRETCHED_FIT, STRETCHED_TIF], "starnet": [STARLESS],
             "finish": [f"{FINAL}.tif", f"{FINAL}.jpg"]}[stage]
    return [work / n for n in names]


def inputs(work: Path, stage: str, params: dict) -> list[Path]:
    work = _abs(work)
    if stage == "stack":
        subs = Path(params["subs"])
        files = sorted(subs.glob("*.fit")) if subs.is_dir() else []
        if params["dark"] != "none":
            files.append(Path(params["dark"]))
        return files
    names = {"bge": [STACK], "color": [BGE], "denoise": [SPCC], "stretch": [DENOISED],
             "starnet": [STRETCHED_TIF], "finish": [STRETCHED_TIF, STARLESS]}[stage]
    return [work / n for n in names]


def params_file(work: Path, stage: str) -> Path:
    return _abs(work) / "logs" / f"{stage}.params.json"


def _plain(params: dict) -> dict:
    return json.loads(json.dumps(params, sort_keys=True))


def record_params(work: Path, stage: str, params: dict) -> None:
    """Remember the parameters of a successful run of ``stage``."""
    p = params_file(work, stage)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"stage": stage, "darkroom": __version__, "params": _plain(params)},
                              indent=2, sort_keys=True) + "\n")
    tmp.replace(p)


def recorded_params(work: Path, stage: str) -> dict | None:
    try:
        return json.loads(params_file(work, stage).read_text())["params"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def forget_params(work: Path, stage: str) -> None:
    params_file(work, stage).unlink(missing_ok=True)


def up_to_date(work: Path, stage: str, params: dict) -> tuple[bool, str]:
    """(True, "") if ``stage`` can be skipped, else (False, reason it has to run)."""
    outs = outputs(work, stage)
    missing = [p.name for p in outs if not p.is_file()]
    if missing:
        return False, f"{missing[0]} does not exist"
    old = recorded_params(work, stage)
    if old is None:
        return False, "no record of the parameters of its last run"
    new = _plain(params)
    if old != new:
        changed = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
        return False, "changed: " + ", ".join(changed)
    ins = inputs(work, stage, params)
    gone = [p for p in ins if not p.exists()]
    if gone:
        if stage == "stack":
            return True, f"inputs not available ({gone[0]})"    # e.g. the subs drive is unplugged
        return False, f"input {gone[0].name} does not exist"
    oldest_out = min(p.stat().st_mtime_ns for p in outs)
    for p in ins:
        if p.stat().st_mtime_ns > oldest_out:
            return False, f"{p.name} is newer than {min(outs, key=lambda q: q.stat().st_mtime_ns).name}"
    return True, ""
