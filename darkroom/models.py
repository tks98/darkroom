"""`darkroom fetch-models`: have GraXpert download its AI models.

GraXpert downloads a model the first time it needs it. The background-extraction
model arrives on the first `darkroom bge`; the denoise model (3.0.2) and the
object deconvolution model (1.0.1) only when GraXpert's own denoise or
deconvolution runs, which darkroom does not otherwise do (it runs those models
itself through onnxruntime). So this runs GraXpert's command line once on a small
synthetic image in a temporary folder, then checks that the model file exists.

Neither model is ever put in this repository: both are GraXpert's, under
CC BY-NC-SA 4.0, and GraXpert downloads them from its own server.
"""
from __future__ import annotations

import hashlib
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import tools

MODEL_VERSION = "3.0.2"
SIZE = 64
# GraXpert's AI denoise model 3.0.2 (model.onnx as GraXpert unpacks it). GraXpert's model server
# needs its access keys and publishes no checksums, so these were taken from a copy GraXpert 3.1
# downloaded itself.
DENOISE_BYTES = 477703971
DENOISE_SHA256 = "e61c880e7fb955917e91141660e66801c9de5c4f3d9946dc449e5a7dd7346563"

DECONV_VERSION = "1.0.1"
DECONV_BYTES = 279252211
DECONV_SHA256 = "36b88b422cf98521a27187bc230a5b9bb3565100f66c967911f7f5602bbe7198"
DECONV_SAMPLE = 256        # GraXpert's deconvolution tiles are 512 px with a 448 stride: needs >= 224 px


def write_sample(path: Path, size: int = SIZE) -> None:
    """A 3 x size x size float32 FITS of faint noise (nothing from any real image)."""
    import numpy as np
    from astropy.io import fits
    rng = np.random.default_rng(0)
    data = (0.1 + 0.01 * rng.standard_normal((3, size, size))).astype(np.float32)
    fits.PrimaryHDU(data).writeto(path, overwrite=True)


def denoise_command(exe: Path, src: Path, out_base: Path) -> list[str]:
    return [str(exe), "-cli", "-cmd", "denoising", "-ai_version", MODEL_VERSION, "-output", str(out_base), str(src)]


def deconv_command(exe: Path, src: Path, out_base: Path) -> list[str]:
    return [str(exe), "-cli", "-cmd", "deconv-obj", "-ai_version", DECONV_VERSION, "-strength", "0.5",
            "-psfsize", "3", "-output", str(out_base), str(src)]


@dataclass(frozen=True)
class _Kind:
    what: str               # "denoise", "deconvolution"
    version: str
    envvar: str
    sample: int
    command: Callable
    default: Callable       # where GraXpert keeps it


_DENOISE = _Kind("denoise", MODEL_VERSION, "DARKROOM_GRAXPERT_MODEL", SIZE, denoise_command,
                 lambda: tools.default_graxpert_model())
_DECONV = _Kind("deconvolution", DECONV_VERSION, "DARKROOM_DECONV_MODEL", DECONV_SAMPLE, deconv_command,
                lambda: tools.default_deconv_model())


def _fetch(kind: _Kind, model: Path, log_path: Path | None, log, run: Callable, exe: Path | None) -> Path:
    if model.is_file():
        log(f"{kind.what} model {kind.version} already present: {model}")
        return model
    exe = tools.require_executable(exe or tools.graxpert(), "DARKROOM_GRAXPERT")
    if log_path is None:
        tools.cache_dir().mkdir(parents=True, exist_ok=True)
        log_path = tools.cache_dir() / "fetch-models.log"
        log_path.write_text("")
    tmp = Path(tempfile.mkdtemp(prefix="darkroom-fetch-models-"))
    failure = None
    try:
        src = tmp / "sample.fits"
        write_sample(src, kind.sample)
        log(f"running GraXpert's {kind.what} once on a {kind.sample}x{kind.sample} synthetic image so that it "
            f"downloads AI model {kind.version} (log: {log_path})")
        try:
            run(kind.command(exe, src, tmp / "sample_out"), log_path)
        except tools.StageError as e:
            failure = e                 # the download may still have worked; the file decides
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if model.is_file():
        log(f"{kind.what} model {kind.version}: {model}"
            + (f" (GraXpert's own {kind.what} of the sample failed, but the model was downloaded)"
               if failure else ""))
        return model
    where = kind.default()
    msg = f"GraXpert did not leave a {kind.what} model at {model}"
    if str(model) != str(Path(where).expanduser()):
        msg += f" (GraXpert keeps it at {where}; {kind.envvar} points elsewhere)"
    if failure:
        msg += f"\n{failure}"
    else:
        msg += f"; last lines of {log_path}:\n" + tools.tail(log_path)
    raise tools.StageError(msg)


def fetch_denoise_model(log_path: Path | None = None, log=print, run: Callable = tools.run_tool,
                        exe: Path | None = None, model: Path | None = None) -> Path:
    """Make sure the GraXpert denoise model exists; return its path."""
    return _fetch(_DENOISE, model or tools.graxpert_model(), log_path, log, run, exe)


def fetch_deconv_model(log_path: Path | None = None, log=print, run: Callable = tools.run_tool,
                       exe: Path | None = None, model: Path | None = None) -> Path:
    """Make sure GraXpert's object deconvolution model exists and is the expected file; return its path."""
    model = _fetch(_DECONV, model or tools.deconv_model(), log_path, log, run, exe)
    verify_deconv_model(model)
    return model


_SHA_CACHE: dict[tuple[str, int, int], str] = {}
_SHA_LOCK = threading.Lock()


def sha256(path: Path) -> str:
    """SHA-256 of path; remembered for this process while its size and mtime stay the same."""
    st = path.stat()
    key = (str(Path(path).resolve()), st.st_size, st.st_mtime_ns)
    with _SHA_LOCK:                     # doctor checks the model from two threads; hash it once
        if key not in _SHA_CACHE:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for block in iter(lambda: f.read(1 << 22), b""):
                    h.update(block)
            _SHA_CACHE[key] = h.hexdigest()
        return _SHA_CACHE[key]


def denoise_model_problem(path: Path, check_hash: bool = True) -> str | None:
    """Why ``path`` is not GraXpert's AI denoise model 3.0.2, or None if it is."""
    if not path.is_file():
        return f"not found: {path}"
    size = path.stat().st_size
    if size != DENOISE_BYTES:
        return f"{path} is {size} bytes, not the {DENOISE_BYTES} of denoise model {MODEL_VERSION}"
    if check_hash and sha256(path) != DENOISE_SHA256:
        return f"{path} has the size of denoise model {MODEL_VERSION} but not its SHA-256"
    return None


def fixed_batch_path() -> Path:
    """Where denoise.fixed_batch_model caches its fixed-batch copy of the denoise model."""
    from . import denoise
    return tools.cache_dir() / f"graxpert_denoise_{MODEL_VERSION}_b{denoise.BATCH}.onnx"


def _fixed_batch_stamp(dst: Path) -> Path:
    return dst.with_name(dst.name + ".source-sha256")


def verify_denoise_model(path: Path | None = None, log=lambda m: None) -> Path:
    """Check that ``path`` (default: tools.graxpert_model()) is GraXpert's denoise model 3.0.2 by size
    and SHA-256; StageError if not. Also drops the cached fixed-batch copy when it was built from a
    different file (or by a darkroom that did not record its source), so that
    denoise.fixed_batch_model rebuilds it from this one."""
    path = path or tools.graxpert_model()
    problem = denoise_model_problem(path)
    if problem:
        raise tools.StageError(f"denoise model: {problem}. darkroom's denoise is made for GraXpert's AI denoise "
                               f"model {MODEL_VERSION}; delete the file and run `darkroom fetch-models`, or point "
                               "DARKROOM_GRAXPERT_MODEL at that model")
    dst = fixed_batch_path()
    stamp = _fixed_batch_stamp(dst)
    try:
        built_from = stamp.read_text().strip()
    except OSError:
        built_from = None
    if built_from != DENOISE_SHA256:
        if dst.exists():
            log(f"the cached fixed-batch model {dst} was not built from this denoise model; rebuilding it")
            dst.unlink()
        dst.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(DENOISE_SHA256 + "\n")
    return path


def deconv_model_problem(path: Path, check_hash: bool = True) -> str | None:
    """Why ``path`` is not GraXpert's object deconvolution model 1.0.1, or None if it is."""
    if not path.is_file():
        return f"not found: {path}"
    size = path.stat().st_size
    if size != DECONV_BYTES:
        return f"{path} is {size} bytes, not the {DECONV_BYTES} of model {DECONV_VERSION}"
    if check_hash and sha256(path) != DECONV_SHA256:
        return f"{path} has the size of model {DECONV_VERSION} but not its SHA-256"
    return None


def verify_deconv_model(path: Path) -> Path:
    problem = deconv_model_problem(path)
    if problem:
        raise tools.StageError(f"deconvolution model: {problem}. darkroom's PSF scaling is made for GraXpert's "
                               f"object deconvolution model {DECONV_VERSION}; delete the file and run "
                               "`darkroom fetch-models --deconv`, or point DARKROOM_DECONV_MODEL at that model")
    return path
