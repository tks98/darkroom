"""Per-target settings: <work>/darkroom.toml, presets and built-in defaults.

Precedence for every setting, highest first:

  command-line flag > darkroom.toml > look preset > camera preset > built-in default

The built-in defaults equal the shipped ``seestar-s30-pro`` camera preset and
``galaxy`` look preset, so running without any config behaves as it always did.
"""
from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import tools
from .finish import FinishParams, parse_center, parse_crop_spec

CONFIG_NAME = "darkroom.toml"
FRAMINGS = ("min", "cog", "max")
MOSAIC_CROPS = ("rotate", "rect", "none")

_F = FinishParams()

# section -> key -> (type, built-in default, one-line description)
# types: "str", "path", "float", "int", "bool", "auto_num" (a number, or "auto")
SCHEMA: dict[str, dict[str, tuple[str, object, str]]] = {
    "target": {
        "name": ("str", None, "object name (informational)"),
        "subs": ("path", None, "folder with the light subs"),
        "dark": ("str", None, '"auto" (pick from dark_library), "none", a master dark, or a folder of raw darks'),
        "dark_library": ("path", None, 'master-dark library, used when dark = "auto"'),
        "bias": ("str", "none", '"none", a master bias, or a folder of raw bias frames'),
        "flats": ("str", "none", '"none", a master flat, or a folder of raw flats'),
        "camera": ("str", None, "camera preset name, or the path of a camera .toml"),
        "look": ("str", None, "look preset name, or the path of a look .toml"),
    },
    "stack": {
        "framing": ("str", "min", "min (area common to all frames) | cog (centre of gravity) | max (bounding box)"),
        "keep_fwhm": ("float", 90, "percent of frames kept by weighted FWHM"),
        "keep_round": ("float", 90, "percent of frames kept by roundness"),
        "keep_bkg": ("float", 100, "percent of frames kept by background level, darkest first (100 = no filter; lower it to drop cloud)"),
        "keep_stars": ("float", 100, "percent of frames kept by number of stars, most first (100 = no filter)"),
        "clean": ("bool", False, "delete <work>/process after a successful stack"),
        "mosaic": ("bool", False, "mosaic (many pointings): plate-solve each frame, stack onto one canvas (framing max), crop to the data"),
        "mosaic_crop": ("str", "rotate", "rotate (largest rectangle with data everywhere at any angle) | rect (upright) | none"),
    },
    "bge": {
        "smoothing": ("float", 0.1, "GraXpert background smoothing"),
    },
    "color": {
        "sensor": ("str", "Sony IMX585", "Siril SPCC one-shot-colour sensor name"),
        "filter": ("str", "UV/IR Block", "Siril SPCC filter name"),
        "focal": ("float", 160.0, "focal length in mm (plate solve)"),
        "pixel": ("float", 2.9, "pixel size in um (plate solve)"),
        "catalog": ("str", "gaia", "plate-solve catalogue"),
        "whiteref": ("str", "Average Spiral Galaxy", "SPCC white reference"),
        "narrowband": ("str", "none", '"none", or SPCC narrowband mode for a dual-band filter: '
                       '"R_nm/bw,G_nm/bw,B_nm/bw" (e.g. "656.28/12,500.7/12,500.7/12"); replaces filter'),
        "catalog_fallback": ("bool", True, "fetch Gaia DR3 from ESA if Siril's catalogue server is down"),
        "spcc_local": ("bool", True, "keep a local copy of SPCC's Gaia star spectra for the field (fetched "
                       "once) and run SPCC on it, so it does not depend on Siril's catalogue servers"),
    },
    "denoise": {
        "strength": ("float", 0.6, "denoise strength 0..1"),
        "cpu": ("bool", False, "run on the CPU instead of the GPU (CoreML on macOS, CUDA if available)"),
    },
    "deconv": {
        "strength": ("float", 0, "GraXpert object deconvolution strength 0..1; 0 = off (the stage is skipped)"),
        "psf": ("auto_num", "auto", 'star FWHM in pixels, or "auto" to measure it from the denoised image'),
        "cpu": ("bool", False, "run on the CPU instead of the GPU (CoreML on macOS, CUDA if available)"),
    },
    "stretch": {
        "asinh": ("float", 25.0, "asinh -human stretch factor"),
        "shadows": ("float", -2.5, "autostretch shadows clipping (sigma)"),
        "target_bg": ("float", 0.10, "autostretch target background"),
        "sat": ("float", 0.35, "Siril satu amount"),
        "method": ("str", "asinh", '"asinh" (asinh -human) | "ghs" (generalised hyperbolic stretch, Siril ght)'),
        "ghs_d": ("float", 4.0, "ghs stretch strength D (0..10)"),
        "ghs_b": ("float", 12.0, "ghs local intensity b (-5..15; higher concentrates the stretch near SP)"),
        "ghs_hp": ("float", 0.9, "ghs highlight protection HP (0..1; above it the stretch is linear)"),
        "ghs_sp": ("auto_num", "auto", 'ghs symmetry point SP (0..1), or "auto" for the sky level of the input'),
        "balance": ("str", "1,1,1", '"R,G,B" multipliers on the linear image before the stretch '
                    "(cosmetic white balance on top of SPCC; each 0.5..2)"),
    },
    "starnet": {
        "dir": ("path", None, "folder containing starnet2 or starnet++ (else $DARKROOM_STARNET_DIR, then `darkroom get-starnet`'s install)"),
    },
    "finish": {
        "gamma": ("float", _F.gamma, ">1 dims star halos more than cores (smaller stars)"),
        "star_strength": ("float", _F.star_strength, "star layer multiplier"),
        "mid": ("float", _F.mid, "midtone on the starless layer (<0.5 brightens)"),
        "sat": ("float", _F.sat, "saturation on the starless layer"),
        "yb": ("float", _F.yb, ">1 stretches yellow/blue differences inside the galaxy (cosmetic)"),
        "core": ("float", _F.core, "compress the brightest part of the target, keeping its detail (0 = off)"),
        "sharpen": ("float", _F.sharpen, "unsharp-mask amount on the galaxy luminance"),
        "sharpen_radius": ("float", _F.sharpen_radius, "unsharp-mask Gaussian sigma (px)"),
        "black": ("float", _F.black, "black point lift on the starless layer"),
        "erode": ("int", _F.erode, "3x3 erosion passes on the star layer"),
        "neutral": ("bool", _F.neutral, "neutralise the sky colour"),
        "crop": ("str", "none", '"none", pixels "x,y,w,h", or an aspect ratio like "2:3" around the target'),
        "crop_center": ("str", None, 'centre "x,y" for an aspect-ratio crop (default: brightest part of the starless image)'),
        "crop_scale": ("float", 1.0, "aspect-ratio crop: fraction of the largest box that fits (1 = largest; lower for a small target)"),
        "blue_shift": ("float", _F.blue_shift, "turn purple hues toward blue, 0 (off) to 1 (cosmetic)"),
        "core_white": ("float", _F.core_white, "take colour out of the brightest part of the image, 0 (off) to 1 (cosmetic)"),
        "stars": ("str", "reduce", '"reduce" (remove stars with StarNet, enhance, add them back smaller) | "keep" (no star removal: for clusters)'),
    },
}

# Camera facts used when no camera preset is named, and the base a preset's [camera], [filters]
# and [darks] tables are laid over. [camera] sub_glob "*.fit" takes every FITS file in the subs
# folder (.fit, .fits and .fts, in any case: see darks.matches), since without a preset the
# subs' naming is unknown. [darks] must stay equal to presets/cameras/seestar-s30-pro.toml
# (the Seestar dark-library layout).
DEFAULT_CAMERA_META = {
    "camera": {"sub_glob": "*.fit"},
    "filters": {},
    "darks": {
        "glob": "MasterDark_*.fit",
        "filename": r"MasterDark_(?P<nframes>\d+)_(?P<exposure>\d+(?:\.\d+)?)s_(?P<temp>-?\d+(?:\.\d+)?)C_"
                    r"(?P<date>\d{8}-\d{6})\.fit",
    },
}
_CAMERA_META_SECTIONS = ("camera", "filters", "darks")
# the keys a camera preset's [camera] and [darks] may hold ([filters] maps any FITS FILTER value)
CAMERA_META_KEYS = {
    "camera": {"name": "camera name (informational)",
               "match_keyword": "FITS keyword that identifies the camera",
               "match": "text that keyword must contain",
               "bayer": "expected BAYERPAT of the subs",
               "sub_glob": "light frames inside the subs folder"},
    "darks": {"glob": "master darks inside the dark library",
              "filename": "regular expression for a master dark's file name"},
}
_PRESET_SETTING_SECTIONS = tuple(s for s in SCHEMA if s != "target")


# [target] keys that hold a calibration file or folder (or "none"; dark also "auto")
CALIBRATION_KEYS = (("target", "dark"), ("target", "bias"), ("target", "flats"))


STRETCH_METHODS = ("asinh", "ghs")
BALANCE_RANGE = (0.5, 2.0)


def parse_balance(value) -> tuple[float, float, float]:
    """[stretch] balance: "R,G,B" multipliers, e.g. "0.92,0.90,1.18" -> (0.92, 0.9, 1.18)."""
    lo, hi = BALANCE_RANGE
    bad = ValueError(f'balance should be three numbers "R,G,B", each from {lo:g} to {hi:g} '
                     f'(1,1,1 = unchanged), like "0.92,0.90,1.18"; got {value!r}')
    parts = [p.strip() for p in str(value).split(",")]
    if len(parts) != 3:
        raise bad
    try:
        out = tuple(float(p) for p in parts)
    except ValueError:
        raise bad from None
    if not all(math.isfinite(v) and lo <= v <= hi for v in out):
        raise bad
    return out


def parse_narrowband(value) -> tuple[tuple[float, float], ...] | None:
    """[color] narrowband: None for "none", else ((wavelength, bandwidth) in nm) for R, G, B."""
    if value is None or str(value).strip().lower() == "none":
        return None
    bad = ValueError(f'narrowband should be "none" or three "wavelength/bandwidth" pairs in nm for R, G '
                     f'and B, like "656.28/12,500.7/12,500.7/12"; got {value!r}')
    parts = [p.strip() for p in str(value).split(",")]
    if len(parts) != 3:
        raise bad
    out = []
    for p in parts:
        try:
            wl, bw = (float(x) for x in p.split("/"))
        except ValueError:
            raise bad from None
        if not (300 <= wl <= 1100 and 0 < bw <= 200):
            raise ValueError(f"narrowband: {p!r} should be a wavelength of 300 to 1100 nm and a "
                             "bandwidth of 0 to 200 nm") from None
        out.append((wl, bw))
    return tuple(out)


class ConfigError(tools.StageError):
    """A problem in darkroom.toml or a preset; shown to the user like any stage error."""


@dataclass
class Settings:
    values: dict[str, dict[str, object]]
    sources: dict[tuple[str, str], str]
    camera_meta: dict = field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_CAMERA_META.items()})
    config_path: Path | None = None

    def __getitem__(self, section: str) -> dict[str, object]:
        return self.values[section]

    def get(self, section: str, key: str):
        return self.values[section][key]


def presets_dir() -> Path:
    here = Path(__file__).resolve().parent
    for d in (here / "presets", here.parent / "presets"):   # wheel install, then source checkout
        if d.is_dir():
            return d
    raise ConfigError("presets not found (expected darkroom/presets or ../presets)")


def preset_names(kind: str) -> list[str]:
    return sorted(p.stem for p in (presets_dir() / kind).glob("*.toml"))


def _read_toml(path: Path, what: str) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{what} not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{what} {path} is not valid TOML: {e}") from None


def is_preset_path(name: str) -> bool:
    """Whether a camera or look setting names a .toml file rather than a shipped preset."""
    return name.endswith(".toml") or "/" in name


def preset_path(kind: str, name: str, base: Path | None = None) -> Path:
    """kind is 'cameras' or 'looks'; name is a preset name or the path of a .toml file."""
    if is_preset_path(name):
        p = Path(name).expanduser()
        if not p.is_absolute() and base is not None:
            p = base / p
        return p
    p = presets_dir() / kind / f"{name}.toml"
    if not p.is_file():
        raise ConfigError(f"unknown {kind[:-1]} preset {name!r}; available: "
                          + (", ".join(preset_names(kind)) or "none"))
    return p


def _coerce(section: str, key: str, value, where: str, base: Path | None):
    typ = SCHEMA[section][key][0]
    want = {"float": "a number", "int": "a number", "auto_num": 'a number or "auto"'}.get(typ, typ)
    bad = ConfigError(f"{where}: [{section}] {key} should be {want}, got {value!r}")
    if typ in ("auto_num", "float", "int") and isinstance(value, float) and not math.isfinite(value):
        raise ConfigError(f"{where}: [{section}] {key} should be a finite number, got {value!r}")
    if typ == "auto_num":
        if isinstance(value, str):
            if value.strip().lower() == "auto":
                return "auto"
            try:
                v = float(value)
            except ValueError:
                raise bad from None
            if not math.isfinite(v):
                raise bad
            return v
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise bad
        return value
    if typ == "bool":
        if not isinstance(value, bool):
            raise bad
        return value
    if isinstance(value, bool):
        raise bad
    if typ == "float":
        if not isinstance(value, (int, float)):
            raise bad
        return value
    if typ == "int":
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if not isinstance(value, int):
            raise bad
        return value
    if isinstance(value, Path):
        value = str(value)
    if not isinstance(value, str):
        raise bad
    if typ == "path" or (section, key) in CALIBRATION_KEYS and value.lower() not in ("auto", "none"):
        p = Path(value).expanduser()
        if not p.is_absolute() and base is not None:
            p = base / p
        return str(p)
    return value


# (section, key, test, what the value should be) checked by _check; every shipped preset and
# documented example lies well inside these ranges
_RANGES = (
    ("bge", "smoothing", lambda v: 0 <= v <= 1, "0 to 1"),
    ("color", "focal", lambda v: v > 0, "a focal length in mm (> 0)"),
    ("color", "pixel", lambda v: v > 0, "a pixel size in um (> 0)"),
    ("denoise", "strength", lambda v: 0 <= v <= 1, "0 to 1"),
    ("stretch", "asinh", lambda v: v > 0, "a stretch factor > 0 (the looks use 25 to 100)"),
    ("stretch", "shadows", lambda v: -10 <= v <= 0, "a clipping point in sigma, -10 to 0 (usually about -2.5)"),
    ("stretch", "target_bg", lambda v: 0 < v < 1, "a background level between 0 and 1 (e.g. 0.10)"),
    ("stretch", "sat", lambda v: v >= -1, "a saturation amount >= -1 (0 = unchanged)"),
    ("stretch", "ghs_d", lambda v: 0 <= v <= 10, "a stretch strength from 0 to 10 (e.g. 4)"),
    ("stretch", "ghs_b", lambda v: -5 <= v <= 15, "a local intensity from -5 to 15 (e.g. 12)"),
    ("stretch", "ghs_hp", lambda v: 0 <= v <= 1, "a highlight protection point from 0 to 1 (e.g. 0.9)"),
    ("finish", "gamma", lambda v: v > 0, "> 0 (1 = unchanged)"),
    ("finish", "star_strength", lambda v: 0 <= v <= 1, "0 to 1"),
    ("finish", "mid", lambda v: 0 < v < 1, "between 0 and 1 (0.5 = unchanged)"),
    ("finish", "sat", lambda v: v >= 0, ">= 0 (1 = unchanged)"),
    ("finish", "yb", lambda v: v >= 0, ">= 0 (1 = off)"),
    ("finish", "core", lambda v: 0 <= v <= 1, "0 (off) to 1"),
    ("finish", "sharpen", lambda v: v >= 0, ">= 0 (0 = off)"),
    ("finish", "sharpen_radius", lambda v: v > 0, "a radius in pixels (> 0)"),
    ("finish", "black", lambda v: 0 <= v < 1, "0 or more and below 1"),
    ("finish", "erode", lambda v: v >= 0, "a whole number of passes, 0 or more"),
    ("finish", "blue_shift", lambda v: 0 <= v <= 1, "0 (off) to 1"),
    ("finish", "core_white", lambda v: 0 <= v <= 1, "0 (off) to 1"),
)


def _check(values: dict[str, dict[str, object]]) -> None:
    s = values["stack"]
    if s["framing"] not in FRAMINGS:
        raise ConfigError(f"[stack] framing should be one of {', '.join(FRAMINGS)}, got {s['framing']!r}")
    if s["mosaic_crop"] not in MOSAIC_CROPS:
        raise ConfigError(f"[stack] mosaic_crop should be one of {', '.join(MOSAIC_CROPS)}, got {s['mosaic_crop']!r}")
    for k in ("keep_fwhm", "keep_round", "keep_bkg", "keep_stars"):
        if not 0 < s[k] <= 100:
            raise ConfigError(f"[stack] {k} is a percentage (0 < value <= 100), got {s[k]!r}")
    f = values["finish"]
    try:
        parse_crop_spec(f["crop"])
        if f["crop_center"] is not None:
            parse_center(f["crop_center"])
    except ValueError as e:
        raise ConfigError(f"[finish] {e}") from None
    if f["stars"] not in ("reduce", "keep"):
        raise ConfigError(f'[finish] stars should be "reduce" or "keep", got {f["stars"]!r}')
    if not 0 < f["crop_scale"] <= 1:
        raise ConfigError(f"[finish] crop_scale is a fraction (0 < value <= 1), got {f['crop_scale']!r}")
    for section, key, ok, want in _RANGES:
        v = values[section][key]
        if not ok(v):
            raise ConfigError(f"[{section}] {key} should be {want}, got {v!r}")
    for key in ("sensor", "filter", "catalog", "whiteref"):
        if '"' in values["color"][key]:
            raise ConfigError(f"[color] {key} cannot contain a double quote (Siril scripts cannot quote it), "
                              f"got {values['color'][key]!r}")
    dc = values["deconv"]
    if not 0 <= dc["strength"] <= 1:
        raise ConfigError(f"[deconv] strength is 0 (off) to 1, got {dc['strength']!r}")
    if dc["psf"] != "auto" and not 0 < dc["psf"] <= 50:
        raise ConfigError(f'[deconv] psf is the star FWHM in pixels (0 < value <= 50) or "auto", got {dc["psf"]!r}')
    st = values["stretch"]
    st["method"] = st["method"].strip().lower()
    if st["method"] not in STRETCH_METHODS:
        raise ConfigError(f'[stretch] method should be "asinh" or "ghs", got {st["method"]!r}')
    if st["ghs_sp"] != "auto" and not 0 <= st["ghs_sp"] <= st["ghs_hp"]:
        raise ConfigError(f'[stretch] ghs_sp should be "auto" or a symmetry point from 0 to ghs_hp '
                          f'({st["ghs_hp"]:g}), got {st["ghs_sp"]!r}')
    try:
        parse_balance(st["balance"])
    except ValueError as e:
        raise ConfigError(f"[stretch] {e}") from None
    t = values["target"]
    for _section, key in CALIBRATION_KEYS:
        if t[key] is not None and t[key].lower() in ("auto", "none"):
            t[key] = t[key].lower()
    for key in ("bias", "flats"):
        if t[key] == "auto":
            raise ConfigError(f'[target] {key} should be "none", a master file or a folder of raw frames; '
                              '"auto" is only for dark')
    try:
        parse_narrowband(values["color"]["narrowband"])
    except ValueError as e:
        raise ConfigError(f"[color] {e}") from None
    if values["color"]["narrowband"].strip().lower() == "none":
        values["color"]["narrowband"] = "none"


def _layer(values, sources, data: dict, label: str, where: str, base: Path | None, sections) -> None:
    for section, body in data.items():
        if section not in sections:
            if section in SCHEMA or section in _CAMERA_META_SECTIONS:
                raise ConfigError(f"{where}: section [{section}] is not allowed here")
            raise ConfigError(f"{where}: unknown section [{section}]; known: {', '.join(sections)}")
        if not isinstance(body, dict):
            raise ConfigError(f"{where}: [{section}] should be a table")
        for key, value in body.items():
            if key not in SCHEMA[section]:
                raise ConfigError(f"{where}: unknown key [{section}] {key}; known: "
                                  + ", ".join(SCHEMA[section]))
            values[section][key] = _coerce(section, key, value, where, base)
            sources[(section, key)] = label


def load_camera(name: str, base: Path | None = None) -> tuple[dict, dict, Path]:
    """Return (settings sections, metadata sections, path) of a camera preset."""
    path = preset_path("cameras", name, base)
    data = _read_toml(path, "camera preset")
    meta = {k: dict(v) for k, v in DEFAULT_CAMERA_META.items()}
    for k in _CAMERA_META_SECTIONS:
        body = data.get(k, {})
        if not isinstance(body, dict):
            raise ConfigError(f"{path}: [{k}] should be a table")
        for key, value in body.items():
            if k in CAMERA_META_KEYS and key not in CAMERA_META_KEYS[k]:
                raise ConfigError(f"{path}: unknown key [{k}] {key}; known: " + ", ".join(CAMERA_META_KEYS[k]))
            if not isinstance(value, str):
                raise ConfigError(f"{path}: [{k}] {key} should be a string, got {value!r}")
        meta[k].update(body)
    try:
        re.compile(meta["darks"]["filename"])
    except re.error as e:
        raise ConfigError(f"{path}: [darks] filename is not a valid regular expression ({e}): "
                          f"{meta['darks']['filename']!r}") from None
    settings = {k: v for k, v in data.items() if k not in _CAMERA_META_SECTIONS}
    return settings, meta, path


def config_file(work: Path, config: Path | None) -> Path | None:
    """The config to use: --config (must exist) or <work>/darkroom.toml if present."""
    if config is not None:
        p = Path(config).expanduser().resolve()
        if not p.is_file():
            raise ConfigError(f"config file not found: {p}")
        return p
    p = Path(work).expanduser().resolve() / CONFIG_NAME
    return p if p.is_file() else None


def resolve(work: Path, config: Path | None = None,
            overrides: dict[tuple[str, str], object] | None = None, use_file: bool = True) -> Settings:
    """Merge built-in defaults, presets, the config file and explicit flags.
    ``use_file=False`` ignores any config file (used by `darkroom init`)."""
    values = {s: {k: spec[1] for k, spec in keys.items()} for s, keys in SCHEMA.items()}
    sources = {(s, k): "built-in" for s, keys in SCHEMA.items() for k in keys}
    out = Settings(values, sources)

    path = config_file(work, config) if use_file else None
    cfg = _read_toml(path, "config file") if path else {}
    base = path.parent if path else None
    out.config_path = path
    target = cfg.get("target", {}) if isinstance(cfg.get("target", {}), dict) else {}
    camera = (overrides or {}).get(("target", "camera"), target.get("camera"))
    look = (overrides or {}).get(("target", "look"), target.get("look"))

    if camera:
        settings, out.camera_meta, cpath = load_camera(str(camera), base)
        _layer(values, sources, settings, f"camera preset {cpath.stem}", str(cpath), cpath.parent,
               _PRESET_SETTING_SECTIONS)
    if look:
        lpath = preset_path("looks", str(look), base)
        _layer(values, sources, _read_toml(lpath, "look preset"), f"look preset {lpath.stem}", str(lpath),
               lpath.parent, _PRESET_SETTING_SECTIONS)
    if cfg:
        _layer(values, sources, cfg, path.name, str(path), base, tuple(SCHEMA))
    for (section, key), value in (overrides or {}).items():
        values[section][key] = _coerce(section, key, value, "command line", Path.cwd())
        sources[(section, key)] = "flag"
    _check(values)
    return out


# ---------------------------------------------------------------- writing TOML

def toml_value(v) -> str:
    """Format one scalar as TOML (enough for this config: str, bool, int, float)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() and abs(v) < 1e15 else repr(v)
    s = str(v)
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def render_config(explicit: dict[tuple[str, str], object], effective: Settings,
                  header_lines: list[str]) -> str:
    """A commented darkroom.toml: ``explicit`` keys are written, every other key is shown
    commented out with the value currently in effect and where it comes from."""
    lines = [f"# {l}".rstrip() for l in header_lines]
    lines += ["#",
              "# Precedence: command-line flag > this file > look preset > camera preset > built-in default.",
              "# Keys that are commented out show the value in effect; remove the '#' to change one.",
              "# Relative paths are relative to this file."]
    for section, keys in SCHEMA.items():
        lines += ["", f"[{section}]"]
        for key, (_typ, _default, doc) in keys.items():
            if (section, key) in explicit:
                lines.append(f"{key} = {toml_value(explicit[(section, key)])}".ljust(34) + f" # {doc}")
                continue
            value = effective.values[section][key]
            src = effective.sources[(section, key)]
            if value is None:
                example = {"path": "/path/to/...", "str": "..."}.get(SCHEMA[section][key][0], "...")
                if key == "crop_center":
                    example = "x,y"
                lines.append(f"# {key} = {toml_value(example)}".ljust(34) + f" # {doc}")
            else:
                lines.append(f"# {key} = {toml_value(value)}".ljust(34) + f" # {doc} [{src}]")
    return "\n".join(lines) + "\n"
