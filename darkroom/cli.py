"""darkroom command-line interface."""
from __future__ import annotations

import argparse
import sys
import time
from collections import namedtuple
from pathlib import Path

from . import __version__, config, doctor, get_starnet, installer, stages, tools, toolset
from .finish import FinishParams, parse_aspect, parse_center, parse_crop

def env_help() -> str:
    """The settings/environment epilog, showing this platform's actual defaults."""
    return f"""\
settings:
  A flag given on the command line wins. Anything not given comes from
  <work>/darkroom.toml (see `darkroom init`), then the look preset, then the
  camera preset, then the built-in default shown above.

environment (defaults for this platform, {"macOS" if tools.is_macos() else "Linux"}):
  DARKROOM_SIRIL            siril-cli (default: the one `darkroom setup` installed, else
                            {tools.default_siril()})
  DARKROOM_GRAXPERT         GraXpert (default: the one `darkroom setup` installed, else
                            {tools.default_graxpert()})
  DARKROOM_TOOLS_DIR        where `darkroom setup` installs Siril and GraXpert
                            (default {toolset.default_tools_dir()})
  DARKROOM_GRAXPERT_MODEL   GraXpert 3.0.2 denoise model; `darkroom fetch-models` downloads it
                            (default {tools.default_graxpert_model()})
  DARKROOM_DECONV_MODEL     GraXpert 1.0.1 object deconvolution model, for the optional deconv
                            stage; `darkroom fetch-models --deconv` downloads it
                            (default {tools.default_deconv_model()})
  DARKROOM_CACHE           cache for the fixed-batch denoise model and StarNet2 installed by
                            `darkroom get-starnet` (default {tools.default_cache()})
  DARKROOM_STARNET_DIR      folder containing starnet2 (StarNet2 2.6) or starnet++ (StarNet v2.0.2);
                            used when neither --starnet-dir nor [starnet] dir is set
                            (default: the newest StarNet2 installed by `darkroom get-starnet`)
  DARKROOM_SIRIL_CACHE      write fallback star catalogues here instead of the folder named in
                            Siril's log (for testing)
"""


FETCH_MODELS_EPILOG = """\
GraXpert downloads its AI models itself the first time it needs them. The
background-extraction model arrives on the first `darkroom bge`. The denoise
model (3.0.2) only arrives when GraXpert's own denoise runs, which darkroom
otherwise never does, so this runs it once on a tiny synthetic image in a
temporary folder and then checks that the model file exists. `darkroom denoise`
does the same by itself when the model is missing.

--deconv does the same for the object deconvolution model (1.0.1), which only the
optional deconv stage uses; `darkroom deconv` also fetches it itself when missing.
Both models are GraXpert's (CC BY-NC-SA 4.0) and come from GraXpert's server.

environment: DARKROOM_GRAXPERT, DARKROOM_GRAXPERT_MODEL, DARKROOM_DECONV_MODEL (see `darkroom --help`)
"""


Aspect = namedtuple("Aspect", "w h")


def _crop(s: str):
    """--crop value: None for 'none', (x, y, w, h) for pixels, Aspect(w, h) for 'W:H'."""
    try:
        if s.strip().lower() == "none":
            return None
        if ":" in s:
            return Aspect(*parse_aspect(s))
        return parse_crop(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def _center(s: str):
    try:
        return "%d,%d" % parse_center(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def _crop_spec(v) -> str:
    if v is None:
        return "none"
    if isinstance(v, Aspect):
        return f"{v.w}:{v.h}"
    return ",".join(str(int(x)) for x in v)


def _explicit(cls):
    """An argparse action that also records that its flag was given, so that argparse
    defaults (kept for --help) never override the config file."""
    class Action(cls):
        def __call__(self, parser, namespace, values, option_string=None):
            super().__call__(parser, namespace, values, option_string)
            given = getattr(namespace, "_given", None)
            if given is None:
                given = set()
                setattr(namespace, "_given", given)
            given.add(self.dest)
    return Action


_ACTIONS = {"store": _explicit(argparse._StoreAction), "store_true": _explicit(argparse._StoreTrueAction),
            "store_false": _explicit(argparse._StoreFalseAction),
            "store_const": _explicit(argparse._StoreConstAction)}


class _Opts:
    """Adds options to a parser and remembers which config key each one sets."""

    def __init__(self, parser, group=None):
        self.parser = parser
        self.target = group or parser
        self.keys = parser.get_default("_keys")
        if self.keys is None:
            self.keys = {}
            parser.set_defaults(_keys=self.keys)

    def group(self, g):
        return _Opts(self.parser, g)

    def add(self, *flags, key=None, action="store", **kw):
        act = self.target.add_argument(*flags, action=_ACTIONS[action], **kw)
        if key:
            self.keys.setdefault(act.dest, []).append(key)
        return act


class _Fmt(argparse.RawDescriptionHelpFormatter):
    """Show '(default: X)' only for options that take a value and have a real default."""

    def _get_help_string(self, action):
        h = action.help or ""
        if (action.option_strings and action.nargs != 0 and action.default not in (None, argparse.SUPPRESS)
                and "default" not in h):
            h += " (default: %(default)s)"
        return h


def _sub(sp, name, help_, epilog=None):
    p = sp.add_parser(name, help=help_, description=help_, epilog=epilog or env_help(), formatter_class=_Fmt)
    return p, _Opts(p)


def _work(o):
    o.parser.add_argument("--work", required=True, type=Path, metavar="DIR", help="work directory")
    o.parser.add_argument("--config", type=Path, default=None, metavar="FILE",
                          help="settings file (default: <work>/darkroom.toml if it exists)")


def _stack_args(o, run=False):
    o.add("--subs", type=Path, key=("target", "subs"), metavar="DIR",
          help="directory with the light subs (*.fit; other files ignored)")
    g = o.group(o.parser.add_mutually_exclusive_group())
    g.add("--dark", key=("target", "dark"), metavar="FILE",
          help='master dark; also "auto" (pick from the dark library) or "none"')
    g.add("--dark-library", type=Path, key=("target", "dark_library"), metavar="DIR",
          help="master-dark library; picks the dark matching the subs' exposure, gain and temperature")
    g.add("--no-dark", dest="dark", action="store_const", const="none", key=("target", "dark"),
          help="stack without dark subtraction")
    if run:
        return
    o.add("--framing", choices=config.FRAMINGS, default="min", key=("stack", "framing"),
          help="min: area common to all frames; cog: frame at the centre of gravity; max: bounding box")
    o.add("--keep-fwhm", type=float, default=90, key=("stack", "keep_fwhm"), metavar="PCT",
          help="percent of frames kept by weighted FWHM")
    o.add("--keep-round", type=float, default=90, key=("stack", "keep_round"), metavar="PCT",
          help="percent of frames kept by roundness")
    o.add("--keep-bkg", type=float, default=100, key=("stack", "keep_bkg"), metavar="PCT",
          help="percent of frames kept by background level, darkest first (drops cloud and haze)")
    o.add("--keep-stars", type=float, default=100, key=("stack", "keep_stars"), metavar="PCT",
          help="percent of frames kept by number of stars detected, most first")
    o.add("--clean", action="store_true", key=("stack", "clean"),
          help="delete <work>/process after a successful stack")
    g = o.group(o.parser.add_mutually_exclusive_group())
    g.add("--mosaic", dest="mosaic", action="store_true", key=("stack", "mosaic"),
          help="the subs are a mosaic: plate-solve every frame instead of registering against one, stack "
               "onto one canvas (framing is then max) and crop it to the area with data everywhere")
    g.add("--no-mosaic", dest="mosaic", action="store_false", key=("stack", "mosaic"),
          help="register and stack normally (overrides [stack] mosaic = true)")
    g = o.parser.add_mutually_exclusive_group()
    g.add_argument("--force", action="store_true",
                   help="delete <work>/process from an earlier run first (otherwise that is an error)")
    g.add_argument("--reuse-registration", dest="reuse", action="store_true",
                   help="re-frame and re-stack the registered frames already in <work>/process "
                        "(for trying another --framing or --keep-*; same subs and dark)")
    o.parser.add_argument("--out", default="stack", metavar="NAME",
                          help="output basename inside the work dir (.fit); later stages read stack.fit")


def _bge_args(o):
    o.add("--smoothing", type=float, default=0.1, key=("bge", "smoothing"), help="GraXpert background smoothing")


def _color_args(o):
    o.add("--sensor", default="Sony IMX585", metavar="NAME", key=("color", "sensor"), help="SPCC OSC sensor name")
    o.add("--filter", dest="filt", default="UV/IR Block", metavar="NAME", key=("color", "filter"),
          help="SPCC OSC filter name")
    o.add("--focal", type=float, default=160.0, key=("color", "focal"), help="focal length in mm (plate solve)")
    o.add("--pixel", type=float, default=2.9, key=("color", "pixel"), help="pixel size in um (plate solve)")
    o.add("--catalog", default="gaia", key=("color", "catalog"), help="plate-solve catalogue")
    o.add("--whiteref", default="Average Spiral Galaxy", metavar="NAME", key=("color", "whiteref"),
          help="SPCC white reference")
    o.add("--no-catalog-fallback", dest="catalog_fallback", action="store_false",
          key=("color", "catalog_fallback"),
          help="do not fetch Gaia DR3 from the ESA archive when Siril's catalogue server is unreachable")


def _denoise_args(o):
    o.add("--strength", type=float, default=0.6, key=("denoise", "strength"), help="denoise strength 0..1")
    o.add("--cpu", action="store_true", key=("denoise", "cpu"), help="run on the CPU instead of the GPU (CoreML on macOS, CUDA if onnxruntime has it)")


def _psf(s: str):
    """--psf value: "auto" or a star FWHM in pixels."""
    if s.strip().lower() == "auto":
        return "auto"
    try:
        v = float(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f'expected a FWHM in pixels or "auto", got {s!r}') from None
    if not 0 < v <= 50:
        raise argparse.ArgumentTypeError(f"the FWHM is in pixels (0 < value <= 50), got {s!r}")
    return v


def _deconv_args(o):
    o.add("--strength", type=float, default=0.0, key=("deconv", "strength"),
          help="deconvolution strength 0..1; 0 = off")
    o.add("--psf", type=_psf, default="auto", metavar="FWHM", key=("deconv", "psf"),
          help='star FWHM in pixels, or "auto" to measure it from denoised.fits')
    o.add("--cpu", action="store_true", key=("deconv", "cpu"), help="run on the CPU instead of the GPU (CoreML on macOS, CUDA if onnxruntime has it)")


def _stretch_args(o):
    o.add("--asinh", type=float, default=25.0, key=("stretch", "asinh"), help="asinh -human stretch factor")
    o.add("--shadows", type=float, default=-2.5, key=("stretch", "shadows"),
          help="autostretch shadows clipping (sigma)")
    o.add("--target-bg", type=float, default=0.10, key=("stretch", "target_bg"), help="autostretch target background")
    o.add("--sat", type=float, default=0.35, key=("stretch", "sat"), help="Siril satu amount")


def _starnet_args(o):
    o.add("--starnet-dir", metavar="DIR", default=None, key=("starnet", "dir"),
          help="folder containing starnet2 (StarNet2 2.6) or starnet++ (StarNet v2.0.2); if both, starnet2 "
               "is used (default: [starnet] dir, then $DARKROOM_STARNET_DIR, then the newest StarNet2 "
               "installed by `darkroom get-starnet`)")


def _finish_args(o):
    d = FinishParams()
    o.add("--gamma", type=float, default=d.gamma, key=("finish", "gamma"),
          help=">1 dims star halos more than cores (smaller stars)")
    o.add("--star-strength", type=float, default=d.star_strength, key=("finish", "star_strength"),
          help="star layer multiplier")
    o.add("--mid", type=float, default=d.mid, key=("finish", "mid"), help="MTF midtone on starless (<0.5 brightens)")
    o.add("--sat", type=float, default=d.sat, key=("finish", "sat"), help="saturation on starless")
    o.add("--yb", type=float, default=d.yb, key=("finish", "yb"),
          help=">1 stretches yellow/blue differences inside the galaxy")
    o.add("--sharpen", type=float, default=d.sharpen, key=("finish", "sharpen"),
          help="unsharp-mask amount on galaxy luminance")
    o.add("--sharpen-radius", type=float, default=d.sharpen_radius, key=("finish", "sharpen_radius"),
          help="unsharp-mask gaussian sigma (px)")
    o.add("--black", type=float, default=d.black, key=("finish", "black"), help="black point lift on starless")
    o.add("--erode", type=int, default=d.erode, key=("finish", "erode"), help="3x3 erosion passes on the star layer")
    o.add("--no-neutral", dest="neutral", action="store_false", key=("finish", "neutral"),
          help="skip sky neutralization (on by default)")
    _crop_arg(o)
    o.parser.add_argument("--out", default="final", metavar="NAME",
                          help="output basename inside the work dir (.tif and .jpg)")


def _crop_arg(o):
    o.add("--crop", type=_crop, default=None, metavar="CROP", key=("finish", "crop"),
          help='crop the final image: pixels "x,y,w,h", or an aspect ratio "W:H" (e.g. 2:3) for the '
               'largest such box centred on the target, or "none" (default: no crop)')
    o.add("--crop-center", type=_center, default=None, metavar="x,y", key=("finish", "crop_center"),
          help="centre for an aspect-ratio crop (default: brightest part of the blurred starless image)")
    o.add("--crop-scale", type=float, default=1.0, metavar="FRACTION", key=("finish", "crop_scale"),
          help="aspect-ratio crop: fraction of the largest box that fits (lower it for a small target)")
    o.add("--stars", choices=("reduce", "keep"), default="reduce", key=("finish", "stars"),
          help="reduce: remove stars with StarNet, enhance, add them back smaller; "
               "keep: no star removal (for clusters; StarNet is not run)")


def _finish_params(a) -> FinishParams:
    """FinishParams straight from parsed flags (no config file); kept for tests and scripts."""
    crop = a.crop
    return FinishParams(gamma=a.gamma, star_strength=a.star_strength, mid=a.mid, sat=a.sat, yb=a.yb,
                        sharpen=a.sharpen, sharpen_radius=a.sharpen_radius, black=a.black,
                        erode=a.erode, neutral=a.neutral,
                        crop=None if isinstance(crop, Aspect) else crop,
                        crop_aspect=tuple(crop) if isinstance(crop, Aspect) else None,
                        crop_center=parse_center(a.crop_center) if getattr(a, "crop_center", None) else None,
                        crop_scale=getattr(a, "crop_scale", 1.0), stars=getattr(a, "stars", "reduce"))


INIT_HELP = """\
Reads the FITS headers of the subs (object, exposure, gain, filter, sensor
temperature, focal length, pixel size, Bayer pattern, and the pointing of each
frame to tell whether the subs are a mosaic) and writes
<work>/darkroom.toml with every setting listed and commented. Site coordinates
and the telescope/device name in the headers are never stored, printed or
written.
"""


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="darkroom", description="Astrophotography pipeline: stack, background "
                                 "extraction, colour calibration, denoise, optional deconvolution, stretch, star removal, finish.",
                                 epilog=env_help(), formatter_class=_Fmt)
    ap.add_argument("--version", action="version", version=f"darkroom {__version__}")
    sp = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    p, o = _sub(sp, "init", "write a commented darkroom.toml for a new target from the subs' FITS headers",
                epilog=INIT_HELP)
    p.add_argument("--work", required=True, type=Path, metavar="DIR", help="work directory (created if missing)")
    p.add_argument("--subs", required=True, type=Path, metavar="DIR", help="directory with the light subs")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dark", type=Path, metavar="FILE", help="master dark to use")
    g.add_argument("--dark-library", type=Path, metavar="DIR",
                   help="master-dark library; the dark is picked automatically at stack time")
    g.add_argument("--no-dark", action="store_true", help="stack without dark subtraction")
    p.add_argument("--camera", metavar="NAME", help="camera preset name or .toml path "
                   "(default: detected from the FITS header)")
    p.add_argument("--look", metavar="NAME", default="galaxy", help="look preset name or .toml path")
    p.add_argument("--name", metavar="TEXT", help="target name (default: OBJECT from the FITS header)")
    p.add_argument("--force", action="store_true", help="overwrite an existing darkroom.toml")

    p, o = _sub(sp, "stack", "calibrate, register and stack subs with Siril -> stack.fit")
    _work(o); _stack_args(o)

    p, o = _sub(sp, "bge", "GraXpert AI background extraction: stack.fit -> bge.fits, bge_background.fits")
    _work(o); _bge_args(o)

    p, o = _sub(sp, "color", "Siril plate solve + SPCC: bge.fits -> spcc.fit")
    _work(o); _color_args(o)

    p, o = _sub(sp, "denoise", "GraXpert 3.0.2 AI denoise via onnxruntime: spcc.fit -> denoised.fits")
    _work(o); _denoise_args(o)

    p, o = _sub(sp, "deconv", "optional GraXpert object deconvolution via onnxruntime (off unless [deconv] "
                "strength > 0): denoised.fits -> deconvolved.fits")
    _work(o); _deconv_args(o)

    p, o = _sub(sp, "stretch", "Siril asinh + autostretch + saturation: denoised.fits (deconvolved.fits when "
                "[deconv] is on) -> stretched.fit, stretched.tif")
    _work(o); _stretch_args(o)

    p, o = _sub(sp, "starnet", "StarNet star removal (StarNet2 2.6 or StarNet v2.0.2): stretched.tif -> starless.tif")
    _work(o); _starnet_args(o)

    p, o = _sub(sp, "finish", "recombine starless + reduced stars, polish, crop: -> final.tif, final.jpg")
    _work(o); _finish_args(o)

    p, o = _sub(sp, "run", "run the stages in order, skipping those whose output is up to date")
    _work(o); _stack_args(o, run=True)
    o.add("--from", dest="from_", choices=stages.STAGES, default=stages.STAGES[0], metavar="STAGE",
          help="start at this stage and re-run it and everything after it, up to date or not "
               "(stages: " + ", ".join(stages.STAGES) + ")")
    p.add_argument("--to", choices=stages.STAGES, default=stages.STAGES[-1], metavar="STAGE", help="last stage")
    p.add_argument("--force", action="store_true", help="re-run every stage, up to date or not")
    _starnet_args(o)
    act = o.add("--cpu", action="store_true", key=("denoise", "cpu"),
                help="denoise (and deconvolve, when [deconv] is on) on the CPU instead of the GPU "
                     "(CoreML on macOS, CUDA if onnxruntime has it)")
    o.keys[act.dest].append(("deconv", "cpu"))
    _crop_arg(o)

    p, o = _sub(sp, "star-reduce", "morphological star reduction on a stretched RGB FITS (non-StarNet fallback)")
    p.add_argument("input", type=Path, help="stretched 3-channel FITS (0..1)")
    p.add_argument("output", type=Path, help="output FITS")
    p.add_argument("--amount", type=float, default=0.85, help="0..1 strength of reduction")
    p.add_argument("--iters", type=int, default=2, help="3x3 erosion passes (bigger = smaller stars)")
    p.add_argument("--radius", type=int, default=6, help="top-hat radius: max star radius in px")
    p.add_argument("--k", type=float, default=4.0, help="detection threshold in noise sigmas")
    p.add_argument("--mask", type=Path, default=None, help="optionally save the star mask here")
    doctor.add_parser(sp)
    get_starnet.add_parser(sp)
    installer.add_parser(sp)
    p = sp.add_parser("fetch-models", help="make GraXpert download its AI denoise model 3.0.2",
                      description="make GraXpert download its AI denoise model 3.0.2",
                      epilog=FETCH_MODELS_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--deconv", action="store_true",
                   help="also its object deconvolution model 1.0.1 (279 MB), for the optional deconv stage")
    p.set_defaults(func=_fetch_models)
    return ap


def _fetch_models(a) -> int:
    from . import models
    models.fetch_denoise_model()
    if getattr(a, "deconv", False):
        models.fetch_deconv_model()
    return 0


# ---------------------------------------------------------------- settings

def _overrides(a) -> dict:
    """Config keys set by flags that were actually given on the command line."""
    out = {}
    keys = getattr(a, "_keys", {})
    for dest in getattr(a, "_given", ()):
        for key in keys.get(dest, ()):
            value = getattr(a, dest)
            if key == ("finish", "crop"):
                value = _crop_spec(value)
            elif isinstance(value, Path):
                value = str(value)
            # a dest shared by several flags (--dark / --no-dark) maps to one key; skip unrelated ones
            if key == ("target", "dark_library") and dest != "dark_library":
                continue
            out[key] = value
    if ("target", "dark_library") in out and ("target", "dark") not in out:
        out[("target", "dark")] = "auto"
    return out


def _settings(a) -> config.Settings:
    return config.resolve(a.work, a.config, _overrides(a))


def _stack_table(s: config.Settings) -> dict:
    """[stack] settings plus the subs folder and the dark to use (auto-selected if asked)."""
    from . import darks
    t = s["target"]
    if not t["subs"] or not t["dark"]:
        raise tools.StageError("the stack stage needs --subs and --dark (or --dark-library / --no-dark), "
                               "or [target] subs and dark in darkroom.toml")
    subs = Path(t["subs"]).expanduser().resolve()
    glob = s.camera_meta["camera"]["sub_glob"]
    if t["dark"] == "none":
        dark = None
        print("dark: none (stacking without dark subtraction)", flush=True)
    elif t["dark"] == "auto":
        if not t["dark_library"]:
            raise tools.StageError('dark = "auto" needs a dark library: set [target] dark_library or pass '
                                   "--dark-library DIR (or give a master dark with --dark FILE)")
        if not subs.is_dir():
            raise tools.StageError(f"subs directory not found: {subs}")
        files = darks.list_subs(subs, glob) or darks.list_subs(subs, "*.fit")
        if not files:
            raise tools.StageError(f"no *.fit files in {subs}")
        sv = darks.survey(files)
        if len(sv.exposures) > 1:
            raise tools.StageError("the subs have more than one exposure time ("
                                   + ", ".join(f"{e:g} s" for e in sv.exposures)
                                   + "); stack them separately or give one master dark with --dark")
        dark = darks.select_dark(Path(t["dark_library"]).expanduser(), s.camera_meta["darks"], sv.exposure,
                                 sv.gain, sv.temp_median, log=lambda m: print(m, flush=True))
        dark = dark.resolve()
    else:
        dark = Path(t["dark"]).expanduser().resolve()
    table = {"subs": subs, "dark": dark, "sub_glob": glob, **s["stack"]}
    if table["mosaic"] and table["framing"] != "max":
        if s.sources[("stack", "framing")] != "built-in":
            print(f"WARNING: [stack] framing = {table['framing']} ({s.sources[('stack', 'framing')]}) is "
                  "ignored: a mosaic is always stacked with framing max", flush=True)
        table["framing"] = "max"
    return table


def _starnet_dir(s: config.Settings):
    return s["starnet"]["dir"]


def _call(stage: str, work: Path, s: config.Settings, table: dict | None = None, **extra) -> None:
    """Run one stage with resolved settings (table: the stack table when stage is 'stack')."""
    if stage == "stack":
        stages.stack(work, table["subs"], table["dark"], table["framing"], table["keep_fwhm"],
                     table["keep_round"], table["clean"], sub_glob=table["sub_glob"],
                     keep_bkg=table["keep_bkg"], keep_stars=table["keep_stars"], mosaic=table["mosaic"],
                     focal=s["color"]["focal"], pixel=s["color"]["pixel"], **extra)
    elif stage == "bge":
        stages.bge(work, s["bge"]["smoothing"])
    elif stage == "color":
        c = s["color"]
        stages.color(work, c["sensor"], c["filter"], c["focal"], c["pixel"], c["catalog"], c["whiteref"],
                     c["catalog_fallback"])
    elif stage == "denoise":
        stages.denoise(work, s["denoise"]["strength"], s["denoise"]["cpu"])
    elif stage == "deconv":
        d = s["deconv"]
        stages.deconv(work, d["strength"], d["psf"], d["cpu"])
    elif stage == "stretch":
        t = s["stretch"]
        stages.stretch(work, t["asinh"], t["shadows"], t["target_bg"], t["sat"],
                       src=stages.stretch_source(s["deconv"]))
    elif stage == "starnet":
        stages.starnet(work, _starnet_dir(s))
    elif stage == "finish":
        stages.finish(work, stages.finish_params(s["finish"]), **extra)


def _params(stage: str, s: config.Settings, table: dict | None = None) -> dict:
    if stage == "starnet":
        return stages.result_params(stage, s[stage]) | stages.starnet_params(_starnet_dir(s))
    if stage == "stretch":
        return stages.stretch_params(s["stretch"], s["deconv"])
    return stages.result_params(stage, table if stage == "stack" else s[stage])


def _timed(name, fn, *args, **kw) -> float:
    print(f"== {name}", flush=True)
    t = time.time()
    fn(*args, **kw)
    dt = time.time() - t
    print(f"== {name} done in {dt:.1f}s", flush=True)
    return dt


# -------------------------------------------------------------------- init

def _init(a) -> None:
    from . import darks
    work = a.work.expanduser().resolve()
    dest = work / config.CONFIG_NAME
    if dest.exists() and not a.force:
        raise tools.StageError(f"{dest} already exists; pass --force to overwrite it")
    subs = a.subs.expanduser().resolve()
    if not subs.is_dir():
        raise tools.StageError(f"subs directory not found: {subs}")
    every = darks.list_subs(subs, "*.fit") or darks.list_subs(subs, "*.fits")
    if not every:
        raise tools.StageError(f"no *.fit files in {subs}")
    notes = []

    camera, how = a.camera, "given"
    if camera is None:
        tables = {n: config.load_camera(n)[1]["camera"] for n in config.preset_names("cameras")}
        camera, how = darks.detect_camera(every[0], tables), "detected from the FITS header"
        if camera is None:
            notes.append("no camera preset matches these subs; using the built-in defaults "
                         "(check [color] sensor, filter, focal and pixel)")
    explicit = {}
    if camera:
        explicit[("target", "camera")] = camera
    explicit[("target", "look")] = a.look
    base = config.resolve(work, None, {k: v for k, v in explicit.items()}, use_file=False)
    meta = base.camera_meta

    glob = meta["camera"]["sub_glob"]
    files = darks.list_subs(subs, glob)
    if not files:
        notes.append(f"no file in the subs folder matches {glob}; using every .fit file")
        files, glob = every, "*.fit"
    sv = darks.survey(files)

    name = a.name or sv.object
    ordered = {}
    if name:
        ordered[("target", "name")] = name
    ordered[("target", "subs")] = str(subs)
    dark_line = "not set"
    if a.no_dark:
        ordered[("target", "dark")] = "none"
        dark_line = "none (no dark subtraction)"
    elif a.dark:
        d = a.dark.expanduser().resolve()
        tools.require_file(d, "master dark")
        ordered[("target", "dark")] = str(d)
        info = darks.read_dark(d, meta["darks"]["filename"])
        dark_line = str(d)
        if info.exposure is not None and sv.exposure is not None and abs(info.exposure - sv.exposure) > 0.01:
            notes.append(f"the master dark is {info.exposure:g} s but the subs are {sv.exposure:g} s")
        if info.temp is not None and sv.temp_median is not None:
            dark_line += f" ({info.temp:.1f} C, {info.temp - sv.temp_median:+.1f} C from the subs)"
    elif a.dark_library:
        lib = a.dark_library.expanduser().resolve()
        ordered[("target", "dark")] = "auto"
        ordered[("target", "dark_library")] = str(lib)
        picked = []
        try:
            d = darks.select_dark(lib, meta["darks"], sv.exposure, sv.gain, sv.temp_median, log=picked.append)
            dark_line = f"auto, from {lib}\n" + "\n".join("                " + m for m in picked)
        except tools.StageError as e:
            dark_line = f"auto, from {lib}"
            notes.append(f"no dark can be picked yet: {e}")
    else:
        ordered[("target", "dark")] = "auto"
        notes.append("no dark given: set [target] dark_library (or dark) in the config before stacking")
        dark_line = "auto, but no dark library set yet"
    ordered.update(explicit)

    filt_line = ", ".join(sv.filters) or "not in the header"
    if len(sv.filters) == 1:
        spcc = meta["filters"].get(sv.filters[0])
        if spcc:
            ordered[("color", "filter")] = spcc
            filt_line += f' -> SPCC filter "{spcc}"'
        elif camera:
            notes.append(f"FITS filter {sv.filters[0]!r} is not in the camera preset's filter map; "
                         "set [color] filter to the Siril SPCC filter name")
    elif len(sv.filters) > 1:
        notes.append("the subs were taken through more than one filter: " + ", ".join(sv.filters))
    if sv.focal:
        ordered[("color", "focal")] = round(sv.focal, 1)
        if abs(sv.focal - base["color"]["focal"]) > 0.02 * base["color"]["focal"] and camera:
            notes.append(f"header focal length {sv.focal:g} mm differs from the camera preset "
                         f"({base['color']['focal']:g} mm); using the header value")
    if sv.pixel:
        ordered[("color", "pixel")] = round(sv.pixel, 2)
    want = meta["camera"].get("bayer")
    if want and sv.bayer and want != sv.bayer:
        notes.append(f"Bayer pattern in the header is {sv.bayer}, the camera preset says {want}")
    from . import mosaic
    sp = mosaic.detect(files, sv.focal or base["color"]["focal"], sv.pixel or base["color"]["pixel"])
    mosaic_line = None
    if sp and sp.is_mosaic:
        ordered[("stack", "mosaic")] = True
        mosaic_line = f"yes: {sp.describe()}"
        notes.append("the subs look like a mosaic (the mount moved between frames), so [stack] mosaic = true: "
                     "every frame is plate-solved and stacked onto one canvas, cropped to the area with data; "
                     "set it to false to register and stack as usual")
    if len(sv.exposures) > 1:
        notes.append("the subs have more than one exposure time: " + ", ".join(f"{e:g} s" for e in sv.exposures))
    if len(sv.gains) > 1:
        notes.append("the subs have more than one gain: " + ", ".join(f"{g:g}" for g in sv.gains))

    def temp():
        if sv.temp_median is None:
            return "not in the header"
        return f"median {sv.temp_median:.1f} C (range {sv.temp_min:.1f} to {sv.temp_max:.1f})"
    exposure = (f"{sv.exposure:g} s each, {darks.format_duration(sv.total_seconds)} in total"
                if sv.exposure is not None else "mixed")
    summary = [
        ("target", name or "not in the header"),
        ("subs", f"{sv.count} frames matching {glob}"),
        ("exposure", exposure),
        ("gain", ", ".join(f"{g:g}" for g in sv.gains) or "not in the header"),
        ("filter", filt_line),
        ("sensor temp", temp()),
        ("optics", ", ".join(x for x in (
            f"focal length {sv.focal:g} mm" if sv.focal else "",
            f"pixel {round(sv.pixel, 2):g} um" if sv.pixel else "",
            f"Bayer {sv.bayer}" if sv.bayer else "") if x) or "not in the header"),
        ("camera", f"{camera} ({how})" if camera else "no preset"),
        ("look", a.look),
        ("dark", dark_line),
    ] + ([("mosaic", mosaic_line)] if mosaic_line else [])
    head = [f"darkroom.toml for {name or 'a new target'}, written by `darkroom init`.",
            f"Subs: {sv.count} x {exposure}; gain {summary[3][1]}; filter {', '.join(sv.filters) or '?'}; "
            f"sensor {temp()}."] + ([f"Mosaic detected from the pointings in the headers ({sp.describe()})."]
                                    if mosaic_line else [])
    effective = config.resolve(work, None, dict(ordered), use_file=False)
    work.mkdir(parents=True, exist_ok=True)
    dest.write_text(config.render_config(ordered, effective, head))
    print(f"wrote {dest}")
    for k, v in summary:
        print(f"  {k:<13} {v}")
    for n in notes:
        print(f"NOTE: {n}")
    print(f"next: look over {dest.name} (crop, [starnet] dir), then `darkroom run --work {a.work}`")


# ---------------------------------------------------------------- dispatch

def _run(a) -> None:
    s = _settings(a)
    i, j = stages.STAGES.index(a.from_), stages.STAGES.index(a.to)
    if i > j:
        raise tools.StageError(f"--from {a.from_} comes after --to {a.to}")
    todo = stages.STAGES[i:j + 1]
    force_all = a.force or "from_" in getattr(a, "_given", ())
    table = None
    if "stack" in todo:
        try:
            table = _stack_table(s)
        except tools.StageError as e:
            # e.g. the dark-library or subs drive is unplugged: an existing stack made with the
            # same subs folder and [stack] settings is kept rather than failing the whole run
            old = stages.recorded_params(a.work, "stack")
            t = s["target"]
            same = False
            if old is not None and t["subs"]:
                now = stages.result_params("stack", {"subs": Path(t["subs"]).expanduser().resolve(),
                                                     "dark": old.get("dark"), **s["stack"],
                                                     **({"framing": "max"} if s["stack"]["mosaic"] else {})})
                same = now == old
            if force_all or not same or not all(p.is_file() for p in stages.outputs(a.work, "stack")):
                raise
            print(f"-- stack: kept as it is; its dark cannot be checked ({str(e).splitlines()[0]})", flush=True)
            todo = todo[1:]
    times, ran_any, starnet_checked = {}, False, False
    use_starnet = stages.starnet_needed(s["finish"])
    for stage in todo:
        if stage == "starnet" and not use_starnet:
            print('-- starnet: not needed ([finish] stars = "keep")', flush=True)
            continue
        if stage == "deconv" and not stages.deconv_on(s["deconv"]):
            print("-- deconv: off ([deconv] strength = 0)", flush=True)
            continue
        params = _params(stage, s, table)
        forced = force_all or ran_any
        if not forced:
            ok, why = stages.up_to_date(a.work, stage, params)
            if ok:
                print(f"-- {stage}: up to date, skipped" + (f" ({why})" if why else ""), flush=True)
                continue
            print(f"-- {stage}: has to run ({why})", flush=True)
        if use_starnet and "starnet" in todo[todo.index(stage):] and not starnet_checked:
            tools.starnet_dir(_starnet_dir(s))       # fail before hours of work, not after
            starnet_checked = True
        stages.forget_params(a.work, stage)
        extra = {"force": force_all} if stage == "stack" else {}
        times[stage] = _timed(stage, _call, stage, a.work, s, table, **extra)
        stages.record_params(a.work, stage, params)
        ran_any = True
    if times:
        print("timings: " + ", ".join(f"{k} {v:.1f}s" for k, v in times.items())
              + f", total {sum(times.values()):.1f}s")
    else:
        print("nothing to do: every stage is up to date")


def _dispatch(a) -> None:
    if a.cmd == "star-reduce":
        from . import star_reduce
        star_reduce.run(a.input, a.output, a.amount, a.iters, a.radius, a.k, a.mask)
        return
    if a.cmd == "init":
        _init(a)
        return
    a.work.mkdir(parents=True, exist_ok=True)
    if a.cmd == "run":
        _run(a)
        return
    s = _settings(a)
    stage, table, extra = a.cmd, None, {}
    record = True
    if stage == "stack":
        table = _stack_table(s)
        extra = {"force": a.force, "reuse": a.reuse, "out": a.out}
        record = a.out == "stack"
        if a.reuse:
            old = stages.recorded_params(a.work, "stack")
            new = _params(stage, s, table)
            if old is None or (old["subs"], old["dark"]) != (new["subs"], new["dark"]):
                raise tools.StageError("--reuse-registration needs the frames in <work>/process to come from "
                                       "the same subs and dark as now, and the record of the last stack run "
                                       "says otherwise (or is missing)")
    elif stage == "finish":
        extra = {"out": a.out}
        record = a.out == stages.FINAL
    params = _params(stage, s, table)
    if record:
        stages.forget_params(a.work, stage)
    _timed(stage, _call, stage, a.work, s, table, **extra)
    if record:
        stages.record_params(a.work, stage, params)


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if a.cmd == "doctor":  # reports only: no work dir is created, and its exit code is the result
        return a.func(a)
    try:
        if a.cmd in ("get-starnet", "fetch-models", "setup"):
            return a.func(a)
        _dispatch(a)
    except tools.StageError as e:
        tools.eprint(f"darkroom {a.cmd}: error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
