"""Which subs the stack stage stacks, and how its record notices changes to them; per-stage
algorithm versions and tool versions in the records (Siril and the stages are faked)."""
import json
import os
import re

import pytest

from darkroom import cli, stages
from test_run import fake, names, run  # noqa: F401  (fixture and helpers)


def touch(p, data=b"x", t=1000):
    p.write_bytes(data)
    os.utime(p, (t, t))
    return p


# --- selection

def test_select_subs_matches_the_glob_and_falls_back_to_every_fits_file(tmp_path):
    for n in ("Light_1.fit", "Light_2.FIT", "Light_3.fits", "Light_4.fts", "Flat_1.fit", "Light_1.jpg",
              "._Light_5.fit", "notes.txt"):
        touch(tmp_path / n)
    (tmp_path / "Light_6.fit").mkdir()                          # a folder is not a sub
    every = ["Flat_1.fit", "Light_1.fit", "Light_2.FIT", "Light_3.fits", "Light_4.fts"]
    assert [p.name for p in stages.fits_files(tmp_path)] == every
    files, matched = stages.select_subs(tmp_path, "Light_*.fit")
    assert matched and [p.name for p in files] == every[1:]        # .fit in a glob: any FITS extension
    files, matched = stages.select_subs(tmp_path, "Light_*")
    assert matched and [p.name for p in files] == every[1:]        # FITS only, not the jpg
    files, matched = stages.select_subs(tmp_path, "Dark_*.fit")
    assert not matched and [p.name for p in files] == every


def fake_siril(monkeypatch, scripts):
    def run_script(script, log, initfile=None):
        scripts.append(script.read_text())
        log.write_text("log: Welcome to siril 1.4.5 for test\nlog: 2 images have been stacked.\n")
        out = re.findall(r'"-out=([^"]+)"', scripts[-1])[-1]
        open(out + ".fit", "wb").write(b"s")
    monkeypatch.setattr(stages.siril, "run_script", run_script)


def test_stack_links_only_the_selected_subs(tmp_path, monkeypatch, capsys):
    scripts = []
    fake_siril(monkeypatch, scripts)
    subs, work = tmp_path / "subs", tmp_path / "work"
    subs.mkdir()
    for n in ("Light_1.fit", "Light_2.fit", "Flat_1.fit", "Light_1.jpg"):
        touch(subs / n)
    before = sorted(p.name for p in subs.iterdir())
    stages.stack(work, subs, None, sub_glob="Light_*.fit")
    staged = work.resolve() / "process" / "subs"
    assert f'cd "{staged}"' in scripts[-1]
    assert sorted(p.name for p in staged.iterdir()) == ["Light_1.fit", "Light_2.fit"]
    assert sorted(p.name for p in subs.iterdir()) == before              # nothing written there
    assert "1 FITS file(s)" in capsys.readouterr().out


def test_stack_takes_fits_and_fts_subs(tmp_path, monkeypatch, capsys):
    scripts = []
    fake_siril(monkeypatch, scripts)
    subs, work = tmp_path / "subs", tmp_path / "work"
    subs.mkdir()
    for n in ("Light_1.fits", "Light_2.FTS", "Light_3.fit"):
        touch(subs / n)
    stages.stack(work, subs, None, sub_glob="Light_*3.fit")            # only Light_3 matches
    staged = work.resolve() / "process" / "subs"
    assert sorted(p.name for p in staged.iterdir()) == ["Light_3.fit"]
    stages.stack(work, subs, None, sub_glob="Seestar_*.fit", force=True)  # none match: every FITS file
    assert sorted(p.name for p in staged.iterdir()) == ["Light_1.fits", "Light_2.FTS", "Light_3.fit"]
    assert "stacking every FITS file there (3)" in capsys.readouterr().out
    assert [p.name for p in stages.inputs(work, "stack", {"subs": str(subs), "dark": "none"})] == \
        ["Light_1.fits", "Light_2.FTS", "Light_3.fit"]


def test_stack_without_fits_files_fails(tmp_path, monkeypatch):
    fake_siril(monkeypatch, [])
    subs = tmp_path / "subs"
    subs.mkdir()
    touch(subs / "Light_1.jpg")
    with pytest.raises(stages.tools.StageError, match=r"no FITS files \(\*\.fit, \*\.fits, \*\.fts\)"):
        stages.stack(tmp_path / "w", subs, None)


# --- the stack record

def stack_table(subs, **kw):
    return {"subs": subs, "dark": None, "sub_glob": "Light_*.fit", "framing": "min", "keep_fwhm": 90,
            "keep_round": 90, "focal": 160.0, "pixel": 2.9, **kw}


def test_stack_record_has_the_selection_and_the_mosaic_optics(tmp_path):
    for n in ("Light_1.fit", "Light_2.fit", "Flat_1.fit"):
        touch(tmp_path / n)
    p = stages.result_params("stack", stack_table(tmp_path))
    assert p["subs_count"] == 2 and len(p["subs_digest"]) == 16
    assert "focal" not in p and "pixel" not in p                        # only for a mosaic
    m = stages.result_params("stack", stack_table(tmp_path, mosaic=True))
    assert (m["focal"], m["pixel"]) == (160.0, 2.9)
    touch(tmp_path / "Flat_2.fit")                                       # not selected: no change
    assert stages.result_params("stack", stack_table(tmp_path)) == p
    touch(tmp_path / "Light_0.fit")
    assert stages.result_params("stack", stack_table(tmp_path))["subs_digest"] != p["subs_digest"]
    # no sub_glob (the fallback in `run` when the calibration cannot be checked) or no folder
    assert "subs_digest" not in stages.result_params("stack", {**stack_table(tmp_path), "sub_glob": None})
    t = stack_table(tmp_path / "gone")
    assert "subs_digest" not in stages.result_params("stack", t)
    del t["sub_glob"]
    assert "subs_count" not in stages.result_params("stack", t)


def test_optional_keys_count_only_when_both_records_have_them():
    old = {"subs": "/s", "framing": "min"}
    new = {"subs": "/s", "framing": "min", "subs_count": 3, "subs_digest": "ab", "focal": 150.0}
    assert stages.changed_keys("stack", old, new) == []
    assert stages.same_params("stack", new, old)
    assert stages.changed_keys("stack", new, new | {"subs_count": 2, "subs_digest": "cd"}) == \
        ["subs_count", "subs_digest"]
    assert stages.changed_keys("stack", new, new | {"focal": 160.0}) == ["focal"]
    assert stages.changed_keys("finish", {"a": 1}, {"a": 1, "b": 2}) == ["b"]   # other stages: strict
    assert not stages.same_params("stack", None, new)


def subs_of(work):
    return cli.config.resolve(work)["target"]["subs"]


def test_removing_or_adding_a_sub_reruns_the_stack(fake):
    work, ran, _ = fake
    subs = cli.Path(subs_of(work))
    touch(subs / "Light_2.fit")
    assert run(work) == 0 and names(ran)[0] == "stack"
    assert stages.recorded_params(work, "stack")["subs_count"] == 2
    assert run(work) == 0 and names(ran) == []
    (subs / "Light_2.fit").unlink()
    assert run(work) == 0 and names(ran)[0] == "stack"
    assert run(work) == 0 and names(ran) == []
    touch(subs / "Light_0.fit", t=10)                                    # dated long before the stack
    assert run(work) == 0 and names(ran)[0] == "stack"
    touch(subs / "notes.fit.txt")                                        # not a sub
    assert run(work) == 0 and names(ran) == []


def test_an_old_stack_record_without_the_selection_stays_up_to_date(fake, capsys):
    work, ran, _ = fake
    assert run(work) == 0
    names(ran)
    p = stages.params_file(work, "stack")
    rec = json.loads(p.read_text())
    for k in ("subs_count", "subs_digest"):
        del rec["params"][k]
    del rec["algorithm"], rec["tools"]                                   # as darkroom 0.1.0 wrote it
    p.write_text(json.dumps(rec))
    touch(cli.Path(subs_of(work)) / "Light_0.fit", t=10)                # cannot be noticed without it
    capsys.readouterr()
    assert run(work) == 0 and names(ran) == []
    assert "-- stack: up to date, skipped" in capsys.readouterr().out
    assert run(work, "--from", "stack", "--to", "stack") == 0 and names(ran) == ["stack"]
    assert stages.recorded_params(work, "stack")["subs_count"] == 2   # written on the next real run


def test_unplugged_subs_keep_the_stack(fake, capsys):
    work, ran, _ = fake
    assert run(work) == 0
    names(ran)
    subs = cli.Path(subs_of(work))
    subs.rename(subs.with_name("away"))
    assert run(work) == 0 and names(ran) == []
    assert "-- stack: up to date, skipped" in capsys.readouterr().out


# --- algorithm versions

def test_records_carry_the_algorithm_version(fake, monkeypatch, capsys):
    work, ran, _ = fake
    assert run(work) == 0
    names(ran)
    rec = json.loads(stages.params_file(work, "stretch").read_text())
    assert rec["algorithm"] == 1 and isinstance(rec["tools"], dict)
    monkeypatch.setitem(stages.ALGORITHM, "stretch", 2)
    capsys.readouterr()
    assert run(work) == 0 and names(ran) == ["stretch", "starnet", "finish"]
    assert "stretch algorithm changed (version 1 -> 2)" in capsys.readouterr().out
    assert json.loads(stages.params_file(work, "stretch").read_text())["algorithm"] == 2
    assert run(work) == 0 and names(ran) == []


def test_a_record_without_an_algorithm_version_counts_as_version_1(fake):
    work, ran, _ = fake
    assert run(work) == 0
    names(ran)
    p = stages.params_file(work, "finish")
    rec = json.loads(p.read_text())
    del rec["algorithm"]
    p.write_text(json.dumps(rec))
    assert run(work) == 0 and names(ran) == []
    assert set(stages.ALGORITHM) == set(stages.STAGES)
    assert all(isinstance(v, int) and v >= 1 for v in stages.ALGORITHM.values())


def test_tool_versions_come_from_the_logs(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "stack.log").write_text("$ siril-cli -s x\nlog: Welcome to siril 1.4.5 for macos (aarch64) - CLI\n")
    (logs / "bge.log").write_text(
        "2026-11-18 19:48:48,048 MainProcess root INFO     Starting GraXpert CLI, Background-Extraction, "
        "version: 3.1.0rc2 release: Umbriel\n"
        "2026-11-18 19:48:48,485 MainProcess root INFO     Using AI version 1.0.1. You can overwrite this\n")
    (logs / "starnet.log").write_text("[darkroom] StarNet2 2.6.2 (build 0242) in /some/dir\n")
    assert stages.tool_versions(tmp_path, "stack") == {"siril": "1.4.5"}
    assert stages.tool_versions(tmp_path, "bge") == {"graxpert": "3.1.0rc2", "graxpert_bge_model": "1.0.1"}
    assert stages.tool_versions(tmp_path, "starnet") == {"starnet": "StarNet2 2.6.2 (build 0242)"}
    assert stages.tool_versions(tmp_path, "color") == {}                # no log
    d = stages.tool_versions(tmp_path, "denoise")
    assert d["graxpert_denoise_model"] == "3.0.2"
    assert stages.tool_versions(tmp_path, "deconv")["graxpert_deconv_model"] == "1.0.1"
    stages.record_params(tmp_path, "stack", {"subs": "/s"})
    rec = json.loads(stages.params_file(tmp_path, "stack").read_text())
    assert rec["tools"] == {"siril": "1.4.5"} and rec["params"] == {"subs": "/s"}
    stages.record_params(tmp_path, "master_bias", {"frames": "/b"})     # calib's records: as before
    assert set(json.loads(stages.params_file(tmp_path, "master_bias").read_text())) == \
        {"stage", "darkroom", "params"}


def test_run_keeps_a_stack_whose_calibration_cannot_be_checked(fake, monkeypatch, capsys):
    """The fallback in `run` compares a record without the selection of subs (no sub_glob there)
    with one that has it: still the same stack."""
    work, ran, _ = fake
    assert run(work) == 0
    names(ran)
    assert "subs_digest" in stages.recorded_params(work, "stack")

    (work.parent / "dark.fit").unlink()             # the drive with the dark is unplugged
    capsys.readouterr()
    assert run(work) == 0 and names(ran) == []
    assert "-- stack: kept as it is" in capsys.readouterr().out
