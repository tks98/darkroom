"""[stretch] method (asinh or ghs), the ghs settings and balance (Siril is not run)."""
import json

import numpy as np
import pytest
from astropy.io import fits

from darkroom import cli, config, siril, stages


def write(tmp_path, text):
    (tmp_path / "darkroom.toml").write_text(text)
    return tmp_path


def settings(tmp_path, *argv):
    a = cli.build_parser().parse_args(["stretch", "--work", str(tmp_path), *argv])
    return cli._settings(a)


# --- settings

def test_defaults_are_asinh_and_no_balance(tmp_path):
    st = config.resolve(tmp_path)["stretch"]
    assert (st["method"], st["balance"]) == ("asinh", "1,1,1")
    assert (st["ghs_d"], st["ghs_b"], st["ghs_hp"], st["ghs_sp"]) == (4.0, 12.0, 0.9, "auto")


def test_config_values(tmp_path):
    write(tmp_path, '[stretch]\nmethod = "GHS"\nghs_d = 5\nghs_b = -2\nghs_hp = 0.8\nghs_sp = 0.0023\n'
                    'balance = "0.92, 0.90, 1.18"\n')
    st = config.resolve(tmp_path)["stretch"]
    assert st["method"] == "ghs" and st["ghs_sp"] == 0.0023 and st["ghs_b"] == -2
    assert config.parse_balance(st["balance"]) == (0.92, 0.9, 1.18)
    write(tmp_path, '[stretch]\nghs_sp = "Auto"\n')
    assert config.resolve(tmp_path)["stretch"]["ghs_sp"] == "auto"


@pytest.mark.parametrize("text,match", [
    ('[stretch]\nmethod = "linear"\n', r'\[stretch\] method should be "asinh" or "ghs"'),
    ("[stretch]\nghs_d = 11\n", r"\[stretch\] ghs_d"),
    ("[stretch]\nghs_d = -1\n", r"\[stretch\] ghs_d"),
    ("[stretch]\nghs_b = 16\n", r"\[stretch\] ghs_b"),
    ("[stretch]\nghs_b = -6\n", r"\[stretch\] ghs_b"),
    ("[stretch]\nghs_hp = 1.5\n", r"\[stretch\] ghs_hp"),
    ("[stretch]\nghs_sp = 1.2\n", r"\[stretch\] ghs_sp"),
    ("[stretch]\nghs_sp = 0.95\n", r"\[stretch\] ghs_sp .*ghs_hp"),       # above HP (0.9)
    ('[stretch]\nghs_sp = "sky"\n', r"\[stretch\] ghs_sp should be a number or \"auto\""),
    ('[stretch]\nbalance = "1,1"\n', r"\[stretch\] balance should be three numbers"),
    ('[stretch]\nbalance = "1,1,x"\n', r"\[stretch\] balance"),
    ('[stretch]\nbalance = "0.4,1,1"\n', r"\[stretch\] balance"),
    ('[stretch]\nbalance = "1,1,2.5"\n', r"\[stretch\] balance"),
    ('[stretch]\nbalance = "1,-1,1"\n', r"\[stretch\] balance"),
    ("[stretch]\nbalance = 1\n", r"\[stretch\] balance should be str"),
])
def test_bad_values_are_config_errors(tmp_path, text, match):
    write(tmp_path, text)
    with pytest.raises(config.ConfigError, match=match):
        config.resolve(tmp_path)


def test_init_writes_the_new_keys(tmp_path):
    text = config.render_config({}, config.resolve(tmp_path), ["test"])
    for key in ("method", "ghs_d", "ghs_b", "ghs_hp", "ghs_sp", "balance"):
        assert f"# {key} = " in text
    assert '# method = "asinh"' in text and '# balance = "1,1,1"' in text


# --- flags

def test_flags(tmp_path):
    a = cli.build_parser().parse_args(["stretch", "--work", "w"])
    assert (a.method, a.ghs_d, a.ghs_b, a.ghs_hp, a.ghs_sp, a.balance) == ("asinh", 4.0, 12.0, 0.9, "auto", "1,1,1")
    a = cli.build_parser().parse_args(["stretch", "--work", str(tmp_path), "--method", "ghs", "--ghs-d", "5",
                                       "--ghs-b", "10", "--ghs-hp", "0.8", "--ghs-sp", "0.002",
                                       "--balance", "0.92,0.90,1.18"])
    assert cli._overrides(a) == {("stretch", "method"): "ghs", ("stretch", "ghs_d"): 5.0,
                                 ("stretch", "ghs_b"): 10.0, ("stretch", "ghs_hp"): 0.8,
                                 ("stretch", "ghs_sp"): 0.002, ("stretch", "balance"): "0.92,0.90,1.18"}
    assert cli.build_parser().parse_args(["stretch", "--work", "w", "--ghs-sp", "AUTO"]).ghs_sp == "auto"
    for bad in (["--method", "linear"], ["--ghs-sp", "sky"], ["--ghs-d", "x"]):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["stretch", "--work", "w", *bad])
    with pytest.raises(config.ConfigError, match=r"\[stretch\] balance"):
        settings(tmp_path, "--balance", "3,1,1")
    with pytest.raises(config.ConfigError, match=r"\[stretch\] ghs_b"):
        settings(tmp_path, "--ghs-b", "20")


def test_stretch_command_passes_the_settings(tmp_path, monkeypatch):
    got = {}
    monkeypatch.setattr(stages, "stretch", lambda w, *a, **kw: got.update(kw))
    write(tmp_path, '[stretch]\nmethod = "ghs"\nbalance = "0.92,0.90,1.18"\n')
    assert cli.main(["stretch", "--work", str(tmp_path), "--ghs-sp", "0.003"]) == 0
    assert got == {"src": "denoised.fits", "method": "ghs", "ghs_d": 4.0, "ghs_b": 12.0, "ghs_hp": 0.9,
                   "ghs_sp": 0.003, "balance": "0.92,0.90,1.18"}


# --- the script

TPL_BASE = {"WORK": "/w", "ASINH": "25", "SHADOWS": "-2.5", "TARGET_BG": "0.1", "SAT": "0.35",
            "INPUT": "deconvolved.fits"}


def body(text):
    return [l for l in text.splitlines() if l and not l.startswith("#")]


def test_template_asinh_is_unchanged():
    tpl = (siril.template_dir() / "stretch.ssf").read_text()
    assert body(siril.render(tpl, TPL_BASE))[3:7] == [
        "load deconvolved.fits", "asinh -human 25", "autostretch -linked -2.5 0.1", "satu 0.35 1.5"]


def test_template_ghs_with_balance():
    tpl = (siril.template_dir() / "stretch.ssf").read_text()
    out = siril.render(tpl, TPL_BASE | {"BALANCE": "0.92 0 0 0 0.9 0 0 0 1.18", "GHS": "1", "GHS_D": "4",
                                        "GHS_B": "12", "GHS_SP": "0.0023", "GHS_HP": "0.9"})
    assert body(out)[3:8] == [          # the script that made the chosen two-night M31
        "load deconvolved.fits", "ccm 0.92 0 0 0 0.9 0 0 0 1.18", "ght -D=4 -B=12 -SP=0.0023 -HP=0.9 -human",
        "autostretch -linked -2.5 0.1", "satu 0.35 1.5"]
    assert "asinh" not in "\n".join(body(out))


def fits_image(path, rgb):
    fits.PrimaryHDU(np.asarray(rgb, dtype=np.float32)).writeto(path, overwrite=True)


@pytest.fixture
def scripts(tmp_path, monkeypatch):
    """stages.stretch with run_script faked: returns the work dir and the scripts it ran."""
    ran = []

    def run_script(script, log):
        ran.append(script.read_text())
        log.write_text("siril says hello\n")
        for n in (stages.STRETCHED_FIT, stages.STRETCHED_TIF):
            (tmp_path / n).write_bytes(b"x")
    monkeypatch.setattr(siril, "run_script", run_script)
    rgb = np.zeros((3, 10, 10), np.float32)
    rgb[:, 2:, :] = [[[0.002]], [[0.004]], [[0.003]]]      # rows 0-1: no data (0 in every channel)
    rgb[:, 9, 9] = 0.9                                      # a star does not move the median
    fits_image(tmp_path / stages.DENOISED, rgb)
    return tmp_path, ran


def test_stage_asinh_default_has_no_ccm(scripts):
    work, ran = scripts
    stages.stretch(work)
    lines = body(ran[0])
    assert "asinh -human 25" in lines
    assert not any(l.startswith(("ccm", "ght")) for l in lines)


def test_stage_ghs_auto_sp_and_balance(scripts, capsys):
    work, ran = scripts
    stages.stretch(work, method="ghs", balance="1.5,1,0.5")
    lines = body(ran[0])
    sp = (1.5 * 0.002 + 0.004 + 0.5 * 0.003) / 3                 # balanced mean of the sky pixels
    assert "ccm 1.5 0 0 0 1 0 0 0 0.5" in lines
    (ght,) = [l for l in lines if l.startswith("ght")]
    assert ght == f"ght -D=4 -B=12 -SP={siril.fmt_num(sp)} -HP=0.9 -human"
    assert not any(l.startswith("asinh") for l in lines)
    assert lines.index("ccm 1.5 0 0 0 1 0 0 0 0.5") < lines.index(ght)
    assert f"ghs SP = {siril.fmt_num(sp)} (auto" in capsys.readouterr().out
    log = (work / "logs" / "stretch.log").read_text()
    assert "siril says hello" in log and f"ghs SP = {siril.fmt_num(sp)}" in log


def test_stage_ghs_given_sp(scripts):
    work, ran = scripts
    stages.stretch(work, method="ghs", ghs_d=5, ghs_b=10, ghs_hp=0.8, ghs_sp=0.0023)
    assert "ght -D=5 -B=10 -SP=0.0023 -HP=0.8 -human" in body(ran[0])


def test_sky_level(tmp_path):
    rgb = np.zeros((3, 4, 5), np.float32)
    rgb[:, :3] = 0.01
    rgb[0, 3] = 0.0                    # one channel 0 but not the others: still data
    rgb[1:, 3] = 0.04
    fits_image(tmp_path / "a.fits", rgb)
    # 15 pixels at 0.01, 5 at (0 + 0.04 + 0.04) / 3
    assert stages.sky_level(tmp_path / "a.fits") == pytest.approx(0.01)
    assert stages.sky_level(tmp_path / "a.fits", (2, 2, 2)) == pytest.approx(0.02)
    fits_image(tmp_path / "z.fits", np.zeros((3, 4, 5)))
    with pytest.raises(stages.tools.StageError, match="every pixel is 0"):
        stages.sky_level(tmp_path / "z.fits")


def test_stage_errors(scripts):
    work, _ = scripts
    with pytest.raises(stages.tools.StageError, match=r"\[stretch\] balance"):
        stages.stretch(work, balance="1,1")
    with pytest.raises(stages.tools.StageError, match=r"\[stretch\] method"):
        stages.stretch(work, method="linear")
    with pytest.raises(stages.tools.StageError, match="ghs_hp"):
        stages.stretch(work, method="ghs", ghs_hp=0.001)          # sky (0.003) above HP


# --- records

def test_records_only_carry_the_new_keys_when_in_use(tmp_path):
    st = config.resolve(tmp_path)["stretch"]
    plain = {"asinh": 25.0, "shadows": -2.5, "target_bg": 0.1, "sat": 0.35}
    assert stages.result_params("stretch", st) == plain
    assert stages.result_params("stretch", st | {"balance": "1.0, 1, 1.00"}) == plain
    assert stages.result_params("stretch", st | {"balance": "0.92,0.90,1.18"}) == plain | {"balance": [0.92, 0.9, 1.18]}
    ghs = stages.result_params("stretch", st | {"method": "ghs"})
    assert ghs == {"shadows": -2.5, "target_bg": 0.1, "sat": 0.35, "method": "ghs",
                   "ghs_d": 4.0, "ghs_b": 12.0, "ghs_hp": 0.9, "ghs_sp": "auto"}


def test_a_record_made_before_these_keys_is_up_to_date(tmp_path):
    work = tmp_path
    (work / stages.DENOISED).write_bytes(b"x")
    for p in stages.outputs(work, "stretch"):
        p.write_bytes(b"x")
    old = {"stage": "stretch", "darkroom": "0.1", "algorithm": 1,
           "params": {"asinh": 25.0, "shadows": -2.5, "target_bg": 0.1, "sat": 0.35}}
    stages.params_file(work, "stretch").parent.mkdir(exist_ok=True)
    stages.params_file(work, "stretch").write_text(json.dumps(old))
    import os
    os.utime(work / stages.DENOISED, (1000, 1000))
    s = config.resolve(work)
    assert stages.up_to_date(work, "stretch", stages.stretch_params(s["stretch"], s["deconv"])) == (True, "")
    ok, why = stages.up_to_date(work, "stretch", stages.stretch_params(s["stretch"] | {"method": "ghs"}, s["deconv"]))
    assert not ok and "method" in why
    ok, why = stages.up_to_date(work, "stretch",
                                stages.stretch_params(s["stretch"] | {"balance": "0.92,0.9,1.18"}, s["deconv"]))
    assert not ok and why == "changed: balance"
