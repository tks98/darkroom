"""`darkroom setup`: install the pinned Siril and GraXpert builds natively, then the denoise model,
offer StarNet2, and run doctor.

For each tool it first looks for a working one (DARKROOM_SIRIL / DARKROOM_GRAXPERT, an earlier
managed install, or the platform default) and leaves it alone unless --force is given. A missing
tool is downloaded from its official site (or taken from --from DIR), checked against the pinned
size and SHA-256, and installed into <tools>/<tool>-<version>/ through a temporary folder that is
renamed into place only when everything has worked.

macOS: the dmg is mounted read-only, the app copied out with ditto and its signature checked with
codesign. darkroom then marks the copy as downloaded from the internet (com.apple.quarantine), as
a browser would, so that Gatekeeper assesses it like any download: Siril (notarized) must be
accepted by `spctl`, and GraXpert (not notarized) is installed only after the user agrees, and
stays blocked until the user allows it. darkroom never removes a quarantine flag.

Linux: the Siril AppImage is extracted and started through a small siril-cli wrapper that sets
APPDIR and runs its AppRun; the GraXpert zip is unpacked, and missing X11 client libraries are
reported with the apt command that installs them (setup does not install system packages).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import __version__, doctor, starnet, tools, toolset
from .toolset import Build

USER_AGENT = "darkroom"
CHUNK = 1 << 20
TIMEOUT = 60
DOWNLOAD_HOSTS = {"free-astro.org"}                 # no redirects allowed
GITHUB_HOST = "github.com"
GITHUB_PATH = "/Steffenhir/GraXpert/releases/download/"
GITHUB_REDIRECT_HOSTS = {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}

# GraXpert's Linux command line loads Tk, so it needs the X11 client libraries (Debian package names)
X11_LIBS = {"libX11.so.6": "libx11-6", "libXft.so.2": "libxft2", "libXss.so.1": "libxss1",
            "libXext.so.6": "libxext6", "libXrender.so.1": "libxrender1", "libGL.so.1": "libgl1",
            "libglib-2.0.so.0": "libglib2.0-0"}

SIRIL_WRAPPER = """\
#!/bin/sh
# Written by darkroom setup. Siril's AppImage ships its own dynamic loader and C library;
# starting siril-cli directly crashes on newer glibc, so go through the AppImage's launcher.
# APPDIR is what the AppImage runtime would set; Siril finds its bundled Python module with it.
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
APPDIR="$here/appdir"
export APPDIR
exec "$APPDIR/AppRun" siril-cli "$@"
"""


def run_cmd(cmd: list[str], timeout: float = 600, cwd: Path | None = None) -> tuple[int, str]:
    p = subprocess.run([str(c) for c in cmd], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=timeout)
    return p.returncode, p.stdout.decode(errors="replace")


@dataclass
class Ctx:
    plat: str
    run: Callable = run_cmd
    opener: Callable | None = None
    ask: Callable[[str], str] = input
    out: Callable = print
    interactive: bool = False
    failures: list[str] = field(default_factory=list)

    @property
    def macos(self) -> bool:
        return self.plat.startswith("macos")

    def must(self, cmd: list, what: str, timeout: float = 600, cwd: Path | None = None) -> str:
        try:
            rc, out = self.run([str(c) for c in cmd], timeout=timeout, cwd=cwd)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise tools.StageError(f"{what} failed: {e}") from None
        if rc != 0:
            last = "\n".join(out.strip().splitlines()[-5:])
            raise tools.StageError(f"{what} failed (exit {rc})" + (f":\n{last}" if last else ""))
        return out


# ---------------------------------------------------------------- download

def check_url(url: str) -> None:
    """Only https URLs of the pinned files on free-astro.org or GraXpert's GitHub releases."""
    u = urllib.parse.urlsplit(url)
    ok = (u.scheme == "https" and u.port in (None, 443) and not (u.username or u.password or u.query or u.fragment)
          and (u.hostname in DOWNLOAD_HOSTS
               or (u.hostname == GITHUB_HOST and u.path.startswith(GITHUB_PATH))))
    if not ok:
        raise tools.StageError(f"refusing to download {url}: not an official Siril or GraXpert download")


def allowed_redirects(url: str) -> set[str]:
    return GITHUB_REDIRECT_HOSTS if urllib.parse.urlsplit(url).hostname == GITHUB_HOST else set()


class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed: set[str]):
        super().__init__()
        self.allowed = allowed

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urllib.parse.urlsplit(newurl)
        if u.scheme != "https" or u.hostname not in self.allowed:
            raise urllib.error.HTTPError(newurl, code, f"redirect to {u.scheme}://{u.hostname} refused",
                                         headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_open(url: str, timeout: float = TIMEOUT):
    """GET with a generic User-Agent and TLS verification. Redirects are followed only for GitHub
    release downloads, and only to GitHub's own hosts."""
    from .catalog import _ssl_context
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ssl_context()),
                                         _Redirects(allowed_redirects(url)))
    return opener.open(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout)


def _short(e: BaseException) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return str(e.reason)
    return str(e) or type(e).__name__


def download(b: Build, dest: Path, opener: Callable | None = None, log=print) -> str:
    """Stream b.url into dest, checking the pinned size and SHA-256; return the SHA-256."""
    opener = opener or http_open
    check_url(b.url)
    allowed = {urllib.parse.urlsplit(b.url).hostname} | allowed_redirects(b.url)
    h, n = hashlib.sha256(), 0
    try:
        with opener(b.url, timeout=TIMEOUT) as r:
            final = r.geturl() if hasattr(r, "geturl") else b.url
            fu = urllib.parse.urlsplit(final)
            if fu.scheme != "https" or fu.hostname not in allowed:
                raise tools.StageError(f"the download was redirected to {fu.scheme}://{fu.hostname}; refused")
            cl = r.headers.get("Content-Length")
            expected = int(cl) if cl and cl.strip().isdigit() else None
            if expected is not None and expected != b.size:
                raise tools.StageError(f"the server says {b.filename} is {expected:,} bytes; the pinned size is "
                                       f"{b.size:,}. Not downloading it")
            log(f"downloading {b.url} ({b.size / 1e6:.0f} MB)")
            t, last = time.time(), 0
            with open(dest, "wb") as f:
                while True:
                    chunk = r.read(CHUNK)
                    if not chunk:
                        break
                    n += len(chunk)
                    if n > b.size:
                        raise tools.StageError(f"{b.filename} is larger than the pinned {b.size:,} bytes; stopped")
                    f.write(chunk)
                    h.update(chunk)
                    if n - last >= b.size // 10:
                        last = n
                        log(f"  {n / 1e6:.0f}/{b.size / 1e6:.0f} MB  {time.time() - t:.0f}s")
    except tools.StageError:
        raise
    except Exception as e:
        raise tools.StageError(f"download of {b.url} failed: {_short(e)}") from None
    if n != b.size:
        raise tools.StageError(f"download of {b.filename} is {n:,} bytes; the pinned size is {b.size:,}")
    digest = h.hexdigest()
    if digest != b.sha256:
        raise tools.StageError(f"SHA-256 mismatch for {b.filename}: got {digest}, expected {b.sha256}")
    return digest


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def local_file(from_dir: Path, b: Build) -> tuple[Path, str]:
    """The official file in from_dir (matched by its official name), checked; (path, sha256)."""
    p = from_dir / b.filename
    if not p.is_file():
        raise tools.StageError(f"{b.filename} not found in {from_dir}; --from needs the file under its "
                               f"official name (from {b.url})")
    size = p.stat().st_size
    if size != b.size:
        raise tools.StageError(f"{p} is {size:,} bytes; the pinned size of {b.filename} is {b.size:,}")
    digest = sha256_file(p)
    if digest != b.sha256:
        raise tools.StageError(f"SHA-256 mismatch for {p}: got {digest}, expected {b.sha256}")
    return p, digest


# ---------------------------------------------------------------- macOS

@contextmanager
def mounted(ctx: Ctx, dmg: Path):
    """Mount dmg read-only on a fresh temporary mount point; always detach afterwards."""
    mnt = Path(tempfile.mkdtemp(prefix="darkroom-dmg-"))
    attached = False
    try:
        ctx.must(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", mnt, dmg],
                 f"mounting {dmg.name}")
        attached = True
        yield mnt
    finally:
        if attached:
            try:
                rc, _ = ctx.run(["hdiutil", "detach", str(mnt)], timeout=120)
            except (OSError, subprocess.TimeoutExpired):
                rc = 1
            if rc != 0:
                try:
                    rc, _ = ctx.run(["hdiutil", "detach", "-force", str(mnt)], timeout=120)
                except (OSError, subprocess.TimeoutExpired):
                    rc = 1
            if rc != 0:
                ctx.out(f"warning: could not detach {mnt}; detach it with: hdiutil detach '{mnt}'")
        try:
            mnt.rmdir()
        except OSError:
            pass


def quarantine_stamp(now: float | None = None) -> str:
    """The value a browser writes: flags 0081 (downloaded; not approved), time in hex, agent name."""
    return f"0081;{int(time.time() if now is None else now):08x};darkroom;"


def bundle_version(app: Path) -> str | None:
    try:
        with open(app / "Contents" / "Info.plist", "rb") as f:
            return str(plistlib.load(f).get("CFBundleShortVersionString"))
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None


def stage_macos(ctx: Ctx, b: Build, dmg: Path, stage: Path) -> dict:
    app = stage / b.app
    with mounted(ctx, dmg) as mnt:
        src = mnt / b.app
        if not src.is_dir():
            raise tools.StageError(f"{dmg.name} has no {b.app}")
        ctx.out(f"copying {b.app} out of {dmg.name}")
        ctx.must(["ditto", src, app], f"copying {b.app}")
    if not (stage / b.exe).is_file():
        raise tools.StageError(f"{b.app} has no {Path(b.exe).relative_to(b.app)}")
    ctx.must(["codesign", "--verify", "--deep", "--strict", app], f"codesign --verify of {b.app}")
    ctx.out(f"codesign: {b.app} signature verified")
    bv = bundle_version(app)
    if b.tool == "siril" and bv != b.version:
        raise tools.StageError(f"{b.app} reports version {bv}, expected {b.version}")
    stamp = quarantine_stamp()
    ctx.must(["xattr", "-w", "-r", toolset.QUARANTINE, stamp, app], f"marking {b.app} as downloaded")
    value = toolset.quarantine_value(stage / b.exe, ctx.run)
    if value != stamp:
        raise tools.StageError(f"the quarantine flag on {b.app} did not stick (got {value!r})")
    accepted, source = toolset.assess(app, ctx.run)
    ctx.out(f"Gatekeeper: {'accepted' if accepted else 'rejected'}" + (f" ({source})" if source else ""))
    if b.notarized and not accepted:
        raise tools.StageError(f"Gatekeeper does not accept {b.app} ({source or 'rejected'}), although the "
                               f"official {b.name} build is notarized; not installing it")
    return {"bundle_version": bv, "codesign": "verified", "quarantined": True, "quarantine": stamp,
            "notarized": accepted, "gatekeeper": source}


# ---------------------------------------------------------------- Linux

def squashfs_offset(appimage: Path) -> int:
    """Where the squashfs image starts in an AppImage: after the ELF section headers."""
    with open(appimage, "rb") as f:
        d = f.read(64)
    if d[:4] != b"\x7fELF":
        raise tools.StageError(f"{appimage.name} is not an ELF AppImage")
    shoff = struct.unpack_from("<Q", d, 0x28)[0]
    shentsize, shnum = struct.unpack_from("<HH", d, 0x3A)
    return shoff + shentsize * shnum


def stage_appimage(ctx: Ctx, b: Build, appimage: Path, stage: Path, scratch: Path) -> dict:
    work = scratch / "extract"
    work.mkdir()
    img = work / b.filename
    shutil.copyfile(appimage, img)
    img.chmod(0o755)
    ctx.out(f"extracting {b.filename}")
    method = "--appimage-extract"
    try:
        rc, out = ctx.run([str(img), "--appimage-extract"], timeout=900, cwd=work)
    except (OSError, subprocess.TimeoutExpired) as e:
        rc, out = 1, str(e)
    root = work / "squashfs-root"
    if rc != 0 or not (root / "AppRun").exists():
        shutil.rmtree(root, ignore_errors=True)
        if not shutil.which("unsquashfs"):
            raise tools.StageError(f"{b.filename} --appimage-extract failed (exit {rc}) and unsquashfs is not "
                                   "installed (apt install squashfs-tools)")
        method = "unsquashfs"
        ctx.must(["unsquashfs", "-q", "-o", str(squashfs_offset(img)), "-d", root, img],
                 f"unpacking {b.filename} with unsquashfs")
    img.unlink()
    if not (root / "AppRun").exists():
        raise tools.StageError(f"{b.filename} has no AppRun")
    os.rename(root, stage / "appdir")
    wrapper = stage / b.exe
    wrapper.write_text(SIRIL_WRAPPER)
    wrapper.chmod(0o755)
    return {"extracted_with": method}


def _safe(name: str) -> bool:
    p = Path(name)
    return bool(name) and not name.startswith(("/", "\\")) and ".." not in p.parts and ":" not in p.parts[0]


def unzip(zip_path: Path, into: Path) -> None:
    """Unpack a zip keeping executable bits and in-tree symlinks; refuse unsafe paths."""
    try:
        z = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as e:
        raise tools.StageError(f"{zip_path.name} is not a readable zip: {e}") from None
    with z:
        infos = [i for i in z.infolist() if not i.filename.startswith("__MACOSX/")]
        for i in infos:
            if not _safe(i.filename):
                raise tools.StageError(f"{zip_path.name} contains an unsafe path: {i.filename!r}")
        into.mkdir(parents=True, exist_ok=True)
        for i in infos:
            target = into / i.filename
            mode = i.external_attr >> 16
            if i.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if stat.S_ISLNK(mode):
                link = z.read(i).decode()
                rel = os.path.normpath(os.path.join(os.path.dirname(i.filename), link))
                if os.path.isabs(link) or rel.startswith(".."):
                    raise tools.StageError(f"{zip_path.name} has a symlink pointing outside it: {i.filename}")
                os.symlink(link, target)
                continue
            with z.open(i) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, CHUNK)
            os.chmod(target, 0o755 if mode & 0o111 else 0o644)


def stage_zip(ctx: Ctx, b: Build, zip_path: Path, stage: Path) -> dict:
    ctx.out(f"unpacking {b.filename}")
    unzip(zip_path, stage)
    exe = stage / b.exe
    if not exe.is_file():
        raise tools.StageError(f"{b.filename} has no {b.exe}")
    exe.chmod(0o755)
    return {}


def missing_x11(run: Callable = run_cmd) -> list[str] | None:
    """Debian packages for the X11 libraries GraXpert needs that `ldconfig -p` does not list;
    None when ldconfig cannot be run."""
    ldconfig = shutil.which("ldconfig") or "/sbin/ldconfig"
    try:
        rc, out = run([ldconfig, "-p"], timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if rc != 0:
        return None
    have = {line.strip().split(" ", 1)[0] for line in out.splitlines()}
    return [pkg for lib, pkg in X11_LIBS.items() if lib not in have]


def x11_note(ctx: Ctx) -> None:
    missing = missing_x11(ctx.run)
    if missing is None:
        ctx.out("note: could not run ldconfig to check GraXpert's X11 libraries; on Debian or Ubuntu they are: "
                "sudo apt-get install " + " ".join(X11_LIBS.values()))
    elif missing:
        ctx.out("GraXpert's command line needs X11 client libraries that are missing here. darkroom does not "
                "install system packages; on Debian or Ubuntu run:\n  sudo apt-get install " + " ".join(missing))
        ctx.failures.append("graxpert: X11 libraries missing")


# ---------------------------------------------------------------- install

def replace_into(stage: Path, dest: Path) -> None:
    """Move the finished install into place; an older one at dest is replaced only after the
    rename has worked (and put back if it fails)."""
    old = None
    if dest.exists() or dest.is_symlink():
        old = dest.with_name(f".old-{dest.name}-{os.getpid()}")
        os.rename(dest, old)
    try:
        os.rename(stage, dest)
    except OSError:
        if old is not None:
            os.rename(old, dest)
        raise
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)


def install(ctx: Ctx, b: Build, from_dir: Path | None) -> toolset.Installed:
    root = toolset.tools_dir()
    root.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f".incoming-{b.tool}-", dir=root))
    try:
        if from_dir is not None:
            src, digest = local_file(from_dir, b)
            source = f"local file {src.name}"
            ctx.out(f"{b.filename}: size and SHA-256 match the pinned values")
        else:
            src = scratch / b.filename
            digest = download(b, src, ctx.opener, ctx.out)
            source = "download"
            ctx.out(f"sha256 {digest} (matches the pinned hash)")
        stage = scratch / b.folder
        stage.mkdir()
        if b.kind == "dmg":
            extra = stage_macos(ctx, b, src, stage)
        elif b.kind == "appimage":
            extra = stage_appimage(ctx, b, src, stage, scratch)
        elif b.kind == "zip":
            extra = stage_zip(ctx, b, src, stage)
        else:
            raise tools.StageError(f"unknown package kind {b.kind}")
        record = {"tool": b.tool, "version": b.version, "platform": b.platform, "url": b.url, "sha256": digest,
                  "size": b.size, "filename": b.filename, "source": source,
                  "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "exe": b.exe,
                  "installed_by": f"darkroom {__version__}", **extra}
        (stage / toolset.RECORD).write_text(json.dumps(record, indent=2) + "\n")
        dest = root / b.folder
        replace_into(stage, dest)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    ctx.out(f"installed {b.describe()} in {dest}")
    return toolset.Installed(dest, record)


# ---------------------------------------------------------------- steps

def check_existing(tool: str, path: Path | None = None) -> doctor.Result:
    return (doctor.check_siril if tool == "siril" else doctor.check_graxpert)(path)


def works(tool: str, r: doctor.Result) -> bool:
    """A version was read from it (doctor's OK, or a WARN about an unverified version)."""
    return r.status != doctor.FAIL and r.detail.startswith(toolset.TOOL_NAMES[tool] + " ")


UNNOTARIZED = """\
{name}'s macOS build is not notarized: Apple has not checked it for malicious software.
darkroom checks that the file is the official one (pinned size and SHA-256), installs it in
{dest}, and marks it as downloaded from the internet, as a browser would.
macOS will then block {name} until you allow it yourself; darkroom will not run it before that
and never clears the quarantine flag. How to allow it is printed after the install."""


def confirm_unnotarized(ctx: Ctx, b: Build, allow_flag: bool, dest: Path) -> bool:
    ctx.out(UNNOTARIZED.format(name=b.name, dest=dest))
    if allow_flag:
        ctx.out("Confirmed with --allow-unnotarized.")
        return True
    if not ctx.interactive:
        ctx.out(f"Not installing {b.name}: this is not an interactive terminal. Run again in a terminal and "
                "type yes, or pass --allow-unnotarized.")
        return False
    try:
        answer = ctx.ask(f'Type "yes" to install {b.name} anyway (anything else skips it): ')
    except (EOFError, KeyboardInterrupt):
        ctx.out("")
        return False
    return answer.strip().lower() == "yes"


def setup_tool(ctx: Ctx, tool: str, a) -> None:
    b = toolset.build_for(tool, ctx.plat)
    env = toolset.ENV_VARS[tool]
    if not a.force:
        r = check_existing(tool)
        if works(tool, r):
            ctx.out(f"{b.name}: found {r.detail}; nothing to install (--force installs a managed copy)")
            return
        if "quarantined" in r.detail and "not notarized" in r.detail:
            ctx.out(f"{b.name}: {r.detail}. Not reinstalling it: {r.fix}")
            ctx.failures.append(f"{tool}: blocked by Gatekeeper until you allow it")
            return
        ctx.out(f"{b.name}: {r.detail}; installing {b.describe()}")
    else:
        ctx.out(f"{b.name}: --force: installing {b.describe()}")
    if a.from_ is None and a.offline:
        ctx.out(f"{b.name}: not installed: --offline and no --from DIR")
        ctx.failures.append(f"{tool}: not installed (offline)")
        return
    dest = toolset.tools_dir() / b.folder
    if ctx.macos and b.notarized is False and not confirm_unnotarized(ctx, b, a.allow_unnotarized, dest):
        ctx.out(f"{b.name}: not installed; nothing was downloaded")
        ctx.failures.append(f"{tool}: not notarized, not confirmed")
        return
    try:
        inst = install(ctx, b, a.from_)
    except tools.StageError as e:
        ctx.out(f"{b.name}: install failed, nothing was installed: {e}")
        ctx.failures.append(f"{tool}: {e}".splitlines()[0])
        return
    if os.environ.get(env):
        ctx.out(f"note: ${env} is set and takes precedence over this install; unset it to use {inst.exe}")
    if ctx.macos and not inst.record.get("notarized"):
        app = inst.dir / b.app
        how = toolset.allow_instructions(app)
        ctx.out(f"{b.name} is installed but quarantined, and macOS will block it until you allow it. "
                + how[0].upper() + how[1:] + ". Then run `darkroom fetch-models` and `darkroom doctor`.")
    if not ctx.macos and tool == "graxpert":
        x11_note(ctx)


def fetch_models(ctx: Ctx) -> None:
    from . import models
    ctx.out("== denoise model")
    try:
        models.fetch_denoise_model(log=ctx.out)
    except tools.StageError as e:
        ctx.out(f"could not fetch the denoise model: {e}")
        ctx.failures.append("denoise model: not fetched")


def starnet_found(starnet_dir: str | None) -> str | None:
    if starnet_dir:
        return starnet_dir
    if os.environ.get("DARKROOM_STARNET_DIR"):
        return os.environ["DARKROOM_STARNET_DIR"]
    found = starnet.installed()
    return str(found[0].dir) if found else None


def offer_starnet(ctx: Ctx, starnet_dir: str | None, get_starnet_run: Callable | None = None) -> None:
    ctx.out("== StarNet")
    where = starnet_found(starnet_dir)
    if where:
        ctx.out(f"StarNet: found {where}")
        return
    if not ctx.interactive:
        ctx.out("StarNet2 is not installed; skipped because this is not an interactive terminal. Run "
                "`darkroom get-starnet` to install it (you will be shown its licence and asked to accept it).")
        return
    try:
        answer = ctx.ask("StarNet2 (star removal) is not installed. Install it now? Its licence will be shown "
                         "and you must accept it. [y/N] ")
    except (EOFError, KeyboardInterrupt):
        ctx.out("")
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        ctx.out("StarNet2 skipped; `darkroom get-starnet` installs it later.")
        return
    from . import get_starnet
    # get-starnet shows the licence and asks for acceptance itself; acceptance is never passed on
    ns = argparse.Namespace(list=False, zip=None, dry_run=False, force=False, accept_license=False)
    try:
        (get_starnet_run or get_starnet.run)(ns, interactive=True, ask=ctx.ask, out=ctx.out)
    except tools.StageError as e:
        ctx.out(f"StarNet2 not installed: {e}")


def dry_run(ctx: Ctx, only: list[str], a) -> int:
    root = toolset.tools_dir()
    ctx.out(f"platform:  {ctx.plat} ({toolset.PLATFORM_NAMES[ctx.plat]})")
    ctx.out(f"tools dir: {root}")
    for tool in only:
        b = toolset.build_for(tool, ctx.plat)
        exe, source = tools.resolve_tool(tool)
        state = "exists" if exe.is_file() else "missing"
        ctx.out(f"\n{b.describe()}")
        ctx.out(f"  now:      {exe} ({source}; {state}); setup checks its version and skips it if it works"
                + ("" if not a.force else "; --force installs anyway"))
        ctx.out(f"  source:   {(a.from_ / b.filename) if a.from_ else b.url}")
        ctx.out(f"  size:     {b.size:,} bytes")
        ctx.out(f"  sha256:   {b.sha256}")
        ctx.out(f"  install:  {root / b.folder}")
        ctx.out(f"  run:      {root / b.folder / b.exe}")
        if ctx.macos:
            ctx.out("  macOS:    " + ("notarized; Gatekeeper must accept it" if b.notarized else
                                      "NOT notarized; needs your confirmation (or --allow-unnotarized), "
                                      "and stays blocked until you allow it"))
    ctx.out("\nthen: " + ", ".join(
        (["fetch-models"] if not (a.no_models or a.offline) else [])
        + (["offer StarNet2 (get-starnet, with its own licence prompt)"] if not (a.no_starnet or a.offline) else [])
        + ["doctor"]))
    ctx.out("dry run: nothing was downloaded or installed")
    return 0


def parse_only(s: str | None) -> list[str]:
    if not s:
        return list(toolset.TOOLS)
    names = [x.strip().lower() for x in s.split(",") if x.strip()]
    bad = [n for n in names if n not in toolset.TOOLS]
    if bad or not names:
        raise tools.StageError(f"--only takes a comma-separated list of {', '.join(toolset.TOOLS)}; got {s!r}")
    return [t for t in toolset.TOOLS if t in names]


def run(a, ctx: Ctx | None = None, get_starnet_run: Callable | None = None,
        doctor_checks: Callable | None = None) -> int:
    only = parse_only(a.only)
    if a.from_ is not None:
        a.from_ = Path(a.from_).expanduser().resolve()
        if not a.from_.is_dir():
            raise tools.StageError(f"--from {a.from_}: not a directory")
    if ctx is None:
        ctx = Ctx(plat=toolset.platform_key(), interactive=sys.stdin.isatty())
    if a.dry_run:
        return dry_run(ctx, only, a)
    ctx.out(f"== tools ({toolset.PLATFORM_NAMES[ctx.plat]}; managed installs go to {toolset.tools_dir()})")
    for tool in only:
        setup_tool(ctx, tool, a)
    if a.no_models:
        ctx.out("== denoise model skipped (--no-models)")
    elif a.offline:
        ctx.out("== denoise model skipped (--offline)")
    else:
        fetch_models(ctx)
    if a.no_starnet:
        ctx.out("== StarNet offer skipped (--no-starnet)")
    elif a.offline:
        ctx.out("== StarNet offer skipped (--offline)")
    else:
        offer_starnet(ctx, a.starnet_dir, get_starnet_run)
    ctx.out("== darkroom doctor")
    ns = argparse.Namespace(work=None, subs=None, starnet_dir=a.starnet_dir, run_starnet=False,
                            offline=a.offline, json=False)
    results = (doctor_checks or doctor.run_checks)(ns)
    ctx.out(doctor.format_table(results))
    if ctx.failures:
        ctx.out("\nsetup did not finish: " + "; ".join(ctx.failures))
        return 1
    return doctor.exit_code(results)


HELP = "install Siril and GraXpert natively, fetch the denoise model, offer StarNet2, then run doctor"

EPILOG = """\
For Siril and GraXpert, setup first checks the one darkroom would use now
($DARKROOM_SIRIL / $DARKROOM_GRAXPERT, an earlier `darkroom setup` install, or the
standard location) and leaves a working one alone. A missing one is downloaded from
free-astro.org (Siril 1.4.5) or GraXpert's GitHub releases (3.1.0rc2), checked
against the pinned size and SHA-256, and installed in <tools>/<tool>-<version>/,
which darkroom then uses unless the environment variable names another.

tools: $DARKROOM_TOOLS_DIR (default ~/Library/Application Support/darkroom/tools on
macOS, ${XDG_DATA_HOME:-~/.local/share}/darkroom/tools on Linux)

macOS: the copy is marked as downloaded from the internet (quarantined), as a browser
would. Siril is notarized and must pass Gatekeeper. GraXpert's macOS build is not
notarized: setup asks you to type yes first (or --allow-unnotarized), and macOS
blocks it until you allow it yourself; setup tells you how and never does it.

Linux: the Siril AppImage is extracted and run through a siril-cli wrapper; GraXpert
needs X11 client libraries, which setup reports but does not install.

Then: `darkroom fetch-models`, an offer to run `darkroom get-starnet` (which shows
StarNet2's licence and asks you to accept it), and `darkroom doctor`.
"""


def add_parser(subparsers) -> argparse.ArgumentParser:
    p = subparsers.add_parser("setup", help=HELP, description=HELP, epilog=EPILOG,
                              formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--force", action="store_true",
                   help="install a managed copy even if a working Siril/GraXpert is found (it then takes "
                        "precedence over the standard location); reinstalls an earlier managed copy")
    p.add_argument("--from", dest="from_", metavar="DIR", default=None,
                   help="use the official files already downloaded into DIR (under their official names; "
                        "size and SHA-256 are still checked) instead of downloading")
    p.add_argument("--only", metavar="LIST", default=None, help="comma-separated: siril, graxpert (default: both)")
    p.add_argument("--no-models", action="store_true", help="do not fetch GraXpert's denoise model")
    p.add_argument("--no-starnet", action="store_true", help="do not offer to install StarNet2")
    p.add_argument("--allow-unnotarized", action="store_true",
                   help="macOS: install GraXpert, whose macOS build Apple has not notarized, without the "
                        "yes prompt (it stays quarantined and blocked until you allow it)")
    p.add_argument("--starnet-dir", metavar="DIR", default=None,
                   help="StarNet folder to check (as for doctor); no StarNet2 is offered when it is set")
    p.add_argument("--offline", action="store_true",
                   help="no network: install only from --from, skip fetch-models, the StarNet2 offer and "
                        "doctor's catalogue check")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be downloaded and where (no network, nothing installed)")
    p.set_defaults(func=run)
    return p
