"""darkroom doctor: check logic with fake subprocess / network / filesystem (no real tools run)."""
import argparse
import json
import subprocess
import sys
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
    assert doctor.check_graxpert(exe, run=slow, system="linux").status == WARN
    assert doctor.check_graxpert(tmp_path / "nope").status == FAIL


@pytest.mark.parametrize("probe", [
    lambda cmd, timeout=15, cwd=None: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, timeout)),
    lambda cmd, timeout=15, cwd=None: (_ for _ in ()).throw(FileNotFoundError("xattr")),
    lambda cmd, timeout=15, cwd=None: (1, "xattr: [Errno 1] Operation not permitted"),
])
def test_unreadable_quarantine_flag_is_not_executed(tmp_path, probe):
    exe = fake_exe(tmp_path / "GraXpert")
    ran = []

    def run(cmd, timeout=15, cwd=None):
        if cmd[0] == "xattr":
            return probe(cmd, timeout)
        ran.append(cmd)
        return 0, "GraXpert version: 3.1.0"
    r = doctor.check_graxpert(exe, run=run, system="darwin")
    assert r.status == FAIL and "could not read its quarantine flag" in r.detail and "did not execute" in r.detail
    assert ran == [] and "xattr -p com.apple.quarantine" in r.fix


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


def pin_denoise_model(monkeypatch, data: bytes):
    """Make stand-in bytes the 'pinned' denoise model (the real one is GraXpert's, never in tests)."""
    import hashlib
    from darkroom import models
    monkeypatch.setattr(models, "DENOISE_BYTES", len(data))
    monkeypatch.setattr(models, "DENOISE_SHA256", hashlib.sha256(data).hexdigest())


def test_model_missing(tmp_path, monkeypatch):
    r = doctor.check_model(tmp_path / "model.onnx")
    assert r.status == FAIL and "DARKROOM_GRAXPERT_MODEL" in r.fix
    (tmp_path / "model.onnx").write_bytes(b"x")
    r = doctor.check_model(tmp_path / "model.onnx")
    assert r.status == FAIL and "not the 477703971 of denoise model 3.0.2" in r.detail and "delete it" in r.fix
    pin_denoise_model(monkeypatch, b"x")
    r = doctor.check_model(tmp_path / "model.onnx")
    assert r.status == OK and "size and SHA-256 match" in r.detail
    assert doctor.check_gpu(tmp_path / "absent.onnx").status == FAIL      # no onnxruntime work without a model


def test_model_with_the_right_size_but_wrong_hash(tmp_path, monkeypatch):
    p = tmp_path / "model.onnx"
    p.write_bytes(b"y")
    pin_denoise_model(monkeypatch, b"x")
    r = doctor.check_model(p)
    assert r.status == FAIL and "not its SHA-256" in r.detail
    r = doctor.check_gpu(p, ort=pytest.fail)            # the GPU check does not use a wrong model either
    assert r.status == FAIL and "not its SHA-256" in r.detail


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

    def __init__(self, quarantined=(), arch="Mach-O 64-bit executable x86_64", rosetta_rc=0, starnet_rc=0,
                 notarized=(), flag="0083;0;x;"):
        self.quarantined, self.arch, self.rosetta_rc, self.starnet_rc = set(quarantined), arch, rosetta_rc, starnet_rc
        self.notarized, self.flag = set(notarized), flag
        self.calls = []

    def __call__(self, cmd, timeout=15, cwd=None):
        self.calls.append(cmd)
        if cmd[0] == "file":
            return 0, self.arch
        if cmd[0] == "arch":
            return self.rosetta_rc, ""
        if cmd[0] == "xattr":
            assert cmd[1] == "-p", "doctor must only read attributes"
            return (0, self.flag) if Path(cmd[-1]).name in self.quarantined else (1, "No such xattr")
        if cmd[0] == "spctl":
            assert cmd[:6] == ["spctl", "-a", "-t", "open", "--context", "context:primary-signature"]
            if Path(cmd[-1]).name in self.notarized:
                return 0, f"{cmd[-1]}: accepted\nsource=Notarized Developer ID\n"
            return 3, f"{cmd[-1]}: rejected\n"
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


def test_quarantined_starnet_runs_when_gatekeeper_accepts_or_the_user_allowed_it(tmp_path):
    d = starnet_folder(tmp_path)
    sysm = FakeSystem(quarantined=["starnet++", "libtensorflow.2.dylib"],
                      notarized=["starnet++", "libtensorflow.2.dylib"])
    r = doctor.check_starnet(str(d), run_sample=True, run=sysm, env={}, system="darwin")
    assert r.status == OK and sysm.executed_starnet and "accepted by Gatekeeper: Notarized Developer ID" in r.detail
    sysm = FakeSystem(quarantined=["starnet++"], flag="01c3;6a000000;Safari;UUID")       # Open Anyway
    r = doctor.check_starnet(str(d), run_sample=True, run=sysm, env={}, system="darwin")
    assert r.status == OK and sysm.executed_starnet and "allowed by you" in r.detail
    assert not any(c[0] == "spctl" for c in sysm.calls)


def test_starnet_with_unreadable_quarantine_flag_is_not_run(tmp_path):
    d = starnet_folder(tmp_path)

    def run(cmd, timeout=15, cwd=None):
        if cmd[0] == "xattr":
            raise subprocess.TimeoutExpired(cmd, timeout)
        return FakeSystem()(cmd, timeout, cwd)
    r = doctor.check_starnet(str(d), run_sample=True, run=run, env={}, system="darwin")
    assert r.status == FAIL and "could not read the quarantine flag" in r.detail and "did not answer" in r.detail
    assert "doctor did not execute it" in r.detail and "xattr -p com.apple.quarantine" in r.fix


def starnet2_install(tmp_path, accepted: dict) -> Path:
    d = tmp_path / "starnet2_macos-arm64_2.6.2-0242_COREML_arm64_cli"
    fake_exe(d / "starnet2")
    (d / "StarNet2_weights.mlpackage").mkdir()
    (d / "accepted.json").write_text(json.dumps({"product": "StarNet2", "version": "2.6.2", **accepted}))
    return d


class Mac2(FakeSystem):
    def __call__(self, cmd, timeout=15, cwd=None):
        if cmd[0] == "./starnet2":
            self.calls.append(cmd)
            return 0, '{"product":"starnet2","version":"2.6.2","build":"0242","backend":"coreml"}'
        return super().__call__(cmd, timeout, cwd)


@pytest.mark.parametrize("notarized", [False, True])
def test_starnet2_from_an_older_get_starnet_is_assessed_before_it_runs(tmp_path, notarized):
    """Installed without a quarantine flag, so macOS never judged it: doctor asks spctl first."""
    d = starnet2_install(tmp_path, {})
    sysm = Mac2(arch="Mach-O 64-bit executable arm64", notarized=["starnet2"] if notarized else [])
    r = doctor.check_starnet(str(d), run=sysm, env={}, system="darwin")
    ran = any(c[0] == "./starnet2" for c in sysm.calls)
    if notarized:
        assert r.status == OK and ran and "accepted by Gatekeeper" in r.detail
    else:
        assert r.status == FAIL and not ran and "never assessed it" in r.detail
        assert "darkroom get-starnet --force" in r.fix and "doctor did not execute it" in r.detail
    assert not any(c[0] == "xattr" and c[1] != "-p" for c in sysm.calls)


def test_starnet2_quarantined_by_get_starnet(tmp_path):
    d = starnet2_install(tmp_path, {"quarantine": "0081;6a000000;darkroom;"})
    sysm = Mac2(arch="Mach-O 64-bit executable arm64", quarantined=["starnet2"], flag="0081;6a000000;darkroom;")
    r = doctor.check_starnet(str(d), run=sysm, env={}, system="darwin")
    assert r.status == FAIL and "not notarized" in r.detail and not any(c[0] == "./starnet2" for c in sysm.calls)
    assert f"xattr -dr com.apple.quarantine '{d}'" in r.fix
    sysm = Mac2(arch="Mach-O 64-bit executable arm64")             # the user cleared the flag: their decision
    assert doctor.check_starnet(str(d), run=sysm, env={}, system="darwin").status == OK
    assert not any(c[0] == "spctl" for c in sysm.calls)


def test_starnet_stage_refuses_a_blocked_starnet(tmp_path, monkeypatch):
    from darkroom import starnet as sn, toolset
    d = starnet2_install(tmp_path, {"quarantine": "0081;6a000000;darkroom;"})
    sysm = Mac2(arch="Mach-O 64-bit executable arm64", quarantined=["starnet2"], flag="0081;6a000000;darkroom;")
    monkeypatch.setattr(toolset, "_run", sysm)
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(doctor.tools.StageError, match="darkroom did not execute it. Allowing it is your decision"):
        sn.require_runnable(sn.detect(d))
    monkeypatch.setattr(sys, "platform", "linux")
    sn.require_runnable(sn.detect(d))


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


def test_catalogue_probes_use_https():
    assert all(u.startswith("https://") for u in doctor.VIZIER_URLS + doctor.ESA_URLS)


# ---- home paths

@pytest.mark.parametrize("text, want", [
    ("/home/ann/Library/x not found", "~/Library/x not found"),
    ("at /home/ann (cache)", "at ~ (cache)"),
    ("xattr -dr com.apple.quarantine '/home/ann/Library/Caches/darkroom/starnet/a b'",
     "xattr -dr com.apple.quarantine ~/'Library/Caches/darkroom/starnet/a b'"),
    ('chmod +x "/home/ann/x"', 'chmod +x ~/"x"'),
    ("cd '/home/ann'", "cd ~"),
    ("/home/anna/x and /srv/home/ann/y and /home/ann.bak", "/home/anna/x and /srv/home/ann/y and /home/ann.bak"),
    ("free at /home/ann.", "free at ~."),
])
def test_tilde(text, want):
    assert doctor.tilde(text, home="/home/ann") == want


def test_output_hides_the_home_folder(monkeypatch, capsys):
    home = str(Path.home())
    res = [Result("denoise model", FAIL, f"not found: {home}/Library/model.onnx", f"set X to '{home}/a'")]
    monkeypatch.setattr(doctor, "run_checks", lambda args: res)
    doctor.main(["--json"])
    out = capsys.readouterr().out
    assert home not in out and "~/Library/model.onnx" in out and "~/'a'" in out
    doctor.main([])
    out = capsys.readouterr().out
    assert home not in out and "~/Library/model.onnx" in out
    assert home not in doctor.format_table(res)


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
