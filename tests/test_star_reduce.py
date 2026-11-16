import numpy as np

from darkroom.star_reduce import disk, reduce_stars


def test_disk():
    d = disk(2)
    assert d.shape == (5, 5) and d[2, 2] and not d[0, 0]


def test_reduce_stars_dims_small_star_not_flat_sky():
    rng = np.random.default_rng(0)
    img = (0.1 + rng.normal(0, 0.003, (3, 64, 64))).astype(np.float32)
    y, x = np.mgrid[:64, :64]
    img += (0.6 * np.exp(-((x - 32) ** 2 + (y - 32) ** 2) / (2 * 1.5 ** 2))).astype(np.float32)
    out, mask, sig = reduce_stars(img)
    assert out[:, 32, 32].mean() < img[:, 32, 32].mean()
    assert mask[32, 32] > 0.5
    assert mask[5, 5] < 0.05
