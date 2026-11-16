#!/usr/bin/env python3
"""Compare two images numerically (not a pytest module; run it by hand).

Usage:
  python tests/compare_outputs.py OURS REFERENCE [OURS REFERENCE ...]

FITS (3 x H x W float), TIFF (H x W x 3, 8/16-bit or float) and JPEG/PNG are read as
H x W x C float64 on a 0..1 scale (integer TIFFs divided by their dtype max).
Prints shape, mean and max absolute difference. Exit status 1 if shapes differ.
"""
import sys

import numpy as np


def load(path):
    p = str(path).lower()
    if p.endswith((".fit", ".fits", ".fts")):
        from astropy.io import fits
        with fits.open(path) as h:
            x = np.asarray(h[0].data, dtype=np.float64)
        if x.ndim == 3:
            x = np.moveaxis(x, 0, -1)
        return x
    if p.endswith((".jpg", ".jpeg", ".png")):
        from PIL import Image
        x = np.asarray(Image.open(path))
        return x.astype(np.float64) / 255.0
    import tifffile
    x = tifffile.imread(path)
    if x.ndim == 3 and x.shape[0] in (1, 3) and x.shape[-1] not in (1, 3):
        x = np.moveaxis(x, 0, -1)
    if np.issubdtype(x.dtype, np.integer):
        return x.astype(np.float64) / np.iinfo(x.dtype).max
    return x.astype(np.float64)


def compare(a_path, b_path):
    a, b = load(a_path), load(b_path)
    if a.shape != b.shape:
        return {"shape": (a.shape, b.shape), "mean": None, "max": None}
    d = np.abs(a - b)
    return {"shape": a.shape, "mean": float(d.mean()), "max": float(d.max()),
            "identical": bool(np.array_equal(a, b))}


def main(argv):
    if len(argv) < 2 or len(argv) % 2:
        print(__doc__, file=sys.stderr)
        return 2
    bad = False
    for a, b in zip(argv[::2], argv[1::2]):
        r = compare(a, b)
        if r["mean"] is None:
            bad = True
            print(f"{a}  vs  {b}: SHAPE MISMATCH {r['shape'][0]} vs {r['shape'][1]}")
        else:
            print(f"{a}  vs  {b}: shape {r['shape']}  mean|d| {r['mean']:.3e}  max|d| {r['max']:.3e}"
                  + ("  (identical)" if r["identical"] else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
