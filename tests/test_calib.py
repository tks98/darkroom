"""Calibration frames (bias, darks, flats): settings, scripts, master building and checks.
Synthetic FITS files and a fake Siril; no external tools are run."""
import os
import re
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from darkroom import calib, cli, config, darks, siril, stages, tools

LIGHT = dict(EXPTIME=300.0, GAIN=120, OFFSET=30, CCD_TEMP=0.0, FILTER="HaO3", BAYERPAT="RGGB",
             INSTRUME="ZWO ASI294MC Pro", FOCALLEN=299.0, XPIXSZ=4.63, DATE_OBS="2025-12-14T13:09:58")


def frames(folder, prefix, n, shape=(4, 6), **header):
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        h = fits.Header()
        for k, v in (LIGHT | header).items():
            if v is not None:
                h[k.replace("_", "-")] = v
        fits.PrimaryHDU(np.zeros(shape, np.uint16), header=h).writeto(folder / f"{prefix}_{i + 1:04d}.fit")
    return folder


@pytest.fixture
def data(tmp_path):
    root = tmp_path / "data"
    return {
        "subs": frames(root / "lights", "Light", 3),
        "darks": frames(root / "darks", "Dark", 3, FILTER=None, CCD_TEMP=-0.3),
        "biases": frames(root / "biases", "Bias", 4, EXPTIME=0.001, FILTER=None, CCD_TEMP=-10.0),
        "flats": frames(root / "flats", "Flat", 2, EXPTIME=0.07, CCD_TEMP=9.8),
    }


class FakeSiril:
    """Records the scripts and writes the files their -out= arguments name."""

    def __init__(self):
        self.scripts = []

    def __call__(self, script, log):
        text = script.read_text()
        self.scripts.append(text)
        log.write_text("log: 3 images have been stacked.\n")
        for out in re.findall(r'"-out=([^"]+)"', text):
            if "/masters/master_" in out or out.endswith("/stack"):
                Path(out + ".fit").write_bytes(b"master")

    def last(self):
        return [l for l in self.scripts[-1].splitlines() if l and not l.startswith("#")]


@pytest.fixture
def fake_siril(monkeypatch):
    f = FakeSiril()
    monkeypatch.setattr(siril, "run_script", f)
    return f


def lights(data):
    return darks.survey(darks.list_subs(data["subs"]))


# --- settings

def test_settings_defaults_paths_and_flags(tmp_path):
    s = config.resolve(tmp_path)
    assert (s["target"]["bias"], s["target"]["flats"], s["color"]["narrowband"]) == ("none", "none", "none")
    (tmp_path / "darkroom.toml").write_text('[target]\ndark = "raw/darks"\nbias = "None"\nflats = "cal/flats"\n'
                                            '[color]\nnarrowband = "656.28/12, 500.7/12, 500.7/12"\n')
    s = config.resolve(tmp_path)
    assert s["target"]["dark"] == str(tmp_path / "raw/darks") and s["target"]["bias"] == "none"
    assert s["target"]["flats"] == str(tmp_path / "cal/flats")
    a = cli.build_parser().parse_args(["stack", "--work", str(tmp_path), "--bias", "/b", "--flats", "none",
                                       "--dark", "/raw darks"])
    s = cli._settings(a)
    assert (s["target"]["bias"], s["target"]["flats"], s["target"]["dark"]) == ("/b", "none", "/raw darks")
    a = cli.build_parser().parse_args(["color", "--work", str(tmp_path), "--narrowband", "none"])
    assert cli._settings(a)["color"]["narrowband"] == "none"
    a = cli.build_parser().parse_args(["run", "--work", str(tmp_path), "--bias", "/b2"])
    assert cli._settings(a)["target"]["bias"] == "/b2"


@pytest.mark.parametrize("text, match", [
    ('[target]\nbias = "auto"\n', '"auto" is only for dark'),
    ('[color]\nnarrowband = "656.28/12,500.7/12"\n', "three"),
    ('[color]\nnarrowband = "656/x,500/7,500/7"\n', "three"),
    ('[color]\nnarrowband = "6563/12,500.7/12,500.7/12"\n', "300 to 1100 nm"),
])
def test_setting_errors(tmp_path, text, match):
    (tmp_path / "darkroom.toml").write_text(text)
    with pytest.raises(config.ConfigError, match=match):
        config.resolve(tmp_path)


# --- scripts

def stack_cal(dark="", bias="", flat=""):
    t = (siril.template_dir() / "stack.ssf").read_text()
    text = siril.render(t, {"SUBS": "/s", "PROCESS": "/w/process", "STACK": "/w/stack", "DARK": dark,
                            "BIAS": bias, "FLAT": flat})
    return [l for l in text.splitlines() if l.startswith("calibrate")]


@pytest.mark.parametrize("dark, bias, flat, want", [
    ("", "", "", "calibrate light -cfa -equalize_cfa -debayer"),
    ("/d.fit", "", "", 'calibrate light "-dark=/d.fit" -cc=dark 3 3 -cfa -equalize_cfa -debayer'),
    ("/d.fit", "", "/f.fit", 'calibrate light "-dark=/d.fit" "-flat=/f.fit" -cc=dark 3 3 -cfa -equalize_cfa -debayer'),
    ("", "/b.fit", "", 'calibrate light "-bias=/b.fit" -cfa -equalize_cfa -debayer'),
    ("", "/b.fit", "/f.fit", 'calibrate light "-bias=/b.fit" "-flat=/f.fit" -cfa -equalize_cfa -debayer'),
    ("", "", "/f.fit", 'calibrate light "-flat=/f.fit" -cfa -equalize_cfa -debayer'),
])
def test_one_calibrate_line_per_combination(dark, bias, flat, want):
    assert stack_cal(dark, bias, flat) == [want]
    t = (siril.template_dir() / "mosaic_solve.ssf").read_text()
    base = {"PROCESS": "/w/process", "MAG": "13", "FOCAL": "150", "PIXEL": "2.9", "FRAMES": ""}
    first = siril.render(t, base | {"SUBS": "/s", "DARK": dark, "BIAS": bias, "FLAT": flat, "FORCE": "-force "})
    assert [l for l in first.splitlines() if l.startswith("calibrate")] == [want]
    again = siril.render(t, base | {"SUBS": "", "DARK": "", "FRAMES": "5"})
    assert not any(l.startswith("calibrate") for l in again.splitlines())


def masters_script(**values):
    t = (siril.template_dir() / "masters.ssf").read_text()
    return [l for l in siril.render(t, {"MASTERS": "/w/masters"} | values).splitlines()
            if l and not l.startswith("#")]


def test_masters_script():
    assert masters_script() == ["requires 1.4.0", "setext fit", "set32bits", "close"]
    c = masters_script(BIAS_FRAMES="/w/masters/bias_frames", BIAS_SEQ="/w/masters/bias_seq",
                       DARK_FRAMES="/w/masters/dark_frames", DARK_SEQ="/w/masters/dark_seq",
                       FLAT_FRAMES="/w/masters/flats_frames", FLAT_SEQ="/w/masters/flats_seq",
                       FLAT_BIAS="/w/masters/master_bias.fit")
    assert c[3:] == [
        'cd "/w/masters/bias_frames"', 'link bias "-out=/w/masters/bias_seq"', 'cd "/w/masters/bias_seq"',
        'stack bias rej w 3 3 -nonorm "-out=/w/masters/master_bias"',
        'cd "/w/masters/dark_frames"', 'link dark "-out=/w/masters/dark_seq"', 'cd "/w/masters/dark_seq"',
        'stack dark rej w 3 3 -nonorm "-out=/w/masters/master_dark"',
        'cd "/w/masters/flats_frames"', 'link flat "-out=/w/masters/flats_seq"', 'cd "/w/masters/flats_seq"',
        'calibrate flat "-bias=/w/masters/master_bias.fit"',
        'stack pp_flat rej w 3 3 -norm=mul "-out=/w/masters/master_flat"', "close"]
    c = masters_script(FLAT_FRAMES="/f", FLAT_SEQ="/q")             # flats without a bias
    assert not any(l.startswith("calibrate") for l in c)
    assert 'stack flat rej w 3 3 -norm=mul "-out=/w/masters/master_flat"' in c


def test_narrowband_colour_script_and_record():
    assert stages.narrowband_args("none") == ""
    nb = stages.narrowband_args("656.28/12,500.7/7,500.7/7.5")
    assert nb == "-rwl=656.28 -rbw=12 -gwl=500.7 -gbw=7 -bwl=500.7 -bbw=7.5"
    t = (siril.template_dir() / "color.ssf").read_text()
    v = {"WORK": "/w", "FOCAL": "299", "PIXEL": "4.63", "CATALOG": "gaia", "SENSOR": "Sony IMX294",
         "FILTER": "UV/IR Block"}
    spcc = [l for l in siril.render(t, v | {"NARROWBAND": nb}).splitlines() if l.startswith("spcc")]
    assert spcc == [f'spcc "-oscsensor=Sony IMX294" -narrowband {nb} "-whiteref=Average Spiral Galaxy"']
    spcc = [l for l in siril.render(t, v | {"NARROWBAND": ""}).splitlines() if l.startswith("spcc")]
    assert spcc == ['spcc "-oscsensor=Sony IMX294" "-oscfilter=UV/IR Block" "-whiteref=Average Spiral Galaxy"']
    table = config.resolve(Path("/nonexistent"))["color"]
    assert "narrowband" not in stages.result_params("color", table)     # old work dirs stay up to date
    assert stages.result_params("color", table | {"narrowband": "656.28/12,500.7/12,500.7/12"})[
        "narrowband"] == "656.28/12,500.7/12,500.7/12"


# --- checks

def test_checks_errors_and_warnings(tmp_path, data):
    lt = lights(data)
    assert calib.check("dark", lt, calib.survey_of(data["darks"])) == ([], [])
    assert calib.check("bias", lt, calib.survey_of(data["biases"])) == ([], [])
    assert calib.check("flats", lt, calib.survey_of(data["flats"])) == ([], [])
    bad = frames(tmp_path / "bad", "Dark", 2, shape=(6, 4), BAYERPAT="GRBG", GAIN=200, OFFSET=8,
                 EXPTIME=120.0, CCD_TEMP=12.0)
    errors, warnings = calib.check("dark", lt, calib.survey_of(bad))
    assert len(errors) == 2 and "4x6 pixels and the lights 6x4" in errors[0] and "Bayer pattern GRBG" in errors[1]
    text = "\n".join(warnings)
    assert "gain 200" in text and "offset 8" in text and "120 s and the lights 300 s" in text
    assert "12.0 C from the lights" in text and "more than 5 C" in text
    # exposure and temperature only matter for darks; the filter only for flats
    errors, warnings = calib.check("bias", lt, calib.survey_of(bad))
    assert len(warnings) == 2 and not any("exposure" in w or " C " in w for w in warnings)
    late = frames(tmp_path / "late", "Flat", 1, DATE_OBS="2026-01-20T19:25:07")
    errors, warnings = calib.check("flats", lt, calib.survey_of(late))
    assert errors == [] and len(warnings) == 1 and "37 days from the lights" in warnings[0]
    other = frames(tmp_path / "lp", "Flat", 1, FILTER="L-Pro")
    assert "filter L-Pro" in calib.check("flats", lt, calib.survey_of(other))[1][0]
    assert calib.check("dark", lt, calib.survey_of(other))[1] == []


def test_mismatched_frames_stop_the_stack(tmp_path, data, fake_siril):
    bad = frames(tmp_path / "bad", "Flat", 2, shape=(6, 4))
    with pytest.raises(tools.StageError, match="flats are 4x6 pixels"):
        calib.prepare(tmp_path / "w", lights(data), flats=bad)
    with pytest.raises(tools.StageError, match="bias frames not found"):
        calib.prepare(tmp_path / "w", lights(data), bias=tmp_path / "nope")
    assert not fake_siril.scripts


# --- building masters and skipping them

def test_prepare_builds_masters_once(tmp_path, data, fake_siril):
    work, msgs = tmp_path / "w", []
    cal = calib.prepare(work, lights(data), dark=data["darks"], bias=data["biases"], flats=data["flats"],
                        log=msgs.append)
    m = work / "masters"
    assert cal == calib.Calibration(dark=m / "master_dark.fit", bias=None, flats=m / "master_flat.fit")
    c = fake_siril.last()
    assert c.index('stack bias rej w 3 3 -nonorm "-out=%s/master_bias"' % m) < c.index(
        'calibrate flat "-bias=%s/master_bias.fit"' % m)
    assert any(l.startswith("link dark") for l in c)
    # only the masters are left: the links and sequences Siril worked on are gone
    assert sorted(p.name for p in m.iterdir()) == ["master_bias.fit", "master_dark.fit", "master_flat.fit"]
    assert len(fake_siril.scripts) == 1
    # nothing changed: nothing is rebuilt
    msgs.clear()
    assert calib.prepare(work, lights(data), dark=data["darks"], bias=data["biases"], flats=data["flats"],
                         log=msgs.append) == cal
    assert len(fake_siril.scripts) == 1 and sum("up to date" in m_ for m_ in msgs) == 3
    # a new bias frame rebuilds the master bias and the flat (calibrated with it), not the dark
    frames(tmp_path / "more", "Bias", 1, EXPTIME=0.001, FILTER=None)
    os.replace(tmp_path / "more" / "Bias_0001.fit", data["biases"] / "Bias_0099.fit")
    calib.prepare(work, lights(data), dark=data["darks"], bias=data["biases"], flats=data["flats"])
    c = fake_siril.last()
    assert any(l.startswith("link bias") for l in c) and any(l.startswith("link flat") for l in c)
    assert not any(l.startswith("link dark") for l in c)
    # a master bias given as a file, newer than the flat, rebuilds the flat
    mb = tmp_path / "mb.fit"
    frames(tmp_path / "mbdir", "MB", 1, EXPTIME=0.001)
    os.replace(tmp_path / "mbdir" / "MB_0001.fit", mb)
    cal2 = calib.prepare(work, lights(data), bias=mb, flats=data["flats"])
    assert cal2 == calib.Calibration(bias=mb, flats=m / "master_flat.fit")
    assert f'calibrate flat "-bias={mb}"' in fake_siril.last()
    n = len(fake_siril.scripts)
    calib.prepare(work, lights(data), bias=mb, flats=data["flats"])
    assert len(fake_siril.scripts) == n


def test_master_files_are_used_as_they_are(tmp_path, data, fake_siril):
    d = data["darks"] / "Dark_0001.fit"
    f = data["flats"] / "Flat_0001.fit"
    msgs = []
    cal = calib.prepare(tmp_path / "w", lights(data), dark=d, bias=data["biases"] / "Bias_0001.fit", flats=f,
                        log=msgs.append)
    assert cal == calib.Calibration(dark=d, bias=None, flats=f) and not fake_siril.scripts
    assert any("bias: not used" in m for m in msgs)
    cal = calib.prepare(None, lights(data), bias=data["biases"] / "Bias_0001.fit")
    assert cal.bias == data["biases"] / "Bias_0001.fit" and cal.dark is None
    assert calib.script_values(cal) == {"DARK": "", "BIAS": str(cal.bias), "FLAT": ""}


def test_flats_without_bias_warn(tmp_path, data, fake_siril):
    msgs = []
    calib.prepare(tmp_path / "w", lights(data), flats=data["flats"], log=msgs.append)
    assert any("without a bias" in m for m in msgs)
    assert 'stack flat rej w 3 3 -norm=mul' in fake_siril.scripts[-1]


def test_unplugged_folder_keeps_its_master(tmp_path, data, fake_siril):
    work = tmp_path / "w"
    calib.prepare(work, lights(data), dark=data["darks"])
    data["darks"].rename(tmp_path / "elsewhere")
    msgs = []
    cal = calib.prepare(work, lights(data), dark=data["darks"], log=msgs.append)
    assert cal.dark == work / "masters" / "master_dark.fit" and "not available" in msgs[0]
    assert len(fake_siril.scripts) == 1


def test_failed_build_leaves_no_master(tmp_path, data, monkeypatch):
    def fail(script, log):
        raise tools.StageError("Siril failed")
    monkeypatch.setattr(siril, "run_script", fail)
    with pytest.raises(tools.StageError):
        calib.prepare(tmp_path / "w", lights(data), dark=data["darks"])
    assert not (tmp_path / "w" / "masters" / "master_dark.fit").exists()
    assert stages.recorded_params(tmp_path / "w", "master_dark") is None


# --- the stack stage

def test_stack_table_record_and_script(tmp_path, data, fake_siril):
    work = tmp_path / "w"
    work.mkdir()
    (work / "darkroom.toml").write_text(f'[target]\nsubs = "{data["subs"]}"\ndark = "{data["darks"]}"\n'
                                        f'bias = "{data["biases"]}"\nflats = "{data["flats"]}"\n')
    s = config.resolve(work)
    table = cli._stack_table(s, work)
    m = (work / "masters").resolve()
    assert (table["dark"], table["bias"], table["flats"]) == (m / "master_dark.fit", None, m / "master_flat.fit")
    rec = stages.result_params("stack", table)
    assert rec["dark"] == str(m / "master_dark.fit") and rec["flats"] == str(m / "master_flat.fit")
    assert "bias" not in rec
    assert m / "master_flat.fit" in stages.inputs(work, "stack", rec)
    cli._call("stack", work, s, table)
    cal = [l for l in fake_siril.scripts[-1].splitlines() if l.startswith("calibrate")]
    assert cal == [f'calibrate light "-dark={m}/master_dark.fit" "-flat={m}/master_flat.fit" -cc=dark 3 3 '
                   "-cfa -equalize_cfa -debayer"]
    # without any calibration frame the record is what it always was (plus the selection of subs,
    # which an older record lacks without counting as a change)
    old = stages.result_params("stack", {**table, "dark": None, "bias": None, "flats": None})
    assert set(old) == {"subs", "dark", "framing", "keep_fwhm", "keep_round", "subs_count", "subs_digest"}


def test_run_reruns_the_stack_when_a_master_is_rebuilt(tmp_path, data, fake_siril, monkeypatch, capsys):
    work = tmp_path / "w"
    work.mkdir()
    (work / "darkroom.toml").write_text(f'[target]\nsubs = "{data["subs"]}"\ndark = "none"\n'
                                        f'bias = "{data["biases"]}"\n')
    monkeypatch.setattr(stages, "frames_kept", lambda log: None)
    assert cli.main(["run", "--work", str(work), "--to", "stack"]) == 0
    n = len(fake_siril.scripts)
    assert n == 2 and "calibrate light \"-bias=" in fake_siril.scripts[-1]
    assert cli.main(["run", "--work", str(work), "--to", "stack"]) == 0
    assert len(fake_siril.scripts) == n and "stack: up to date" in capsys.readouterr().out
    os.utime(data["biases"] / "Bias_0001.fit", None)          # a newer frame: rebuild the bias, restack
    master = work / "masters" / "master_bias.fit"
    os.utime(master, (1, 1))
    assert cli.main(["run", "--work", str(work), "--to", "stack"]) == 0
    assert len(fake_siril.scripts) == n + 2


# --- init and camera detection

def test_init_with_calibration_folders(tmp_path, data, capsys):
    work = tmp_path / "w"
    assert cli.main(["init", "--work", str(work), "--subs", str(data["subs"]), "--dark", str(data["darks"]),
                     "--bias", str(data["biases"]), "--flats", str(data["flats"])]) == 0
    out = capsys.readouterr().out
    assert "zwo-asi294mc-pro (detected" in out
    assert re.search(r"dark +3 frames in .*darks: 300 s, gain 120, offset 30, -0.3 C \(-0.3 C from the lights\), "
                     r"2025-12-14", out)
    assert re.search(r"bias +4 frames in .*biases: 1 ms", out)
    assert re.search(r"flats +2 frames in .*flats: 70 ms, gain 120, offset 30, filter HaO3", out)
    assert "narrowband" in out and "differs from the camera preset" not in out
    s = config.resolve(work)
    assert s["target"]["dark"] == str(data["darks"].resolve())
    assert s["target"]["flats"] == str(data["flats"].resolve())
    assert (s["color"]["sensor"], s["color"]["focal"], s["color"]["pixel"]) == ("Sony IMX294", 299, 4.63)
    other = frames(tmp_path / "uvir", "Flat", 1, FILTER="UV/IR")
    assert cli.main(["init", "--work", str(tmp_path / "w2"), "--subs", str(data["subs"]), "--no-dark",
                     "--flats", str(other)]) == 0
    out = capsys.readouterr().out
    assert "filter UV/IR and the lights through HaO3" in out and "without a bias" not in out
    assert cli.main(["init", "--work", str(tmp_path / "w3"), "--subs", str(data["subs"]), "--no-dark",
                     "--flats", str(data["flats"])]) == 0
    assert "no bias" in capsys.readouterr().out


def test_detect_asi294(tmp_path):
    tables = {n: config.load_camera(n)[1]["camera"] for n in config.preset_names("cameras")}
    pro = frames(tmp_path / "pro", "Light", 1)
    plain = frames(tmp_path / "plain", "Light", 1, INSTRUME="ZWO ASI294MC")
    seestar = frames(tmp_path / "s", "Light", 1, INSTRUME=None, CREATOR="ZWO Seestar S30 Pro")
    assert darks.detect_camera(pro / "Light_0001.fit", tables) == "zwo-asi294mc-pro"
    assert darks.detect_camera(plain / "Light_0001.fit", tables) is None
    assert darks.detect_camera(seestar / "Light_0001.fit", tables) == "seestar-s30-pro"
    _, meta, _ = config.load_camera("zwo-asi294mc-pro")
    assert (meta["camera"]["bayer"], meta["camera"]["sub_glob"]) == ("RGGB", "Light_*.fit")
