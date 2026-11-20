"""Siril template rendering: absolute quoted paths, no leftover placeholders."""
import re
from pathlib import Path

import pytest

from darkroom import siril

WORK = Path("/data/work dir")          # deliberately contains a space
VALUES = {
    "stack.ssf": {"SUBS": "/data/M 31_sub", "PROCESS": str(WORK / "process"),
                  "DARK": "/data/masters/dark.fit", "FLAT": "/data/masters/flat.fit", "STACK": str(WORK / "stack")},
    "masters.ssf": {"MASTERS": str(WORK / "masters"), "BIAS_FRAMES": str(WORK / "masters/bias_frames"),
                    "BIAS_SEQ": str(WORK / "masters/bias_seq"), "FLAT_FRAMES": str(WORK / "masters/flats_frames"),
                    "FLAT_SEQ": str(WORK / "masters/flats_seq"), "FLAT_BIAS": str(WORK / "masters/master_bias.fit"),
                    "DARK_FRAMES": "/data/raw darks", "DARK_SEQ": str(WORK / "masters/dark_seq")},
    "color.ssf": {"WORK": str(WORK), "FOCAL": "160", "PIXEL": "2.9", "CATALOG": "gaia",
                  "SENSOR": "Sony IMX585", "FILTER": "UV/IR Block"},
    "stretch.ssf": {"WORK": str(WORK), "ASINH": "25", "SHADOWS": "-2.5", "TARGET_BG": "0.1", "SAT": "0.35"},
    "mosaic_solve.ssf": {"SUBS": "/data/M 31_sub", "PROCESS": str(WORK / "process"), "DARK": "/data/masters/dark.fit",
                         "FLAT": "/data/masters/flat.fit", "FORCE": "-force ", "FRAMES": "", "MAG": "13", "FOCAL": "150", "PIXEL": "2.9"},
    "mosaic_stack.ssf": {"PROCESS": str(WORK / "process"), "STACK": str(WORK / "stack")},
}


def rendered(name):
    return siril.render((siril.template_dir() / name).read_text(), VALUES[name])


@pytest.mark.parametrize("name", sorted(VALUES))
def test_no_leftover_placeholders(name):
    text = rendered(name)
    assert "{{" not in text and "}}" not in text


@pytest.mark.parametrize("name", sorted(VALUES))
def test_paths_are_absolute_and_quoted(name):
    text = rendered(name)
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        for m in re.finditer(r"/data/[^\"]*", line):
            start = m.start()
            # the path must sit inside a quoted token: cd "/x" or "-out=/x"
            assert line[:start].count('"') % 2 == 1, line
    cds = [l for l in text.splitlines() if l.startswith("cd ")]
    assert cds and all(re.fullmatch(r'cd "/[^"]+"', l) for l in cds)


def test_stack_script_contents():
    text = rendered("stack.ssf")
    assert 'cd "/data/M 31_sub"' in text
    assert 'link light "-out=/data/work dir/process"' in text
    cal = [l for l in text.splitlines() if l.startswith("calibrate")]
    assert cal == ['calibrate light "-dark=/data/masters/dark.fit" "-flat=/data/masters/flat.fit" -cc=dark 3 3 '
                   '-cfa -equalize_cfa -debayer']
    assert '-weight=wfwhm -32b "-out=/data/work dir/stack"' in text


def test_stretch_script_contents():
    text = rendered("stretch.ssf")
    for line in ["load denoised.fits", "asinh -human 25", "autostretch -linked -2.5 0.1",
                 "satu 0.35 1.5", "save stretched", "savetif stretched"]:
        assert line in text.splitlines()
    lines = text.splitlines()
    # the TIFF must be written from the reloaded FITS, not from memory
    assert lines.index("save stretched") < lines.index("load stretched") < lines.index("savetif stretched")
    assert "-deflate" not in text          # StarNet needs an uncompressed 16-bit TIFF


def test_render_rejects_missing_and_unused_values():
    with pytest.raises(KeyError):
        siril.render("cd \"{{WORK}}\"", {})
    with pytest.raises(KeyError):
        siril.render("cd \"{{WORK}}\"", {"WORK": "/a", "EXTRA": "x"})


def test_render_rejects_double_quote():
    with pytest.raises(ValueError):
        siril.render('cd "{{WORK}}"', {"WORK": '/a"b'})


def test_fmt_num():
    assert siril.fmt_num(25.0) == "25"
    assert siril.fmt_num(-2.5) == "-2.5"
    assert siril.fmt_num(0.10) == "0.1"
    assert siril.fmt_num(160) == "160"


def test_render_file_writes_absolute_script(tmp_path):
    p = siril.render_file("stretch.ssf", tmp_path, VALUES["stretch.ssf"])
    assert p.is_absolute() and p.parent == (tmp_path / "logs").resolve()
    assert "{{" not in p.read_text()


def test_run_script_never_saves_into_the_users_settings(monkeypatch, tmp_path):
    """siril-cli saves its settings when a script ends (set16bits, setext...): every script
    runs on a throwaway copy of them, which starts as the user's settings."""
    user = tmp_path / "siril" / siril.CONFIG_FILE
    user.parent.mkdir()
    user.write_text("[core]\nforce_16bit=true\nextension=.fit\n")
    monkeypatch.setattr(siril, "config_dir", lambda: user.parent)
    monkeypatch.setattr(siril.tools, "require_executable", lambda p, v: Path("/bin/siril-cli"))
    seen = []

    def fake_siril(cmd, log):
        cfg = Path(cmd[cmd.index("-i") + 1])
        seen.append((cfg, cfg.read_text()))
        cfg.write_text("[core]\nforce_16bit=false\nextension=.fits\n")      # what Siril saves
        log.write_text("log: Script execution finished successfully.\n")
    monkeypatch.setattr(siril.tools, "run_tool", fake_siril)
    s = tmp_path / "stack.ssf"
    s.write_text("set32bits\nsetext fits\n")
    for _ in range(2):
        siril.run_script(s, tmp_path / "log")
    assert user.read_text() == "[core]\nforce_16bit=true\nextension=.fit\n"
    assert [text for _, text in seen] == [user.read_text()] * 2           # each run starts afresh
    assert all(cfg.parent != user.parent and not cfg.exists() for cfg, _ in seen)


def test_run_script_failure_still_removes_the_copy(monkeypatch, tmp_path):
    monkeypatch.setattr(siril, "config_dir", lambda: tmp_path / "none")
    monkeypatch.setattr(siril.tools, "require_executable", lambda p, v: Path("/bin/siril-cli"))
    seen = []

    def fake_siril(cmd, log):
        seen.append(Path(cmd[cmd.index("-i") + 1]))
        log.write_text("log: Error in line 3\n")
    monkeypatch.setattr(siril.tools, "run_tool", fake_siril)
    s = tmp_path / "x.ssf"
    s.write_text("")
    with pytest.raises(siril.tools.StageError, match="x.ssf failed"):
        siril.run_script(s, tmp_path / "log")
    assert not seen[0].exists()
