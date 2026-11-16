"""What the second target (M13, a globular cluster) added: frame filters by background and star
count, a finish path that keeps the stars, a smaller aspect-ratio crop, and the cluster look."""
from pathlib import Path

import numpy as np
import pytest

from darkroom import cli, config, finish as fin, siril, stages
from darkroom.finish import FinishParams

from test_run import blob_image, fake, names, run  # noqa: F401  (fake is a fixture)


# --- frame filters

def seqapplyreg(**values):
    base = {"SUBS": "/s", "PROCESS": "/w/process", "DARK": "/d.fit", "STACK": "/w/stack"}
    text = siril.render((siril.template_dir() / "stack.ssf").read_text(), base | values)
    return next(l for l in text.splitlines() if l.startswith("seqapplyreg"))


def test_extra_filters_are_off_by_default():
    assert seqapplyreg() == "seqapplyreg pp_light -filter-wfwhm=90% -filter-round=90% -framing=min"


def test_extra_filters_render():
    line = seqapplyreg(MORE_FILTERS="-filter-bkg=70% -filter-nbstars=80% ")
    assert line == ("seqapplyreg pp_light -filter-wfwhm=90% -filter-round=90% "
                    "-filter-bkg=70% -filter-nbstars=80% -framing=min")


def test_filters_are_recorded_only_when_used():
    t = {"subs": Path("/s"), "dark": None, "framing": "min", "keep_fwhm": 90, "keep_round": 90,
         "keep_bkg": 100, "keep_stars": 100}
    assert "keep_bkg" not in stages.result_params("stack", t)      # old work dirs stay up to date
    assert stages.result_params("stack", t | {"keep_bkg": 70})["keep_bkg"] == 70.0


@pytest.mark.parametrize("text,match", [
    ("[stack]\nkeep_bkg = 0\n", "keep_bkg"),
    ("[stack]\nkeep_stars = 120\n", "keep_stars"),
    ('[finish]\nstars = "remove"\n', "stars"),
    ("[finish]\ncrop_scale = 0\n", "crop_scale"),
    ("[finish]\ncrop_scale = 1.5\n", "crop_scale"),
])
def test_bad_values_are_errors(tmp_path, text, match):
    (tmp_path / "darkroom.toml").write_text(text)
    with pytest.raises(config.ConfigError, match=match):
        config.resolve(tmp_path)


# --- crop scale

def test_crop_scale_shrinks_the_box_around_the_target():
    assert fin.aspect_box(1690, 3778, (1, 1), (1008, 1851), 0.45) == (628, 1471, 760, 760)   # M13
    assert fin.aspect_box(1710, 3632, (2, 3), (977, 1794), 1.0) == (0, 512, 1710, 2565)      # M31 unchanged


def test_crop_scale_box_stays_inside_the_image():
    x, y, w, h = fin.aspect_box(400, 300, (1, 1), (395, 5), 0.5)
    assert (w, h) == (150, 150) and x + w <= 400 and y >= 0


# --- keeping the stars

def test_keep_stars_with_neutral_settings_returns_the_input():
    img = blob_image()
    p = FinishParams(stars="keep", black=0.0, mid=0.5, sat=1.0, neutral=False)
    out = fin.recombine(img, None, p, log=lambda *a: None)       # no starless image needed
    assert np.allclose(out, img, atol=1e-6)


def test_keep_stars_ignores_the_star_and_galaxy_settings():
    img = blob_image()
    a = fin.recombine(img, None, FinishParams(stars="keep", gamma=1.0, yb=1.0, sharpen=0.0), log=lambda *a: None)
    b = fin.recombine(img, None, FinishParams(stars="keep", gamma=3.0, yb=4.0, sharpen=2.0), log=lambda *a: None)
    assert np.array_equal(a, b)


def test_keep_stars_crop_is_centred_on_the_target():
    msgs = []
    p = FinishParams(stars="keep", black=0.0, mid=0.5, sat=1.0, neutral=False, crop_aspect=(1, 1), crop_scale=0.5)
    out = fin.recombine(blob_image(), None, p, log=msgs.append)
    assert out.shape == (100, 100, 3)
    assert "x=120, y=210" in msgs[-1] and "stretched" in msgs[-1]


def test_run_skips_starnet_when_stars_are_kept(fake, capsys):
    work, ran, _ = fake
    with open(work / "darkroom.toml", "a") as f:
        f.write('stars = "keep"\n')
    assert run(work) == 0
    assert names(ran) == [s for s in stages.STAGES if s != "starnet"]
    assert '-- starnet: not needed' in capsys.readouterr().out
    assert run(work) == 0 and names(ran) == []                   # and the result is up to date


def test_finish_inputs_without_starnet(tmp_path):
    assert [p.name for p in stages.inputs(tmp_path, "finish", {"stars": "keep"})] == ["stretched.tif"]
    assert [p.name for p in stages.inputs(tmp_path, "finish", {})] == ["stretched.tif", "starless.tif"]


# --- the cluster look

def test_cluster_look_resolves():
    s = config.resolve(Path("/nonexistent-work"), None, {("target", "look"): "cluster"})
    assert s["finish"]["stars"] == "keep" and s["stretch"]["asinh"] == 100
    assert not stages.starnet_needed(s["finish"])
    p = stages.finish_params(s["finish"])
    assert (p.stars, p.mid, p.sat, p.black) == ("keep", 0.5, 1.3, 0.04)


def test_galaxy_look_still_uses_starnet():
    s = config.resolve(Path("/nonexistent-work"), None, {("target", "look"): "galaxy"})
    assert stages.starnet_needed(s["finish"]) and cli.stages.finish_params(s["finish"]) == FinishParams()
