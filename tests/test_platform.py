"""Linux vs macOS defaults, denoise backend choice, fetch-models and doctor on Linux
(sys.platform, $PATH lookups, onnxruntime and subprocesses are faked)."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from darkroom import cli, denoise as dn, doctor, models, tools
from darkroom.doctor import FAIL, OK, WARN

ENV = ("DARKROOM_SIRIL", "DARKROOM_GRAXPERT", "DARKROOM_GRAXPERT_MODEL", "DARKROOM_CACHE",
       "XDG_CACHE_HOME", "XDG_DATA_HOME")


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    for v in ENV:
        monkeypatch.delenv(v, raising=False)
    return monkeypatch


def test_linux_defaults_from_path(linux):
    found = {"siril-cli": "/usr/local/bin/siril-cli", "GraXpert": "/usr/local/bin/GraXpert"}
    linux.setattr(shutil, "which", lambda name: found.get(name))
    assert tools.siril_cli() == Path("/usr/local/bin/siril-cli")
    assert tools.graxpert() == Path("/usr/local/bin/GraXpert")
    assert tools.graxpert_model() == Path("~/.local/share/GraXpert/denoise-ai-models/3.0.2/model.onnx").expanduser()
    assert tools.cache_dir() == Path("~/.cache/darkroom").expanduser()


def test_linux_fallbacks_xdg_and_env(linux):
    linux.setattr(shutil, "which", lambda name: None)
    assert tools.graxpert() == Path("/opt/graxpert/GraXpert-linux/GraXpert")
    assert tools.siril_cli() == Path("siril-cli")          # not found: require_executable says which variable to set
    linux.setenv("XDG_CACHE_HOME", "/cache/xdg")
    linux.setenv("XDG_DATA_HOME", "/data/xdg")
    assert tools.cache_dir() == Path("/cache/xdg/darkroom")
    assert tools.graxpert_model() == Path("/data/xdg/GraXpert/denoise-ai-models/3.0.2/model.onnx")
    linux.setenv("XDG_CACHE_HOME", "relative/ignored")      # the XDG spec says relative values are ignored
    assert tools.cache_dir() == Path("~/.cache/darkroom").expanduser()
    linux.setenv("DARKROOM_CACHE", "/elsewhere")
    linux.setenv("DARKROOM_GRAXPERT", "/x/GraXpert")
    assert tools.cache_dir() == Path("/elsewhere") and tools.graxpert() == Path("/x/GraXpert")


def test_macos_defaults_unchanged(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    for v in ENV:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: pytest.fail("macOS defaults do not search $PATH"))
    assert tools.siril_cli() == Path("/Applications/Siril.app/Contents/MacOS/siril-cli")
    assert tools.graxpert() == Path("/Applications/GraXpert.app/Contents/MacOS/GraXpert")
    assert tools.graxpert_model() == Path(
        "~/Library/Application Support/GraXpert/denoise-ai-models/3.0.2/model.onnx").expanduser()
    assert tools.cache_dir() == Path("~/Library/Caches/darkroom").expanduser()


def test_help_shows_this_platforms_defaults(linux):
    linux.setattr(shutil, "which", lambda name: None)
    text = cli.env_help()
    assert "(defaults for this platform, Linux)" in text
    assert "/opt/graxpert/GraXpert-linux/GraXpert" in text and "~/.cache/darkroom" in text
    assert "/Applications" not in text
    linux.setattr(sys, "platform", "darwin")
    assert "/Applications/Siril.app" in cli.env_help() and "~/Library/Caches/darkroom" in cli.env_help()


# ---- denoise backend

@pytest.mark.parametrize("cpu, system, available, label", [
    (False, "darwin", ["CoreMLExecutionProvider", "CPUExecutionProvider"], "CoreML"),
    (True, "darwin", ["CoreMLExecutionProvider", "CPUExecutionProvider"], "CPU"),
    (False, "darwin", ["CPUExecutionProvider"], "CPU"),
    (False, "linux", ["CUDAExecutionProvider", "CPUExecutionProvider"], "CUDA"),
    (True, "linux", ["CUDAExecutionProvider", "CPUExecutionProvider"], "CPU"),
    (False, "linux", ["CPUExecutionProvider"], "CPU"),
    (False, "linux", ["CoreMLExecutionProvider", "CPUExecutionProvider"], "CPU"),   # CoreML only on macOS
])
def test_denoise_provider_choice(cpu, system, available, label):
    prov, got = dn.providers(cpu, available, system)
    assert got == label
    assert prov[-1] == "CPUExecutionProvider"
    if label == "CoreML":
        assert prov[0] == ("CoreMLExecutionProvider", {"ModelFormat": "MLProgram", "MLComputeUnits": "ALL"})
    if label == "CUDA":
        assert prov[0] == "CUDAExecutionProvider"


def test_denoise_fetches_a_missing_model(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKROOM_GRAXPERT_MODEL", str(tmp_path / "absent.onnx"))
    seen = []

    def fetch(log_path, log):
        seen.append(log_path)
        raise tools.StageError("fetch called")
    monkeypatch.setattr(models, "fetch_denoise_model", fetch)
    msgs = []
    with pytest.raises(tools.StageError, match="fetch called"):
        dn.run(tmp_path / "in.fits", tmp_path / "out.fits", 0.6, False, tmp_path / "log", msgs.append)
    assert seen == [tmp_path / "log"] and "fetching it with GraXpert" in msgs[0]


# ---- fetch-models

def graxpert_exe(tmp_path):
    p = tmp_path / "GraXpert"
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    return p


def test_fetch_models_runs_graxpert_denoise_once(tmp_path):
    model = tmp_path / "models" / "3.0.2" / "model.onnx"
    seen = []

    def runner(cmd, log_path):
        seen.append(cmd)
        src = Path(cmd[-1])
        from astropy.io import fits
        data = fits.getdata(src)
        assert data.shape == (3, 64, 64) and data.dtype.kind == "f" and data.dtype.itemsize == 4
        model.parent.mkdir(parents=True)
        model.write_bytes(b"onnx")                   # what GraXpert's download does
    exe = graxpert_exe(tmp_path)
    msgs = []
    assert models.fetch_denoise_model(tmp_path / "log", msgs.append, run=runner, exe=exe, model=model) == model
    (cmd,) = seen
    assert cmd[:7] == [str(exe), "-cli", "-cmd", "denoising", "-ai_version", "3.0.2", "-output"]
    assert Path(cmd[7]).parent == Path(cmd[8]).parent and cmd[8].endswith(".fits")
    assert not Path(cmd[8]).parent.exists()          # temp folder removed
    assert models.fetch_denoise_model(tmp_path / "log", msgs.append, run=pytest.fail, exe=exe, model=model) == model
    assert "already present" in msgs[-1]


def test_fetch_models_failures(tmp_path):
    exe, model = graxpert_exe(tmp_path), tmp_path / "model.onnx"

    def downloads_then_fails(cmd, log_path):
        model.write_bytes(b"onnx")
        raise tools.StageError("GraXpert exited with code 1")
    msgs = []
    assert models.fetch_denoise_model(tmp_path / "log", msgs.append, run=downloads_then_fails, exe=exe,
                                      model=model) == model
    assert "the model was downloaded" in msgs[-1]
    model.unlink()
    (tmp_path / "log").write_text("no network\n")
    with pytest.raises(tools.StageError, match="did not leave a denoise model"):
        models.fetch_denoise_model(tmp_path / "log", msgs.append, run=lambda c, l: None, exe=exe, model=model)
    with pytest.raises(tools.StageError, match="DARKROOM_GRAXPERT"):
        models.fetch_denoise_model(tmp_path / "log", msgs.append, exe=tmp_path / "nope", model=model)


def test_fetch_models_cli(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(models, "fetch_denoise_model", lambda: called.append(1))
    assert cli.main(["fetch-models"]) == 0 and called == [1]


# ---- doctor on Linux

class LinuxSystem:
    """No file / arch / xattr on Linux: any such call is a bug."""

    def __init__(self, info='{"product":"starnet2","version":"2.6.2","build":"0242","backend":"ort-cpu"}', rc=0):
        self.info, self.rc, self.calls = info, rc, []

    def __call__(self, cmd, timeout=15, cwd=None):
        self.calls.append((cmd, cwd))
        assert cmd[0] not in ("file", "arch", "xattr"), f"macOS-only command on Linux: {cmd}"
        if cmd == ["./starnet2", "--machine-info"]:
            return self.rc, self.info
        raise AssertionError(f"unexpected command {cmd}")


def sn_folder(root: Path, kind: str) -> Path:
    d = root / kind
    d.mkdir(parents=True)
    exe = d / kind
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    (d / ("starnet2_weights.pb" if kind == "starnet++" else "StarNet2_weights.onnx")).write_bytes(b"w")
    return d


def test_doctor_linux_legacy_starnet_skips_macos_checks(tmp_path):
    sysm = LinuxSystem()
    r = doctor.check_starnet(str(sn_folder(tmp_path, "starnet++")), run=sysm, env={}, system="linux")
    assert r.status == OK and "starnet++" in r.detail and sysm.calls == []


def test_doctor_starnet2_machine_info(tmp_path):
    d = sn_folder(tmp_path, "starnet2")
    sysm = LinuxSystem()
    r = doctor.check_starnet(str(d), run=sysm, env={}, system="linux")
    assert r.status == OK and "StarNet2 2.6.2 build 0242, backend ort-cpu" in r.detail
    assert sysm.calls == [(["./starnet2", "--machine-info"], d.resolve())]
    r = doctor.check_starnet(str(d), run=LinuxSystem(info="./starnet2: GLIBC_2.38 not found", rc=1), env={},
                             system="linux")
    assert r.status == FAIL and "GLIBC_2.38" in r.detail and "glibc 2.38" in r.fix
    r = doctor.check_starnet(str(d), run=LinuxSystem(info='{"product":"starnet2","version":"3.0.0"}'), env={},
                             system="linux")
    assert r.status == WARN and "verified with StarNet2 2.6" in r.detail
    (d / "StarNet2_weights.onnx").unlink()
    assert doctor.check_starnet(str(d), run=LinuxSystem(), env={}, system="linux").status == FAIL


def test_doctor_starnet2_quarantined_on_macos_is_not_run(tmp_path):
    d = sn_folder(tmp_path, "starnet2")
    (d / "StarNet2_weights.onnx").rename(d / "StarNet2_weights.mlpackage")
    (d / "lib").mkdir()
    (d / "lib" / "libx.dylib").write_bytes(b"d")
    ran = []

    def mac(cmd, timeout=15, cwd=None):
        ran.append(cmd)
        if cmd[0] == "file":
            return 0, "Mach-O 64-bit executable arm64"
        if cmd[0] == "xattr":
            assert cmd[1] == "-p"
            return (0, "q") if cmd[-1].endswith("libx.dylib") else (1, "No such xattr: com.apple.quarantine")
        if cmd[0] == "spctl":
            return 3, f"{cmd[-1]}: rejected\n"
        if cmd[0] == "./starnet2":
            return 0, '{"product":"starnet2","version":"2.6.2","build":"0242","backend":"coreml"}'
        raise AssertionError(cmd)
    r = doctor.check_starnet(str(d), run=mac, env={}, system="darwin")
    assert r.status == FAIL and "lib/libx.dylib" in r.detail and "your decision" in r.fix
    assert not any(c[0] == "./starnet2" for c in ran)
    (d / "lib" / "libx.dylib").unlink()
    r = doctor.check_starnet(str(d), run=mac, env={}, system="darwin")
    assert r.status == OK and "backend coreml" in r.detail


def test_doctor_starnet_missing_and_cache_install(tmp_path):
    r = doctor.check_starnet(None, env={}, system="linux", installs=lambda: [])
    assert r.status == WARN and r.detail == "not installed: run `darkroom get-starnet`"
    from darkroom import starnet
    d = sn_folder(tmp_path, "starnet2")
    r = doctor.check_starnet(None, run=LinuxSystem(), env={}, system="linux",
                             installs=lambda: [starnet.Backend(d, "starnet2", "2.6.2", "0242")])
    assert r.status == OK and "installed by darkroom get-starnet" in r.detail


def test_doctor_system_check():
    r = doctor.check_system("linux", "x86_64", glibc=lambda: (2, 41), container=lambda: True)
    assert r.status == OK and r.detail == "Linux x86_64, glibc 2.41, inside a container"
    r = doctor.check_system("linux", "x86_64", glibc=lambda: (2, 36), container=lambda: False)
    assert r.status == WARN and "2.38" in r.detail
    r = doctor.check_system("darwin", "arm64", container=lambda: False, mac_ver=lambda: ("27.0", "", ""))
    assert r.status == OK and r.detail == "macOS 27.0 arm64"
    assert doctor.in_container([Path("/definitely/not/here")]) is False


class FakeOrt:
    def __init__(self, available):
        self.available = available

    def set_default_logger_severity(self, n):
        pass

    def get_available_providers(self):
        return self.available

    class SessionOptions:
        log_severity_level = 0

    def InferenceSession(self, model, sess_options=None, providers=()):
        names = [p[0] if isinstance(p, tuple) else p for p in providers]

        class S:
            def run(self, out, feed):
                return [feed["gen_input_image"] * 0.5]

            def get_providers(self):
                return names
        return S()


def test_doctor_gpu_linux(tmp_path):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"m")
    r = doctor.check_gpu(model, ort=FakeOrt(["CPUExecutionProvider"]), build=lambda: model, system="linux")
    assert r.status == WARN and "denoise will be slow" in r.detail and "CPU only" in r.detail
    r = doctor.check_gpu(model, ort=FakeOrt(["CUDAExecutionProvider", "CPUExecutionProvider"]),
                         build=lambda: model, system="linux")
    assert "providers CUDA+CPU" in r.detail and r.status in (OK, WARN)     # WARN only if not clearly faster
    r = doctor.check_gpu(model, ort=FakeOrt(["CoreMLExecutionProvider", "CPUExecutionProvider"]),
                         build=lambda: model, system="darwin")
    assert "providers CoreML+CPU" in r.detail
