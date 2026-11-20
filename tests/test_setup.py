"""`darkroom setup` and the tool manifest, with a fake HTTP layer, a fake command runner (hdiutil,
ditto, codesign, xattr, spctl, AppImage extraction) and stand-in files: nothing is downloaded,
mounted, executed or installed outside temporary folders."""
import argparse
import dataclasses
import hashlib
import io
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from darkroom import cli, doctor, installer, tools, toolset
from darkroom.doctor import FAIL, OK, Result

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- helpers

def args(**kw):
    base = dict(force=False, from_=None, only=None, no_models=True, no_starnet=True, allow_unnotarized=False,
                starnet_dir=None, offline=False, dry_run=False)
    base.update(kw)
    return argparse.Namespace(**base)


class Resp:
    def __init__(self, url, data, final=None, length="auto"):
        self.url, self._b, self.final = url, io.BytesIO(data), final or url
        self.headers = {} if length is None else {"Content-Length": str(len(data) if length == "auto" else length)}

    def read(self, n=-1):
        return self._b.read(n)

    def geturl(self):
        return self.final

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    def __init__(self, files: dict, final: dict | None = None):
        self.files, self.final, self.calls = files, final or {}, []

    def __call__(self, url, timeout=None):
        self.calls.append(url)
        return Resp(url, self.files[url], self.final.get(url))


def pin(monkeypatch, tool, plat, data: bytes):
    """Point the manifest entry at stand-in bytes (same URL and file name)."""
    b = toolset.MANIFEST[(tool, plat)]
    nb = dataclasses.replace(b, size=len(data), sha256=hashlib.sha256(data).hexdigest())
    monkeypatch.setitem(toolset.MANIFEST, (tool, plat), nb)
    return nb


def plist(version):
    return plistlib.dumps({"CFBundleShortVersionString": version})


class FakeMac:
    """Stands in for hdiutil, ditto, codesign, xattr and spctl."""

    def __init__(self, notarized=("Siril.app",), codesign_rc=0):
        self.calls, self.xattrs, self.mounted = [], {}, set()
        self.notarized, self.codesign_rc = set(notarized), codesign_rc
        self.executed = []

    def __call__(self, cmd, timeout=None, cwd=None):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        prog = cmd[0]
        if prog == "hdiutil" and cmd[1] == "attach":
            mnt, dmg = Path(cmd[cmd.index("-mountpoint") + 1]), cmd[-1]
            name = "Siril.app" if "siril" in Path(dmg).name.lower() else "GraXpert.app"
            exe = "siril-cli" if name == "Siril.app" else "GraXpert"
            m = mnt / name / "Contents" / "MacOS"
            m.mkdir(parents=True)
            (m / exe).write_text("#!/bin/sh\necho stand-in\n")
            (m / exe).chmod(0o755)
            (mnt / name / "Contents" / "Info.plist").write_bytes(
                plist("1.4.5" if name == "Siril.app" else "Beta-Release (v1.0.6cAI)"))
            os.symlink("/Applications", mnt / "Applications")
            self.mounted.add(str(mnt))
            return 0, ""
        if prog == "hdiutil" and cmd[1] == "detach":
            mnt = Path(cmd[-1])
            for p in mnt.iterdir():
                (p.unlink() if p.is_symlink() else shutil.rmtree(p))
            self.mounted.discard(str(mnt))
            return 0, ""
        if prog == "ditto":
            shutil.copytree(cmd[1], cmd[2], symlinks=True)
            return 0, ""
        if prog == "codesign":
            return self.codesign_rc, "" if self.codesign_rc == 0 else "invalid signature"
        if prog == "xattr" and cmd[1] == "-w":
            assert cmd[2] == "-r" and cmd[3] == toolset.QUARANTINE
            root = Path(cmd[5])
            for p in [root, *root.rglob("*")]:
                self.xattrs[str(p)] = cmd[4]
            return 0, ""
        if prog == "xattr" and cmd[1] == "-p":
            # xattrs move with the files when the staged install is renamed into place
            moved = {re.sub(r"/\.incoming-[^/]+/", "/", k): v for k, v in self.xattrs.items()}
            v = self.xattrs.get(cmd[3]) or moved.get(cmd[3])
            return (0, v + "\n") if v else (1, "No such xattr")
        if prog == "spctl":
            target = Path(cmd[-1])
            if target.name in self.notarized:
                return 0, f"{target}: accepted\nsource=Notarized Developer ID\n"
            return 3, f"{target}: rejected\n"
        self.executed.append(cmd)
        raise AssertionError(f"unexpected command {cmd}")


def mac_ctx(run, opener=None, interactive=False, ask=None, out=None):
    return installer.Ctx(plat="macos-arm64", run=run, opener=opener, interactive=interactive,
                         ask=ask or (lambda q: pytest.fail("must not ask")), out=out or (lambda m: None))


@pytest.fixture
def absent(monkeypatch):
    """No working Siril or GraXpert is found, and doctor's checks are not run."""
    monkeypatch.setattr(installer, "check_existing",
                        lambda tool, path=None: Result(tool, FAIL, "/nowhere is not an executable", "x"))
    for v in ("DARKROOM_SIRIL", "DARKROOM_GRAXPERT"):
        monkeypatch.delenv(v, raising=False)


def no_doctor(ns):
    return []


def tools_root() -> Path:
    return toolset.tools_dir()


def assert_clean(root: Path, expect: set[str]):
    """Exactly these installs and nothing else (no .incoming-* or .old-* left behind)."""
    assert ({p.name for p in root.iterdir()} if root.exists() else set()) == expect


# ---------------------------------------------------------------- manifest and platform

def test_platform_keys():
    assert toolset.platform_key("darwin", "arm64") == "macos-arm64"
    assert toolset.platform_key("darwin", "x86_64", rosetta=lambda: False) == "macos-x86_64"
    assert toolset.platform_key("darwin", "x86_64", rosetta=lambda: True) == "macos-arm64"
    assert toolset.platform_key("linux", "x86_64") == "linux-x86_64"
    for system, machine in (("linux", "aarch64"), ("win32", "AMD64"), ("freebsd14", "amd64")):
        with pytest.raises(tools.StageError, match=rf"{system} {machine.lower()}.*Docker image"):
            toolset.platform_key(system, machine)


def test_manifest_is_complete_and_official():
    for plat in toolset.PLATFORM_NAMES:
        for tool in toolset.TOOLS:
            b = toolset.build_for(tool, plat)
            installer.check_url(b.url)
            assert re.fullmatch(r"[0-9a-f]{64}", b.sha256) and b.size > 10_000_000
            assert (b.kind == "dmg") == plat.startswith("macos")
    assert toolset.build_for("siril", "macos-arm64").notarized is True
    assert toolset.build_for("graxpert", "macos-arm64").notarized is False


def test_dockerfile_pins_equal_manifest():
    text = (REPO / "Dockerfile").read_text()
    arg = {m.group(1): m.group(2) for m in re.finditer(r"^ARG (\w+)=(\S+)$", text, re.M)}
    siril, graxpert = (toolset.build_for(t, "linux-x86_64") for t in ("siril", "graxpert"))
    assert arg["SIRIL_URL"] == siril.url and arg["SIRIL_SHA256"] == siril.sha256
    assert arg["GRAXPERT_URL"] == graxpert.url and arg["GRAXPERT_SHA256"] == graxpert.sha256


def test_tools_dir_locations(monkeypatch):
    monkeypatch.delenv("DARKROOM_TOOLS_DIR")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert toolset.default_tools_dir("darwin") == "~/Library/Application Support/darkroom/tools"
    assert toolset.default_tools_dir("linux") == "~/.local/share/darkroom/tools"
    monkeypatch.setenv("XDG_DATA_HOME", "/data")
    assert toolset.default_tools_dir("linux") == "/data/darkroom/tools"
    monkeypatch.setenv("DARKROOM_TOOLS_DIR", "/somewhere/tools")
    assert toolset.tools_dir() == Path("/somewhere/tools")


# ---------------------------------------------------------------- resolution

def fake_install(root: Path, tool="siril", plat="macos-arm64", version=None, exe=None, make_exe=True):
    b = toolset.build_for(tool, plat)
    d = root / f"{tool}-{version or b.version}"
    rel = exe or b.exe
    if make_exe:
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text("#!/bin/sh\n")
        (d / rel).chmod(0o755)
    d.mkdir(parents=True, exist_ok=True)
    (d / toolset.RECORD).write_text(json.dumps({"tool": tool, "version": version or b.version, "platform": plat,
                                                "exe": rel, "installed_at": "2026-01-01T00:00:00Z"}))
    return d


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(toolset, "platform_key", lambda *a, **k: "macos-arm64")
    for v in ("DARKROOM_SIRIL", "DARKROOM_GRAXPERT"):
        monkeypatch.delenv(v, raising=False)
    return monkeypatch


def test_macos_default_unchanged_without_managed_install(mac):
    assert tools.resolve_tool("siril") == (Path("/Applications/Siril.app/Contents/MacOS/siril-cli"),
                                           "platform default")
    assert tools.resolve_tool("graxpert") == (Path("/Applications/GraXpert.app/Contents/MacOS/GraXpert"),
                                              "platform default")
    assert tools.siril_cli() == Path(tools.DEFAULT_SIRIL) and tools.graxpert() == Path(tools.DEFAULT_GRAXPERT)


def test_resolution_precedence(mac):
    root = tools_root()
    d = fake_install(root, "siril")
    exe, source = tools.resolve_tool("siril")
    assert exe == d / "Siril.app/Contents/MacOS/siril-cli" and source.startswith("installed by darkroom setup")
    mac.setenv("DARKROOM_SIRIL", "/opt/elsewhere/siril-cli")
    assert tools.resolve_tool("siril") == (Path("/opt/elsewhere/siril-cli"), "$DARKROOM_SIRIL")
    assert tools.graxpert() == Path(tools.DEFAULT_GRAXPERT)        # only siril is managed


def test_managed_ignores_other_platforms_missing_exes_and_prefers_pinned(mac):
    root = tools_root()
    fake_install(root, "graxpert", plat="linux-x86_64")
    fake_install(root, "siril", make_exe=False)
    (root / ".incoming-siril-x").mkdir()
    assert toolset.managed("graxpert") is None and toolset.managed("siril") is None
    old = fake_install(root, "siril", version="1.4.4")
    assert toolset.managed("siril").dir == old
    pinned = fake_install(root, "siril")
    assert toolset.managed("siril").dir == pinned


def test_linux_managed_takes_precedence_over_path(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(toolset, "platform_key", lambda *a, **k: "linux-x86_64")
    monkeypatch.delenv("DARKROOM_SIRIL", raising=False)
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/" + n)
    assert tools.siril_cli() == Path("/usr/bin/siril-cli")
    d = fake_install(tools_root(), "siril", plat="linux-x86_64")
    assert tools.siril_cli() == d / "siril-cli"


# ---------------------------------------------------------------- Gatekeeper helpers

def test_gatekeeper_block_rules(tmp_path):
    app = tmp_path / "GraXpert.app"
    exe = app / "Contents/MacOS/GraXpert"
    exe.parent.mkdir(parents=True)
    exe.write_text("x")
    fake = FakeMac()
    assert toolset.gatekeeper_block(exe, fake, system="darwin") is None          # not quarantined
    fake.xattrs[str(exe)] = "0081;6a000000;darkroom;"
    msg = toolset.gatekeeper_block(exe, fake, system="darwin")
    assert "not notarized" in msg and f"xattr -dr com.apple.quarantine '{app}'" in msg and "Open Anyway" in msg
    assert ["spctl", "-a", "-t", "exec", "-vv", str(app)] in fake.calls
    fake.xattrs[str(exe)] = "01c3;6a000000;Safari;UUID"                             # the user allowed it
    assert toolset.gatekeeper_block(exe, fake, system="darwin") is None
    assert toolset.gatekeeper_block(exe, lambda *a, **k: pytest.fail("no calls on Linux"), system="linux") is None
    assert not any(c[:2] in (["xattr", "-d"], ["xattr", "-c"]) or "-dr" in c for c in fake.calls)


def test_require_executable_refuses_blocked_binary_on_macos(tmp_path, monkeypatch):
    exe = tmp_path / "GraXpert"
    exe.write_text("x")
    exe.chmod(0o755)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(toolset, "gatekeeper_block", lambda p: f"{p} is quarantined; darkroom did not run it")
    with pytest.raises(tools.StageError, match="did not run it"):
        tools.require_executable(exe, "DARKROOM_GRAXPERT")
    monkeypatch.setattr(toolset, "gatekeeper_block", lambda p: None)
    assert tools.require_executable(exe, "DARKROOM_GRAXPERT") == exe


def test_quarantine_stamp_format():
    assert installer.quarantine_stamp(0x6a000000) == "0081;6a000000;darkroom;"
    assert not toolset.user_approved(installer.quarantine_stamp())


# ---------------------------------------------------------------- macOS installs

def test_macos_siril_download_and_install(mac, absent):
    data = b"stand-in siril dmg"
    b = pin(mac, "siril", "macos-arm64", data)
    fake, opener, msgs = FakeMac(), Opener({b.url: data}), []
    rc = installer.run(args(only="siril"), mac_ctx(fake, opener, out=msgs.append), doctor_checks=no_doctor)
    assert rc == 0 and opener.calls == [b.url]
    dest = tools_root() / "siril-1.4.5"
    assert_clean(tools_root(), {"siril-1.4.5"})
    rec = json.loads((dest / "installed.json").read_text())
    assert rec["tool"] == "siril" and rec["sha256"] == b.sha256 and rec["notarized"] is True
    assert rec["quarantined"] is True and re.fullmatch(r"0081;[0-9a-f]{8};darkroom;", rec["quarantine"])
    assert rec["exe"] == "Siril.app/Contents/MacOS/siril-cli" and rec["gatekeeper"] == "Notarized Developer ID"
    progs = [c[0] + " " + c[1] for c in fake.calls]
    order = [p for p in progs if p.split()[0] in ("hdiutil", "ditto", "codesign", "spctl") or p == "xattr -w"]
    assert order == ["hdiutil attach", "ditto " + order[1].split()[1], "hdiutil detach", "codesign --verify",
                     "xattr -w", "spctl -a"]
    attach = fake.calls[0]
    assert {"-nobrowse", "-readonly", "-noautoopen", "-mountpoint"} <= set(attach)
    assert not fake.mounted and not fake.executed
    assert not any(c[0] == "xattr" and c[1] in ("-d", "-c") for c in fake.calls)
    assert tools.resolve_tool("siril")[0] == dest / "Siril.app/Contents/MacOS/siril-cli"


def test_notarized_app_rejected_by_gatekeeper_is_not_installed(mac, absent):
    data = b"dmg"
    b = pin(mac, "siril", "macos-arm64", data)
    fake, msgs = FakeMac(notarized=()), []
    rc = installer.run(args(only="siril"), mac_ctx(fake, Opener({b.url: data}), out=msgs.append),
                       doctor_checks=no_doctor)
    assert rc == 1 and any("Gatekeeper does not accept" in m for m in msgs)
    assert_clean(tools_root(), set())
    assert not fake.mounted


def test_codesign_failure_leaves_nothing_and_keeps_old_install(mac, absent):
    data = b"dmg"
    b = pin(mac, "siril", "macos-arm64", data)
    old = fake_install(tools_root(), "siril")
    rc = installer.run(args(only="siril", force=True), mac_ctx(FakeMac(codesign_rc=1), Opener({b.url: data})),
                       doctor_checks=no_doctor)
    assert rc == 1
    assert_clean(tools_root(), {"siril-1.4.5"})
    assert json.loads((old / "installed.json").read_text())["installed_at"] == "2026-01-01T00:00:00Z"


def test_force_replaces_managed_install(mac, absent):
    data = b"dmg"
    b = pin(mac, "siril", "macos-arm64", data)
    fake_install(tools_root(), "siril")
    rc = installer.run(args(only="siril", force=True), mac_ctx(FakeMac(), Opener({b.url: data})),
                       doctor_checks=no_doctor)
    assert rc == 0
    assert_clean(tools_root(), {"siril-1.4.5"})
    assert json.loads((tools_root() / "siril-1.4.5/installed.json").read_text())["notarized"] is True


def test_skip_when_present_and_force(mac, monkeypatch):
    monkeypatch.setattr(installer, "check_existing",
                        lambda tool, path=None: Result(tool, OK, f"{toolset.TOOL_NAMES[tool]} 9 at /x (platform default)"))
    msgs = []
    rc = installer.run(args(), mac_ctx(FakeMac(), Opener({}), out=msgs.append), doctor_checks=no_doctor)
    assert rc == 0 and sum("nothing to install" in m for m in msgs) == 2
    assert_clean(tools_root(), set())
    data = b"dmg"
    b = pin(mac, "siril", "macos-arm64", data)
    opener = Opener({b.url: data})
    assert installer.run(args(only="siril", force=True), mac_ctx(FakeMac(), opener), doctor_checks=no_doctor) == 0
    assert opener.calls == [b.url] and (tools_root() / "siril-1.4.5").is_dir()


def test_blocked_managed_graxpert_is_not_reinstalled(mac, monkeypatch):
    monkeypatch.setattr(installer, "check_existing", lambda tool, path=None: Result(
        tool, FAIL, "/t/GraXpert: quarantined by macOS and not notarized (rejected); doctor did not execute it",
        "Allowing it is your decision"))
    opener, msgs = Opener({}), []
    rc = installer.run(args(only="graxpert"), mac_ctx(FakeMac(), opener, out=msgs.append), doctor_checks=no_doctor)
    assert rc == 1 and opener.calls == [] and any("Not reinstalling" in m for m in msgs)


@pytest.mark.parametrize("answer, installed", [("yes", True), ("YES ", True), ("y", False), ("no", False), ("", False)])
def test_unnotarized_graxpert_needs_yes(mac, absent, answer, installed):
    data = b"graxpert dmg"
    b = pin(mac, "graxpert", "macos-arm64", data)
    fake, opener, asked, msgs = FakeMac(), Opener({b.url: data}), [], []

    def ask(q):
        asked.append(q)
        assert opener.calls == [], "asks before downloading"
        return answer
    rc = installer.run(args(only="graxpert"), mac_ctx(fake, opener, interactive=True, ask=ask, out=msgs.append),
                       doctor_checks=no_doctor)
    assert len(asked) == 1 and 'Type "yes"' in asked[0]
    assert any("not notarized" in m for m in msgs)
    if installed:
        assert rc == 0 and opener.calls == [b.url]
        rec = json.loads((tools_root() / "graxpert-3.1.0rc2/installed.json").read_text())
        assert rec["notarized"] is False and rec["quarantined"] is True
        assert any("Open Anyway" in m and "xattr -dr com.apple.quarantine" in m for m in msgs)
    else:
        assert rc == 1 and opener.calls == []
        assert_clean(tools_root(), set())
    assert not fake.executed and not any(c[0] == "xattr" and c[1] in ("-d", "-c") for c in fake.calls)


def test_unnotarized_noninteractive_refuses_without_flag(mac, absent):
    data = b"graxpert dmg"
    b = pin(mac, "graxpert", "macos-arm64", data)
    opener, msgs = Opener({b.url: data}), []
    rc = installer.run(args(only="graxpert"), mac_ctx(FakeMac(), opener, out=msgs.append), doctor_checks=no_doctor)
    assert rc == 1 and opener.calls == [] and any("--allow-unnotarized" in m for m in msgs)
    assert_clean(tools_root(), set())
    rc = installer.run(args(only="graxpert", allow_unnotarized=True), mac_ctx(FakeMac(), opener),
                       doctor_checks=no_doctor)
    assert rc == 0 and (tools_root() / "graxpert-3.1.0rc2").is_dir()


def test_doctor_reports_quarantined_managed_graxpert_without_running_it(mac, absent):
    data = b"graxpert dmg"
    b = pin(mac, "graxpert", "macos-arm64", data)
    fake = FakeMac()
    assert installer.run(args(only="graxpert", allow_unnotarized=True), mac_ctx(fake, Opener({b.url: data})),
                         doctor_checks=no_doctor) == 0
    exe = tools_root() / "graxpert-3.1.0rc2/GraXpert.app/Contents/MacOS/GraXpert"
    assert tools.graxpert() == exe
    r = doctor.check_graxpert(run=fake, system="darwin")      # fake raises on anything it does not know
    assert r.status == FAIL and "quarantined" in r.detail and "not notarized" in r.detail
    assert "did not execute" in r.detail and "Open Anyway" in r.fix
    assert not fake.executed and not any(c[0] == str(exe) for c in fake.calls)


def test_doctor_runs_quarantined_notarized_siril_and_names_source(mac, absent):
    data = b"dmg"
    b = pin(mac, "siril", "macos-arm64", data)
    fake = FakeMac()
    installer.run(args(only="siril"), mac_ctx(fake, Opener({b.url: data})), doctor_checks=no_doctor)
    exe = tools_root() / "siril-1.4.5/Siril.app/Contents/MacOS/siril-cli"

    def run(cmd, timeout=None, cwd=None):
        if cmd[0] == str(exe):
            return 0, "siril 1.4.5\n"
        return fake(cmd, timeout, cwd)
    r = doctor.check_siril(run=run, system="darwin")
    assert r.status == OK and "installed by darkroom setup" in r.detail and "Notarized Developer ID" in r.detail


# ---------------------------------------------------------------- --from and downloads

def test_from_dir_checks_name_size_and_hash(mac, absent, tmp_path):
    data = b"the official dmg"
    b = pin(mac, "siril", "macos-arm64", data)
    src = tmp_path / "dl"
    src.mkdir()
    opener = Opener({})
    msgs = []

    def go():
        msgs.clear()
        return installer.run(args(only="siril", from_=str(src)), mac_ctx(FakeMac(), opener, out=msgs.append),
                             doctor_checks=no_doctor)
    (src / "siril.dmg").write_bytes(data)                       # wrong name
    assert go() == 1 and any(f"{b.filename} not found" in m for m in msgs)
    (src / b.filename).write_bytes(data + b"x")                 # wrong size
    assert go() == 1 and any("pinned size" in m for m in msgs)
    (src / b.filename).write_bytes(b"X" * len(data))            # same size, wrong hash
    assert go() == 1 and any("SHA-256 mismatch" in m for m in msgs)
    assert_clean(tools_root(), set())
    (src / b.filename).write_bytes(data)
    assert go() == 0 and opener.calls == []
    assert json.loads((tools_root() / "siril-1.4.5/installed.json").read_text())["source"].startswith("local file")


def test_from_must_be_a_directory(tmp_path):
    with pytest.raises(tools.StageError, match="not a directory"):
        installer.run(args(from_=str(tmp_path / "nope")), mac_ctx(FakeMac()))


def test_download_checks(monkeypatch, tmp_path):
    data = b"payload"
    b = pin(monkeypatch, "graxpert", "macos-arm64", data)
    gh = "https://release-assets.githubusercontent.com/github-production-release-asset/1/2"
    assert installer.download(b, tmp_path / "a", Opener({b.url: data}, {b.url: gh}), log=lambda m: None) == b.sha256
    with pytest.raises(tools.StageError, match="redirected"):
        installer.download(b, tmp_path / "b", Opener({b.url: data}, {b.url: "https://evil.example/x"}),
                           log=lambda m: None)
    s = toolset.build_for("siril", "macos-arm64")
    sp = pin(monkeypatch, "siril", "macos-arm64", data)
    with pytest.raises(tools.StageError, match="redirected"):    # free-astro.org: no redirects at all
        installer.download(sp, tmp_path / "c", Opener({s.url: data}, {s.url: gh}), log=lambda m: None)
    with pytest.raises(tools.StageError, match="SHA-256 mismatch"):
        installer.download(sp, tmp_path / "d", Opener({s.url: b"PAYLOAD"}), log=lambda m: None)

    class Big(Opener):
        def __call__(self, url, timeout=None):
            return Resp(url, data, length=len(data) + 1)
    with pytest.raises(tools.StageError, match="pinned size"):
        installer.download(sp, tmp_path / "e", Big({}), log=lambda m: None)


def test_url_and_redirect_policy():
    for bad in ("http://free-astro.org/download/x.dmg", "https://evil.example/siril.dmg",
                "https://github.com/someone/else/releases/download/1/x.zip",
                "https://free-astro.org/download/x.dmg?y=1"):
        with pytest.raises(tools.StageError):
            installer.check_url(bad)
    assert installer.allowed_redirects(toolset.build_for("graxpert", "linux-x86_64").url) == \
        {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
    assert installer.allowed_redirects(toolset.build_for("siril", "linux-x86_64").url) == set()


def test_refused_redirect_names_the_host(monkeypatch, tmp_path):
    h = installer._Redirects(installer.GITHUB_REDIRECT_HOSTS)
    with pytest.raises(installer.RedirectRefused) as e:
        h.redirect_request(None, None, 302, "Found", {}, "https://assets.github-cdn.example/x.zip")
    msg = installer._short(e.value)
    assert "https://assets.github-cdn.example" in msg and "release-assets.githubusercontent.com" in msg
    assert "needs an update" in msg
    b = pin(monkeypatch, "graxpert", "macos-arm64", b"payload")
    with pytest.raises(tools.StageError, match=r"redirected to https://new-host\.example; refused \(darkroom follows "
                                               r"redirects only to github\.com"):
        installer.download(b, tmp_path / "a", Opener({b.url: b"payload"}, {b.url: "https://new-host.example/x"}),
                           log=lambda m: None)
    s = pin(monkeypatch, "siril", "macos-arm64", b"payload")
    with pytest.raises(tools.StageError, match="follows no redirects for this download"):
        installer.download(s, tmp_path / "b", Opener({s.url: b"payload"}, {s.url: "https://mirror.example/x"}),
                           log=lambda m: None)


def test_bare_executables_are_assessed_by_their_signature(tmp_path):
    exe = tmp_path / "siril-cli"
    exe.write_text("x")
    assert toolset.assess_command(tmp_path / "Siril.app")[:4] == ["spctl", "-a", "-t", "exec"]
    assert toolset.assess_command(exe) == ["spctl", "-a", "-t", "open", "--context", "context:primary-signature",
                                           "-vv", str(exe)]
    fake = FakeMac(notarized=())
    fake.xattrs[str(exe)] = "0081;6a000000;darkroom;"
    msg = toolset.gatekeeper_block(exe, fake, system="darwin")
    assert "not notarized" in msg and f"run it once yourself in Terminal ('{exe}'" in msg


@pytest.mark.parametrize("answer", [subprocess.TimeoutExpired(["xattr"], 5), FileNotFoundError("xattr"),
                                    (1, "xattr: [Errno 13] Permission denied")])
def test_unreadable_quarantine_flag_is_not_run(tmp_path, answer):
    exe = tmp_path / "GraXpert"
    exe.write_text("x")

    def run(cmd, timeout=None):
        assert cmd[:2] == ["xattr", "-p"], "nothing else may run when the flag cannot be read"
        if isinstance(answer, BaseException):
            raise answer
        return answer
    assert toolset.quarantine_probe(exe, run)[0] == "unknown"
    msg = toolset.gatekeeper_block(exe, run, system="darwin")
    assert msg and "could not tell whether macOS has quarantined" in msg and "did not run it" in msg
    assert toolset.quarantine_probe(exe, lambda c, timeout=None: (1, "No such xattr: com.apple.quarantine")) == \
        ("none", "")


# ---------------------------------------------------------------- Linux installs

class FakeLinux:
    def __init__(self, extract_ok=True, ldconfig="", ldconfig_rc=0):
        self.calls, self.extract_ok, self.ldconfig, self.ldconfig_rc = [], extract_ok, ldconfig, ldconfig_rc

    def __call__(self, cmd, timeout=None, cwd=None):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        if cmd[-1] == "--appimage-extract":
            if not self.extract_ok:
                return 1, "cannot run"
            root = Path(cwd) / "squashfs-root"
            (root / "usr/bin").mkdir(parents=True)
            (root / "AppRun").write_text("#!/bin/sh\n")
            (root / "AppRun").chmod(0o755)
            return 0, ""
        if cmd[1:] == ["-p"]:
            return self.ldconfig_rc, self.ldconfig
        raise AssertionError(f"unexpected command {cmd}")


def linux_ctx(run, opener=None, out=None):
    return installer.Ctx(plat="linux-x86_64", run=run, opener=opener, out=out or (lambda m: None))


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(toolset, "platform_key", lambda *a, **k: "linux-x86_64")
    return monkeypatch


def test_linux_siril_appimage_and_wrapper(linux, absent):
    data = b"\x7fELF stand-in appimage"
    b = pin(linux, "siril", "linux-x86_64", data)
    fake = FakeLinux()
    assert installer.run(args(only="siril"), linux_ctx(fake, Opener({b.url: data})), doctor_checks=no_doctor) == 0
    dest = tools_root() / "siril-1.4.5"
    assert sorted(p.name for p in dest.iterdir()) == ["appdir", "installed.json", "siril-cli"]
    wrapper = (dest / "siril-cli").read_text()
    assert 'APPDIR="$here/appdir"' in wrapper and 'exec "$APPDIR/AppRun" siril-cli "$@"' in wrapper
    assert os.access(dest / "siril-cli", os.X_OK) and (dest / "appdir/AppRun").is_file()
    assert json.loads((dest / "installed.json").read_text())["extracted_with"] == "--appimage-extract"
    assert tools.siril_cli() == dest / "siril-cli"
    assert_clean(tools_root(), {"siril-1.4.5"})


def test_linux_appimage_extract_failure_without_unsquashfs(linux, absent):
    data = b"\x7fELF stand-in"
    b = pin(linux, "siril", "linux-x86_64", data)
    linux.setattr(shutil, "which", lambda n: None)
    msgs = []
    rc = installer.run(args(only="siril"), linux_ctx(FakeLinux(extract_ok=False), Opener({b.url: data}),
                                                     out=msgs.append), doctor_checks=no_doctor)
    assert rc == 1 and any("squashfs-tools" in m for m in msgs)
    assert_clean(tools_root(), set())


def graxpert_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data, mode in (("GraXpert-linux/GraXpert", "#!/bin/sh\n", 0o755),
                                 ("GraXpert-linux/lib/libx.so", "lib", 0o644)):
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFREG | mode) << 16
            z.writestr(info, data)
    return buf.getvalue()


def test_linux_graxpert_zip_and_x11_hint(linux, absent):
    data = graxpert_zip()
    b = pin(linux, "graxpert", "linux-x86_64", data)
    ld = "\tlibX11.so.6 (libc6,x86-64) => /lib/libX11.so.6\n\tlibGL.so.1 (libc6,x86-64) => /lib/libGL.so.1\n"
    msgs = []
    rc = installer.run(args(only="graxpert"), linux_ctx(FakeLinux(ldconfig=ld), Opener({b.url: data}), msgs.append),
                       doctor_checks=no_doctor)
    exe = tools_root() / "graxpert-3.1.0rc2/GraXpert-linux/GraXpert"
    assert exe.is_file() and os.access(exe, os.X_OK) and tools.graxpert() == exe
    hint = [m for m in msgs if "apt-get install" in m]
    assert hint and "libxft2" in hint[0] and "libx11-6" not in hint[0] and "libgl1 " not in hint[0] + " "
    assert rc == 1                                   # installed, but not usable until the libraries are there


def test_linux_graxpert_all_x11_present(linux, absent):
    data = graxpert_zip()
    b = pin(linux, "graxpert", "linux-x86_64", data)
    ld = "".join(f"\t{lib} (libc6,x86-64) => /lib/{lib}\n" for lib in installer.X11_LIBS)
    msgs = []
    rc = installer.run(args(only="graxpert"), linux_ctx(FakeLinux(ldconfig=ld), Opener({b.url: data}), msgs.append),
                       doctor_checks=no_doctor)
    assert rc == 0 and not any("apt-get" in m for m in msgs)


def test_unzip_refuses_unsafe_paths(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil", "x")
    (tmp_path / "e.zip").write_bytes(buf.getvalue())
    with pytest.raises(tools.StageError, match="unsafe path"):
        installer.unzip(tmp_path / "e.zip", tmp_path / "out")


def test_squashfs_offset():
    head = bytearray(64)
    head[:4] = b"\x7fELF"
    head[0x28:0x30] = (1000).to_bytes(8, "little")
    head[0x3A:0x3C] = (64).to_bytes(2, "little")
    head[0x3C:0x3E] = (5).to_bytes(2, "little")
    import tempfile
    with tempfile.NamedTemporaryFile() as f:
        f.write(bytes(head))
        f.flush()
        assert installer.squashfs_offset(Path(f.name)) == 1000 + 64 * 5


# ---------------------------------------------------------------- StarNet offer, models, dry run, CLI

def test_starnet_offer_never_passes_acceptance(mac, absent):
    seen = []

    def get_starnet_run(ns, **kw):
        seen.append((ns, kw))
        raise tools.StageError("licence not accepted: nothing was installed")
    asked = []
    ctx = mac_ctx(FakeMac(), Opener({}), interactive=True, ask=lambda q: asked.append(q) or "yes")
    rc = installer.run(args(only="siril", no_starnet=False), ctx, get_starnet_run=get_starnet_run,
                       doctor_checks=no_doctor)
    # siril install fails here (no download stub), but the StarNet offer still runs
    assert len(seen) == 1 and seen[0][0].accept_license is False and seen[0][1]["interactive"] is True
    assert "licence will be shown and you must accept it" in asked[-1]
    assert rc == 1


def test_starnet_offer_skipped_when_noninteractive_found_or_declined(mac, monkeypatch, tmp_path):
    def never(ns, **kw):
        pytest.fail("get-starnet must not run")
    msgs = []
    installer.offer_starnet(mac_ctx(FakeMac(), out=msgs.append), None, never)
    assert any("not an interactive terminal" in m and "darkroom get-starnet" in m for m in msgs)
    installer.offer_starnet(mac_ctx(FakeMac(), interactive=True, ask=lambda q: "n"), None, never)
    monkeypatch.setenv("DARKROOM_STARNET_DIR", str(tmp_path))
    installer.offer_starnet(mac_ctx(FakeMac(), interactive=True), None, never)


def test_no_starnet_and_no_models_flags(mac, monkeypatch):
    monkeypatch.setattr(installer, "check_existing",
                        lambda tool, path=None: Result(tool, OK, f"{toolset.TOOL_NAMES[tool]} 1 at /x"))
    monkeypatch.setattr(installer, "fetch_models", lambda ctx: pytest.fail("--no-models"))
    monkeypatch.setattr(installer, "offer_starnet", lambda *a: pytest.fail("--no-starnet"))
    seen = []
    assert installer.run(args(), mac_ctx(FakeMac()), doctor_checks=lambda ns: seen.append(ns) or []) == 0
    assert seen and seen[0].offline is False and seen[0].work is None


def test_models_fetched_by_default(mac, monkeypatch):
    monkeypatch.setattr(installer, "check_existing",
                        lambda tool, path=None: Result(tool, OK, f"{toolset.TOOL_NAMES[tool]} 1 at /x"))
    from darkroom import models
    calls = []
    monkeypatch.setattr(models, "fetch_denoise_model", lambda **kw: calls.append(kw))
    assert installer.run(args(no_models=False), mac_ctx(FakeMac()), doctor_checks=no_doctor) == 0
    assert len(calls) == 1


def test_doctor_failures_set_exit_code(mac, monkeypatch):
    monkeypatch.setattr(installer, "check_existing",
                        lambda tool, path=None: Result(tool, OK, f"{toolset.TOOL_NAMES[tool]} 1 at /x"))
    msgs = []
    rc = installer.run(args(), mac_ctx(FakeMac(), out=msgs.append),
                       doctor_checks=lambda ns: [Result("denoise model", FAIL, "not found", "fetch it")])
    assert rc == 1 and any("FAIL  denoise model" in m for m in msgs)


def test_offline_does_not_download(mac, absent):
    opener, msgs = Opener({}), []
    rc = installer.run(args(offline=True, no_models=False, no_starnet=False), mac_ctx(FakeMac(), opener,
                       out=msgs.append), doctor_checks=lambda ns: [] if ns.offline else pytest.fail("offline"))
    assert rc == 1 and opener.calls == [] and any("--offline and no --from" in m for m in msgs)


def test_dry_run_prints_plan_without_network(mac):
    msgs = []
    ctx = mac_ctx(lambda *a, **k: pytest.fail("no commands"), lambda *a, **k: pytest.fail("no network"),
                  out=msgs.append)
    assert installer.run(args(dry_run=True), ctx) == 0
    text = "\n".join(msgs)
    for tool in toolset.TOOLS:
        b = toolset.build_for(tool, "macos-arm64")
        assert b.url in text and b.sha256 in text and str(tools_root() / b.folder) in text
    assert "NOT notarized" in text and "nothing was downloaded" in text
    assert not tools_root().exists() or not any(tools_root().iterdir())


def test_only_parsing():
    assert installer.parse_only(None) == ["siril", "graxpert"]
    assert installer.parse_only("graxpert, SIRIL") == ["siril", "graxpert"]
    with pytest.raises(tools.StageError, match="--only"):
        installer.parse_only("starnet")


def test_cli_registers_setup(monkeypatch):
    a = cli.build_parser().parse_args(["setup", "--dry-run", "--from", "/x", "--only", "siril",
                                       "--allow-unnotarized", "--no-models", "--no-starnet", "--force"])
    assert a.func is installer.run and a.from_ == "/x" and a.only == "siril" and a.allow_unnotarized
    monkeypatch.setattr(toolset, "platform_key", lambda *a, **k: (_ for _ in ()).throw(
        tools.StageError("darkroom setup has no Siril and GraXpert builds for plan9 mips")))
    assert cli.main(["setup", "--dry-run"]) == 1
