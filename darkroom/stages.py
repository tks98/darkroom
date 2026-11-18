"""Pipeline stages. Each reads/writes fixed filenames inside the work directory."""
from __future__ import annotations

import json
import os
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
          out: str = "stack", sub_glob: str = "*.fit", keep_bkg: float = 100,
          keep_stars: float = 100, mosaic: bool = False, focal: float = 160.0, pixel: float = 2.9) -> None:
    """dark=None stacks without dark subtraction. ``reuse`` re-frames and re-stacks the
    registered sequence already in <work>/process instead of starting from the subs.
    ``mosaic`` plate-solves every frame (with ``focal`` and ``pixel``) instead of registering
    them against one reference, stacks them onto one canvas (framing is always max) and crops
    it to the area with data everywhere."""
    work = _abs(work)
    if Path(out).name != out:
        raise tools.StageError("--out is a basename inside the work directory, not a path")
    process = work / PROCESS
    if mosaic:
        framing = "max"
    values = {"PROCESS": str(process), "STACK": str(work / out),
              "FRAMING": framing, "KEEP_FWHM": siril.fmt_num(keep_fwhm), "KEEP_ROUND": siril.fmt_num(keep_round),
              "MAXIMIZE": "1" if framing == "max" else ""}
    more = ""                                   # extra seqapplyreg filters; none at the default 100%
    if keep_bkg < 100:
        more += f"-filter-bkg={siril.fmt_num(keep_bkg)}% "
    if keep_stars < 100:
        more += f"-filter-nbstars={siril.fmt_num(keep_stars)}% "
    if more:
        values["MORE_FILTERS"] = more
    if mosaic:
        del values["FRAMING"], values["MAXIMIZE"]
    if reuse:
        if force:
            raise tools.StageError("--force and --reuse-registration contradict each other")
        if not (process / "pp_light_.seq").is_file():
            raise tools.StageError(f"nothing to reuse: no registered sequence (pp_light_.seq) in {process}")
        name = "mosaic_stack.ssf" if mosaic else "restack.ssf"
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
        siril_subs = subs
        if not os.access(subs, os.W_OK):
            siril_subs = stage_subs(subs, process)
        sub_files = sorted(subs.glob("*.fit"))
        if mosaic:
            _mosaic_solve(work, sub_files, siril_subs, dark, focal, pixel)
            name = "mosaic_stack.ssf"
        else:
            warn_if_mosaic(sub_files, focal, pixel)
            values |= {"SUBS": str(siril_subs), "DARK": str(dark) if dark is not None else ""}
            name = "stack.ssf"
    script = siril.render_file(name, work, values)
    log = tools.logs_dir(work) / "stack.log"
    stacked = work / f"{out}.fit"
    if mosaic:
        stacked.unlink(missing_ok=True)       # so a failed run cannot leave the old crop looking current
    siril.run_script(script, log)
    tools.check_outputs([stacked], log)
    kept = frames_kept(log)
    if kept:
        print(f"stack: {kept[0]} frames stacked" + (f" of {kept[1]}" if kept[1] else ""), flush=True)
    if mosaic:
        from . import mosaic as mo
        with open(log, "a") as f:
            def say(m):
                print(m, flush=True)
                f.write(f"[darkroom] {m}\n")
            mo.crop_to_data(stacked, work / f"{out}_full.fit", log=say)
    if clean:
        remove_process(work)


MOSAIC_SOLVE_LOG = "stack_solve.log"


def solved_count(log_text: str) -> tuple[int, int] | None:
    """(frames solved, frames included) from Siril's last seqplatesolve summary. Siril's
    "successfully platesolved" count already includes frames that were solved before and skipped."""
    m = re.findall(r"(\d+) images successfully platesolved out of (\d+) included", log_text)
    if not m:
        return None
    done, total = map(int, m[-1])
    return done, total


def sequence_length(seq: Path) -> int:
    """Number of images in a Siril .seq file (its 'S' line: name, start, number of images, ...)."""
    for line in Path(seq).read_text(errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "S":
            return int(parts[3])
    raise tools.StageError(f"cannot read the number of images from {seq}")


def _mosaic_solve(work: Path, sub_files: list[Path], siril_subs: Path, dark: Path | None,
                  focal: float, pixel: float) -> None:
    """Calibrate and plate-solve every frame of a mosaic: prefill Siril's catalogue cache from
    the subs' headers, solve, and if Siril still could not get some catalogues, fetch those
    and solve the remaining frames again."""
    from . import catalog as cat, mosaic as mo
    say = lambda m: print(m, flush=True)
    try:
        mo.prefill(sub_files, focal, pixel, mo.LIMIT_MAG, log=say)
    except (tools.StageError, ValueError, OSError) as e:
        say(f"mosaic: could not prefill the star catalogues ({e}); Siril will download them itself")
    process = work / PROCESS
    base = {"PROCESS": str(process), "MAG": siril.fmt_num(mo.LIMIT_MAG), "FOCAL": siril.fmt_num(focal),
            "PIXEL": siril.fmt_num(pixel)}
    first = siril.render_file("mosaic_solve.ssf", work, base | {
        "SUBS": str(siril_subs), "DARK": str(dark) if dark is not None else "",
        "NODARK": "" if dark is not None else "1", "FORCE": "-force ", "FRAMES": ""})
    log = tools.logs_dir(work) / MOSAIC_SOLVE_LOG
    error = None
    try:
        siril.run_script(first, log)
    except tools.StageError as e:
        error = e
    text = log.read_text(errors="replace") if log.is_file() else ""
    missing = cat.failed_requests(text)
    if missing:
        if not (process / "pp_light_.seq").is_file():
            raise error or tools.StageError(f"calibration failed; see {log}")
        kept = log.with_name("stack_solve.attempt1.log")
        kept.write_text(text)
        say(f"mosaic: Siril could not get the star catalogue for {len(missing)} pointing(s); "
            "fetching them and solving the remaining frames again")
        try:
            cat.recover(text, log=say)
        except tools.StageError as e:
            say(f"mosaic: catalogue fallback failed ({e}); those frames stay unsolved")
        else:
            again = siril.render_file("mosaic_solve.ssf", work, base | {
                "SUBS": "", "DARK": "", "NODARK": "", "FRAMES": str(sequence_length(process / "pp_light_.seq"))})
            error = None
            try:
                siril.run_script(again, log)
            except tools.StageError as e:
                error = e
            text = log.read_text(errors="replace")
            with open(log, "a") as f:
                f.write(f"[darkroom] second solve after the catalogue fallback; first log is {kept.name}\n")
    n = solved_count(text)
    if n is None or n[0] == 0:
        detail = f"\n{error}" if error else ("\nlast lines of the log:\n" + tools.tail(log))
        raise tools.StageError(f"mosaic: no frame was plate-solved (see {log}){detail}")
    say(f"mosaic: {n[0]} of {n[1]} frames plate-solved")


def warn_if_mosaic(sub_files: list[Path], focal: float, pixel: float) -> None:
    """Warn when the subs' pointings say mosaic but [stack] mosaic is off."""
    from . import mosaic as mo
    try:
        sp = mo.detect(sub_files, focal, pixel)
    except Exception:           # a warning only: never fail a stack over it
        return
    if sp and sp.is_mosaic:
        print(f"WARNING: these subs look like a mosaic ({sp.describe()}), but [stack] mosaic is off; "
              "frames that do not overlap the reference frame will be dropped. Consider --mosaic.",
              flush=True)


STAGED_SUBS = "subs"


def stage_subs(subs: Path, process: Path, log=print) -> Path:
    """Siril (1.4.5 on Linux at least) refuses to `cd` into a read-only folder, such as subs
    mounted read-only into a container. Give it <process>/subs instead: one symlink per *.fit
    sub, same names. It is deleted with the rest of <work>/process."""
    staged = process / STAGED_SUBS
    if staged.is_symlink() or staged.is_file():
        staged.unlink()
    elif staged.is_dir():
        shutil.rmtree(staged)
    staged.mkdir(parents=True)
    files = sorted(subs.glob("*.fit"))
    for f in files:
        (staged / f.name).symlink_to(subs / f.name)
    log(f"stack: {subs} is not writable, which Siril cannot work from; linked its {len(files)} "
        f"*.fit files into {staged}")
    return staged


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
    from . import starnet as sn
    work = _abs(work)
    src = tools.require_file(work / STRETCHED_TIF, "stretched TIFF (run `darkroom stretch` first)")
    d = tools.starnet_dir(starnet_dir)
    backend = sn.detect(d)
    if backend is None:
        raise tools.StageError(f"neither starnet2 nor starnet++ found in {d}")
    log = tools.logs_dir(work) / "starnet.log"
    log.write_text(f"[darkroom] {backend.label} in {d}\n")
    print(f"starnet: {backend.label} in {d}", flush=True)
    # Both StarNet versions find their weights relative to their own folder, so they run from it.
    if backend.kind == sn.LEGACY:
        tools.run_tool(backend.command(src, work / STARLESS), log, cwd=d)
        tools.check_outputs([work / STARLESS], log)
        return
    # StarNet2 writes to a temporary name first, so a failed run cannot leave an old starless.tif
    # looking current. Its output is kept as written (LZW-compressed 16-bit TIFF, read with imagecodecs).
    tmp = work / "starless.starnet2-tmp.tif"
    tmp.unlink(missing_ok=True)
    try:
        tools.run_tool(backend.command(src, tmp), log, cwd=d)
        tools.check_outputs([tmp], log)
        check_starless(tmp, src)
        tmp.replace(work / STARLESS)
    finally:
        tmp.unlink(missing_ok=True)


def check_starless(starless: Path, stretched: Path) -> None:
    """Fail clearly if StarNet2's output cannot be read or does not match the input's size."""
    import tifffile
    try:
        with tifffile.TiffFile(starless) as t:
            page = t.pages[0]
            shape = page.shape
            page.asarray()          # decodes; LZW needs the imagecodecs package
        with tifffile.TiffFile(stretched) as t:
            want = t.pages[0].shape
    except Exception as e:
        hint = (" (StarNet2 writes LZW-compressed TIFF; install imagecodecs: pip install imagecodecs)"
                if "imagecodecs" in str(e).lower() else "")
        raise tools.StageError(f"cannot read StarNet's output {starless.name}: {e}{hint}") from None
    def hwc(sh):        # (3, H, W) planar -> (H, W, 3), as finish.load reads it
        sh = tuple(sh)
        return sh[1:] + sh[:1] if len(sh) == 3 and sh[0] == 3 else sh
    if hwc(shape) != hwc(want):
        raise tools.StageError(f"StarNet's output has shape {tuple(shape)}, the input {tuple(want)}")


def starnet_params(starnet_dir: str | None) -> dict:
    """Recorded settings of the starnet stage: the StarNet2 product and version, or nothing for
    StarNet v2.0.2 (and nothing when no StarNet can be found; the stage then fails before running)."""
    from . import starnet as sn
    try:
        backend = sn.detect(tools.starnet_dir(starnet_dir))
    except tools.StageError:
        return {}
    return backend.params() if backend else {}


def finish(work: Path, params: FinishParams, out: str = FINAL) -> None:
    from . import finish as fin
    work = _abs(work)
    orig = tools.require_file(work / STRETCHED_TIF, "stretched TIFF (run `darkroom stretch` first)")
    sl = None
    if params.stars != "keep":
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
                        crop_center=center if kind == "aspect" else None,
                        crop_scale=float(f.get("crop_scale", 1.0)), stars=f.get("stars", "reduce"))


def starnet_needed(finish_table: dict) -> bool:
    """StarNet is skipped when the finish stage keeps the stars as they are."""
    return finish_table.get("stars", "reduce") != "keep"


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
                "keep_round": float(table["keep_round"]),
                # only recorded when in use, so work dirs made before these keys existed stay up to date
                **{k: float(table[k]) for k in ("keep_bkg", "keep_stars") if float(table.get(k, 100)) < 100},
                **({"mosaic": True} if table.get("mosaic") else {})}
    out = {}
    for k in _RESULT_KEYS[stage]:
        v = table[k]
        out[k] = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and k != "erode" else v
    if stage == "finish":
        aspect = parse_crop_spec(out["crop"])[0] == "aspect"
        if not aspect:
            out["crop_center"] = None           # only used by an aspect-ratio crop
        # recorded only when in use, so work dirs made before these keys existed stay up to date
        if table.get("stars", "reduce") != "reduce":
            out["stars"] = table["stars"]
        if aspect and float(table.get("crop_scale", 1.0)) != 1.0:
            out["crop_scale"] = float(table["crop_scale"])
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
    if stage == "finish" and params.get("stars") == "keep":
        names = [STRETCHED_TIF]                 # the starless image is not read
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
