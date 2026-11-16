"""Pipeline stages. Each reads/writes fixed filenames inside the work directory."""
from __future__ import annotations

from pathlib import Path

from . import siril, tools
from .finish import FinishParams

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


def _abs(p) -> Path:
    return Path(p).expanduser().resolve()


def stack(work: Path, subs: Path, dark: Path) -> None:
    work, subs, dark = _abs(work), _abs(subs), _abs(dark)
    if not subs.is_dir():
        raise tools.StageError(f"subs directory not found: {subs}")
    if not any(subs.glob("*.fit")):
        raise tools.StageError(f"no *.fit files in {subs}")
    tools.require_file(dark, "master dark")
    process = work / "process"
    process.mkdir(parents=True, exist_ok=True)
    script = siril.render_file("stack.ssf", work, {
        "SUBS": str(subs), "PROCESS": str(process), "DARK": str(dark),
        "STACK": str(work / Path(STACK).stem),
    })
    log = tools.logs_dir(work) / "stack.log"
    siril.run_script(script, log)
    tools.check_outputs([work / STACK], log)


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
          catalog="gaia") -> None:
    work = _abs(work)
    tools.require_file(work / BGE, "background-extracted image (run `darkroom bge` first)")
    script = siril.render_file("color.ssf", work, {
        "WORK": str(work), "FOCAL": siril.fmt_num(focal), "PIXEL": siril.fmt_num(pixel),
        "CATALOG": catalog, "SENSOR": sensor, "FILTER": filt,
    })
    log = tools.logs_dir(work) / "color.log"
    siril.run_script(script, log)
    tools.check_outputs([work / SPCC], log)


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


def finish(work: Path, params: FinishParams, out: str = "final") -> None:
    from . import finish as fin
    work = _abs(work)
    orig = tools.require_file(work / STRETCHED_TIF, "stretched TIFF (run `darkroom stretch` first)")
    sl = tools.require_file(work / STARLESS, "starless TIFF (run `darkroom starnet` first)")
    if Path(out).name != out:
        raise tools.StageError("--out is a basename inside the work directory, not a path")
    with tools.StageLog(work, "finish") as log:
        log(f"params: {params}")
        fin.run(orig, sl, work / out, params, log)
    tools.check_outputs([work / f"{out}.tif", work / f"{out}.jpg"], log.path)
