"""`darkroom fetch-models`: have GraXpert download its AI denoise model 3.0.2.

GraXpert downloads a model the first time it needs it. The background-extraction
model arrives on the first `darkroom bge`; the denoise model only when GraXpert's
own denoise runs, which darkroom does not otherwise do (it runs that model itself
through onnxruntime). So this runs GraXpert's command-line denoise once on a tiny
synthetic image in a temporary folder, then checks that the model file exists.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Callable

from . import tools

MODEL_VERSION = "3.0.2"
SIZE = 64


def write_sample(path: Path) -> None:
    """A 3 x 64 x 64 float32 FITS of faint noise (nothing from any real image)."""
    import numpy as np
    from astropy.io import fits
    rng = np.random.default_rng(0)
    data = (0.1 + 0.01 * rng.standard_normal((3, SIZE, SIZE))).astype(np.float32)
    fits.PrimaryHDU(data).writeto(path, overwrite=True)


def denoise_command(exe: Path, src: Path, out_base: Path) -> list[str]:
    return [str(exe), "-cli", "-cmd", "denoising", "-ai_version", MODEL_VERSION, "-output", str(out_base), str(src)]


def fetch_denoise_model(log_path: Path | None = None, log=print, run: Callable = tools.run_tool,
                        exe: Path | None = None, model: Path | None = None) -> Path:
    """Make sure the GraXpert denoise model exists; return its path."""
    model = model or tools.graxpert_model()
    if model.is_file():
        log(f"denoise model {MODEL_VERSION} already present: {model}")
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
        write_sample(src)
        log(f"running GraXpert's denoise once on a {SIZE}x{SIZE} synthetic image so that it downloads "
            f"AI model {MODEL_VERSION} (log: {log_path})")
        try:
            run(denoise_command(exe, src, tmp / "sample_denoised"), log_path)
        except tools.StageError as e:
            failure = e                 # the download may still have worked; the file decides
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if model.is_file():
        log(f"denoise model {MODEL_VERSION}: {model}"
            + (" (GraXpert's own denoise of the sample failed, but the model was downloaded)" if failure else ""))
        return model
    where = tools.default_graxpert_model()
    msg = f"GraXpert did not leave a denoise model at {model}"
    if str(model) != str(Path(where).expanduser()):
        msg += f" (GraXpert keeps it at {where}; DARKROOM_GRAXPERT_MODEL points elsewhere)"
    if failure:
        msg += f"\n{failure}"
    else:
        msg += f"; last lines of {log_path}:\n" + tools.tail(log_path)
    raise tools.StageError(msg)
