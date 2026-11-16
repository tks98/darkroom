"""Morphological star reduction for a stretched RGB FITS (values 0..1).

The non-StarNet fallback, ported unchanged from the hand-run star_reduce.py.

1. Star mask: white top-hat of luminance (keeps features smaller than
   radius px, so the galaxy core/arms/M32/M110 are not masked), thresholded
   against the background noise, grown and feathered.
2. Inside the mask, blend toward a grey-eroded (minimum-filtered) image, which
   shrinks star discs while the soft mask edges keep it seamless.
"""
from __future__ import annotations

import numpy as np
from scipy import ndimage as ndi


def disk(r):
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return x * x + y * y <= r * r


def reduce_stars(img, amount=0.85, iters=2, radius=6, k=4.0):
    """img: 3 x H x W float32. Returns (out, mask, sig)."""
    L = img.mean(axis=0)

    # 1. star mask
    th = ndi.white_tophat(L, footprint=disk(radius))
    sig = 1.4826 * np.median(np.abs(th - np.median(th)))
    mask = th > k * sig
    mask = ndi.binary_opening(mask, structure=np.ones((2, 2)))          # drop single noisy pixels
    mask = ndi.binary_dilation(mask, structure=disk(2), iterations=1)   # cover the star halo
    mask = ndi.gaussian_filter(mask.astype(np.float32), 1.5)
    mask = np.clip(mask * 1.5, 0, 1) * amount

    # 2. erode each channel and blend through the mask
    er = img.copy()
    for c in range(3):
        for _ in range(iters):
            er[c] = ndi.grey_erosion(er[c], footprint=disk(1))
    out = img * (1 - mask) + er * mask
    return out, mask, sig


def run(src, dst, amount=0.85, iters=2, radius=6, k=4.0, mask_path=None, log=print):
    from astropy.io import fits

    with fits.open(src) as h:
        hdr, img = h[0].header, h[0].data.astype(np.float32)   # 3 x H x W
    out, mask, sig = reduce_stars(img, amount, iters, radius, k)

    log(f"noise sigma {sig:.5f}, masked fraction {(mask > 0.05).mean():.3f}")
    hdr["HISTORY"] = f"star_reduce amount={amount} iters={iters} radius={radius} k={k}"
    fits.PrimaryHDU(out.astype(np.float32), header=hdr).writeto(dst, overwrite=True)
    if mask_path:
        fits.PrimaryHDU(mask.astype(np.float32)).writeto(mask_path, overwrite=True)
