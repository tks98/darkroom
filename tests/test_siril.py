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
