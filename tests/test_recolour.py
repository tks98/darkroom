"""finish blue_shift and core_white: the Oklab colour pass on the finished image."""
import numpy as np
import pytest

from darkroom import cli, config, stages
from darkroom import finish as fin


def oklab_hue_chroma(rgb):
    lab = np.cbrt(fin._srgb_to_linear(np.asarray(rgb, dtype=np.float64)) @ fin._LMS.T) @ fin._LAB.T
    return np.degrees(np.arctan2(lab[..., 2], lab[..., 1])) % 360, np.hypot(lab[..., 1], lab[..., 2]), lab[..., 0]


PURPLE = [0.47, 0.33, 0.50]       # a faint purple disk pixel
YELLOW = [0.95, 0.80, 0.50]       # a bright yellow core pixel
DIM_YELLOW = [0.35, 0.28, 0.15]   # a faint yellow pixel


def test_off_is_identity():
    img = np.random.default_rng(0).random((8, 8, 3)).astype(np.float32)
    assert fin.recolour(img, 0.0, 0.0) is img


def test_blue_shift_turns_purple_toward_blue_and_leaves_yellow():
    img = np.array([[PURPLE, YELLOW]], dtype=np.float32)
    h0, c0, l0 = oklab_hue_chroma(img)
    assert 310 < h0[0, 0] < 340                       # the test colour is purple
    out = fin.recolour(img, blue_shift=1.0)
    h1, c1, l1 = oklab_hue_chroma(out)
    assert abs(h1[0, 0] - fin.BLUE_HUE) < abs(h0[0, 0] - fin.BLUE_HUE) - 20
    assert np.allclose(l1, l0, atol=2e-3)             # lightness kept
    assert np.allclose(out[0, 1], img[0, 1], atol=2e-3)   # yellow untouched


def test_core_white_desaturates_bright_but_not_faint():
    img = np.array([[YELLOW, DIM_YELLOW]], dtype=np.float32)
    _, c0, l0 = oklab_hue_chroma(img)
    assert l0[0, 0] > fin.CORE_L1 and l0[0, 1] < fin.CORE_L0
    out = fin.recolour(img, core_white=0.75)
    _, c1, l1 = oklab_hue_chroma(out)
    assert c1[0, 0] == pytest.approx(0.25 * c0[0, 0], rel=0.05)
    assert c1[0, 1] == pytest.approx(c0[0, 1], rel=1e-3)
    assert np.allclose(l1, l0, atol=2e-3)


def test_recolour_keeps_range_and_dtype():
    img = np.random.default_rng(1).random((16, 16, 3)).astype(np.float32)
    out = fin.recolour(img, 1.0, 1.0)
    assert out.dtype == np.float32 and out.min() >= 0 and out.max() <= 1


def test_cli_flags():
    a = cli.build_parser().parse_args(["finish", "--work", "w", "--blue-shift", "1", "--core-white", "0.75"])
    p = cli._finish_params(a)
    assert (p.blue_shift, p.core_white) == (1.0, 0.75)
    d = cli._finish_params(cli.build_parser().parse_args(["finish", "--work", "w"]))
    assert (d.blue_shift, d.core_white) == (0.0, 0.0)


def test_settings_reach_finish_params(tmp_path):
    (tmp_path / "darkroom.toml").write_text("[finish]\nblue_shift = 1\ncore_white = 0.75\n")
    p = stages.finish_params(config.resolve(tmp_path)["finish"])
    assert (p.blue_shift, p.core_white) == (1.0, 0.75)


@pytest.mark.parametrize("text", ["[finish]\nblue_shift = 1.5\n", "[finish]\ncore_white = -0.1\n"])
def test_out_of_range_is_an_error(tmp_path, text):
    (tmp_path / "darkroom.toml").write_text(text)
    with pytest.raises(config.ConfigError, match="blue_shift|core_white"):
        config.resolve(tmp_path)


def test_recorded_only_when_on():
    base = config.resolve("/nonexistent-work")["finish"]
    rec = stages.result_params("finish", base)
    assert "blue_shift" not in rec and "core_white" not in rec
    rec = stages.result_params("finish", base | {"blue_shift": 1.0, "core_white": 0.75})
    assert rec["blue_shift"] == 1.0 and rec["core_white"] == 0.75
