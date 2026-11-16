"""darkroom command-line interface."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__, stages, tools
from .finish import FinishParams, parse_crop

ENV_HELP = """\
environment:
  DARKROOM_SIRIL            siril-cli (default /Applications/Siril.app/Contents/MacOS/siril-cli)
  DARKROOM_GRAXPERT         GraXpert (default /Applications/GraXpert.app/Contents/MacOS/GraXpert)
  DARKROOM_GRAXPERT_MODEL   GraXpert 3.0.2 denoise model
                            (default ~/Library/Application Support/GraXpert/denoise-ai-models/3.0.2/model.onnx)
  DARKROOM_CACHE            cache for the fixed-batch denoise model (default ~/Library/Caches/darkroom)
  DARKROOM_STARNET_DIR      folder containing starnet++ and its weights (no default)
"""


def _crop(s: str):
    try:
        return parse_crop(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def _sub(sp, name, help_):
    return sp.add_parser(name, help=help_, description=help_, epilog=ENV_HELP, formatter_class=_Fmt)


class _Fmt(argparse.RawDescriptionHelpFormatter):
    """Show '(default: X)' only for options that take a value and have a real default."""

    def _get_help_string(self, action):
        h = action.help or ""
        if (action.option_strings and action.nargs != 0 and action.default not in (None, argparse.SUPPRESS)
                and "default" not in h):
            h += " (default: %(default)s)"
        return h


def _work(p):
    p.add_argument("--work", required=True, type=Path, metavar="DIR", help="work directory")


def _stack_args(p, required):
    p.add_argument("--subs", type=Path, required=required, metavar="DIR",
                   help="directory with the light subs (*.fit; other files ignored)")
    p.add_argument("--dark", type=Path, required=required, metavar="FILE", help="master dark")


def _bge_args(p):
    p.add_argument("--smoothing", type=float, default=0.1, help="GraXpert background smoothing")


def _color_args(p):
    p.add_argument("--sensor", default="Sony IMX585", metavar="NAME", help="SPCC OSC sensor name")
    p.add_argument("--filter", dest="filt", default="UV/IR Block", metavar="NAME", help="SPCC OSC filter name")
    p.add_argument("--focal", type=float, default=160.0, help="focal length in mm (plate solve)")
    p.add_argument("--pixel", type=float, default=2.9, help="pixel size in um (plate solve)")
    p.add_argument("--catalog", default="gaia", help="plate-solve catalogue")


def _denoise_args(p):
    p.add_argument("--strength", type=float, default=0.6, help="denoise strength 0..1")
    p.add_argument("--cpu", action="store_true", help="run on CPU instead of CoreML")


def _stretch_args(p):
    p.add_argument("--asinh", type=float, default=25.0, help="asinh -human stretch factor")
    p.add_argument("--shadows", type=float, default=-2.5, help="autostretch shadows clipping (sigma)")
    p.add_argument("--target-bg", type=float, default=0.10, help="autostretch target background")
    p.add_argument("--sat", type=float, default=0.35, help="Siril satu amount")


def _starnet_args(p):
    p.add_argument("--starnet-dir", metavar="DIR", default=None,
                   help="folder containing starnet++ (default: $DARKROOM_STARNET_DIR)")


def _finish_args(p):
    d = FinishParams()
    p.add_argument("--gamma", type=float, default=d.gamma, help=">1 dims star halos more than cores (smaller stars)")
    p.add_argument("--star-strength", type=float, default=d.star_strength, help="star layer multiplier")
    p.add_argument("--mid", type=float, default=d.mid, help="MTF midtone on starless (<0.5 brightens)")
    p.add_argument("--sat", type=float, default=d.sat, help="saturation on starless")
    p.add_argument("--yb", type=float, default=d.yb, help=">1 stretches yellow/blue differences inside the galaxy")
    p.add_argument("--sharpen", type=float, default=d.sharpen, help="unsharp-mask amount on galaxy luminance")
    p.add_argument("--sharpen-radius", type=float, default=d.sharpen_radius, help="unsharp-mask gaussian sigma (px)")
    p.add_argument("--black", type=float, default=d.black, help="black point lift on starless")
    p.add_argument("--erode", type=int, default=d.erode, help="3x3 erosion passes on the star layer")
    p.add_argument("--no-neutral", dest="neutral", action="store_false",
                   help="skip sky neutralization (on by default)")
    _crop_arg(p)
    p.add_argument("--out", default="final", help="output basename inside the work dir (.tif and .jpg)")


def _crop_arg(p):
    p.add_argument("--crop", type=_crop, default=None, metavar="x,y,w,h",
                   help="crop the final image (pixels); default: no crop")


def _finish_params(a) -> FinishParams:
    return FinishParams(gamma=a.gamma, star_strength=a.star_strength, mid=a.mid, sat=a.sat, yb=a.yb,
                        sharpen=a.sharpen, sharpen_radius=a.sharpen_radius, black=a.black,
                        erode=a.erode, neutral=a.neutral, crop=a.crop)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="darkroom", description="Astrophotography pipeline: stack, background "
                                 "extraction, colour calibration, denoise, stretch, star removal, finish.",
                                 epilog=ENV_HELP, formatter_class=_Fmt)
    ap.add_argument("--version", action="version", version=f"darkroom {__version__}")
    sp = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    p = _sub(sp, "stack", "calibrate, register and stack subs with Siril -> stack.fit")
    _work(p); _stack_args(p, True)

    p = _sub(sp, "bge", "GraXpert AI background extraction: stack.fit -> bge.fits, bge_background.fits")
    _work(p); _bge_args(p)

    p = _sub(sp, "color", "Siril plate solve + SPCC: bge.fits -> spcc.fit")
    _work(p); _color_args(p)

    p = _sub(sp, "denoise", "GraXpert 3.0.2 AI denoise via onnxruntime: spcc.fit -> denoised.fits")
    _work(p); _denoise_args(p)

    p = _sub(sp, "stretch", "Siril asinh + autostretch + saturation: denoised.fits -> stretched.fit, stretched.tif")
    _work(p); _stretch_args(p)

    p = _sub(sp, "starnet", "StarNet v2 star removal: stretched.tif -> starless.tif")
    _work(p); _starnet_args(p)

    p = _sub(sp, "finish", "recombine starless + reduced stars, polish, crop: -> final.tif, final.jpg")
    _work(p); _finish_args(p)

    p = _sub(sp, "run", "run stages in order with their default settings")
    _work(p); _stack_args(p, False)
    p.add_argument("--from", dest="from_", choices=stages.STAGES, default=stages.STAGES[0], help="first stage")
    p.add_argument("--to", choices=stages.STAGES, default=stages.STAGES[-1], help="last stage")
    _starnet_args(p)
    p.add_argument("--cpu", action="store_true", help="denoise on CPU instead of CoreML")
    _crop_arg(p)

    p = _sub(sp, "star-reduce", "morphological star reduction on a stretched RGB FITS (non-StarNet fallback)")
    p.add_argument("input", type=Path, help="stretched 3-channel FITS (0..1)")
    p.add_argument("output", type=Path, help="output FITS")
    p.add_argument("--amount", type=float, default=0.85, help="0..1 strength of reduction")
    p.add_argument("--iters", type=int, default=2, help="3x3 erosion passes (bigger = smaller stars)")
    p.add_argument("--radius", type=int, default=6, help="top-hat radius: max star radius in px")
    p.add_argument("--k", type=float, default=4.0, help="detection threshold in noise sigmas")
    p.add_argument("--mask", type=Path, default=None, help="optionally save the star mask here")
    return ap


def _timed(name, fn, *args, **kw) -> float:
    print(f"== {name}", flush=True)
    t = time.time()
    fn(*args, **kw)
    dt = time.time() - t
    print(f"== {name} done in {dt:.1f}s", flush=True)
    return dt


def _dispatch(a) -> None:
    if a.cmd == "star-reduce":
        from . import star_reduce
        star_reduce.run(a.input, a.output, a.amount, a.iters, a.radius, a.k, a.mask)
        return
    a.work.mkdir(parents=True, exist_ok=True)
    if a.cmd == "stack":
        _timed("stack", stages.stack, a.work, a.subs, a.dark)
    elif a.cmd == "bge":
        _timed("bge", stages.bge, a.work, a.smoothing)
    elif a.cmd == "color":
        _timed("color", stages.color, a.work, a.sensor, a.filt, a.focal, a.pixel, a.catalog)
    elif a.cmd == "denoise":
        _timed("denoise", stages.denoise, a.work, a.strength, a.cpu)
    elif a.cmd == "stretch":
        _timed("stretch", stages.stretch, a.work, a.asinh, a.shadows, a.target_bg, a.sat)
    elif a.cmd == "starnet":
        _timed("starnet", stages.starnet, a.work, a.starnet_dir)
    elif a.cmd == "finish":
        _timed("finish", stages.finish, a.work, _finish_params(a), a.out)
    elif a.cmd == "run":
        i, j = stages.STAGES.index(a.from_), stages.STAGES.index(a.to)
        if i > j:
            raise tools.StageError(f"--from {a.from_} comes after --to {a.to}")
        todo = stages.STAGES[i:j + 1]
        if "stack" in todo and (a.subs is None or a.dark is None):
            raise tools.StageError("the stack stage needs --subs and --dark")
        if "starnet" in todo:
            tools.starnet_dir(a.starnet_dir)   # fail before hours of work, not after
        times = {}
        for s in todo:
            if s == "stack":
                times[s] = _timed(s, stages.stack, a.work, a.subs, a.dark)
            elif s == "denoise":
                times[s] = _timed(s, stages.denoise, a.work, cpu=a.cpu)
            elif s == "starnet":
                times[s] = _timed(s, stages.starnet, a.work, a.starnet_dir)
            elif s == "finish":
                times[s] = _timed(s, stages.finish, a.work, FinishParams(crop=a.crop))
            else:
                times[s] = _timed(s, getattr(stages, s), a.work)
        print("timings: " + ", ".join(f"{k} {v:.1f}s" for k, v in times.items())
              + f", total {sum(times.values()):.1f}s")


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        _dispatch(a)
    except tools.StageError as e:
        tools.eprint(f"darkroom {a.cmd}: error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
