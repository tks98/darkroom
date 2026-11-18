"""External tool locations, logging and subprocess helpers.

Every machine-specific location comes from an environment variable (or a CLI
flag); the defaults are the standard install locations on macOS and on Linux
(the Linux ones match the darkroom Docker image).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# macOS defaults
DEFAULT_SIRIL = "/Applications/Siril.app/Contents/MacOS/siril-cli"
DEFAULT_GRAXPERT = "/Applications/GraXpert.app/Contents/MacOS/GraXpert"
DEFAULT_GRAXPERT_MODEL = "~/Library/Application Support/GraXpert/denoise-ai-models/3.0.2/model.onnx"
DEFAULT_DECONV_MODEL = "~/Library/Application Support/GraXpert/deconvolution-object-ai-models/1.0.1/model.onnx"
DEFAULT_CACHE = "~/Library/Caches/darkroom"

# Linux defaults (siril-cli and GraXpert are looked up on $PATH first)
LINUX_SIRIL = "siril-cli"
LINUX_GRAXPERT = "/opt/graxpert/GraXpert-linux/GraXpert"
LINUX_GRAXPERT_MODEL_REL = "GraXpert/denoise-ai-models/3.0.2/model.onnx"   # under $XDG_DATA_HOME
LINUX_DECONV_MODEL_REL = "GraXpert/deconvolution-object-ai-models/1.0.1/model.onnx"


def is_macos() -> bool:
    return sys.platform == "darwin"


def default_siril() -> str:
    if is_macos():
        return DEFAULT_SIRIL
    return shutil.which(LINUX_SIRIL) or LINUX_SIRIL


def default_graxpert() -> str:
    if is_macos():
        return DEFAULT_GRAXPERT
    return shutil.which("GraXpert") or LINUX_GRAXPERT


def _xdg(var: str, fallback: str) -> str:
    v = os.environ.get(var)
    return v if v and os.path.isabs(os.path.expanduser(v)) else fallback


def default_graxpert_model() -> str:
    """Where GraXpert itself keeps the 3.0.2 denoise model on this platform."""
    if is_macos():
        return DEFAULT_GRAXPERT_MODEL
    return _xdg("XDG_DATA_HOME", "~/.local/share").rstrip("/") + "/" + LINUX_GRAXPERT_MODEL_REL


def default_deconv_model() -> str:
    """Where GraXpert itself keeps the 1.0.1 object deconvolution model on this platform."""
    if is_macos():
        return DEFAULT_DECONV_MODEL
    return _xdg("XDG_DATA_HOME", "~/.local/share").rstrip("/") + "/" + LINUX_DECONV_MODEL_REL


def default_cache() -> str:
    if is_macos():
        return DEFAULT_CACHE
    return _xdg("XDG_CACHE_HOME", "~/.cache").rstrip("/") + "/darkroom"


class StageError(RuntimeError):
    """A stage failed; the message is shown to the user and the CLI exits non-zero."""


def resolve_tool(tool: str) -> tuple[Path, str]:
    """(executable, where it came from) for "siril" or "graxpert", in this order:
    $DARKROOM_SIRIL / $DARKROOM_GRAXPERT, then a managed install made by `darkroom setup`
    (under $DARKROOM_TOOLS_DIR or its platform default), then the platform default."""
    from . import toolset
    var = toolset.ENV_VARS[tool]
    if os.environ.get(var):
        return Path(os.environ[var]).expanduser(), f"${var}"
    m = toolset.managed(tool)
    if m is not None:
        return m.exe, f"installed by darkroom setup in {m.dir}"
    return Path(default_siril() if tool == "siril" else default_graxpert()).expanduser(), "platform default"


def siril_cli() -> Path:
    return resolve_tool("siril")[0]


def graxpert() -> Path:
    return resolve_tool("graxpert")[0]


def graxpert_model() -> Path:
    return Path(os.environ.get("DARKROOM_GRAXPERT_MODEL") or default_graxpert_model()).expanduser()


def deconv_model() -> Path:
    return Path(os.environ.get("DARKROOM_DECONV_MODEL") or default_deconv_model()).expanduser()


def cache_dir() -> Path:
    return Path(os.environ.get("DARKROOM_CACHE") or default_cache()).expanduser()


STARNET_LOOKUP = ("--starnet-dir DIR", "[starnet] dir in darkroom.toml", "$DARKROOM_STARNET_DIR",
                  "the newest StarNet2 installed by `darkroom get-starnet`")


def starnet_dir(flag: str | None) -> Path:
    """The StarNet folder to use. ``flag`` is --starnet-dir or, failing that, [starnet] dir
    (the settings resolver has already merged the two). Then $DARKROOM_STARNET_DIR, then the
    newest StarNet2 that `darkroom get-starnet` installed under <cache>/starnet."""
    from . import starnet
    if flag:
        d, where = flag, "--starnet-dir / [starnet] dir"
    elif os.environ.get("DARKROOM_STARNET_DIR"):
        d, where = os.environ["DARKROOM_STARNET_DIR"], "$DARKROOM_STARNET_DIR"
    else:
        found = starnet.installed()
        if found:
            return found[0].dir
        raise StageError(
            "StarNet not found. darkroom looks, in this order, at: " + "; ".join(STARNET_LOOKUP)
            + f" (none in {starnet.installs_root()}). Run `darkroom get-starnet` to install StarNet2, "
            "or point one of the others at the folder containing starnet2 (StarNet2 2.6) or "
            "starnet++ (StarNet v2.0.2)")
    p = Path(d).expanduser().resolve()
    if starnet.detect(p) is None:
        raise StageError(f"neither starnet2 nor starnet++ found in {p} (from {where})")
    return p


def require_executable(p: Path, envvar: str) -> Path:
    if not (p.is_file() and os.access(p, os.X_OK)):
        raise StageError(f"{p} is not an executable; set {envvar} to its location")
    if is_macos():
        from . import toolset
        key = (str(p), p.stat().st_mtime_ns)
        if key not in _GATEKEEPER_OK:            # spctl can take a second; check each binary once
            blocked = toolset.gatekeeper_block(p)
            if blocked:
                raise StageError(blocked)
            _GATEKEEPER_OK.add(key)
    return p


_GATEKEEPER_OK: set[tuple[str, int]] = set()


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
