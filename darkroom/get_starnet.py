"""`darkroom get-starnet`: install StarNet2 from its official site, after the user accepts its licence.

What it does, in order:

1. Picks the command-line build for this machine (macOS arm64: CoreML; macOS x86_64 and
   Linux x86_64: ONNX Runtime).
2. Reads the official CLI page (CLI_PAGE) for the current download link and the SHA-256 it
   publishes for it; if the page cannot be read or has no such link, it uses the pinned 2.6.2
   link and says so.
3. Downloads only from https://download.starnetastro.com/ (redirects elsewhere are refused),
   with TLS verification, checking the size and SHA-256. The 2.6.2 builds are pinned in
   PINNED. For any other build darkroom has no pinned hash: it prints the file's size and
   SHA-256 as downloaded, says so loudly, and continues only when the user types ``yes`` or
   passes --allow-unpinned (a download without a pinned size is capped at MAX_UNPINNED).
4. Reads only the package's LICENSE.txt out of the zip, prints it in full, and asks for
   acceptance: type ``accept`` at the prompt, or pass --accept-license (the licence is
   printed either way). Without acceptance it deletes the download and installs nothing;
   nothing else in the zip has been unpacked at that point.
5. Unpacks the zip (see darkroom.archive) into a temporary folder. On macOS it marks the
   package as downloaded from the internet (com.apple.quarantine), as a browser would, and
   asks Gatekeeper (spctl) for its verdict, like `darkroom setup` does for Siril and GraXpert;
   it never removes a quarantine flag.
6. Moves the package to <cache>/starnet/<zip name>/ and writes accepted.json there.

StarNet is never copied anywhere else, and nothing it produces is kept by darkroom.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from . import __version__, archive, starnet, tools, toolset
from .installer import RedirectRefused, redirect_refused_message

CLI_PAGE = "https://starnetastro.com/cli-tools/starnet/"
PAGE_HOSTS = {"starnetastro.com", "www.starnetastro.com"}
DOWNLOAD_HOST = "download.starnetastro.com"
DOWNLOAD_BASE = f"https://{DOWNLOAD_HOST}/"
USER_AGENT = "darkroom"
PAGE_LIMIT = 4_000_000
CHUNK = 1 << 20
TIMEOUT = 60
MAX_UNPINNED = 1 << 30          # the 2.6.2 zips are 120-150 MB; nothing without a pinned size may exceed this
LICENSE_LIMIT = 1_000_000
LICENSE_NAMES = ("LICENSE.txt", "LICENSE", "License.txt", "license.txt")

PLATFORM_NAMES = {"macos-arm64": "macOS on Apple Silicon", "macos-x64": "macOS on Intel",
                  "linux": "Linux x86_64", "win": "Windows"}
BACKEND_PREFIX = {"macos-arm64": "COREML", "macos-x64": "ORT", "linux": "ORT"}

_LINK = re.compile(r"""href\s*=\s*["'](https://download\.starnetastro\.com/"""
                   r"""starnet2_(?P<platform>[a-z0-9-]+?)_(?P<version>\d+(?:\.\d+)+)-(?P<build>\d+)_"""
                   r"""(?P<backend>[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*?)_cli\.zip)["']""")
_FILE = re.compile(r"starnet2_(?P<platform>[a-z0-9-]+?)_(?P<version>\d+(?:\.\d+)+)-(?P<build>\d+)_"
                   r"(?P<backend>[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*?)_cli\.zip")
_HASH = re.compile(r"<code>\s*([0-9a-fA-F]{64})\s*</code>")


@dataclass(frozen=True)
class Package:
    platform: str               # macos-arm64 | macos-x64 | linux | win
    version: str
    build: str
    backend: str                # e.g. COREML_arm64, ORT_x64
    sha256: str | None = None   # pinned in darkroom (PINNED)
    size: int | None = None     # pinned in darkroom
    published_sha256: str | None = None     # as shown on CLI_PAGE next to the link

    @property
    def filename(self) -> str:
        return f"starnet2_{self.platform}_{self.version}-{self.build}_{self.backend}_cli.zip"

    @property
    def stem(self) -> str:
        return self.filename[:-len(".zip")]

    @property
    def url(self) -> str:
        return DOWNLOAD_BASE + self.filename

    def describe(self) -> str:
        return (f"StarNet2 {self.version} (build {self.build}), command-line version for "
                f"{PLATFORM_NAMES.get(self.platform, self.platform)} ({self.backend.replace('_', ', ')})")


# The SHA-256 values are the ones CLI_PAGE publishes for these zips; the sizes are the server's
# Content-Length (from a HEAD request). None of them needed a download, or the licence.
PINNED = {
    "macos-arm64": Package("macos-arm64", "2.6.2", "0242", "COREML_arm64",
                           sha256="dfeb471e983a9de1233b439a8c56d3e373ed26e37c85aababe737187d164d20f",
                           size=127_977_753),
    "macos-x64": Package("macos-x64", "2.6.2", "0242", "ORT_x64",
                         sha256="e0af667ae9d1a0e6d869d5fdef1738aa2c9077dc62bcbada706a88d9cc6a524a",
                         size=151_943_354),
    "linux": Package("linux", "2.6.2", "0242", "ORT_x64",
                     sha256="066a7683f0321ae8400d3bc1023a48747ac228e1d14e76b5d39c4e48cbce4920", size=151_481_786),
}


def _with_pins(pkg: Package) -> Package:
    pin = PINNED.get(pkg.platform)
    if pin and pin.filename == pkg.filename:
        return replace(pkg, sha256=pin.sha256, size=pin.size)
    return pkg


# ---------------------------------------------------------------- platform

def _rosetta(run=subprocess.run) -> bool:
    """True when this (x86_64) Python runs under Rosetta on an Apple Silicon Mac."""
    try:
        p = run(["sysctl", "-in", "sysctl.proc_translated"], capture_output=True, text=True, timeout=5)
        return p.stdout.strip() == "1"
    except (OSError, subprocess.TimeoutExpired):
        return False


def platform_key(system: str | None = None, machine: str | None = None, rosetta: Callable = _rosetta) -> str:
    system = sys.platform if system is None else system
    machine = (_platform.machine() if machine is None else machine).lower()
    if system == "darwin":
        if machine in ("arm64", "aarch64"):
            return "macos-arm64"
        if machine in ("x86_64", "amd64"):
            return "macos-arm64" if rosetta() else "macos-x64"
    elif system.startswith("linux") and machine in ("x86_64", "amd64"):
        return "linux"
    raise tools.StageError(f"no StarNet2 command-line build is known for {system} {machine}; darkroom can fetch "
                           "the builds for macOS (arm64, x86_64) and Linux x86_64. Download StarNet from "
                           "starnetastro.com yourself and pass --starnet-dir")


# ---------------------------------------------------------------- URLs

def check_official(url: str) -> None:
    """Refuse anything that is not an https URL of a StarNet2 CLI zip on the official download host."""
    u = urllib.parse.urlsplit(url)
    if (u.scheme != "https" or u.hostname != DOWNLOAD_HOST or u.port not in (None, 443)
            or u.username or u.password or u.query or u.fragment
            or not _FILE.fullmatch(u.path.lstrip("/"))):
        raise tools.StageError(f"refusing to download {url}: StarNet2 is only fetched from {DOWNLOAD_BASE}")


def published_hashes(html: str) -> dict[str, str]:
    """{zip file name: SHA-256} as CLI_PAGE shows them: each hash is followed, before the next hash,
    by the links of the package it belongs to."""
    out: dict[str, str] = {}
    found = list(_HASH.finditer(html))
    for k, m in enumerate(found):
        end = found[k + 1].start() if k + 1 < len(found) else len(html)
        f = _FILE.search(html, m.end(), min(end, m.end() + 4000))
        if f:
            out.setdefault(f.group(0), m.group(1).lower())
    return out


def parse_page(html: str, plat: str) -> Package | None:
    """The newest StarNet2 CLI zip for ``plat`` linked from the official page, or None."""
    best = None
    for m in _LINK.finditer(html):
        if m.group("platform") != plat or not m.group("backend").startswith(BACKEND_PREFIX.get(plat, "?")):
            continue
        pkg = Package(plat, m.group("version"), m.group("build"), m.group("backend"))
        try:
            check_official(m.group(1))
        except tools.StageError:
            continue
        key = starnet.version_tuple(pkg.version) + starnet.version_tuple(pkg.build)
        if best is None or key > best[0]:
            best = (key, pkg)
    if not best:
        return None
    pkg = best[1]
    return _with_pins(replace(pkg, published_sha256=published_hashes(html).get(pkg.filename)))


# ---------------------------------------------------------------- HTTP

class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed: set[str]):
        super().__init__()
        self.allowed = allowed

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urllib.parse.urlsplit(newurl)
        if u.scheme != "https" or u.hostname not in self.allowed:
            why = "the server " + redirect_refused_message(u.scheme, u.hostname, self.allowed)
            raise RedirectRefused(newurl, code, why, headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_open(url: str, timeout: float = TIMEOUT):
    """GET with a generic User-Agent and TLS verification; redirects only within the same site."""
    from .catalog import _ssl_context
    host = urllib.parse.urlsplit(url).hostname
    allowed = {DOWNLOAD_HOST} if host == DOWNLOAD_HOST else PAGE_HOSTS
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ssl_context()), _Redirects(allowed))
    return opener.open(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout)


def resolve(plat: str, opener: Callable | None = None) -> tuple[Package, str | None]:
    """(package, note): from the official page, else the pinned build (the note says why)."""
    opener = opener or http_open
    pinned = PINNED.get(plat)
    try:
        with opener(CLI_PAGE, timeout=30) as r:
            html = r.read(PAGE_LIMIT).decode("utf-8", errors="replace")
    except Exception as e:
        if pinned is None:
            raise tools.StageError(f"could not read {CLI_PAGE} ({_short(e)}) and no build is pinned for {plat}")
        return pinned, f"could not read {CLI_PAGE} ({_short(e)}); using the pinned {pinned.version} link"
    pkg = parse_page(html, plat)
    if pkg is None:
        if pinned is None:
            raise tools.StageError(f"{CLI_PAGE} has no StarNet2 command-line link for {plat}")
        return pinned, f"found no {plat} download link on {CLI_PAGE}; using the pinned {pinned.version} link"
    return pkg, None


def _short(e: BaseException) -> str:
    if isinstance(e, RedirectRefused):
        return str(e.reason)
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return str(e.reason)
    return str(e) or type(e).__name__


def download(pkg: Package, dest: Path, opener: Callable | None = None, log=print) -> str:
    """Stream pkg.url into dest; check its size (pinned, else Content-Length, never more than
    MAX_UNPINNED without a pinned size) and SHA-256 (pinned and published); return the SHA-256."""
    opener = opener or http_open
    check_official(pkg.url)
    h, n = hashlib.sha256(), 0
    try:
        with opener(pkg.url, timeout=TIMEOUT) as r:
            final = r.geturl() if hasattr(r, "geturl") else pkg.url
            fu = urllib.parse.urlsplit(final)
            if fu.scheme != "https" or fu.hostname != DOWNLOAD_HOST:
                raise tools.StageError(f"the download of {pkg.filename} was "
                                       + redirect_refused_message(fu.scheme, fu.hostname, {DOWNLOAD_HOST}))
            cl = r.headers.get("Content-Length")
            expected = int(cl) if cl and cl.strip().isdigit() else None
            if pkg.size is not None and expected is not None and expected != pkg.size:
                raise tools.StageError(f"the server says {pkg.filename} is {expected:,} bytes; the pinned size "
                                       f"is {pkg.size:,}. Not downloading it")
            if pkg.size is None and expected is not None and expected > MAX_UNPINNED:
                raise tools.StageError(f"the server says {pkg.filename} is {expected:,} bytes, more than the "
                                       f"{MAX_UNPINNED:,} darkroom accepts for a build without a pinned size. "
                                       "Not downloading it")
            cap = pkg.size if pkg.size is not None else (expected if expected is not None else MAX_UNPINNED)
            if expected is None:
                log("note: the server sent no Content-Length"
                    + ("" if pkg.size is not None else
                       f"; the download is stopped if it grows past {MAX_UNPINNED / 1e6:.0f} MB"))
            size = expected or pkg.size
            log(f"downloading {pkg.url}" + (f" ({size / 1e6:.0f} MB)" if size else ""))
            t, last = time.time(), 0
            with open(dest, "wb") as f:
                while True:
                    chunk = r.read(CHUNK)
                    if not chunk:
                        break
                    n += len(chunk)
                    if n > cap:
                        raise tools.StageError(f"{pkg.filename} is larger than the {cap:,} bytes expected; stopped")
                    f.write(chunk)
                    h.update(chunk)
                    if size and n - last >= size // 10:
                        last = n
                        log(f"  {n / 1e6:.0f}/{size / 1e6:.0f} MB  {time.time() - t:.0f}s")
    except tools.StageError:
        raise
    except Exception as e:
        raise tools.StageError(f"download of {pkg.url} failed: {_short(e)}") from None
    if expected is not None and n != expected:
        raise tools.StageError(f"download incomplete: got {n:,} of {expected:,} bytes")
    if pkg.size is not None and n != pkg.size:
        raise tools.StageError(f"download is {n:,} bytes; the pinned size is {pkg.size:,}")
    digest = h.hexdigest()
    check_digest(pkg, digest, pkg.filename)
    return digest


def check_digest(pkg: Package, digest: str, what: str) -> None:
    if pkg.sha256 and digest != pkg.sha256:
        raise tools.StageError(f"SHA-256 mismatch for {what}: got {digest}, expected {pkg.sha256}")
    if pkg.published_sha256 and digest != pkg.published_sha256:
        raise tools.StageError(f"SHA-256 mismatch for {what}: got {digest}, but {CLI_PAGE} publishes "
                               f"{pkg.published_sha256}")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- unpinned builds

UNPINNED = """\
{bar}
WARNING: darkroom has no pinned SHA-256 for this StarNet2 build, so it cannot check that the
file is the one StarNet's authors released.
  file:     {file}
  size:     {size:,} bytes
  SHA-256:  {digest}  (as {how})
{published}
Continue only if you trust this file.
{bar}"""


def confirm_unpinned(pkg: Package, digest: str, size: int, source: str, allow_flag: bool, interactive: bool,
                     ask: Callable[[str], str], out: Callable) -> str | None:
    """Warn that the file is not pinned and get the user's go-ahead: "--allow-unpinned", "prompt" or None."""
    if pkg.published_sha256:
        published = (f"It matches the SHA-256 that {CLI_PAGE} shows for it. That page is on the same site as\n"
                     "the download, so this rules out a damaged download, not a compromised site.")
    else:
        published = f"darkroom found no published SHA-256 for it either; compare it yourself with {CLI_PAGE}."
    out(UNPINNED.format(bar="!" * 78, file=pkg.filename, size=size, digest=digest, how=source,
                        published=published))
    if allow_flag:
        out("Continuing with --allow-unpinned.")
        return "--allow-unpinned"
    if not interactive:
        out("Not installing it: this is not an interactive terminal. Run again in a terminal and type yes, or "
            "pass --allow-unpinned.")
        return None
    try:
        answer = ask('Type "yes" to continue with this unverified file (anything else cancels): ')
    except (EOFError, KeyboardInterrupt):
        out("")
        return None
    return "prompt" if answer.strip().lower() == "yes" else None


# ---------------------------------------------------------------- the zip

def package_prefix(z: zipfile.ZipFile) -> str:
    """'<folder>/' when every entry is inside one top folder (as in the official zips), else ''."""
    parts = [archive._parts(i.filename) for i in archive.members(z)]
    tops = {p[0] for p in parts if p}
    if len(tops) == 1:
        top = next(iter(tops))
        only_top = [i for i, p in zip(archive.members(z), parts) if p == [top]]
        if any(len(p) > 1 for p in parts) and not any(archive.is_symlink(i) or not i.is_dir() for i in only_top):
            return top + "/"
    return ""


def _regular(z: zipfile.ZipFile, name: str) -> bool:
    try:
        i = z.getinfo(name)
    except KeyError:
        return False
    return not i.is_dir() and not archive.is_symlink(i)


def license_member(z: zipfile.ZipFile, prefix: str) -> str:
    for name in LICENSE_NAMES:
        if _regular(z, prefix + name):
            return name
    for i in sorted(archive.members(z), key=lambda i: i.filename):
        rel = i.filename[len(prefix):] if i.filename.startswith(prefix) else None
        if rel and "/" not in rel and rel.lower().startswith("licen") and _regular(z, i.filename):
            return rel
    raise tools.StageError("the package has no LICENSE.txt, so its terms cannot be shown; nothing was installed")


def read_package(zip_path: Path) -> tuple[zipfile.ZipFile, str, str, bytes]:
    """Open and check the zip without unpacking it: (zip, prefix, licence name, licence bytes)."""
    try:
        z = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as e:
        raise tools.StageError(f"{zip_path.name} is not a readable zip: {e}") from None
    try:
        bad = z.testzip()
        if bad:
            raise tools.StageError(f"{zip_path.name} is corrupt (bad CRC in {bad})")
        for i in archive.members(z):
            if not archive.safe_name(i.filename):
                raise tools.StageError(f"{zip_path.name} contains an unsafe path: {i.filename!r}")
        prefix = package_prefix(z)
        if not _regular(z, prefix + starnet.STARNET2):
            raise tools.StageError(f"{zip_path.name} has no {starnet.STARNET2} executable; is it a StarNet2 "
                                   "command-line package?")
        name = license_member(z, prefix)
        return z, prefix, name, archive.read_member(z, prefix + name, LICENSE_LIMIT)
    except BaseException:
        z.close()
        raise


def unpack(zip_path: Path, z: zipfile.ZipFile, prefix: str, into: Path, license_name: str,
           license_bytes: bytes) -> Path:
    """Unpack the whole zip (after acceptance); return the package folder."""
    archive.extract(zip_path, into, z)
    root = into / prefix.rstrip("/") if prefix else into
    exe = root / starnet.STARNET2
    if exe.is_symlink() or not exe.is_file():
        raise tools.StageError(f"{zip_path.name} has no {starnet.STARNET2} executable")
    if (root / license_name).read_bytes() != license_bytes:
        raise tools.StageError(f"{license_name} changed while unpacking {zip_path.name}; nothing was installed")
    exe.chmod(0o755)
    return root


def license_revision(text: str) -> str | None:
    for pattern in (r"\brevision\b", r"\b(last (updated|revised)|effective date|revised)\b"):
        for line in text.splitlines():
            if re.search(pattern, line, re.IGNORECASE):
                return line.strip()[:200]
    return None


# ---------------------------------------------------------------- acceptance

def ask_acceptance(text: str, pkg: Package, source: str, accept_flag: bool, interactive: bool,
                   ask: Callable[[str], str] = input, out: Callable = print) -> str | None:
    """Print the full licence and get affirmative acceptance. Returns how it was accepted
    ("prompt" or "--accept-license"), or None."""
    bar = "=" * 78
    out(bar)
    out(f"{pkg.describe()}")
    out(f"Licence terms, printed in full from the downloaded package's LICENSE.txt ({source}):")
    out(bar)
    out(text.rstrip("\n"))
    out(bar)
    out("StarNet2 is not part of darkroom. It is used under the licence above, which is between you and "
        "its authors. darkroom only runs it as a separate program.")
    if accept_flag:
        out("Licence accepted with --accept-license.")
        return "--accept-license"
    if not interactive:
        out("Not accepted: this is not an interactive terminal. To accept the licence above, run again with "
            "--accept-license, or run in a terminal and type accept.")
        return None
    try:
        answer = ask('To accept these terms and install StarNet2, type "accept" (anything else cancels): ')
    except (EOFError, KeyboardInterrupt):
        out("")
        return None
    return "prompt" if answer.strip() == "accept" else None


# ---------------------------------------------------------------- macOS

def quarantine(root: Path, run: Callable) -> dict:
    """Mark the unpacked package as downloaded from the internet, as a browser would, and ask
    Gatekeeper about the starnet2 executable. Never removes a flag."""
    stamp = toolset.quarantine_stamp()
    try:
        rc, out = run(["xattr", "-w", "-r", toolset.QUARANTINE, stamp, str(root)], timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        rc, out = 1, str(e)
    if rc != 0:
        last = out.strip().splitlines()[-1] if out.strip() else ""
        raise tools.StageError(f"could not mark StarNet2 as downloaded from the internet (xattr exit {rc}"
                               + (f": {last}" if last else "") + "); nothing was installed")
    exe = root / starnet.STARNET2
    value = toolset.quarantine_value(exe, run)
    if value != stamp:
        raise tools.StageError(f"the quarantine flag on {starnet.STARNET2} did not stick (got {value!r}); "
                               "nothing was installed")
    accepted, source = toolset.assess(exe, run)
    return {"quarantined": True, "quarantine": stamp, "notarized": accepted, "gatekeeper": source}


# ---------------------------------------------------------------- install

def package_from_zip(zip_path: Path, plat: str) -> Package:
    m = _FILE.fullmatch(zip_path.name)
    if not m:
        raise tools.StageError(f"{zip_path.name} is not named like an official StarNet2 command-line zip "
                               "(starnet2_<platform>_<version>-<build>_<backend>_cli.zip); keep the name it "
                               "was downloaded with")
    pkg = _with_pins(Package(m.group("platform"), m.group("version"), m.group("build"), m.group("backend")))
    if pkg.platform != plat:
        raise tools.StageError(f"{zip_path.name} is the {PLATFORM_NAMES.get(pkg.platform, pkg.platform)} build; "
                               f"this machine needs the {PLATFORM_NAMES.get(plat, plat)} one")
    return pkg


def install(root: Path, dest: Path, record: dict, force: bool) -> None:
    """Write accepted.json into the unpacked package, then move it into place atomically."""
    (root / starnet.ACCEPTED).write_text(json.dumps(record, indent=2) + "\n")
    dest.parent.mkdir(parents=True, exist_ok=True)
    old = None
    if dest.exists() or dest.is_symlink():
        if not force:
            raise tools.StageError(f"{dest} already exists; pass --force to reinstall")
        old = dest.with_name(f".old-{dest.name}-{os.getpid()}")
        os.rename(dest, old)
    try:
        os.rename(root, dest)
    except OSError:
        if old is not None:
            os.rename(old, dest)
        raise
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)


def list_installed(out=print) -> int:
    root = starnet.installs_root()
    found = starnet.installed(root)
    if not found:
        out(f"no StarNet2 installed by darkroom in {root}")
        return 0
    for k, b in enumerate(found):
        acc = starnet.read_accepted(b.dir) or {}
        out(f"{b.label}  {acc.get('platform', '?')} {acc.get('backend', '')}  licence accepted "
            f"{acc.get('accepted_at', '?')}  {b.dir}" + ("  [default]" if k == 0 else ""))
    return 0


def run(a, opener: Callable | None = None, interactive: bool | None = None, ask: Callable = input,
        out: Callable = print, plat: str | None = None, cmd: Callable | None = None,
        system: str | None = None) -> int:
    """cmd runs xattr and spctl (macOS); system defaults to sys.platform."""
    if a.list:
        return list_installed(out)
    plat = plat or platform_key()
    system = sys.platform if system is None else system
    zip_arg = Path(a.zip).expanduser().resolve() if a.zip else None
    if zip_arg is not None:
        if not zip_arg.is_file():
            raise tools.StageError(f"zip not found: {zip_arg}")
        pkg, note = package_from_zip(zip_arg, plat), None
    else:
        pkg, note = resolve(plat, opener)
    if note:
        out(f"note: {note}")
    root = starnet.installs_root()
    dest = root / pkg.stem
    if a.dry_run:
        out(f"platform:   {plat} ({PLATFORM_NAMES.get(plat, plat)})")
        out(f"package:    {pkg.describe()}")
        out(f"source:     {zip_arg.name + ' (local zip)' if zip_arg else pkg.url}")
        out(f"size:       {f'{pkg.size:,} bytes (pinned)' if pkg.size else 'not pinned; checked against Content-Length'}")
        sha = (f"{pkg.sha256} (pinned)" if pkg.sha256 else
               "NOT pinned for this build: you will be shown the downloaded file's SHA-256 and asked to "
               "confirm (or pass --allow-unpinned)"
               + (f"; {CLI_PAGE} publishes {pkg.published_sha256}" if pkg.published_sha256 else ""))
        out(f"sha256:     {sha}")
        out(f"install to: {dest}" + ("  (already installed)" if dest.exists() else ""))
        out("dry run: nothing was downloaded or installed")
        return 0
    if dest.exists() and not a.force:
        out(f"{pkg.describe()} is already installed in {dest}; pass --force to reinstall")
        return 0
    if interactive is None:
        interactive = sys.stdin.isatty()
    macos = system == "darwin" and plat.startswith("macos")
    root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".incoming-", dir=root))
    z = None
    try:
        if zip_arg is not None:
            size = zip_arg.stat().st_size
            if pkg.size is None and size > MAX_UNPINNED:
                raise tools.StageError(f"{zip_arg.name} is {size:,} bytes, more than the {MAX_UNPINNED:,} darkroom "
                                       "accepts for a build without a pinned size")
            digest = sha256_file(zip_arg)
            if pkg.sha256 and digest != pkg.sha256:
                raise tools.StageError(f"SHA-256 mismatch for {zip_arg.name}: got {digest}, expected {pkg.sha256}; "
                                       "download it again from starnetastro.com")
            if pkg.size is not None and size != pkg.size:
                raise tools.StageError(f"{zip_arg.name} is {size:,} bytes; the pinned size is {pkg.size:,}")
            zip_path, source = zip_arg, f"local zip {zip_arg.name}"
        else:
            zip_path = tmp / pkg.filename
            digest = download(pkg, zip_path, opener, out)
            size = zip_path.stat().st_size
            source = pkg.url
        unpinned_with = None
        if pkg.sha256:
            check = "pinned"
            out(f"sha256 {digest} (matches the pinned hash)")
        else:
            check = "published" if pkg.published_sha256 else "none"
            unpinned_with = confirm_unpinned(pkg, digest, size, "read from your zip" if zip_arg else "downloaded",
                                             getattr(a, "allow_unpinned", False), interactive, ask, out)
            if unpinned_with is None:
                raise tools.StageError("not installed: darkroom has no pinned SHA-256 for this build and it was not "
                                       "confirmed" + ("" if zip_arg else "; the download was deleted"))
        z, prefix, lic_name, lic_bytes = read_package(zip_path)
        text = lic_bytes.decode("utf-8", errors="replace")
        how = ask_acceptance(text, pkg, source, a.accept_license, interactive, ask, out)
        if how is None:
            raise tools.StageError("licence not accepted: nothing was installed"
                                   + ("" if zip_arg else ", and the download was deleted"))
        pkg_root = unpack(zip_path, z, prefix, tmp / "unpacked", lic_name, lic_bytes)
        z.close()
        z = None
        if zip_path.parent == tmp:
            zip_path.unlink()
        mac = quarantine(pkg_root, cmd or toolset._run) if macos else {}
        record = {"product": "StarNet2", "version": pkg.version, "build": pkg.build, "platform": pkg.platform,
                  "backend": pkg.backend, "license_file": lic_name, "license_revision": license_revision(text),
                  "license_sha256": hashlib.sha256(text.encode()).hexdigest(),
                  "accepted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "accepted_with": how,
                  "url": pkg.url, "source": "download" if zip_arg is None else "local zip",
                  "zip_sha256": digest, "zip_size": size, "sha256_check": check,
                  **({"unpinned_confirmed_with": unpinned_with} if unpinned_with else {}),
                  "installed_by": f"darkroom {__version__}", **mac}
        install(pkg_root, dest, record, a.force)
    finally:
        if z is not None:
            z.close()
        shutil.rmtree(tmp, ignore_errors=True)
    out(f"installed {pkg.describe()} in {dest}")
    if macos:
        if mac.get("notarized"):
            out(f"Gatekeeper: accepted ({mac.get('gatekeeper') or 'no source given'}); it is marked as "
                "downloaded from the internet, as a browser would")
        else:
            out(f"Gatekeeper: not accepted ({mac.get('gatekeeper') or 'rejected'}). StarNet2 is installed but "
                "quarantined, and macOS will block it until you allow it; darkroom will not run it before "
                "that and never clears the quarantine flag. "
                + toolset.allow_instructions(dest, f"'{dest / starnet.STARNET2}' --machine-info"))
    out("`darkroom starnet` uses the newest install here unless --starnet-dir, [starnet] dir or "
        "DARKROOM_STARNET_DIR names another StarNet folder.")
    return 0


HELP = "download StarNet2 from starnetastro.com and install it after you accept its licence"

EPILOG = f"""\
StarNet2 is third-party software with its own licence. get-starnet prints that
licence in full from the downloaded package and installs StarNet2 only if you
accept it: type "accept" at the prompt, or pass --accept-license (for scripts and
containers; the licence is still printed). Without acceptance the download is
deleted and nothing is installed.

The download link is read from {CLI_PAGE}
(the pinned 2.6.2 link is used if that page cannot be read). Files are fetched
only from {DOWNLOAD_BASE}. The 2.6.2 builds are checked against a pinned
size and SHA-256. For any other build darkroom has no pinned hash: it shows the
file's size and SHA-256 and asks you to type yes (or pass --allow-unpinned).

On macOS the package is marked as downloaded from the internet (quarantined), as a
browser would, so that Gatekeeper judges it; if Gatekeeper does not accept it,
macOS blocks it until you allow it, and darkroom tells you how and never does it.

StarNet2 is installed in <cache>/starnet/<package name>/ (cache: $DARKROOM_CACHE,
see `darkroom --help`) with an accepted.json recording the version, the zip's
SHA-256 and when you accepted the licence.
"""


def add_parser(subparsers) -> argparse.ArgumentParser:
    p = subparsers.add_parser("get-starnet", help=HELP, description=HELP, epilog=EPILOG,
                              formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accept-license", action="store_true",
                   help="accept the StarNet2 licence without the prompt (it is printed in full first)")
    p.add_argument("--allow-unpinned", action="store_true",
                   help="install a build for which darkroom has no pinned SHA-256 without the yes prompt "
                        "(the file's size and SHA-256 are still printed)")
    p.add_argument("--zip", metavar="FILE", default=None,
                   help="install from a StarNet2 command-line zip you downloaded from starnetastro.com "
                        "(keep its original name); the licence must still be accepted")
    p.add_argument("--force", action="store_true", help="reinstall even if this version is already installed")
    p.add_argument("--list", action="store_true", help="list the StarNet2 versions installed by darkroom")
    p.add_argument("--dry-run", action="store_true",
                   help="show the build and URL that would be downloaded (reads only the web page)")
    p.set_defaults(func=run)
    return p
