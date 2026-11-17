"""The pinned Siril and GraXpert builds that `darkroom setup` installs, and where it puts them.

Each install lives in <tools>/<tool>-<version>/ with an installed.json describing it
(tools: $DARKROOM_TOOLS_DIR, else ~/Library/Application Support/darkroom/tools on macOS
and ${XDG_DATA_HOME:-~/.local/share}/darkroom/tools on Linux). `tools.siril_cli()` and
`tools.graxpert()` use a managed install when no DARKROOM_SIRIL / DARKROOM_GRAXPERT is set.

The URLs and SHA-256 values for Linux must equal the ARGs in the Dockerfile; a test checks it.
"""
from __future__ import annotations

import json
import os
import platform as _platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import tools

TOOLS = ("siril", "graxpert")
RECORD = "installed.json"
DOCKER_HINT = "use the darkroom Docker image instead (see docs/docker.md)"

PLATFORM_NAMES = {"macos-arm64": "macOS on Apple Silicon", "macos-x86_64": "macOS on Intel",
                  "linux-x86_64": "Linux x86_64"}
TOOL_NAMES = {"siril": "Siril", "graxpert": "GraXpert"}
ENV_VARS = {"siril": "DARKROOM_SIRIL", "graxpert": "DARKROOM_GRAXPERT"}


@dataclass(frozen=True)
class Build:
    tool: str                   # siril | graxpert
    version: str
    platform: str               # macos-arm64 | macos-x86_64 | linux-x86_64
    url: str
    size: int
    sha256: str
    kind: str                   # dmg | appimage | zip
    exe: str                    # executable, relative to the install folder
    app: str | None = None      # the .app inside a dmg
    notarized: bool | None = None   # macOS: whether the official build is notarized by Apple

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[1]

    @property
    def name(self) -> str:
        return TOOL_NAMES[self.tool]

    @property
    def folder(self) -> str:
        return f"{self.tool}-{self.version}"

    def describe(self) -> str:
        return f"{self.name} {self.version} for {PLATFORM_NAMES[self.platform]}"


SIRIL_VERSION = "1.4.5"
GRAXPERT_VERSION = "3.1.0rc2"
_FA = "https://free-astro.org/download/"
_GH = f"https://github.com/Steffenhir/GraXpert/releases/download/{GRAXPERT_VERSION}/"
_SIRIL_APP = "Siril.app/Contents/MacOS/siril-cli"
_GRAXPERT_APP = "GraXpert.app/Contents/MacOS/GraXpert"

MANIFEST: dict[tuple[str, str], Build] = {(b.tool, b.platform): b for b in [
    Build("siril", SIRIL_VERSION, "macos-arm64", _FA + "siril-1.4.5-arm64.dmg", 76039084,
          "df85b1135539399ead0e2c56bff9bba771dfbec946c7c3995213e05038d7dc2b", "dmg", _SIRIL_APP, "Siril.app", True),
    Build("siril", SIRIL_VERSION, "macos-x86_64", _FA + "siril-1.4.5-x86_64.dmg", 92967112,
          "2342364fa6edcfdfc5a6f6ecfcf7eee33e7b50d66463647fc2969be42ad1a07f", "dmg", _SIRIL_APP, "Siril.app", True),
    Build("siril", SIRIL_VERSION, "linux-x86_64", _FA + "Siril-1.4.5-x86_64.AppImage", 96631288,
          "99b371b5edae28db8e66862acbcf483606d77e4fbbdf89613f6f02ffce0d5ecb", "appimage", "siril-cli"),
    Build("graxpert", GRAXPERT_VERSION, "macos-arm64", _GH + "graxpert-macos-arm64.dmg", 121176508,
          "47cb6ad094643756d575f36fe17dc7c200d528081bf33f669e9028b60b5ca33a", "dmg", _GRAXPERT_APP,
          "GraXpert.app", False),
    Build("graxpert", GRAXPERT_VERSION, "macos-x86_64", _GH + "graxpert-macos-amd64.dmg", 174063671,
          "b9787787c832bbbbe6add180cf77142b548d36805d9a1a1378079d19ae3cc5a6", "dmg", _GRAXPERT_APP,
          "GraXpert.app", False),
    Build("graxpert", GRAXPERT_VERSION, "linux-x86_64", _GH + "graxpert-linux-amd64.zip", 546833150,
          "f651be473fdd6522b2e8e47d3acfcadedbf3cf2be90552aae9333396fc355cf9", "zip", "GraXpert-linux/GraXpert"),
]}


# ---------------------------------------------------------------- platform

def _rosetta() -> bool:
    try:
        p = subprocess.run(["sysctl", "-in", "sysctl.proc_translated"], capture_output=True, text=True, timeout=5)
        return p.stdout.strip() == "1"
    except (OSError, subprocess.TimeoutExpired):
        return False


def platform_key(system: str | None = None, machine: str | None = None, rosetta: Callable = _rosetta) -> str:
    """macos-arm64, macos-x86_64 or linux-x86_64; anything else is a StageError."""
    system = sys.platform if system is None else system
    machine = (_platform.machine() if machine is None else machine).lower()
    if system == "darwin":
        if machine in ("arm64", "aarch64"):
            return "macos-arm64"
        if machine in ("x86_64", "amd64"):
            return "macos-arm64" if rosetta() else "macos-x86_64"
    elif system.startswith("linux") and machine in ("x86_64", "amd64"):
        return "linux-x86_64"
    raise tools.StageError(f"darkroom setup has no Siril and GraXpert builds for {system} {machine}; it knows "
                           f"macOS (arm64, x86_64) and Linux x86_64. Install them yourself and set "
                           f"DARKROOM_SIRIL and DARKROOM_GRAXPERT, or {DOCKER_HINT}")


def build_for(tool: str, plat: str) -> Build:
    try:
        return MANIFEST[(tool, plat)]
    except KeyError:
        raise tools.StageError(f"no pinned {TOOL_NAMES.get(tool, tool)} build for {plat}") from None


# ---------------------------------------------------------------- locations

def default_tools_dir(system: str | None = None) -> str:
    system = sys.platform if system is None else system
    if system == "darwin":
        return "~/Library/Application Support/darkroom/tools"
    return tools._xdg("XDG_DATA_HOME", "~/.local/share").rstrip("/") + "/darkroom/tools"


def tools_dir() -> Path:
    return Path(os.environ.get("DARKROOM_TOOLS_DIR") or default_tools_dir()).expanduser()


@dataclass(frozen=True)
class Installed:
    dir: Path
    record: dict

    @property
    def exe(self) -> Path:
        return self.dir / self.record["exe"]

    @property
    def version(self) -> str:
        return str(self.record.get("version", "?"))


def read_record(folder: Path) -> dict | None:
    try:
        data = json.loads((folder / RECORD).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("exe"), str):
        return None
    return data


def managed(tool: str, root: Path | None = None, plat: str | None = None) -> Installed | None:
    """The managed install of ``tool`` for this platform whose executable exists, or None.
    The pinned version wins; otherwise the most recently installed one."""
    root = tools_dir() if root is None else root
    if plat is None:
        try:
            plat = platform_key()
        except tools.StageError:
            return None
    pinned = MANIFEST.get((tool, plat))
    found = []
    try:
        candidates = sorted(root.glob(f"{tool}-*"))
    except OSError:
        return None
    for d in candidates:
        if not d.is_dir() or d.name.startswith("."):
            continue
        rec = read_record(d)
        if not rec or rec.get("tool") != tool or rec.get("platform") != plat:
            continue
        exe = d / rec["exe"]
        if not exe.is_file():
            continue
        found.append(Installed(d, rec))
    if not found:
        return None
    if pinned:
        for i in found:
            if i.version == pinned.version:
                return i
    return max(found, key=lambda i: str(i.record.get("installed_at", "")))


# ---------------------------------------------------------------- Gatekeeper (macOS)

QUARANTINE = "com.apple.quarantine"
QTN_USER_APPROVED = 0x0040


def _run(cmd: list[str], timeout: float = 30) -> tuple[int, str]:
    p = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=timeout)
    return p.returncode, p.stdout.decode(errors="replace")


def quarantine_value(p: Path, run: Callable = _run) -> str | None:
    """The com.apple.quarantine value of p, or None when it has none."""
    try:
        rc, out = run(["xattr", "-p", QUARANTINE, str(p)], timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.strip() if rc == 0 else None


def user_approved(value: str) -> bool:
    """True when the quarantine flags say the user already allowed it (Open Anyway / first launch)."""
    try:
        return bool(int(value.split(";", 1)[0], 16) & QTN_USER_APPROVED)
    except ValueError:
        return False


def bundle_of(exe: Path) -> Path:
    """The .app bundle containing exe, or exe itself."""
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent
    return exe


def assess(target: Path, run: Callable = _run) -> tuple[bool, str]:
    """(accepted, source) from `spctl -a -t exec -vv`; source is e.g. 'Notarized Developer ID'."""
    try:
        rc, out = run(["spctl", "-a", "-t", "exec", "-vv", str(target)], timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"spctl failed: {e}"
    source = ""
    for line in out.splitlines():
        if line.strip().startswith("source="):
            source = line.split("=", 1)[1].strip()
    if not source and out.strip():
        last = out.strip().splitlines()[-1].strip()
        source = last.rsplit(": ", 1)[-1] if last.startswith(str(target)) else last
    return rc == 0, source


def allow_instructions(app: Path) -> str:
    return ("Allowing it is your decision. Either open it once in Finder (it will be blocked), then go to "
            "System Settings > Privacy & Security and click Open Anyway; or clear the quarantine flag "
            f"yourself with: xattr -dr {QUARANTINE} '{app}'")


def gatekeeper_block(exe: Path, run: Callable = _run, system: str | None = None) -> str | None:
    """macOS only: a message when exe is quarantined, not yet allowed by the user and not accepted by
    Gatekeeper (so running it would be blocked with a system dialog); None when it can run."""
    system = sys.platform if system is None else system
    if system != "darwin":
        return None
    value = quarantine_value(exe, run)
    if value is None or user_approved(value):
        return None
    target = bundle_of(exe)
    ok, source = assess(target, run)
    if ok:
        return None
    return (f"{target} is quarantined by macOS and not notarized by Apple ({source or 'rejected'}), so "
            f"Gatekeeper will block it; darkroom did not run it. {allow_instructions(target)}")
