"""Images the maths must not turn into NaN: no target in the mask, no sky outside it, constant
channels and tiles (the empty corners of a mosaic canvas). Synthetic arrays and fake sessions."""
import numpy as np
import pytest

from darkroom import deconv as dc, denoise as dn, finish as fin, tools
from darkroom.finish import FinishParams


def quiet(m):
    pass


# --- finish

def flat_image(level=0.2, h=120, w=160, seed=0, noise=0.002):
    rng = np.random.default_rng(seed)
    img = level + noise * rng.standard_normal((h, w, 3))
    return np.clip(img, 0, 1).astype(np.float32)


def test_recombine_without_a_target_skips_yb_and_stays_finite():
    sl = flat_image()                                   # all sky: the target mask is empty
    msgs = []
    out = fin.recombine(sl, sl, FinishParams(), log=msgs.append)
    assert np.isfinite(out).all()
    assert any("yb: no pixel is inside the target mask" in m for m in msgs)


def test_recombine_of_a_constant_image_is_finite():
    sl = np.full((64, 80, 3), 0.3, np.float32)          # no pixel below the 60th percentile
    out = fin.recombine(sl, sl, FinishParams(), log=quiet)
    assert np.isfinite(out).all()
    out, gm = fin.keep_stars(sl, FinishParams(stars="keep"))
    assert np.isfinite(gm).all()


def test_neutralize_without_sky_uses_the_faintest_part_of_the_mask():
    h, w = 100, 100
    gm = np.linspace(0.2, 1.0, h * w).reshape(h, w, 1)   # a nebula that fills the frame
    out = np.empty((h, w, 3))
    out[:] = [0.5, 0.4, 0.3]
    out[gm[..., 0] <= np.percentile(gm, 5)] = [0.30, 0.20, 0.10]   # its faintest part has a cast
    msgs = []
    res = fin.neutralize(out, gm, log=msgs.append)
    assert np.isfinite(res).all()
    assert any("faintest 5%" in m for m in msgs)
    faint = res[gm[..., 0] <= np.percentile(gm, 5)]
    np.testing.assert_allclose(faint, [[0.2, 0.2, 0.2]] * len(faint), atol=1e-9)


def test_neutralize_skips_when_the_mask_covers_everything():
    out = np.full((20, 20, 3), 0.4)
    msgs = []
    res = fin.neutralize(out, np.ones((20, 20, 1)), log=msgs.append)
    assert res is out and "skipped" in msgs[-1]


def test_neutralize_with_sky_is_unchanged():
    gm = np.zeros((10, 10, 1))
    gm[:5] = 1
    out = np.full((10, 10, 3), [0.3, 0.2, 0.1])
    msgs = []
    res = fin.neutralize(out, gm, log=msgs.append)
    np.testing.assert_allclose(res, 0.2)
    assert len(msgs) == 1 and msgs[0].startswith("sky RGB before neutralization")


def test_save_refuses_non_finite_pixels(tmp_path):
    img = np.full((4, 4, 3), 0.5, np.float32)
    img[1, 2, 0] = np.nan
    with pytest.raises(ValueError, match="1 NaN or infinite"):
        fin.save(img, tmp_path / "final", log=quiet)
    assert not list(tmp_path.iterdir())


# --- denoise

class EchoSession:
    """Returns its input: denoise() then gives back the clipped input."""

    def run(self, _, feed):
        return [feed["gen_input_image"].copy()]


def test_robust_spread_of_an_ordinary_image_is_the_plain_median_and_mad():
    rng = np.random.default_rng(1)
    img = rng.uniform(0, 1, (40, 50, 3)).astype(np.float32)
    med, mad = dn.robust_spread(img, log=quiet)
    np.testing.assert_array_equal(med, np.median(img, axis=(0, 1)))
    np.testing.assert_array_equal(mad, np.median(np.abs(img - np.median(img, axis=(0, 1))), axis=(0, 1)))


def test_robust_spread_of_a_mostly_empty_channel_is_guarded():
    img = np.zeros((40, 50, 3), np.float32)
    img[:10, :, 0] = np.linspace(0.1, 0.3, 50)          # a quarter of channel 0 has data
    img[..., 1] = 0.25                                  # channel 1: one level
    img[..., 2] = np.random.default_rng(2).uniform(0, 1, (40, 50))
    msgs = []
    med, mad = dn.robust_spread(img, log=msgs.append)
    assert list(mad[:2]) == [dn.MIN_MAD, dn.MIN_MAD] and mad[2] > 0.1
    assert list(med[:2]) == [0, np.float32(0.25)]
    assert len(msgs) == 2 and "channel 0" in msgs[0]


def test_denoise_of_a_mostly_empty_image_is_finite():
    img = np.zeros((300, 260, 3), np.float32)
    img[100:180, 90:170] = 0.3 + 0.01 * np.random.default_rng(4).standard_normal((80, 80, 3))
    out = dn.denoise(img, EchoSession(), 0.6, log=quiet)
    assert np.isfinite(out).all()
    np.testing.assert_allclose(out, img, atol=1e-6)


def test_check_finite():
    dn.check_finite(np.zeros(3), "denoise")
    with pytest.raises(tools.StageError, match="deconv: the result has 1 NaN"):
        dc.check_finite(np.array([0.0, np.inf]), "deconv")


# --- deconv

class ShiftSession:
    """A model that changes every tile it sees, and counts the calls."""

    def __init__(self):
        self.calls = 0

    def run(self, _, feed):
        self.calls += 1
        return [np.full_like(feed["gen_input_image"], -0.01)]


def test_constant_tiles_pass_through_unchanged():
    H, W = 1200, 1200
    img = np.zeros((H, W, 3), np.float32)                # a mostly empty canvas
    img[500:700, 500:700] = np.random.default_rng(5).uniform(0.1, 0.5, (200, 200, 3))
    sess = ShiftSession()
    msgs = []
    out = dc.deconvolve(img, sess, 0.5, 3.0, log=msgs.append)
    assert np.isfinite(out).all()
    n = (H // dc.STRIDE + 1) * (W // dc.STRIDE + 1)
    assert 0 < sess.calls < n
    assert any(f"{n - sess.calls} of {n} tiles are of one level" in m for m in msgs)
    np.testing.assert_array_equal(out[:300, :300], 0)    # a corner far from the data: as it was
    assert not np.array_equal(out[550:650, 550:650], img[550:650, 550:650])


def test_a_constant_image_comes_back_as_it_was():
    img = np.full((600, 600, 3), 0.25, np.float32)
    sess = ShiftSession()
    out = dc.deconvolve(img, sess, 0.5, 3.0, log=quiet)
    assert sess.calls == 0
    np.testing.assert_array_equal(out, img)
