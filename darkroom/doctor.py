"""`darkroom doctor`: check that every external dependency works before a long run.

Each check is a small function returning a ``Result``. Everything that touches
the outside world goes through ``run_cmd`` (subprocess), ``http_get`` (urllib)
or plain filesystem calls on paths passed in, so the tests can replace them.

Doctor only reports. Apart from building the cached fixed-batch denoise model on
first use, it never changes files, settings or quarantine flags, it never executes
StarNet when macOS has it quarantined, and it never executes a quarantined Siril or
GraXpert that Gatekeeper would block (not notarized and not yet allowed by the user). On Linux the macOS-only
checks (quarantine flag, Rosetta, executable architecture) are skipped.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from . import tools

OK, WARN, FAIL = "OK", "WARN", "FAIL"

DEPS = ["numpy", "scipy", "astropy", "tifffile", "imagecodecs", "PIL", "onnx", "onnxruntime"]
MIN_PYTHON = (3, 11)

SIRIL_MIN = (1, 4, 0)
GRAXPERT_VERIFIED = "3.1"
MODEL_VERSION = "3.0.2"

STARNET_EXE = "starnet++"
STARNET_WEIGHTS = "starnet2_weights.pb"
STARNET2_EXE = "starnet2"
STARNET2_WEIGHTS = ("StarNet2_weights.mlpackage", "StarNet2_weights.onnx")
STARNET2_VERIFIED = "2.6"
STARNET2_GLIBC = (2, 38)
STARNET_SAMPLE = "rgb_test.tif"
QUARANTINE = "com.apple.quarantine"

VIZIER_URLS = ["https://tapvizier.cds.unistra.fr/TAPVizieR/tap/availability",
               "http://tapvizier.u-strasbg.fr/TAPVizieR/tap/availability"]
ESA_URLS = ["https://gea.esac.esa.int/tap-server/tap/availability"]
HTTP_TIMEOUT = 6.0
USER_AGENT = "darkroom-doctor"

BYTES_PER_SUB = 80e6        # calibrated + registered 16-bit RGB frame (404 subs -> ~31 GB)
DISK_OVERHEAD = 2e9


@dataclass
class Result:
    name: str
    status: str
    detail: str
    fix: str | None = None


# ---------------------------------------------------------------- injectable I/O

def run_cmd(cmd: list[str], timeout: float = 15.0, cwd: Path | None = None) -> tuple[int, str]:
    """Run cmd, return (returncode, stdout+stderr). Raises OSError / subprocess.TimeoutExpired."""
    p = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=timeout)
    return p.returncode, p.stdout.decode(errors="replace")


SYSTEM_CA = Path("/etc/ssl/cert.pem")


def _ssl_context() -> ssl.SSLContext:
    """Default verifying context; if Python has no CA bundle of its own (python.org builds before
    'Install Certificates.command' is run), verify against the macOS system bundle instead."""
    ctx = ssl.create_default_context()
    paths = ssl.get_default_verify_paths()
    if not paths.cafile and not (paths.capath and any(Path(paths.capath).glob("*"))) and SYSTEM_CA.is_file():
        ctx.load_verify_locations(cafile=str(SYSTEM_CA))
    return ctx


def http_get(url: str, timeout: float = HTTP_TIMEOUT) -> int:
    """GET url with only a generic User-Agent; return the HTTP status. Raises on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
        r.read(4096)
        return r.status


def _is_exec(p: Path) -> bool:
    return p.is_file() and os.access(p, os.X_OK)


def _short_err(e: BaseException) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return str(e.reason)
    return str(e) or type(e).__name__


# ---------------------------------------------------------------- 0. system

CONTAINER_MARKERS = (Path("/.dockerenv"), Path("/run/.containerenv"))


def glibc_version(libc_ver: Callable = None) -> tuple[int, ...] | None:
    import platform
    name, ver = (libc_ver or platform.libc_ver)()
    if name != "glibc" or not ver:
        return None
    try:
        return tuple(int(x) for x in ver.split("."))
    except ValueError:
        return None


def in_container(markers=CONTAINER_MARKERS) -> bool:
    return any(m.exists() for m in markers)


def check_system(system: str | None = None, machine: str | None = None, glibc: Callable = glibc_version,
                 container: Callable = in_container, mac_ver: Callable | None = None) -> Result:
    import platform
    system = sys.platform if system is None else system
    machine = platform.machine() if machine is None else machine
    name = "system"
    box = ", inside a container" if container() else ""
    if system == "darwin":
        v = (mac_ver or platform.mac_ver)()[0]
        return Result(name, OK, f"macOS {v} {machine}{box}")
    if system.startswith("linux"):
        g = glibc()
        gs = ".".join(map(str, g)) if g else "unknown"
        detail = f"Linux {machine}, glibc {gs}{box}"
        if g is not None and g < STARNET2_GLIBC:
            return Result(name, WARN, detail + "; the StarNet2 Linux build needs glibc 2.38 or newer",
                          "use a newer distribution (the darkroom Docker image has one), or StarNet will not run")
        return Result(name, OK, detail)
    return Result(name, WARN, f"{system} {machine}: darkroom is made for macOS and Linux",
                  "expect the tool defaults to be wrong; set the DARKROOM_* variables")


# ---------------------------------------------------------------- 1. python

def pinned_onnxruntime() -> str | None:
    """The onnxruntime version darkroom pins, from the installed metadata or pyproject.toml."""
    try:
        for req in importlib.metadata.requires("darkroom") or []:
            m = re.match(r"\s*onnxruntime\s*==\s*([\w.]+)", req)
            if m:
                return m.group(1)
    except importlib.metadata.PackageNotFoundError:
        pass
    pp = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        m = re.search(r'"onnxruntime\s*==\s*([\w.]+)"', pp.read_text())
        return m.group(1) if m else None
    except OSError:
        return None


def check_python(version_info=None, importer: Callable = importlib.import_module,
                 version_of: Callable = importlib.metadata.version, pinned: str | None = "auto") -> Result:
    name = "python"
    v = version_info or sys.version_info
    vs = f"{v[0]}.{v[1]}.{v[2]}"
    if tuple(v[:2]) < MIN_PYTHON:
        return Result(name, FAIL, f"Python {vs} at {sys.executable}",
                      "use Python 3.11 or newer (run ./setup.sh, which creates .venv with it)")
    missing = []
    for mod in DEPS:
        try:
            importer(mod)
        except Exception as e:      # ImportError, or a broken binary wheel
            missing.append(f"{mod} ({type(e).__name__})")
    if missing:
        return Result(name, FAIL, f"Python {vs}; cannot import: {', '.join(missing)}",
                      "pip install -e . inside the darkroom venv (or run ./setup.sh)")
    if pinned == "auto":
        pinned = pinned_onnxruntime()
    try:
        ort_v = version_of("onnxruntime")
    except Exception:
        ort_v = "unknown"
    detail = f"Python {vs}, deps importable, onnxruntime {ort_v}"
    if pinned and ort_v != pinned:
        return Result(name, WARN, detail + f" (verified with {pinned})",
                      f"pip install onnxruntime=={pinned}  (the CoreML denoise was verified with {pinned})")
    return Result(name, OK, detail)


# ---------------------------------------------------------------- 2. siril

def parse_siril_version(text: str) -> tuple[int, int, int] | None:
    for line in text.splitlines():
        m = re.match(r"\s*siril\s+(\d+)\.(\d+)(?:\.(\d+))?", line, re.IGNORECASE)
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    return None


def _resolved(tool: str, path: Path | None) -> tuple[Path, str]:
    if path is not None:
        return path, ""
    exe, source = tools.resolve_tool(tool)
    return exe, f" ({source})"


def _gatekeeper(name: str, exe: Path, run: Callable, system: str | None) -> tuple[Result | None, str]:
    """macOS: (FAIL result, "") when exe is quarantined and Gatekeeper would block it (it is then
    not executed); else (None, note) where note mentions a quarantine that Gatekeeper accepts."""
    from . import toolset
    system = sys.platform if system is None else system
    if system != "darwin":
        return None, ""
    value = toolset.quarantine_value(exe, run)
    if value is None:
        return None, ""
    if toolset.user_approved(value):
        return None, "; quarantined, allowed by you"
    target = toolset.bundle_of(exe)
    ok, source = toolset.assess(target, run)
    if ok:
        return None, f"; quarantined, accepted by Gatekeeper: {source}"
    return Result(name, FAIL, f"{exe}: quarantined by macOS and not notarized ({source or 'rejected by Gatekeeper'}); "
                  "Gatekeeper will block it; doctor did not execute it", toolset.allow_instructions(target)), ""


def check_siril(path: Path | None = None, run: Callable = run_cmd, system: str | None = None) -> Result:
    name = "siril"
    exe, source = _resolved("siril", path)
    fix_install = ("run `darkroom setup`, install Siril 1.4.x from siril.org, or set DARKROOM_SIRIL to its "
                   "siril-cli")
    if not _is_exec(exe):
        return Result(name, FAIL, f"{exe} is not an executable{source}", fix_install)
    blocked, note = _gatekeeper(name, exe, run, system)
    if blocked:
        return blocked
    source = source[:-1] + note + ")" if source else (f" ({note[2:]})" if note else "")
    try:
        rc, out = run([str(exe), "--version"], timeout=20)
    except subprocess.TimeoutExpired:
        return Result(name, WARN, f"{exe} --version did not answer within 20 s", fix_install)
    except OSError as e:
        return Result(name, FAIL, f"cannot run {exe}: {e}", fix_install)
    v = parse_siril_version(out)
    if v is None:
        return Result(name, FAIL, f"{exe} did not report a Siril version (exit {rc})",
                      "check that DARKROOM_SIRIL points to siril-cli, not another program")
    vs = ".".join(map(str, v))
    if v < SIRIL_MIN:
        return Result(name, FAIL, f"Siril {vs} at {exe}{source}; darkroom needs 1.4.x", fix_install)
    if v[:2] != (1, 4):
        return Result(name, WARN, f"Siril {vs} at {exe}{source}; darkroom was verified with 1.4.x",
                      "the Siril scripts may need changes for this version; 1.4.x is known to work")
    return Result(name, OK, f"Siril {vs} at {exe}{source}")


# ---------------------------------------------------------------- 3. graxpert

def parse_graxpert_version(text: str) -> str | None:
    m = re.search(r"GraXpert version:\s*(\S+)", text)
    return m.group(1) if m else None


def check_graxpert(path: Path | None = None, run: Callable = run_cmd, timeout: float = 15.0,
                   system: str | None = None) -> Result:
    name = "graxpert"
    exe, source = _resolved("graxpert", path)
    fix_install = ("run `darkroom setup`, install GraXpert 3.1 from graxpert.com, or set DARKROOM_GRAXPERT to "
                   "its executable")
    if not _is_exec(exe):
        return Result(name, FAIL, f"{exe} is not an executable{source}", fix_install)
    blocked, note = _gatekeeper(name, exe, run, system)
    if blocked:
        return blocked
    source = source[:-1] + note + ")" if source else (f" ({note[2:]})" if note else "")
    out, rc = "", None
    for flag in ("-v", "--version"):
        try:
            rc, out = run([str(exe), flag], timeout=timeout)
        except subprocess.TimeoutExpired:
            return Result(name, WARN, f"{exe} {flag} did not answer within {timeout:.0f} s",
                          "start GraXpert once by hand to see whether it opens; then re-run doctor")
        except OSError as e:
            return Result(name, FAIL, f"cannot run {exe}: {e}", fix_install)
        v = parse_graxpert_version(out)
        if v:
            if not v.startswith(GRAXPERT_VERIFIED):
                return Result(name, WARN, f"GraXpert {v} at {exe}{source}; verified with {GRAXPERT_VERIFIED}.x",
                              "background extraction may behave differently; 3.1 is known to work")
            return Result(name, OK, f"GraXpert {v} at {exe}{source}")
    return Result(name, WARN, f"{exe}: could not parse version (exit {rc})",
                  "check that DARKROOM_GRAXPERT points to the GraXpert executable")


# ---------------------------------------------------------------- 4. model

MODEL_FIX = (f"run `darkroom fetch-models` (GraXpert downloads AI model {MODEL_VERSION}), "
             "or set DARKROOM_GRAXPERT_MODEL to the model.onnx file")


def check_model(path: Path | None = None) -> Result:
    p = path or tools.graxpert_model()
    if not p.is_file():
        return Result("denoise model", FAIL, f"not found: {p}", MODEL_FIX)
    return Result("denoise model", OK, f"{p} ({p.stat().st_size / 1e6:.0f} MB)")


DECONV_FIX = ("run `darkroom fetch-models --deconv` (GraXpert downloads its object deconvolution model 1.0.1), "
              "or set DARKROOM_DECONV_MODEL to that model.onnx file")


def check_deconv_model(path: Path | None = None, enabled: bool = False) -> Result:
    """The optional deconv stage's model: a WARN only when [deconv] is on in the work dir's config."""
    from . import models
    name = "deconv model"
    p = path or tools.deconv_model()
    problem = models.deconv_model_problem(p)
    if problem is None:
        return Result(name, OK, f"{p} ({p.stat().st_size / 1e6:.0f} MB, object model {models.DECONV_VERSION})")
    if enabled:
        return Result(name, WARN, f"{problem}; [deconv] is on, so `darkroom deconv` will fetch it first",
                      DECONV_FIX)
    return Result(name, OK, f"{problem} (only needed when [deconv] strength > 0, which is off by default)")


# ---------------------------------------------------------------- 5. gpu denoise

def _coreml_providers():
    # Same provider list as darkroom.denoise.run (GPU path on macOS).
    from . import denoise as dn
    return [dn.COREML, "CPUExecutionProvider"]


def check_gpu(model_path: Path | None = None, ort=None, build: Callable | None = None,
              system: str | None = None) -> Result:
    """Run one batch on the GPU backend denoise would use and on the CPU; compare speed and output."""
    name = "gpu denoise"
    import numpy as np
    from . import denoise as dn

    system = sys.platform if system is None else system
    src = model_path or tools.graxpert_model()
    if not src.is_file():
        return Result(name, FAIL, "not run: GraXpert denoise model missing", MODEL_FIX)
    if ort is None:
        import onnxruntime as ort
    try:
        ort.set_default_logger_severity(3)
    except Exception:
        pass
    try:
        available = ort.get_available_providers()
    except Exception:
        available = []
    providers, backend = dn.providers(False, available, system)
    if build is None:
        def build():
            tools.cache_dir().mkdir(parents=True, exist_ok=True)
            log = tools.cache_dir() / "doctor_build.log"
            return dn.fixed_batch_model(log, log=lambda m: None)
    try:
        model = build()
    except Exception as e:
        return Result(name, FAIL, f"could not build the fixed-batch model: {e}".splitlines()[0],
                      "check DARKROOM_CACHE is writable; see the build log in it")

    def session(providers):
        so = ort.SessionOptions()
        so.log_severity_level = 3
        return ort.InferenceSession(str(model), sess_options=so, providers=providers)

    x = np.random.default_rng(0).random((dn.BATCH, dn.WIN, dn.WIN, 3), dtype=np.float32)
    feed = {"gen_input_image": x}
    try:
        cpu = session(["CPUExecutionProvider"])
        t = time.time()
        y_cpu = cpu.run(None, feed)[0]
        t_cpu = time.time() - t
    except Exception as e:
        return Result(name, FAIL, f"CPU session failed: {str(e).splitlines()[0][:200]}",
                      "reinstall onnxruntime in the venv (./setup.sh)")
    if backend == "CPU":
        detail = f"CPU only (onnxruntime providers: {', '.join(available) or 'none'}); batch of {dn.BATCH}: {t_cpu:.2f}s"
        if system == "darwin":
            return Result(name, WARN, detail + " (no CoreML provider)",
                          "onnxruntime has no CoreML provider here; denoise will work but slowly "
                          "(install the pinned onnxruntime on Apple Silicon macOS)")
        return Result(name, WARN, detail + "; denoise will be slow",
                      "for an NVIDIA GPU, install onnxruntime-gpu with CUDA (and pass the GPU to the container); "
                      "on the CPU denoise still works")
    try:
        t = time.time()
        gpu = session(providers)
        load = time.time() - t
        gpu.run(None, feed)                      # warm-up (first call compiles)
        t = time.time()
        y_gpu = gpu.run(None, feed)[0]
        t_gpu = time.time() - t
    except Exception as e:
        return Result(name, FAIL, f"{backend} session failed: {str(e).splitlines()[0][:200]}",
                      "use `darkroom denoise --cpu` (much slower) and report the error")
    used = gpu.get_providers()
    diff = float(np.max(np.abs(y_gpu.astype(np.float64) - y_cpu)))
    short = [p.replace("ExecutionProvider", "") for p in used]
    detail = (f"providers {'+'.join(short)}; batch of {dn.BATCH}: {t_gpu:.2f}s vs CPU {t_cpu:.2f}s; "
              f"maxdiff {diff:.3f}; load {load:.1f}s")
    wanted = providers[0][0] if isinstance(providers[0], tuple) else providers[0]
    if wanted not in used:
        return Result(name, WARN, detail + " (fell back to CPU)",
                      f"onnxruntime could not use {backend} here; denoise will work but slowly")
    if not diff < 0.05:
        return Result(name, FAIL, detail + " (GPU output differs from CPU)",
                      "use `darkroom denoise --cpu` until this is understood")
    if t_gpu > 0.5 * t_cpu:
        return Result(name, WARN, detail + " (GPU not clearly faster)",
                      f"{backend} may be running most of the model on the CPU; `--cpu` gives the same result")
    return Result(name, OK, detail)


# ---------------------------------------------------------------- 6. starnet

def is_quarantined(p: Path, run: Callable = run_cmd) -> bool:
    try:
        rc, _ = run(["xattr", "-p", QUARANTINE, str(p)], timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return rc == 0


def _arch_check(exe: Path, run: Callable) -> tuple[Result | None, list[str]]:
    """macOS only: an x86_64-only executable needs a working Rosetta 2."""
    try:
        _, ftype = run(["file", "-b", str(exe)], timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        ftype = ""
    if "x86_64" in ftype and "arm64" not in ftype:
        try:
            rc, _ = run(["arch", "-x86_64", "/usr/bin/true"], timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            rc = 1
        if rc != 0:
            return Result("starnet", FAIL, f"{exe} is x86_64 and Rosetta 2 does not work",
                          "install Rosetta 2: softwareupdate --install-rosetta"), []
        return None, ["x86_64 via Rosetta"]
    return None, []


def _quarantine_check(folder: Path, files: list[Path], run: Callable) -> Result | None:
    flagged = [str(p.relative_to(folder)) for p in files if is_quarantined(p, run)]
    if flagged:
        return Result("starnet", FAIL,
                      f"quarantined by macOS ({', '.join(flagged)}); Gatekeeper will block it; doctor did not execute it",
                      f"allowing it is your decision; this clears the flag for the folder: "
                      f"xattr -dr {QUARANTINE} '{folder}'")
    return None


def parse_machine_info(text: str) -> dict | None:
    """The JSON object `starnet2 --machine-info` prints (possibly among other lines)."""
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        data = json.loads(text[a:b + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _find_backend(info) -> str | None:
    """Backend id from the machine-info JSON, wherever it sits (the key names are not documented)."""
    if isinstance(info, dict):
        for k, v in info.items():
            if any(w in k.lower() for w in ("backend", "provider", "engine")) and isinstance(v, (str, int, float)):
                return str(v)
        for v in info.values():
            r = _find_backend(v)
            if r:
                return r
    elif isinstance(info, list):
        for v in info:
            r = _find_backend(v)
            if r:
                return r
    return None


def _check_starnet2(folder: Path, where: str, run_sample: bool, run: Callable, system: str) -> Result:
    name = "starnet"
    exe = folder / STARNET2_EXE
    if not _is_exec(exe):
        return Result(name, FAIL, f"{exe} not executable", f"chmod +x '{exe}'")
    if not any((folder / w).exists() for w in STARNET2_WEIGHTS):
        return Result(name, FAIL, f"StarNet2 weights ({' or '.join(STARNET2_WEIGHTS)}) missing in {folder}",
                      "the weights ship with the StarNet2 command-line package; reinstall it "
                      "(`darkroom get-starnet --force`)")
    notes = [where] if where else []
    if system == "darwin":
        bad, more = _arch_check(exe, run)
        if bad:
            return bad
        notes += more
        libs = sorted(folder.glob("*.dylib")) + sorted((folder / "lib").glob("*.dylib"))
        bad = _quarantine_check(folder, [exe, *libs], run)
        if bad:
            return bad
    glibc_fix = ""
    if system.startswith("linux"):
        g = glibc_version()
        glibc_fix = (" The Linux build needs glibc 2.38 or newer (this system has "
                     f"{'.'.join(map(str, g)) if g else 'an unknown glibc'}).")
    fix = f"run ./starnet2 --machine-info in {folder} to see the full error." + glibc_fix
    try:
        rc, out = run(["./" + STARNET2_EXE, "--machine-info"], timeout=60, cwd=folder)
    except subprocess.TimeoutExpired:
        return Result(name, FAIL, "starnet2 --machine-info did not answer within 60 s", fix)
    except OSError as e:
        return Result(name, FAIL, f"cannot run {exe}: {e}", fix)
    info = parse_machine_info(out)
    if rc != 0 or info is None:
        last = out.strip().splitlines()[-1][:160] if out.strip() else "no output"
        return Result(name, FAIL, f"starnet2 --machine-info failed (exit {rc}): {last}", fix)
    version, build = str(info.get("version", "?")), info.get("build")
    backend = _find_backend(info)
    detail = (f"StarNet2 {version}" + (f" build {build}" if build else "") + (f", backend {backend}" if backend else "")
              + f" at {folder}")
    if run_sample:
        notes.append("--run-starnet only test-runs StarNet v2.0.2; StarNet2 was checked with --machine-info")
    if notes:
        detail += f" ({'; '.join(notes)})"
    if info.get("product") not in (None, "starnet2"):
        return Result(name, WARN, detail + f"; product {info.get('product')!r} is not starnet2",
                      "point --starnet-dir at a StarNet2 command-line folder")
    if not version.startswith(STARNET2_VERIFIED + "."):
        return Result(name, WARN, detail + f"; darkroom was verified with StarNet2 {STARNET2_VERIFIED}.x",
                      "it will probably work; compare a result with an earlier one")
    return Result(name, OK, detail)


def check_starnet(flag: str | None = None, run_sample: bool = False, run: Callable = run_cmd,
                  env: dict | None = None, system: str | None = None, installs: Callable | None = None) -> Result:
    from . import starnet as sn
    name = "starnet"
    env = os.environ if env is None else env
    system = sys.platform if system is None else system
    d, where = flag, ""
    if not d and env.get("DARKROOM_STARNET_DIR"):
        d = env["DARKROOM_STARNET_DIR"]
    if not d:
        found = (installs or sn.installed)()
        if not found:
            return Result(name, WARN, "not installed: run `darkroom get-starnet`",
                          "`darkroom get-starnet` downloads StarNet2 from starnetastro.com once you accept its "
                          "licence; or set DARKROOM_STARNET_DIR / --starnet-dir to a StarNet folder. Without "
                          "StarNet use the cluster look or `darkroom star-reduce`")
        d, where = str(found[0].dir), "installed by darkroom get-starnet"
    folder = Path(d).expanduser().resolve()
    if not folder.is_dir():
        return Result(name, FAIL, f"{folder} is not a directory",
                      "point DARKROOM_STARNET_DIR / --starnet-dir at a StarNet command-line folder")
    if (folder / STARNET2_EXE).is_file():
        return _check_starnet2(folder, where, run_sample, run, system)
    exe = folder / STARNET_EXE
    if not _is_exec(exe):
        what = "not executable" if exe.is_file() else "missing"
        return Result(name, FAIL, f"{exe} {what} (and no starnet2)",
                      f"chmod +x '{exe}'" if exe.is_file() else
                      "run `darkroom get-starnet`, or point --starnet-dir at a folder with starnet2 or starnet++")
    if not (folder / STARNET_WEIGHTS).is_file():
        return Result(name, FAIL, f"{STARNET_WEIGHTS} missing in {folder}",
                      "the weights file ships with the StarNet CLI download; put it next to starnet++")
    notes = []
    if system == "darwin":
        bad, notes = _arch_check(exe, run)
        if bad:
            return bad
        bad = _quarantine_check(folder, [exe, *sorted(folder.glob("*.dylib"))], run)
        if bad:
            return bad
    if not run_sample:
        return Result(name, OK, f"{folder} (StarNet v2 starnet++; {', '.join(notes + ['not executed; --run-starnet runs it'])})")
    sample = folder / STARNET_SAMPLE
    if not sample.is_file():
        return Result(name, WARN, f"{STARNET_SAMPLE} not in {folder}; cannot test-run",
                      "files look fine; run `darkroom starnet` on real data to confirm")
    tmp = Path(tempfile.mkdtemp(prefix="darkroom-doctor-"))
    out = tmp / "starless.tif"
    try:
        t = time.time()
        try:
            rc, log = run(["./" + STARNET_EXE, STARNET_SAMPLE, str(out)], timeout=600, cwd=folder)
        except subprocess.TimeoutExpired:
            return Result(name, FAIL, "test run did not finish within 600 s",
                          "run ./starnet++ rgb_test.tif out.tif in the StarNet folder to see what happens")
        except OSError as e:
            return Result(name, FAIL, f"cannot run {exe}: {e}",
                          "run ./starnet++ rgb_test.tif out.tif in the StarNet folder to see what happens")
        dt = time.time() - t
        if rc != 0 or not out.is_file():
            last = log.strip().splitlines()[-1][:160] if log.strip() else "no output"
            return Result(name, FAIL, f"test run failed (exit {rc}): {last}",
                          "run ./starnet++ rgb_test.tif out.tif in the StarNet folder to see the full error")
        return Result(name, OK, f"{folder} (StarNet v2 starnet++; {', '.join(notes + [f'sample run {dt:.0f}s'])})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 7. catalogues

def _probe(urls: list[str], get: Callable) -> tuple[bool, list[str]]:
    up, notes = False, []
    for u in urls:
        host = re.sub(r"^\w+://([^/]+).*", r"\1", u)
        try:
            status = get(u)
            ok = 200 <= status < 400
            notes.append(f"{host} {'up' if ok else f'HTTP {status}'}")
            up = up or ok
        except Exception as e:
            notes.append(f"{host} down ({_short_err(e)[:60]})")
    return up, notes


def check_catalogue(get: Callable = http_get) -> Result:
    name = "catalogue"
    with ThreadPoolExecutor(max_workers=2) as ex:
        fv = ex.submit(_probe, VIZIER_URLS, get)
        fe = ex.submit(_probe, ESA_URLS, get)
        (viz, vn), (esa, en) = fv.result(), fe.result()
    detail = "; ".join(vn + en)
    if viz:
        return Result(name, OK, detail)
    if esa:
        return Result(name, WARN, detail + "; VizieR unreachable, the color stage will use its Gaia fallback",
                      "nothing to do: `darkroom color` fetches Gaia DR3 from the ESA archive by itself "
                      "when Siril's catalogue server is down (expect about a minute longer)")
    return Result(name, FAIL, detail,
                  "check the internet connection (VPN, proxy, firewall); the color stage needs a catalogue")


# ---------------------------------------------------------------- 8. disk

def count_subs(subs: Path) -> int:
    return sum(1 for p in subs.glob("*.fit") if p.is_file())


def disk_need(n_subs: int) -> float:
    return n_subs * BYTES_PER_SUB + DISK_OVERHEAD


def _existing(p: Path) -> Path:
    p = p.expanduser().resolve()
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def check_disk(work: Path | None, subs: Path | None, free_of: Callable = lambda p: shutil.disk_usage(p).free,
               counter: Callable = count_subs) -> Result | None:
    name = "disk"
    need = n = None
    if subs is not None:
        subs = subs.expanduser()
        if not subs.is_dir():
            return Result(name, FAIL, f"subs directory {subs} does not exist", "check --subs")
        n = counter(subs)
        need = disk_need(n)
    if work is None:
        if need is None:
            return None
        return Result(name, WARN, f"{n} subs need ~{need / 1e9:.1f} GB; free space not checked",
                      "pass --work DIR to check free space where the run will write")
    vol = _existing(work)
    free = free_of(vol)
    if need is None:
        return Result(name, OK, f"{free / 1e9:.1f} GB free at {vol} (pass --subs to estimate the need)")
    detail = f"{free / 1e9:.1f} GB free at {vol}; {n} subs need ~{need / 1e9:.1f} GB"
    if free < need:
        return Result(name, FAIL, detail, "free up space or choose a --work directory on a bigger volume")
    if free < 1.5 * need:
        return Result(name, WARN, detail + " (less than 1.5x margin)",
                      "process/ can be deleted once stack.fit exists; consider freeing more space")
    return Result(name, OK, detail)


# ---------------------------------------------------------------- runner / output

def run_checks(args) -> list[Result]:
    """Run every applicable check (concurrently, to keep the wall time down); results in fixed order."""
    starnet_flag = args.starnet_dir or _config_starnet_dir(args.work)
    jobs: list[tuple[str, Callable[[], Result | None]]] = [
        ("system", check_system),
        ("python", check_python),
        ("siril", check_siril),
        ("graxpert", check_graxpert),
        ("denoise model", check_model),
        ("deconv model", lambda: check_deconv_model(enabled=_config_deconv_on(args.work))),
        ("gpu denoise", check_gpu),
        ("starnet", lambda: check_starnet(starnet_flag, args.run_starnet)),
    ]
    if not args.offline:
        jobs.append(("catalogue", check_catalogue))
    jobs.append(("disk", lambda: check_disk(args.work, args.subs)))

    def safe(name, fn):
        try:
            return fn()
        except Exception as e:       # a bug in one check must not hide the others
            return Result(name, FAIL, f"check crashed: {type(e).__name__}: {e}", "please report this")

    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = [ex.submit(safe, n, f) for n, f in jobs]
        return [r for r in (f.result() for f in futs) if r is not None]


def _config_starnet_dir(work: Path | None) -> str | None:
    """[starnet] dir from <work>/darkroom.toml, when --work is given and it sets one."""
    if work is None:
        return None
    try:
        from . import config
        return config.resolve(work)["starnet"]["dir"]
    except Exception:
        return None


def _config_deconv_on(work: Path | None) -> bool:
    """Whether <work>/darkroom.toml turns the optional deconv stage on (strength > 0)."""
    if work is None:
        return False
    try:
        from . import config
        return float(config.resolve(work)["deconv"]["strength"]) > 0
    except Exception:
        return False


def format_table(results: list[Result]) -> str:
    w = max((len(r.name) for r in results), default=0)
    lines = [f"{r.status:<4}  {r.name:<{w}}  {r.detail}" for r in results]
    fixes = [r for r in results if r.status != OK and r.fix]
    if fixes:
        lines += ["", "fixes:"] + [f"  {r.name}: {r.fix}" for r in fixes]
    return "\n".join(lines)


def exit_code(results: list[Result]) -> int:
    return 1 if any(r.status == FAIL for r in results) else 0


EPILOG = """\
environment:
  DARKROOM_SIRIL, DARKROOM_GRAXPERT, DARKROOM_GRAXPERT_MODEL, DARKROOM_DECONV_MODEL, DARKROOM_CACHE,
  DARKROOM_STARNET_DIR, DARKROOM_TOOLS_DIR (see `darkroom --help` for defaults)

exit status is 1 if any check FAILs, else 0. Doctor only reports; the one thing it may write is the cached fixed-batch denoise model.
"""

HELP = "check Siril, GraXpert, the denoise model and GPU, StarNet, catalogue servers and disk space"


def _add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--work", type=Path, metavar="DIR", default=None,
                   help="work directory: check free disk space on its volume")
    p.add_argument("--subs", type=Path, metavar="DIR", default=None,
                   help="directory with the light subs: estimate the space a run needs")
    p.add_argument("--starnet-dir", metavar="DIR", default=None,
                   help="folder containing starnet2 or starnet++ (default: [starnet] dir of --work, then "
                        "$DARKROOM_STARNET_DIR, then the newest StarNet2 installed by `darkroom get-starnet`)")
    p.add_argument("--run-starnet", action="store_true",
                   help="also run StarNet v2.0.2 on its bundled rgb_test.tif (about a minute); "
                        "StarNet2 is always checked with --machine-info instead")
    p.add_argument("--offline", action="store_true", help="skip the catalogue server checks")
    p.add_argument("--json", action="store_true", help="print results as JSON")


def run_args(args) -> int:
    results = run_checks(args)
    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        print(format_table(results))
        if args.offline:
            print("\n(catalogue check skipped: --offline)")
    return exit_code(results)


def add_parser(subparsers) -> argparse.ArgumentParser:
    """Register `doctor` on a subparsers object; sets func=run_args (returns the exit code)."""
    p = subparsers.add_parser("doctor", help=HELP, description=HELP, epilog=EPILOG,
                              formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_args(p)
    p.set_defaults(func=run_args)
    return p


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="darkroom doctor", description=HELP, epilog=EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_args(p)
    return run_args(p.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
