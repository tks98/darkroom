"""Sub survey and automatic dark selection, with tiny synthetic FITS files."""
import numpy as np
import pytest
from astropy.io import fits

from darkroom import cli, config, darks, tools

META = config.DEFAULT_CAMERA_META["darks"]


def make(path, **header):
    path.parent.mkdir(parents=True, exist_ok=True)
    h = fits.Header()
    for k, v in header.items():
        h[k.replace("_", "-")] = v
    fits.PrimaryHDU(np.zeros((4, 4), np.uint16), header=h).writeto(path)
    return path


def dark(lib, exp, temp, stamp="20261115-203934", name_temp=None, **header):
    name = f"MasterDark_5_{exp:.1f}s_{temp if name_temp is None else name_temp:.1f}C_{stamp}.fit"
    hdr = {"EXPTIME": float(exp), "CCD_TEMP": float(temp), "GAIN": 200} | header
    hdr = {k: v for k, v in hdr.items() if v is not None}
    p = make(lib / f"{exp:g}s" / name, **hdr)
    (p.with_suffix(".jpg")).write_bytes(b"preview")
    return p


def pick(lib, exposure=30.0, gain=200.0, temp=10.9):
    msgs = []
    return darks.select_dark(lib, META, exposure, gain, temp, log=msgs.append), "\n".join(msgs)


def test_nearest_temperature_wins(tmp_path):
    dark(tmp_path, 30, 21.5)
    want = dark(tmp_path, 30, 12.0, "20261101-200000")
    dark(tmp_path, 30, 5.0, "20261102-200000")
    got, log = pick(tmp_path)
    assert got == want
    assert "12.0 C" in log and "+1.1 C" in log and "WARNING" not in log


def test_warns_beyond_five_degrees(tmp_path):
    want = dark(tmp_path, 30, 21.5)
    got, log = pick(tmp_path)
    assert got == want and "WARNING" in log and "10.6 C" in log


def test_exposure_is_required_and_error_lists_what_exists(tmp_path):
    dark(tmp_path, 10, 11.0)
    dark(tmp_path, 60, 11.0)
    with pytest.raises(tools.StageError, match=r"30 s.*available: 10 s, 60 s"):
        pick(tmp_path)


def test_gain_must_match_when_the_dark_has_one(tmp_path):
    dark(tmp_path, 30, 11.0, GAIN=100)                      # nearer in temperature, wrong gain
    want = dark(tmp_path, 30, 20.0, "20261101-200000")
    assert pick(tmp_path)[0] == want
    with pytest.raises(tools.StageError, match="gains available: 100, 200"):
        pick(tmp_path, gain=80.0)


def test_dark_without_gain_is_accepted(tmp_path):
    want = dark(tmp_path, 30, 11.0, GAIN=None)
    assert pick(tmp_path)[0] == want


def test_tie_goes_to_the_newest(tmp_path):
    dark(tmp_path, 30, 11.0, "20260101-000000")
    want = dark(tmp_path, 30, 11.0, "20261115-203934")
    dark(tmp_path, 30, 11.0, "20260601-000000")
    assert pick(tmp_path, temp=11.0)[0] == want


def test_header_beats_filename_and_filename_is_the_fallback(tmp_path):
    a = dark(tmp_path, 30, 25.0, "20261101-000000", name_temp=11.0)     # header says 25
    b = dark(tmp_path, 30, 12.0, "20261102-000000", CCD_TEMP=None, EXPTIME=None)   # only the name says 12 C / 30 s
    info = darks.read_dark(a, META["filename"])
    assert info.temp == 25.0
    info = darks.read_dark(b, META["filename"])
    assert (info.exposure, info.temp, info.stamp) == (30.0, 12.0, "20261102-000000")
    assert pick(tmp_path)[0] == b


def test_missing_library_and_empty_library(tmp_path):
    with pytest.raises(tools.StageError, match="dark library not found"):
        pick(tmp_path / "nope")
    with pytest.raises(tools.StageError, match="no master darks"):
        pick(tmp_path)


def subs(tmp_path, n=5, **over):
    d = tmp_path / "subs"
    for i in range(n):
        hdr = dict(OBJECT="M 31", EXPTIME=30.0, CCD_TEMP=10.0 + i, GAIN=200, FILTER="IRCUT", FOCALLEN=160.0,
                   XPIXSZ=2.90000009536743, BAYERPAT="GRBG", CREATOR="ZWO Seestar S30 Pro",
                   TELESCOP="S30 Pro_deadbeef", SITELAT=12.3456, SITELONG=-65.4321) | over
        make(d / f"Light_M 31_30.0s_IRCUT_2026111{i}-204106.fit", **hdr)
        (d / f"Light_M 31_30.0s_IRCUT_2026111{i}-204106.jpg").write_bytes(b"x")
    return d


def test_survey(tmp_path):
    d = subs(tmp_path)
    files = darks.list_subs(d, "Light_*.fit")
    s = darks.survey(files)
    assert (s.count, s.object, s.exposure, s.gain, s.filters) == (5, "M 31", 30.0, 200.0, ["IRCUT"])
    assert (s.temp_median, s.temp_min, s.temp_max) == (12.0, 10.0, 14.0)
    assert (s.focal, round(s.pixel, 2), s.bayer, s.total_seconds) == (160.0, 2.9, "GRBG", 150.0)
    assert not any("deadbeef" in str(v) or "12.3456" in str(v) for v in vars(s).values())
    assert darks.format_duration(404 * 30) == "3 h 22 min" and darks.format_duration(150) == "2 min"


def test_detect_camera(tmp_path):
    d = subs(tmp_path, n=1)
    tables = {n: config.load_camera(n)[1]["camera"] for n in config.preset_names("cameras")}
    assert darks.detect_camera(darks.list_subs(d)[0], tables) == "seestar-s30-pro"
    other = make(tmp_path / "o" / "x.fit", CREATOR="Some Other Camera")
    assert darks.detect_camera(other, tables) is None


def test_init_writes_config_without_identifying_values(tmp_path, capsys):
    d = subs(tmp_path)
    lib = tmp_path / "lib"
    want = dark(lib, 30, 13.0)
    work = tmp_path / "work"
    assert cli.main(["init", "--work", str(work), "--subs", str(d), "--dark-library", str(lib)]) == 0
    out = capsys.readouterr().out
    text = (work / "darkroom.toml").read_text()
    for blob in (out, text):
        assert "deadbeef" not in blob and "12.3456" not in blob and "65.4321" not in blob
    assert "seestar-s30-pro (detected" in out and "5 frames" in out and "M 31" in out
    assert str(want) in out
    s = config.resolve(work)
    t = s["target"]
    assert (t["name"], t["subs"], t["dark"], t["dark_library"], t["camera"], t["look"]) == (
        "M 31", str(d.resolve()), "auto", str(lib.resolve()), "seestar-s30-pro", "galaxy")
    assert (s["color"]["filter"], s["color"]["focal"], s["color"]["pixel"]) == ("UV/IR Block", 160, 2.9)
    table = cli._stack_table(s)
    assert table["dark"] == want.resolve() and table["sub_glob"] == "Light_*.fit"
    # refuses to overwrite, unless forced
    assert cli.main(["init", "--work", str(work), "--subs", str(d), "--no-dark"]) == 1
    assert "already exists" in capsys.readouterr().err
    assert cli.main(["init", "--work", str(work), "--subs", str(d), "--no-dark", "--name", "Andromeda",
                     "--force"]) == 0
    s = config.resolve(work)
    assert (s["target"]["dark"], s["target"]["name"]) == ("none", "Andromeda")
    assert cli._stack_table(s)["dark"] is None


def test_init_maps_lp_filter_and_notes_unknown_camera(tmp_path, capsys):
    d = subs(tmp_path, FILTER="LP")
    assert cli.main(["init", "--work", str(tmp_path / "w1"), "--subs", str(d), "--no-dark"]) == 0
    assert config.resolve(tmp_path / "w1")["color"]["filter"] == "ZWO Seestar LP"
    capsys.readouterr()
    e = tmp_path / "other"
    make(e / "frame_001.fit", CREATOR="Other", EXPTIME=10.0)
    assert cli.main(["init", "--work", str(tmp_path / "w2"), "--subs", str(e), "--no-dark"]) == 0
    assert "no camera preset matches" in capsys.readouterr().out
    assert config.resolve(tmp_path / "w2")["target"]["camera"] is None


def test_stack_table_errors(tmp_path):
    (tmp_path / "darkroom.toml").write_text(f'[target]\nsubs = "{tmp_path}"\ndark = "auto"\n')
    with pytest.raises(tools.StageError, match="dark library"):
        cli._stack_table(config.resolve(tmp_path))
