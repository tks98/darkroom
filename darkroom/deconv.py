"""Optional deconvolution with GraXpert's object deconvolution AI model (1.0.1), run through
onnxruntime on the GPU (CoreML on macOS, CUDA where onnxruntime has it) or the CPU.

This is a port of GraXpert 3.1's deconvolve() (GPL-3, graxpert/deconvolution.py): the same
512-pixel tiles with a 448-pixel stride, the same per-tile log normalisation, and the same
scaling of strength and PSF size into the model's two conditioning inputs. The model itself
(CC BY-NC-SA 4.0) is downloaded by GraXpert and never shipped with darkroom.

``measure_fwhm`` estimates the PSF for ``psf = "auto"``: the sigma-clipped median FWHM of
Gaussian fits to isolated, unsaturated stars on the luminance.
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from . import tools
from .denoise import pad_for_tiles, providers

WIN, STRIDE = 512, 448
STRENGTH_SCALE = 0.95          # GraXpert: a strength of exactly 1.0 has no effect, so it scales by 0.95
FWHM_PER_SIGMA = 2.0 * np.sqrt(2.0 * np.log(2.0))      # 2.3548


def psf_param(fwhm_px: float) -> float:
    """The model's normalised PSF input for a star FWHM in pixels (object model v1.0.1)."""
    return float(np.clip((fwhm_px / 2.355 - 0.5) / 5.5, 0.05, 0.95))


def deconvolve(image, session, strength: float, fwhm_px: float, log=print):
    """image: H x W x C float32 in [0,1] (linear). Returns the deconvolved image, clipped to [0,1].
    One tile (C planes of 1 x 512 x 512) per session call, as GraXpert does for colour images."""
    image = np.asarray(image, dtype=np.float32)
    H, W, C = image.shape
    conds = np.tile(np.array([[psf_param(fwhm_px), strength * STRENGTH_SCALE]], np.float32), (C, 1))
    padded, ith, itw, off = pad_for_tiles(image, WIN, STRIDE)
    output = padded.copy()
    n = ith * itw
    t0 = time.time()
    for idx in range(n):
        i, j = idx % ith, idx // ith
        x, y = STRIDE * i, STRIDE * j
        tile = padded[x:x + WIN, y:y + WIN]
        mn = tile.min(axis=(0, 1))
        tile = np.log(tile - mn + 1e-5)
        mean, std = tile.mean(), tile.std()
        tile = (tile - mean) / std * 0.1
        inp = np.moveaxis(tile[None], -1, 1).reshape(C, 1, WIN, WIN).astype(np.float32)
        res = inp - session.run(None, {"gen_input_image": inp, "params": conds})[0]
        res = np.moveaxis(res.reshape(1, C, WIN, WIN), 1, -1)[0]
        res = np.exp(res * std / 0.1 + mean) + mn - 1e-5
        output[x + off:STRIDE * (i + 1) + off, y + off:STRIDE * (j + 1) + off] = \
            res[off:off + STRIDE, off:off + STRIDE]
        if idx % 10 == 0 or idx == n - 1:
            log(f"  {idx + 1}/{n} tiles  {time.time() - t0:.1f}s")
    return np.clip(output[off:H + off, off:W + off], 0, 1).astype(np.float32)


# ------------------------------------------------------------------ psf auto

def _block_background(lum: np.ndarray, block: int = 64) -> np.ndarray:
    """A smooth background: medians of block x block cells, interpolated back to full size."""
    from scipy import ndimage
    H, W = lum.shape
    nh, nw = max(1, H // block), max(1, W // block)
    cells = lum[:nh * block, :nw * block].reshape(nh, block, nw, block)
    med = np.median(cells.transpose(0, 2, 1, 3).reshape(nh, nw, -1), axis=-1)
    med = ndimage.median_filter(med, size=3, mode="nearest")    # a bright star or the core in one cell
    return ndimage.zoom(med, (H / nh, W / nw), order=1, mode="nearest", grid_mode=True)[:H, :W]


def _fit_star(box: np.ndarray, guess_sigma: float):
    """Elliptical (axis-aligned) Gaussian on a tilted plane. Returns (dx, dy, sx, sy, amp) or None."""
    from scipy.optimize import least_squares
    n = box.shape[0]
    c = (n - 1) / 2
    yy, xx = np.mgrid[:n, :n].astype(np.float64)
    xx -= c
    yy -= c
    z = box.astype(np.float64).ravel()
    edge = np.concatenate([box[0], box[-1], box[:, 0], box[:, -1]])
    b0 = float(np.median(edge))
    p0 = [float(box[int(c), int(c)]) - b0, 0.0, 0.0, guess_sigma, guess_sigma, b0, 0.0, 0.0]

    def resid(p):
        a, x0, y0, sx, sy, b, bx, by = p
        g = a * np.exp(-0.5 * (((xx - x0) / sx) ** 2 + ((yy - y0) / sy) ** 2)) + b + bx * xx + by * yy
        return g.ravel() - z
    try:
        r = least_squares(resid, p0, bounds=([0, -c, -c, 0.2, 0.2, -np.inf, -np.inf, -np.inf],
                                             [np.inf, c, c, n, n, np.inf, np.inf, np.inf]),
                          x_scale="jac", max_nfev=200)
    except (ValueError, np.linalg.LinAlgError):
        return None
    if not r.success:
        return None
    a, x0, y0, sx, sy = r.x[:5]
    return x0, y0, sx, sy, a


def _clipped_median(v: np.ndarray, k: float = 3.0, iters: int = 5) -> tuple[float, np.ndarray]:
    keep = np.ones(len(v), bool)
    for _ in range(iters):
        med = np.median(v[keep])
        s = 1.4826 * np.median(np.abs(v[keep] - med))
        if s == 0:
            break
        new = np.abs(v - med) <= k * s
        if new.sum() < 3 or (new == keep).all():
            break
        keep = new
    return float(np.median(v[keep])), keep


def measure_fwhm(image: np.ndarray, log=None, max_stars: int = 300, min_stars: int = 10) -> tuple[float, int]:
    """Median FWHM in pixels of isolated, unsaturated stars, and how many stars it came from.

    The luminance (mean of the channels) has a coarse background removed; stars are local maxima
    of the lightly smoothed image above 20 sigma of the noise, with no other source within the
    isolation radius, away from the edges, and not near the brightest level or flat-topped
    (saturated). Each is fitted with an elliptical Gaussian on a tilted plane; the FWHM of a
    star is 2.355 * sqrt(sigma_x * sigma_y), and the result is the 3-sigma-clipped median.
    The fit box is widened and the fits redone if the stars turn out larger than it suits."""
    from scipy import ndimage
    from scipy.spatial import cKDTree
    say = log or (lambda m: None)
    img = np.asarray(image, dtype=np.float32)
    lum = img.mean(axis=-1) if img.ndim == 3 else img
    top = img.max(axis=-1) if img.ndim == 3 else img
    H, W = lum.shape
    resid = lum - _block_background(lum)
    noise = 1.4826 * float(np.median(np.abs(np.diff(resid[::2, ::2], axis=1)))) / np.sqrt(2)
    if noise <= 0:
        noise = float(resid.std()) or 1e-6
    sm = ndimage.gaussian_filter(resid, 1.0)
    is_max = sm == ndimage.maximum_filter(sm, size=5)
    faint = np.argwhere(is_max & (sm > 5 * noise))              # every source, for the isolation test
    strong = is_max & (resid > 20 * noise)
    sat_level = 0.95 * float(top.max())

    def measure(half: int) -> np.ndarray:
        cand = np.argwhere(strong)
        cand = cand[(cand[:, 0] >= half) & (cand[:, 0] < H - half) & (cand[:, 1] >= half) & (cand[:, 1] < W - half)]
        if len(cand) == 0:
            return np.array([])
        if len(faint) > 1:
            tree = cKDTree(faint)
            counts = np.array([len(c) for c in tree.query_ball_point(cand, r=1.5 * half)])
            cand = cand[counts <= 1]                            # only itself in the neighbourhood
        peaks = resid[cand[:, 0], cand[:, 1]]
        order = np.argsort(-peaks)
        fwhms = []
        for y, x in cand[order]:
            if len(fwhms) >= max_stars:
                break
            if top[y - 1:y + 2, x - 1:x + 2].max() >= sat_level:
                continue
            core = lum[y - 1:y + 2, x - 1:x + 2]
            if (core >= 0.99 * lum[y, x]).sum() > 2:            # flat top: saturated
                continue
            box = lum[y - half:y + half + 1, x - half:x + half + 1]
            f = _fit_star(box, guess_sigma=max(1.0, half / 6))
            if f is None:
                continue
            dx, dy, sx, sy, a = f
            if abs(dx) > 1.5 or abs(dy) > 1.5 or a < 10 * noise:
                continue
            if not (0.3 < sx < half / 2 and 0.3 < sy < half / 2) or not 0.5 < sx / sy < 2:
                continue
            fwhms.append(FWHM_PER_SIGMA * np.sqrt(sx * sy))
        return np.array(fwhms)

    half = 8
    for _ in range(3):
        v = measure(half)
        if len(v) < min_stars:
            raise tools.StageError(f"psf auto: only {len(v)} isolated, unsaturated stars could be measured "
                                   f"(need {min_stars}); set [deconv] psf to the star FWHM in pixels")
        fwhm, keep = _clipped_median(v)
        want = int(np.ceil(2.5 * fwhm))
        if want <= half or half >= 30:
            break
        half = min(30, want)
    say(f"psf auto: median star FWHM {fwhm:.2f} px from {int(keep.sum())} of {len(v)} stars "
        f"(fit box {2 * half + 1} px)")
    return fwhm, int(keep.sum())


# ---------------------------------------------------------------------- run

def run(src: Path, dst: Path, strength: float, psf, cpu: bool, log_path: Path, log=print) -> None:
    import onnxruntime as ort
    from astropy.io import fits

    from . import models
    model = tools.deconv_model()
    if not model.is_file():
        log(f"deconvolution model not found at {model}; fetching it with GraXpert "
            "(the same as `darkroom fetch-models --deconv`)")
        models.fetch_deconv_model(log_path, log)
    models.verify_deconv_model(model)
    with fits.open(src) as hdul:
        hdr, data = hdul[0].header, hdul[0].data.astype(np.float32)
    if not (data.ndim == 3 and data.shape[0] == 3):
        raise tools.StageError(f"{src}: expected a 3-channel Siril/GraXpert FITS, got shape {data.shape}")
    img = np.ascontiguousarray(np.moveaxis(data, 0, -1))
    if min(img.shape[:2]) < WIN // 2:
        raise tools.StageError(f"{src}: the image is {img.shape[1]}x{img.shape[0]}; deconvolution needs at "
                               f"least {WIN // 2} pixels on each side")

    if psf == "auto":
        t = time.time()
        fwhm, _ = measure_fwhm(img, log)
        log(f"  (measured in {time.time() - t:.1f}s)")
    else:
        fwhm = float(psf)
        log(f"psf: star FWHM {fwhm:g} px (given)")
    log(f"strength {strength:g}; model inputs: psf {psf_param(fwhm):.4f}, strength {strength * STRENGTH_SCALE:.4f}")

    prov, backend = providers(cpu, ort.get_available_providers())
    log(f"deconvolution backend: {backend}" + (" (--cpu)" if cpu else "")
        + ("" if cpu or backend != "CPU" else " (onnxruntime has no CoreML or CUDA provider here; this is slow)"))
    t = time.time()
    sess = ort.InferenceSession(str(model), providers=prov)
    log(f"providers: {sess.get_providers()}  (load {time.time() - t:.1f}s)")

    t = time.time()
    out = deconvolve(img, sess, strength, fwhm, log)
    log(f"deconvolution done in {time.time() - t:.1f}s")
    hdr["HISTORY"] = (f"GraXpert object deconvolution {models.DECONV_VERSION} strength {strength:g} "
                      f"psf {fwhm:.2f}px via onnxruntime {ort.__version__} {backend}")
    fits.PrimaryHDU(np.moveaxis(out, -1, 0), header=hdr).writeto(dst, overwrite=True)
