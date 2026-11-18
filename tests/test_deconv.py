"""The optional deconv stage: tiling, PSF scaling and measurement, settings, model handling
(synthetic images and fake sessions; no model, GraXpert or network)."""
import hashlib
from pathlib import Path

import numpy as np
import pytest

from darkroom import cli, config, deconv as dc, denoise as dn, doctor, models, siril, stages, tools
from darkroom.doctor import OK, WARN


# --- PSF scaling

def test_psf_param_object_model_101():
    assert dc.psf_param(3.2) == pytest.approx((3.2 / 2.355 - 0.5) / 5.5)
    assert dc.psf_param(2.5) == pytest.approx(0.10210, abs=1e-5)
    assert dc.psf_param(1.0) == 0.05                     # clipped low
    assert dc.psf_param(40.0) == 0.95                    # clipped high


# --- tiling

class ZeroSession:
    """Predicts no change: deconvolve() must then give back its input (log/exp round trip)."""

    def __init__(self):
        self.calls = []

    def run(self, _, feed):
        self.calls.append({k: v.copy() for k, v in feed.items()})
        return [np.zeros_like(feed["gen_input_image"])]


@pytest.mark.parametrize("shape", [(600, 1000), (448, 448), (230, 300), (1000, 457), (896, 900)])
def test_zero_model_gives_back_the_input(shape):
    rng = np.random.default_rng(3)
    img = rng.uniform(0.001, 0.9, shape + (3,)).astype(np.float32)
    sess = ZeroSession()
    out = dc.deconvolve(img, sess, 0.5, 3.2, log=lambda m: None)
    assert out.shape == img.shape and out.dtype == np.float32
    np.testing.assert_allclose(out, img, rtol=2e-5, atol=2e-6)
    ith, itw = shape[0] // dc.STRIDE + 1, shape[1] // dc.STRIDE + 1
    assert len(sess.calls) == ith * itw                   # one tile (3 planes) per call
    feed = sess.calls[0]
    assert feed["gen_input_image"].shape == (3, 1, 512, 512) and feed["gen_input_image"].dtype == np.float32
    np.testing.assert_allclose(feed["params"], [[dc.psf_param(3.2), 0.5 * 0.95]] * 3, rtol=1e-6)
    assert feed["params"].dtype == np.float32


def test_tile_centres_cover_the_image_exactly():
    H, W = 1000, 457
    idx = np.arange(H * W, dtype=np.float64).reshape(H, W, 1)
    padded, ith, itw, off = dn.pad_for_tiles(idx, dc.WIN, dc.STRIDE)
    out = np.full_like(padded, -1.0)
    for i in range(ith):
        for j in range(itw):
            tile = padded[dc.STRIDE * i:dc.STRIDE * i + dc.WIN, dc.STRIDE * j:dc.STRIDE * j + dc.WIN]
            assert tile.shape[:2] == (dc.WIN, dc.WIN)
            out[dc.STRIDE * i + off:dc.STRIDE * (i + 1) + off, dc.STRIDE * j + off:dc.STRIDE * (j + 1) + off] = \
                tile[off:off + dc.STRIDE, off:off + dc.STRIDE]
    np.testing.assert_array_equal(out[off:H + off, off:W + off], idx)


def test_too_small_for_the_tiles():
    with pytest.raises(ValueError, match="too small"):
        dn.pad_for_tiles(np.zeros((100, 300, 3), np.float32), dc.WIN, dc.STRIDE)


# --- psf auto

def star_field(fwhm, n=200, size=800, seed=0):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[:size, :size]
    img = 0.1 + 0.03 * np.exp(-((xx - 500) ** 2 + (yy - 300) ** 2) / (2 * 150.0 ** 2))   # a faint "galaxy"
    s = fwhm / 2.3548
    r = int(6 * s) + 2
    for _ in range(n):
        x, y = rng.uniform(10, size - 10, 2)
        a = 10 ** rng.uniform(-2.3, -0.4)
        sl = np.s_[max(0, int(y) - r):int(y) + r + 1, max(0, int(x) - r):int(x) + r + 1]
        img[sl] += a * np.exp(-((xx[sl] - x) ** 2 + (yy[sl] - y) ** 2) / (2 * s * s))
    img = np.clip(img + 0.001 * rng.standard_normal(img.shape), 0, 0.6)       # the brightest saturate
    return np.repeat(img[..., None], 3, -1).astype(np.float32)


@pytest.mark.parametrize("fwhm", [2.5, 4.0])
def test_measure_fwhm_of_gaussian_stars(fwhm):
    msgs = []
    got, n = dc.measure_fwhm(star_field(fwhm), msgs.append)
    assert got == pytest.approx(fwhm, rel=0.05)
    assert n >= 30
    assert "median star FWHM" in msgs[-1]


def test_measure_fwhm_needs_stars():
    flat = np.full((300, 300, 3), 0.1, np.float32) + np.random.default_rng(0).normal(0, 0.001, (300, 300, 3)).astype(np.float32)
    with pytest.raises(tools.StageError, match=r"set \[deconv\] psf"):
        dc.measure_fwhm(flat)


# --- settings

def test_deconv_defaults_are_off():
    s = config.resolve(Path("/nonexistent-work"))
    assert s["deconv"] == {"strength": 0, "psf": "auto", "cpu": False}
    assert not stages.deconv_on(s["deconv"])
    a = cli.build_parser().parse_args(["deconv", "--work", "w"])
    assert (a.strength, a.psf, a.cpu) == (0.0, "auto", False)


@pytest.mark.parametrize("text,want", [
    ("[deconv]\nstrength = 0.5\npsf = 3.2\n", {"strength": 0.5, "psf": 3.2, "cpu": False}),
    ('[deconv]\nstrength = 1\npsf = "AUTO"\ncpu = true\n', {"strength": 1, "psf": "auto", "cpu": True}),
    ("[deconv]\npsf = 3\n", {"strength": 0, "psf": 3, "cpu": False}),
])
def test_deconv_config_values(tmp_path, text, want):
    (tmp_path / "darkroom.toml").write_text(text)
    assert config.resolve(tmp_path)["deconv"] == want


@pytest.mark.parametrize("text,match", [
    ("[deconv]\nstrength = 1.5\n", "strength is 0"),
    ("[deconv]\nstrength = -0.1\n", "strength is 0"),
    ('[deconv]\npsf = "wide"\n', 'a number or "auto"'),
    ("[deconv]\npsf = 0\n", "FWHM in pixels"),
    ("[deconv]\npsf = true\n", 'a number or "auto"'),
])
def test_deconv_config_errors(tmp_path, text, match):
    (tmp_path / "darkroom.toml").write_text(text)
    with pytest.raises(config.ConfigError, match=match):
        config.resolve(tmp_path)


def test_deconv_flags(tmp_path):
    a = cli.build_parser().parse_args(["deconv", "--work", str(tmp_path), "--strength", "0.4", "--psf", "2.8",
                                       "--cpu"])
    assert cli._overrides(a) == {("deconv", "strength"): 0.4, ("deconv", "psf"): 2.8, ("deconv", "cpu"): True}
    assert cli._settings(a)["deconv"] == {"strength": 0.4, "psf": 2.8, "cpu": True}
    a = cli.build_parser().parse_args(["deconv", "--work", str(tmp_path), "--psf", "Auto"])
    assert cli._overrides(a) == {("deconv", "psf"): "auto"}
    for bad in ("x", "0", "-1"):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["deconv", "--work", "w", "--psf", bad])
    a = cli.build_parser().parse_args(["run", "--work", str(tmp_path), "--cpu"])         # run --cpu: both stages
    assert cli._overrides(a) == {("denoise", "cpu"): True, ("deconv", "cpu"): True}


def test_recorded_settings_and_inputs(tmp_path):
    on = {"strength": 0.5, "psf": "auto", "cpu": False}
    off = {"strength": 0, "psf": "auto", "cpu": False}
    st = config.resolve(tmp_path)["stretch"]
    assert stages.result_params("deconv", on) == {"strength": 0.5, "psf": "auto", "cpu": False}
    assert stages.result_params("deconv", on | {"psf": 3}) == {"strength": 0.5, "psf": 3.0, "cpu": False}
    assert stages.stretch_params(st, off) == stages.result_params("stretch", st)       # as before deconv existed
    assert stages.stretch_params(st, on) == stages.result_params("stretch", st) | {"input": "deconvolved.fits"}
    assert [p.name for p in stages.inputs(tmp_path, "stretch", stages.stretch_params(st, off))] == ["denoised.fits"]
    assert [p.name for p in stages.inputs(tmp_path, "stretch", stages.stretch_params(st, on))] == ["deconvolved.fits"]
    assert [p.name for p in stages.inputs(tmp_path, "deconv", {})] == ["denoised.fits"]
    assert [p.name for p in stages.outputs(tmp_path, "deconv")] == ["deconvolved.fits"]
    assert stages.stretch_source(off) == "denoised.fits" and stages.stretch_source(on) == "deconvolved.fits"


def test_stretch_script_input():
    tpl = (siril.template_dir() / "stretch.ssf").read_text()
    base = {"WORK": "/w", "ASINH": "25", "SHADOWS": "-2.5", "TARGET_BG": "0.1", "SAT": "0.35"}
    assert "load denoised.fits" in siril.render(tpl, base).splitlines()
    assert "load denoised.fits" in siril.render(tpl, base | {"INPUT": "denoised.fits"}).splitlines()
    assert "load deconvolved.fits" in siril.render(tpl, base | {"INPUT": "deconvolved.fits"}).splitlines()


def test_stage_refuses_when_off(tmp_path):
    with pytest.raises(tools.StageError, match="strength = 0"):
        stages.deconv(tmp_path, 0.0)
    assert cli.main(["deconv", "--work", str(tmp_path)]) == 1
    assert stages.recorded_params(tmp_path, "deconv") is None


def test_stretch_names_the_missing_deconvolved_file(tmp_path):
    with pytest.raises(tools.StageError, match="darkroom deconv"):
        stages.stretch(tmp_path, src=stages.DECONVOLVED)


# --- the model

@pytest.fixture
def small_model(tmp_path, monkeypatch):
    """A stand-in model file, with the expected size and hash pointed at it."""
    p = tmp_path / "deconvolution-object-ai-models" / "1.0.1" / "model.onnx"
    data = b"not really onnx"
    monkeypatch.setattr(models, "DECONV_BYTES", len(data))
    monkeypatch.setattr(models, "DECONV_SHA256", hashlib.sha256(data).hexdigest())
    return p, data


def test_model_checks(small_model):
    p, data = small_model
    assert "not found" in models.deconv_model_problem(p)
    p.parent.mkdir(parents=True)
    p.write_bytes(data + b"x")
    assert "bytes" in models.deconv_model_problem(p)
    p.write_bytes(data[::-1])
    assert "SHA-256" in models.deconv_model_problem(p)
    with pytest.raises(tools.StageError, match="fetch-models --deconv"):
        models.verify_deconv_model(p)
    p.write_bytes(data)
    assert models.deconv_model_problem(p) is None and models.verify_deconv_model(p) == p


def test_fetch_deconv_model_runs_graxpert_once(tmp_path, small_model):
    p, data = small_model
    exe = tmp_path / "GraXpert"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    seen = []

    def runner(cmd, log_path):
        seen.append(cmd)
        from astropy.io import fits
        assert fits.getdata(cmd[-1]).shape == (3, 256, 256)       # big enough for one 512-pixel tile
        p.parent.mkdir(parents=True)
        p.write_bytes(data)
    msgs = []
    assert models.fetch_deconv_model(tmp_path / "log", msgs.append, run=runner, exe=exe, model=p) == p
    (cmd,) = seen
    assert cmd[:6] == [str(exe), "-cli", "-cmd", "deconv-obj", "-ai_version", "1.0.1"]
    assert models.fetch_deconv_model(tmp_path / "log", msgs.append, run=pytest.fail, exe=exe, model=p) == p
    assert "already present" in msgs[-1]
    p.unlink()
    with pytest.raises(tools.StageError, match="did not leave a deconvolution model"):
        models.fetch_deconv_model(tmp_path / "log", msgs.append, run=lambda c, l: None, exe=exe, model=p)


def test_run_fetches_a_missing_model(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKROOM_DECONV_MODEL", str(tmp_path / "absent.onnx"))
    seen = []

    def fetch(log_path, log):
        seen.append(log_path)
        raise tools.StageError("fetch called")
    monkeypatch.setattr(models, "fetch_deconv_model", fetch)
    msgs = []
    with pytest.raises(tools.StageError, match="fetch called"):
        dc.run(tmp_path / "in.fits", tmp_path / "out.fits", 0.5, "auto", False, tmp_path / "log", msgs.append)
    assert seen == [tmp_path / "log"] and "fetch-models --deconv" in msgs[0]


def test_fetch_models_cli_deconv(monkeypatch):
    called = []
    monkeypatch.setattr(models, "fetch_denoise_model", lambda: called.append("denoise"))
    monkeypatch.setattr(models, "fetch_deconv_model", lambda: called.append("deconv"))
    assert cli.main(["fetch-models"]) == 0 and called == ["denoise"]
    assert cli.main(["fetch-models", "--deconv"]) == 0 and called == ["denoise", "denoise", "deconv"]


def test_doctor_reports_the_model(tmp_path, small_model):
    p, data = small_model
    r = doctor.check_deconv_model(p, enabled=False)
    assert r.status == OK and "not found" in r.detail and "off by default" in r.detail
    r = doctor.check_deconv_model(p, enabled=True)
    assert r.status == WARN and "fetch-models --deconv" in r.fix
    p.parent.mkdir(parents=True)
    p.write_bytes(data)
    assert doctor.check_deconv_model(p, enabled=True).status == OK


def test_doctor_reads_deconv_from_the_work_config(tmp_path):
    assert doctor._config_deconv_on(None) is False
    assert doctor._config_deconv_on(tmp_path) is False
    (tmp_path / "darkroom.toml").write_text("[deconv]\nstrength = 0.3\n")
    assert doctor._config_deconv_on(tmp_path) is True


def test_model_location(monkeypatch):
    monkeypatch.delenv("DARKROOM_DECONV_MODEL", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr("sys.platform", "linux")
    assert tools.deconv_model() == Path("~/.local/share/GraXpert/deconvolution-object-ai-models/1.0.1/model.onnx").expanduser()
    monkeypatch.setattr("sys.platform", "darwin")
    assert tools.deconv_model() == Path(
        "~/Library/Application Support/GraXpert/deconvolution-object-ai-models/1.0.1/model.onnx").expanduser()
    monkeypatch.setenv("DARKROOM_DECONV_MODEL", "/m/model.onnx")
    assert tools.deconv_model() == Path("/m/model.onnx")
