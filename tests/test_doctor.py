"""darkroom doctor: check logic with fake subprocess / network / filesystem (no real tools run)."""
import argparse
import json
import subprocess
from pathlib import Path

import pytest

from darkroom import doctor
from darkroom.doctor import FAIL, OK, WARN, Result


def fake_exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def answers(text, rc=0):
    return lambda cmd, timeout=15, cwd=None: (rc, text)


# ---- version parsing

def test_parse_siril_version_ignores_macos_banner():
    assert doctor.parse_siril_version("Siril is started as macOS application\nsiril 1.4.5\n") == (1, 4, 5)
    assert doctor.parse_siril_version("siril 1.2.6") == (1, 2, 6)
    assert doctor.parse_siril_version("Siril is started as macOS application\n") is None


def test_parse_graxpert_version_from_log_line():
    line = "2026-01-01 08:42:06,562 MainProcess root INFO     GraXpert version: 3.1.0rc2 release: Umbriel\n"
    assert doctor.parse_graxpert_version(line) == "3.1.0rc2"
    assert doctor.parse_graxpert_version("usage: something") is None


@pytest.mark.parametrize("out, status", [
    ("Siril is started as macOS application\nsiril 1.4.5\n", OK),
    ("siril 1.2.6\n", FAIL),
    ("siril 1.5.0\n", WARN),
    ("some other program\n", FAIL),
])
def test_siril_status(tmp_path, out, status):
    r = doctor.check_siril(fake_exe(tmp_path / "siril-cli"), run=answers(out))
    assert r.status == status
    assert (r.fix is None) == (status == OK)


def test_siril_missing_is_fail_and_not_run(tmp_path):
    def run(*a, **k):
        raise AssertionError("must not run a missing executable")
    r = doctor.check_siril(tmp_path / "siril-cli", run=run)
    assert r.status == FAIL and "DARKROOM_SIRIL" in r.fix


def test_graxpert_ok_and_timeout(tmp_path):
    exe = fake_exe(tmp_path / "GraXpert")
    assert doctor.check_graxpert(exe, run=answers("x INFO     GraXpert version: 3.1.0rc2 release: U")).status == OK

    def slow(cmd, timeout=15, cwd=None):
        raise subprocess.TimeoutExpired(cmd, timeout)
    assert doctor.check_graxpert(exe, run=slow).status == WARN
    assert doctor.check_graxpert(tmp_path / "nope").status == FAIL


def test_graxpert_falls_back_to_long_flag(tmp_path):
    seen = []

    def run(cmd, timeout=15, cwd=None):
        seen.append(cmd[1])
        return (0, "GraXpert version: 3.1.0" if cmd[1] == "--version" else "")
    assert doctor.check_graxpert(fake_exe(tmp_path / "GraXpert"), run=run, system="linux").status == OK
    assert seen == ["-v", "--version"]


# ---- python / model

def test_python_checks():
    ok = lambda m: None
    assert doctor.check_python((3, 10, 0), importer=ok).status == FAIL

    def broken(m):
        if m == "onnx":
            raise ImportError(m)
    r = doctor.check_python((3, 11, 4), importer=broken)
    assert r.status == FAIL and "onnx" in r.detail
    r = doctor.check_python((3, 11, 4), importer=ok, version_of=lambda n: "1.32.0", pinned="1.32.0")
    assert r.status == OK
    r = doctor.check_python((3, 12, 1), importer=ok, version_of=lambda n: "1.33.0", pinned="1.32.0")
    assert r.status == WARN and "1.32.0" in r.detail


def test_pinned_onnxruntime_matches_pyproject():
    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    assert f'"onnxruntime=={doctor.pinned_onnxruntime()}"' in text


def test_model_missing(tmp_path):
    r = doctor.check_model(tmp_path / "model.onnx")
    assert r.status == FAIL and "DARKROOM_GRAXPERT_MODEL" in r.fix
    (tmp_path / "model.onnx").write_bytes(b"x")
    assert doctor.check_model(tmp_path / "model.onnx").status == OK
    assert doctor.check_gpu(tmp_path / "absent.onnx").status == FAIL      # no onnxruntime work without a model


# ---- starnet

def starnet_folder(tmp_path) -> Path:
    d = tmp_path / "StarNet"
    fake_exe(d / "starnet++")
    (d / "starnet2_weights.pb").write_bytes(b"w")
    (d / "libtensorflow.2.dylib").write_bytes(b"d")
    (d / "rgb_test.tif").write_bytes(b"t")
    return d


class FakeSystem:
    """Stands in for file / arch / xattr / starnet++."""

    def __init__(self, quarantined=(), arch="Mach-O 64-bit executable x86_64", rosetta_rc=0, starnet_rc=0):
        self.quarantined, self.arch, self.rosetta_rc, self.starnet_rc = set(quarantined), arch, rosetta_rc, starnet_rc
        self.calls = []

    def __call__(self, cmd, timeout=15, cwd=None):
        self.calls.append(cmd)
        if cmd[0] == "file":
            return 0, self.arch
        if cmd[0] == "arch":
            return self.rosetta_rc, ""
        if cmd[0] == "xattr":
            assert cmd[1] == "-p", "doctor must only read attributes"
            return (0, "0083;0;x;") if Path(cmd[-1]).name in self.quarantined else (1, "No such xattr")
        if cmd[0] == "./starnet++":
            assert Path(cwd).name == "StarNet" and cmd[1] == "rgb_test.tif"
            if self.starnet_rc == 0:
                Path(cmd[2]).write_bytes(b"out")
            return self.starnet_rc, "done"
        raise AssertionError(f"unexpected command {cmd}")

    @property
    def executed_starnet(self):
        return any(c[0] == "./starnet++" for c in self.calls)


def test_starnet_not_configured_is_warn():
    r = doctor.check_starnet(None, env={}, system="darwin")
    assert r.status == WARN and "star-reduce" in r.fix


def test_starnet_env_var_is_used(tmp_path):
    d = starnet_folder(tmp_path)
    assert doctor.check_starnet(None, run=FakeSystem(), env={"DARKROOM_STARNET_DIR": str(d)}, system="darwin").status == OK


@pytest.mark.parametrize("flagged", ["starnet++", "libtensorflow.2.dylib"])
def test_quarantine_fails_and_never_executes(tmp_path, flagged):
    d = starnet_folder(tmp_path)
    sysm = FakeSystem(quarantined=[flagged])
    r = doctor.check_starnet(str(d), run_sample=True, run=sysm, env={}, system="darwin")
    assert r.status == FAIL and flagged in r.detail
    assert "xattr -dr com.apple.quarantine" in r.fix and "your decision" in r.fix
    assert not sysm.executed_starnet
    assert not any(c[0] == "xattr" and c[1] != "-p" for c in sysm.calls)


def test_starnet_not_run_by_default(tmp_path):
    sysm = FakeSystem()
    r = doctor.check_starnet(str(starnet_folder(tmp_path)), run=sysm, env={}, system="darwin")
    assert r.status == OK and "Rosetta" in r.detail and not sysm.executed_starnet


def test_starnet_sample_run(tmp_path):
    d = starnet_folder(tmp_path)
    sysm = FakeSystem()
    r = doctor.check_starnet(str(d), run_sample=True, run=sysm, env={}, system="darwin")
    assert r.status == OK and "sample run" in r.detail and sysm.executed_starnet
    out = Path(next(c for c in sysm.calls if c[0] == "./starnet++")[2])
    assert not out.exists() and not out.parent.exists()          # temp output cleaned up
    assert sorted(p.name for p in d.iterdir()) == ["libtensorflow.2.dylib", "rgb_test.tif",
                                                   "starnet++", "starnet2_weights.pb"]
    assert doctor.check_starnet(str(d), run_sample=True, run=FakeSystem(starnet_rc=1), env={}, system="darwin").status == FAIL


def test_starnet_broken_installs(tmp_path):
    d = starnet_folder(tmp_path)
    assert doctor.check_starnet(str(d), run=FakeSystem(rosetta_rc=1), env={}, system="darwin").status == FAIL
    assert doctor.check_starnet(str(d), run=FakeSystem(arch="Mach-O 64-bit executable arm64",
                                                       rosetta_rc=1), env={}, system="darwin").status == OK
    (d / "starnet2_weights.pb").unlink()
    assert "weights" in doctor.check_starnet(str(d), run=FakeSystem(), env={}, system="darwin").detail
    (d / "starnet++").unlink()
    assert doctor.check_starnet(str(d), run=FakeSystem(), env={}, system="darwin").status == FAIL
    assert doctor.check_starnet(str(tmp_path / "nowhere"), run=FakeSystem(), env={}, system="darwin").status == FAIL


# ---- catalogue

def net(up_hosts):
    def get(url, timeout=6):
        if any(h in url for h in up_hosts):
            return 200
        raise OSError("timed out")
    return get


@pytest.mark.parametrize("up, status", [
    (["cds.unistra.fr", "esac.esa.int"], OK),
    (["u-strasbg.fr"], OK),                     # the older VizieR host alone is enough
    (["esac.esa.int"], WARN),
    ([], FAIL),
])
def test_catalogue_reachability(up, status):
    r = doctor.check_catalogue(get=net(up))
    assert r.status == status
    if status == WARN:
        assert "Gaia fallback" in r.detail


def test_catalogue_http_error_is_not_up():
    assert doctor.check_catalogue(get=lambda url, timeout=6: 503).status == FAIL


# ---- disk

def test_disk_need_arithmetic():
    assert doctor.disk_need(0) == 2e9
    assert doctor.disk_need(404) == 404 * 80e6 + 2e9          # 34.3 GB: ~31 GiB of frames + 2 GB


def test_disk_statuses(tmp_path):
    subs = tmp_path / "subs"
    subs.mkdir()
    for i in range(10):
        (subs / f"Light_{i}.fit").write_bytes(b"")
    (subs / "notes.txt").write_text("ignored")
    (subs / "thumb.jpg").write_bytes(b"")
    assert doctor.count_subs(subs) == 10
    need = doctor.disk_need(10)                               # 2.8 GB
    work = tmp_path / "not" / "created" / "yet"
    check = lambda free: doctor.check_disk(work, subs, free_of=lambda p: free)
    assert check(need - 1).status == FAIL
    assert check(need).status == WARN
    assert check(1.5 * need - 1).status == WARN
    assert check(1.5 * need).status == OK
    assert "10 subs" in check(need).detail and "2.8 GB" in check(need).detail
    assert not work.exists()                                  # doctor creates nothing
    assert doctor.check_disk(None, None) is None
    assert doctor.check_disk(None, subs).status == WARN
    assert doctor.check_disk(work, None, free_of=lambda p: 5e9).status == OK
    assert doctor.check_disk(work, tmp_path / "missing").status == FAIL


# ---- output, exit code, CLI wiring

SAMPLE = [Result("python", OK, "fine"), Result("starnet", WARN, "not configured", "set it"),
          Result("siril", FAIL, "missing", "install it")]


def test_exit_code():
    assert doctor.exit_code(SAMPLE) == 1
    assert doctor.exit_code(SAMPLE[:2]) == 0
    assert doctor.exit_code([]) == 0


def test_table_is_aligned_with_fixes_below():
    lines = doctor.format_table(SAMPLE).splitlines()
    assert lines[:3] == ["OK    python   fine", "WARN  starnet  not configured", "FAIL  siril    missing"]
    assert lines[3:] == ["", "fixes:", "  starnet: set it", "  siril: install it"]
    assert "fixes" not in doctor.format_table(SAMPLE[:1])


def test_main_json_and_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "run_checks", lambda args: SAMPLE)
    assert doctor.main(["--json", "--offline"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert [set(d) for d in data] == [{"name", "status", "detail", "fix"}] * 3
    assert data[0] == {"name": "python", "status": "OK", "detail": "fine", "fix": None}
    monkeypatch.setattr(doctor, "run_checks", lambda args: SAMPLE[:2])
    assert doctor.main([]) == 0
    assert "WARN  starnet" in capsys.readouterr().out


def test_run_checks_selection_and_crash_isolation(monkeypatch):
    for n in ("system", "python", "siril", "graxpert", "model", "gpu", "catalogue"):
        monkeypatch.setattr(doctor, f"check_{n}", lambda n=n: Result(n, OK, ""))
    monkeypatch.setattr(doctor, "check_deconv_model", lambda enabled: Result("deconv", OK, str(enabled)))
    monkeypatch.setattr(doctor, "check_starnet", lambda flag, run: Result("starnet", OK, f"{flag} {run}"))

    def boom():
        raise RuntimeError("bug")
    monkeypatch.setattr(doctor, "check_siril", boom)
    ns = argparse.Namespace(work=None, subs=None, starnet_dir="S", run_starnet=True, offline=False, json=False)
    res = doctor.run_checks(ns)
    assert [r.name for r in res] == ["system", "python", "siril", "graxpert", "model", "deconv", "gpu", "starnet",
                                     "catalogue"]
    assert res[2].status == FAIL and "bug" in res[2].detail
    assert res[5].detail == "False"                              # no --work: deconv counts as off
    assert res[7].detail == "S True"
    ns.offline = True
    assert "catalogue" not in [r.name for r in doctor.run_checks(ns)]


def test_add_parser_registers_subcommand():
    sp = argparse.ArgumentParser(prog="darkroom").add_subparsers(dest="cmd")
    doctor.add_parser(sp)
    a = sp.choices["doctor"].parse_args(["--work", "w", "--subs", "s", "--starnet-dir", "d",
                                         "--run-starnet", "--offline", "--json"])
    assert (a.work, a.subs, a.starnet_dir) == (Path("w"), Path("s"), "d")
    assert a.run_starnet and a.offline and a.json and a.func is doctor.run_args
