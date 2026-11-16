"""External tool locations, logging and subprocess helpers.

Every machine-specific location comes from an environment variable (or a CLI
flag); the defaults are the standard macOS install locations.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_SIRIL = "/Applications/Siril.app/Contents/MacOS/siril-cli"
DEFAULT_GRAXPERT = "/Applications/GraXpert.app/Contents/MacOS/GraXpert"
DEFAULT_GRAXPERT_MODEL = "~/Library/Application Support/GraXpert/denoise-ai-models/3.0.2/model.onnx"
DEFAULT_CACHE = "~/Library/Caches/darkroom"


class StageError(RuntimeError):
    """A stage failed; the message is shown to the user and the CLI exits non-zero."""


def siril_cli() -> Path:
    return Path(os.environ.get("DARKROOM_SIRIL", DEFAULT_SIRIL)).expanduser()


def graxpert() -> Path:
    return Path(os.environ.get("DARKROOM_GRAXPERT", DEFAULT_GRAXPERT)).expanduser()


def graxpert_model() -> Path:
    return Path(os.environ.get("DARKROOM_GRAXPERT_MODEL", DEFAULT_GRAXPERT_MODEL)).expanduser()


def cache_dir() -> Path:
    return Path(os.environ.get("DARKROOM_CACHE", DEFAULT_CACHE)).expanduser()


def starnet_dir(flag: str | None) -> Path:
    d = flag or os.environ.get("DARKROOM_STARNET_DIR")
    if not d:
        raise StageError("StarNet directory not set: pass --starnet-dir DIR or set DARKROOM_STARNET_DIR "
                         "(the folder containing the starnet++ executable and its weights)")
    p = Path(d).expanduser().resolve()
    if not (p / "starnet++").is_file():
        raise StageError(f"starnet++ not found in {p}")
    return p


def require_executable(p: Path, envvar: str) -> Path:
    if not (p.is_file() and os.access(p, os.X_OK)):
        raise StageError(f"{p} is not an executable; set {envvar} to its location")
    return p


def require_file(p: Path, what: str) -> Path:
    if not p.is_file():
        raise StageError(f"missing {what}: {p}")
    return p


def logs_dir(work: Path) -> Path:
    d = work / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def tail(path: Path, n: int = 25) -> str:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


class StageLog:
    """Write messages to stdout and to <work>/logs/<stage>.log."""

    def __init__(self, work: Path, stage: str, mode: str = "w"):
        self.path = logs_dir(work) / f"{stage}.log"
        self._f = open(self.path, mode, buffering=1)

    def __call__(self, msg: str) -> None:
        print(msg, flush=True)
        self._f.write(msg + "\n")

    def close(self) -> None:
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def run_tool(cmd: list[str], log_path: Path, cwd: Path | None = None) -> None:
    """Run cmd, streaming stdout+stderr into log_path (appended). Raise StageError on failure."""
    t0 = time.time()
    with open(log_path, "a", buffering=1) as f:
        f.write("$ " + " ".join(f"'{c}'" if " " in c else c for c in cmd)
                + (f"   (cwd: {cwd})" if cwd else "") + "\n")
        f.flush()
        try:
            rc = subprocess.run(cmd, cwd=cwd, stdout=f, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL).returncode
        except OSError as e:
            raise StageError(f"could not run {cmd[0]}: {e}") from e
        f.write(f"[exit code {rc}, {time.time() - t0:.1f}s]\n")
    if rc != 0:
        raise StageError(f"{Path(cmd[0]).name} exited with code {rc}; last lines of {log_path}:\n"
                         + tail(log_path))


def check_outputs(paths: list[Path], log_path: Path) -> None:
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise StageError("expected output missing: " + ", ".join(missing)
                         + f"\nlast lines of {log_path}:\n" + tail(log_path))


def eprint(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
