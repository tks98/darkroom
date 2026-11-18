"""GraXpert AI denoise on the GPU (CoreML on macOS, CUDA where onnxruntime has it) or the CPU.

GraXpert 3.1.0rc2's bundled onnxruntime can't compile the denoise model for
CoreML on macOS 27, so this reimplements GraXpert's denoise() (GPL-3,
graxpert/denoising.py) with a fixed-batch copy of the same model (3.0.2).

The fixed-batch copy is built once with
``onnxruntime.tools.make_dynamic_shape_fixed --dim_param batch_size --dim_value 8``
into $DARKROOM_CACHE and reused afterwards.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from . import tools

BATCH, WIN, STRIDE, THR = 8, 256, 128, 10.0        # THR = model_threshold for 2.x/3.x models


def pad_for_tiles(image, win: int, stride: int):
    """GraXpert's tile layout (denoising.py and deconvolution.py use the same one): extend the
    H x W x C image at the bottom and right to a whole number of strides, one stride past
    the edge, by repeating its last rows and columns, then by (win - stride) / 2 on every side.
    Returns (padded image, tiles down, tiles across, offset). Tile (i, j) is
    ``padded[stride*i : stride*i + win, stride*j : stride*j + win]``; its centre
    ``[off : off + stride]`` belongs at ``padded[stride*i + off : stride*(i + 1) + off]``, and
    ``padded[off : H + off, off : W + off]`` is the image."""
    H, W = image.shape[:2]
    off = (win - stride) // 2
    ith, itw = H // stride + 1, W // stride + 1
    dh, dw = ith * stride - H, itw * stride - W
    if dh > H or dw > W or off > H + dh or off > W + dw:
        raise ValueError(f"image {W}x{H} is too small for {win}-pixel tiles")
    image = np.concatenate((image, image[H - dh:]), axis=0)
    image = np.concatenate((image, image[:, W - dw:]), axis=1)
    h, w = image.shape[:2]
    image = np.concatenate((image[:off], image, image[h - off:]), axis=0)
    image = np.concatenate((image[:, :off], image, image[:, w - off:]), axis=1)
    return image, ith, itw, off


def denoise(image, session, strength, log=print):
    """image: H x W x 3 float32 in [0,1]. Mirrors GraXpert's tiling/normalization."""
    original = image.copy()
    median = np.median(image[::4, ::4, :], axis=(0, 1))
    mad = np.median(np.abs(image[::4, ::4, :] - median), axis=(0, 1))

    H, W, _ = image.shape
    image, ith, itw, off = pad_for_tiles(image, WIN, STRIDE)
    output = image.copy()

    n = ith * itw
    t0 = time.time()
    for b in range(0, n, BATCH):
        idx = list(range(b, min(b + BATCH, n)))
        tiles, raw = [], []
        for k in idx:
            i, j = k % ith, k // ith
            t = (image[STRIDE * i:STRIDE * i + WIN, STRIDE * j:STRIDE * j + WIN] - median) / mad * 0.04
            raw.append(t)
            tiles.append(np.clip(t, -THR, THR))
        batch = np.zeros((BATCH, WIN, WIN, 3), np.float32)
        batch[:len(tiles)] = tiles
        res = session.run(None, {"gen_input_image": batch})[0]
        for m, k in enumerate(idx):
            i, j = k % ith, k // ith
            t = np.where(raw[m] < THR, res[m], raw[m]) / 0.04 * mad + median
            output[STRIDE * i + off:STRIDE * (i + 1) + off, STRIDE * j + off:STRIDE * (j + 1) + off] = \
                t[off:off + STRIDE, off:off + STRIDE]
        if (b // BATCH) % 50 == 0:
            log(f"  {min(b + BATCH, n)}/{n} tiles  {time.time() - t0:.1f}s")
    output = output[off:H + off, off:W + off]

    thr = THR / 0.04 * mad + median
    blend = np.where(original < thr, output, original)
    return np.clip(blend * strength + original * (1 - strength), 0, 1).astype(np.float32)


def fixed_batch_model(log_path: Path, log=print) -> Path:
    """Return the batch-8 copy of the GraXpert model, building it in the cache on first use."""
    src = tools.require_file(tools.graxpert_model(), "GraXpert denoise model (set DARKROOM_GRAXPERT_MODEL)")
    cache = tools.cache_dir()
    dst = cache / f"graxpert_denoise_3.0.2_b{BATCH}.onnx"
    if dst.is_file():
        return dst
    cache.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".onnx.tmp")
    log(f"building fixed-batch model {dst} from {src} (one-off)")
    tools.run_tool([sys.executable, "-m", "onnxruntime.tools.make_dynamic_shape_fixed",
                    "--dim_param", "batch_size", "--dim_value", str(BATCH), str(src), str(tmp)], log_path)
    if not tmp.is_file():
        raise tools.StageError(f"make_dynamic_shape_fixed produced no output; see {log_path}")
    os.replace(tmp, dst)
    return dst


COREML = ("CoreMLExecutionProvider", {"ModelFormat": "MLProgram", "MLComputeUnits": "ALL"})


def providers(cpu: bool, available, system: str | None = None) -> tuple[list, str]:
    """(onnxruntime provider list, backend label). CoreML on macOS when onnxruntime has it,
    else CUDA when it has that, else the CPU; ``cpu`` forces the CPU."""
    system = sys.platform if system is None else system
    available = set(available)
    if cpu:
        return ["CPUExecutionProvider"], "CPU"
    if system == "darwin" and "CoreMLExecutionProvider" in available:
        return [COREML, "CPUExecutionProvider"], "CoreML"
    if "CUDAExecutionProvider" in available:
        return ["CUDAExecutionProvider", "CPUExecutionProvider"], "CUDA"
    return ["CPUExecutionProvider"], "CPU"


def run(src: Path, dst: Path, strength: float, cpu: bool, log_path: Path, log=print) -> None:
    import onnxruntime as ort
    from astropy.io import fits

    if not tools.graxpert_model().is_file():
        from . import models
        log(f"denoise model not found at {tools.graxpert_model()}; fetching it with GraXpert "
            "(the same as `darkroom fetch-models`)")
        models.fetch_denoise_model(log_path, log)
    model = fixed_batch_model(log_path, log)
    with fits.open(src) as hdul:
        hdr, data = hdul[0].header, hdul[0].data.astype(np.float32)
    if not (data.ndim == 3 and data.shape[0] == 3):
        raise tools.StageError(f"{src}: expected a 3-channel Siril/GraXpert FITS, got shape {data.shape}")
    img = np.ascontiguousarray(np.moveaxis(data, 0, -1))

    prov, backend = providers(cpu, ort.get_available_providers())
    log(f"denoise backend: {backend}" + (" (--cpu)" if cpu else "")
        + ("" if cpu or backend != "CPU" else " (onnxruntime has no CoreML or CUDA provider here; this is slow)"))
    t = time.time()
    sess = ort.InferenceSession(str(model), providers=prov)
    log(f"providers: {sess.get_providers()}  (load {time.time() - t:.1f}s)")

    t = time.time()
    out = denoise(img, sess, strength, log)
    log(f"denoise done in {time.time() - t:.1f}s")
    hdr["HISTORY"] = f"GraXpert denoise 3.0.2 strength {strength} via onnxruntime {ort.__version__} {backend}"
    fits.PrimaryHDU(np.moveaxis(out, -1, 0), header=hdr).writeto(dst, overwrite=True)
