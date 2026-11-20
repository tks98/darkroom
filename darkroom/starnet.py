"""StarNet backends: StarNet2 2.6.x (``starnet2``) and StarNet v2.0.2 (``starnet++``).

A StarNet folder holds one of the two executables next to its weights:

- StarNet2 2.6.x: ``./starnet2 --input IN --output OUT -q``; it finds its weights next to
  itself, and darkroom passes absolute paths.
- StarNet v2.0.2: ``./starnet++ IN OUT``, run with the folder as the working directory
  (it looks for its weights there).

When a folder has both, ``starnet2`` is used. darkroom only runs the StarNet program the
user installed; it never copies, bundles or caches StarNet itself or its outputs elsewhere.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import tools

STARNET2 = "starnet2"
LEGACY = "starnet++"
ACCEPTED = "accepted.json"          # written by `darkroom get-starnet` after licence acceptance

# Folder (and zip) names of the official command-line builds, e.g.
# starnet2_linux_2.6.2-0242_ORT_x64_cli
NAME_RE = re.compile(r"starnet2_(?P<platform>[a-z0-9-]+?)_(?P<version>\d+(?:\.\d+)+)-(?P<build>\d+)_"
                     r"(?P<backend>[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*?)_cli")


@dataclass(frozen=True)
class Backend:
    dir: Path
    kind: str                       # STARNET2 or LEGACY
    version: str | None = None      # StarNet2 only, when known
    build: str | None = None

    @property
    def exe(self) -> Path:
        return self.dir / self.kind

    @property
    def label(self) -> str:
        if self.kind == LEGACY:
            return "StarNet v2 (starnet++)"
        v = self.version or "version unknown"
        return f"StarNet2 {v}" + (f" (build {self.build})" if self.build else "")

    def command(self, src: Path, dst: Path) -> list[str]:
        """Command line, to be run with cwd = self.dir. src and dst should be absolute."""
        if self.kind == STARNET2:
            return ["./" + STARNET2, "--input", str(src), "--output", str(dst), "-q"]
        return ["./" + LEGACY, str(src), str(dst)]

    def params(self) -> dict:
        """What the skip logic records for the starnet stage. Nothing for starnet++, so work
        directories made with StarNet v2.0.2 before StarNet2 support stay up to date."""
        if self.kind != STARNET2:
            return {}
        out = {"product": "starnet2", "version": self.version or "unknown"}
        if self.build:
            out["build"] = self.build
        return out

    def sort_key(self) -> tuple:
        return version_tuple(self.version) + version_tuple(self.build)


def version_tuple(v: str | None) -> tuple[int, ...]:
    if not v:
        return (-1,)
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (-1,)


def read_accepted(folder: Path) -> dict | None:
    try:
        data = json.loads((Path(folder) / ACCEPTED).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def detect(folder: Path) -> Backend | None:
    """The backend in ``folder``, or None if it holds neither executable."""
    folder = Path(folder)
    if (folder / STARNET2).is_file():
        version = build = None
        acc = read_accepted(folder)
        if acc and acc.get("version"):
            version, build = str(acc["version"]), (str(acc["build"]) if acc.get("build") else None)
        else:
            m = NAME_RE.search(folder.name)
            if m:
                version, build = m.group("version"), m.group("build")
        return Backend(folder, STARNET2, version, build)
    if (folder / LEGACY).is_file():
        return Backend(folder, LEGACY)
    return None


@dataclass(frozen=True)
class Gatekeeper:
    """macOS: whether StarNet in a folder may be executed. problem is None when it may."""
    problem: str | None = None
    fix: str = ""
    note: str = ""


def macos_files(folder: Path, exe: Path) -> list[Path]:
    """The files macOS checks when exe starts: the executable and the libraries next to it."""
    return [exe, *sorted(folder.glob("*.dylib")), *sorted((folder / "lib").glob("*.dylib"))]


def gatekeeper(folder: Path, exe: Path, run=None, system: str | None = None, who: str = "darkroom") -> Gatekeeper:
    """The same policy as for Siril and GraXpert (toolset.gatekeeper_block), for a StarNet folder:
    not executed when a quarantine flag cannot be read, or when a quarantined file is neither
    allowed by the user nor accepted by Gatekeeper. A StarNet2 installed by an earlier
    `darkroom get-starnet`, which did not mark it as downloaded, has never been assessed by
    macOS; it is executed only if Gatekeeper accepts it as it is. Only reads flags."""
    import sys
    from . import toolset
    system = sys.platform if system is None else system
    if system != "darwin":
        return Gatekeeper()
    run = run or toolset._run
    folder = Path(folder)

    def rel(f: Path) -> str:
        try:
            return str(f.relative_to(folder))
        except ValueError:
            return str(f)
    v = toolset.judge(macos_files(folder, exe), run)
    if v.reason == "unknown":
        return Gatekeeper(f"could not read the quarantine flag of {', '.join(rel(f) for f in v.files)} ({v.detail}); "
                          f"{who} did not execute it",
                          f"check that `xattr -p {toolset.QUARANTINE} '{v.files[0]}'` works in Terminal, then try again")
    if v.reason == "blocked":
        return Gatekeeper(f"quarantined by macOS ({', '.join(rel(f) for f in v.files)}) and not notarized "
                          f"({v.detail}); Gatekeeper will block it; {who} did not execute it",
                          f"allowing it is your decision; this clears the flag for the folder: "
                          f"xattr -dr {toolset.QUARANTINE} '{folder}'")
    acc = read_accepted(folder)
    if acc is not None and "quarantine" not in acc and not v.note:
        ok, source = toolset.assess(exe, run)
        if not ok:
            return Gatekeeper(f"{rel(exe)} was installed by an earlier darkroom that did not mark it as downloaded, "
                              f"so macOS has never assessed it, and Gatekeeper does not accept it "
                              f"({source or 'rejected'}); {who} did not execute it",
                              "reinstall it with `darkroom get-starnet --force` (you are asked to accept its licence "
                              "again): it is then marked as downloaded, and macOS asks you before it first runs")
        return Gatekeeper(note=f"accepted by Gatekeeper: {source}")
    return Gatekeeper(note=v.note)


def require_runnable(backend: Backend) -> None:
    """StageError instead of starting a StarNet that macOS would block (see gatekeeper)."""
    g = gatekeeper(backend.dir, backend.exe)
    if g.problem:
        raise tools.StageError(f"StarNet in {backend.dir}: {g.problem}. {g.fix[0].upper() + g.fix[1:]}")


def installs_root() -> Path:
    """Where `darkroom get-starnet` installs StarNet2: <cache>/starnet/<zip name>/."""
    return tools.cache_dir() / "starnet"


def installed(root: Path | None = None) -> list[Backend]:
    """StarNet2 installs made by `darkroom get-starnet` (licence accepted), newest first."""
    root = root or installs_root()
    if not root.is_dir():
        return []
    out = []
    for d in root.iterdir():
        if d.name.startswith(".") or not d.is_dir() or not (d / ACCEPTED).is_file():
            continue
        b = detect(d)
        if b is not None and b.kind == STARNET2:
            out.append(b)
    return sorted(out, key=lambda b: (b.sort_key(), b.dir.name), reverse=True)
