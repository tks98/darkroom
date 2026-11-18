"""Synthetic-array tests for the finish (recombine) maths."""
import numpy as np
import pytest

from darkroom import finish as fin
from darkroom.finish import FinishParams


def gaussian_star(n=41, sigma=2.5, peak=0.9):
    y, x = np.mgrid[:n, :n] - n // 2
    return (peak * np.exp(-(x * x + y * y) / (2 * sigma * sigma))).astype(np.float32)


def half_max_width(profile):
    return int((profile >= profile.max() / 2).sum())


def test_screen_unscreen_round_trip():
    rng = np.random.default_rng(0)
    sl = rng.uniform(0, 0.9, (32, 32, 3)).astype(np.float32)
    stars = rng.uniform(0, 1, (32, 32, 3)).astype(np.float32)
    orig = fin.screen(sl, stars)
    np.testing.assert_allclose(fin.unscreen(orig, sl), stars, atol=1e-5)
    np.testing.assert_allclose(fin.screen(sl, fin.unscreen(orig, sl)), orig, atol=1e-6)


def test_unscreen_clips_to_unit_range():
    orig = np.array([0.1, 0.5, 1.0], np.float32)
    sl = np.array([0.2, 0.5, 1.0], np.float32)        # orig < starless and a saturated pixel
    s = fin.unscreen(orig, sl)
    assert s.min() >= 0 and s.max() <= 1


@pytest.mark.parametrize("m", [0.2, 0.42, 0.45, 0.7])
def test_mtf_fixed_points(m):
    assert fin.mtf(m, 0.0) == pytest.approx(0.0)
    assert fin.mtf(m, 1.0) == pytest.approx(1.0)
    assert fin.mtf(m, m) == pytest.approx(0.5)


def test_mtf_below_half_brightens():
    x = np.linspace(0.01, 0.99, 50)
    assert np.all(fin.mtf(0.45, x) > x)


def test_star_gamma_shrinks_half_max_width():
    star = gaussian_star(n=61, sigma=6.0)
    s = np.repeat(star[..., None], 3, -1)
    out = fin.star_layer(s, gamma=1.6, star_strength=1.0)
    c = star.shape[0] // 2
    before, after = half_max_width(s[c, :, 0]), half_max_width(out[c, :, 0])
    # a gaussian raised to gamma is a gaussian with sigma / sqrt(gamma)
    assert after < before
    assert after == pytest.approx(before / np.sqrt(1.6), abs=1.5)


def test_star_erode_dims_star():
    s = np.repeat(gaussian_star(sigma=1.5)[..., None], 3, -1)
    assert fin.star_layer(s, 1.0, 1.0, erode=1).sum() < s.sum()


def test_neutralize_equalises_tinted_flat_sky():
    rng = np.random.default_rng(1)
    tint = np.array([0.12, 0.10, 0.08], np.float32)
    img = (tint + rng.normal(0, 0.002, (64, 64, 3))).astype(np.float32)
    gm = np.zeros((64, 64, 1), np.float32)          # no galaxy: all sky
    out = fin.neutralize(img, gm, log=lambda m: None)
    med = np.median(out.reshape(-1, 3), axis=0)
    assert np.ptp(med) < 0.002
    assert med.mean() == pytest.approx(np.median(img.reshape(-1, 3), axis=0).mean(), abs=1e-3)


def test_crop_geometry():
    img = np.arange(10 * 20 * 3).reshape(10, 20, 3)
    out = fin.crop(img, (3, 2, 5, 4))                 # x, y, w, h
    assert out.shape == (4, 5, 3)
    np.testing.assert_array_equal(out[0, 0], img[2, 3])
    np.testing.assert_array_equal(out[-1, -1], img[5, 7])


def test_parse_crop():
    assert fin.parse_crop("0,512,1710,2565") == (0, 512, 1710, 2565)
    with pytest.raises(ValueError):
        fin.parse_crop("1,2,3")


def test_default_params_match_final_settings():
    p = FinishParams()
    assert (p.gamma, p.star_strength, p.mid, p.sat, p.yb, p.sharpen, p.sharpen_radius, p.black,
            p.erode, p.neutral, p.crop) == (1.6, 0.95, 0.45, 1.2, 1.8, 0.8, 2.0, 0.02, 0, True, None)


def test_recombine_without_stars_or_polish_is_identity_like():
    """No stars, no black lift, mid 0.5, sat 1, no yb/sharpen/neutral -> output == starless."""
    rng = np.random.default_rng(2)
    sl = rng.uniform(0.05, 0.6, (48, 48, 3)).astype(np.float32)
    p = FinishParams(black=0.0, mid=0.5, sat=1.0, yb=1.0, core=0.0, sharpen=0.0, neutral=False)
    out = fin.recombine(sl, sl, p, log=lambda m: None)
    np.testing.assert_allclose(out, sl, atol=1e-6)


def test_save_round_trip(tmp_path):
    img = np.linspace(0, 1, 8 * 6 * 3, dtype=np.float32).reshape(8, 6, 3)
    fin.save(img, tmp_path / "x", log=lambda m: None)
    back = fin.load(tmp_path / "x.tif")
    assert back.shape == img.shape
    np.testing.assert_allclose(back, img, atol=1 / 65535)
    assert (tmp_path / "x.jpg").is_file()


def test_neutralize_is_not_fooled_by_unequal_channel_noise():
    """A grey sky whose blue channel is noisier must stay grey (regression: measuring the sky
    from the darker half of the pixels reported blue as low and added a blue cast)."""
    rng = np.random.default_rng(0)
    sky = np.full((200, 200, 3), 0.10, np.float32)
    sky += rng.normal(0, [0.004, 0.004, 0.02], sky.shape).astype(np.float32)
    gm = np.zeros((200, 200, 1), np.float32)          # no target: everything is sky
    out = fin.neutralize(sky, gm, log=lambda *a: None)
    med = np.median(out.reshape(-1, 3), axis=0)
    assert np.ptp(med) < 0.001
    assert abs(np.median(out) - 0.10) < 0.002         # and the level is unchanged


def _bulge(n=200, peak=0.97):
    """Synthetic starless image: dim sky, a bright Gaussian bulge with fine ripples, warm colour."""
    yy, xx = np.mgrid[:n, :n].astype(np.float32)
    r2 = (yy - n / 2) ** 2 + (xx - n / 2) ** 2
    L = 0.08 + (peak - 0.08) * np.exp(-r2 / (2 * 30 ** 2)) * (1 + 0.02 * np.sin(xx / 2))
    return np.clip(np.stack([L * 1.05, L, L * 0.9], -1), 0, 1).astype(np.float32)


def test_compress_core_off_is_identity():
    g = _bulge()
    assert np.array_equal(fin.compress_core(g, 0.0), g)


def test_compress_core_lowers_core_keeps_sky_and_colour():
    g = _bulge()
    out = fin.compress_core(g, 0.3)
    c = 100
    assert out[c, c].mean() < g[c, c].mean() - 0.02          # the core comes down
    assert np.allclose(out[:10, :10], g[:10, :10])           # sky far from the core untouched
    ratio = out[c - 5:c + 5, c - 5:c + 5] / g[c - 5:c + 5, c - 5:c + 5]
    assert np.allclose(ratio[..., 0], ratio[..., 2], atol=1e-5)   # same factor on every channel


def test_compress_core_keeps_fine_detail():
    g = _bulge()
    out = fin.compress_core(g, 0.3)
    hp = lambda a: a.mean(-1) - fin.ndi.gaussian_filter(a.mean(-1), 3)
    core = (slice(85, 115), slice(85, 115))
    rel = lambda a: hp(a)[core].std() / a.mean(-1)[core].mean()
    assert rel(out) >= rel(g) * 0.99                          # relative ripple contrast not flattened


def test_default_core():
    assert FinishParams().core == 0.3
