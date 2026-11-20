"""A local copy of the Gaia DR3 XP sampled spectra that Siril's SPCC reads.

Siril 1.4.5's SPCC reads its star spectra from the catalogue Siril distributes as 48
files, ``siril_cat1_healpix8_xpsamp_<chunk>.dat``, one per HEALPix pixel at level 1.
Each file is

* a 128-byte header (title, Gaia release, index level 8, catalogue type, chunk
  level 1, chunk number, first and last level-8 pixel of the chunk);
* an index of 16384 little-endian uint32, one per level-8 pixel of the chunk, holding
  the number of records up to and including that pixel;
* the records, 701 bytes each (``SourceEntryXPsamp``), sorted by pixel.

Online, Siril fetches the header and index and then the records it needs with HTTP
range requests, from Zenodo first. When that server answers 429 or 5xx, Siril 1.4.5
crashes instead of trying its other mirrors. Offline, Siril reads the same files from
the folder in its ``core.catalogue_gaia_photo`` setting when SPCC is given
``-catalog=localgaia`` (src/io/healpix/healpix.cpp, src/core/command.c).

This module builds such a folder for the fields darkroom processes, with only the
level-8 pixels those fields need: each file keeps the real header, an index rewritten
so that pixels not fetched hold no stars, and the real records of the pixels fetched.
Siril reads it like the full file (it only reads the index entries and records of the
pixels a cone search touches). ``coverage.json`` lists the pixels that were fetched,
so a pixel with no stars is told apart from one never fetched.

Downloads use the same mirrors as Siril (its ``spcc_mirrors.json`` when present), with
retries, backoff and mirror fallback here in Python. Only sky coordinates (as byte
ranges of public files) are sent.
"""
from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import math
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

from . import tools

LEVEL = 8                               # index level of Siril's XP sampled catalogue
CHUNK_LEVEL = 1                         # one file per level-1 pixel: 48 files
NSIDE = 1 << LEVEL
NPIX = 12 * NSIDE * NSIDE
PIX_PER_CHUNK = 4 ** (LEVEL - CHUNK_LEVEL)
N_CHUNKS = 12 * 4 ** CHUNK_LEVEL
HEADER_SIZE = 128
INDEX_SIZE = PIX_PER_CHUNK * 4
CAT_TYPE_XP_SAMPLED = 2
PIXEL_DEG = math.degrees(math.sqrt(4 * math.pi / NPIX))     # ~0.229 deg, side of a level-8 pixel
COORD_SCALE = 360.0 / 2147483647.0      # ra_scaled, dec_scaled -> degrees (Siril: 360 / INT32_MAX)

RECORD = np.dtype([("ra", "<i4"), ("dec", "<i4"), ("pmra", "<i2"), ("pmdec", "<i2"), ("mag", "<i2"),
                   ("fexpo", "u1"), ("flux", "<i2", (343,))])
RECORD_SIZE = RECORD.itemsize           # 701

DEFAULT_MIRRORS = [
    "https://zenodo.org/records/17988559/files",
    "http://siril-spcc-public-lz-hel.s3-website.eu-north-lz-hel-a.cloud.ovh.net",
    "http://siril-spcc-public-lz-man.s3-website.eu-west-lz-mnc-a.cloud.ovh.net",
    "http://siril-spcc-public-az-sydney.s3-website.ap-southeast-syd.io.cloud.ovh.net",
    "http://siril-spcc-public-az-mumbai.s3-website.ap-south-mum.io.cloud.ovh.net",
    "http://siril-spcc-public-lz-santiago.s3-website.sa-west-lz-scl-a.cloud.ovh.net",
    "http://gaia.wheep.co.uk",
    "https://huggingface.co/datasets/siril-spcc/gaia/resolve/main",
]
USER_AGENT = "darkroom"
TIMEOUT = 60.0
ATTEMPTS = 3                            # tries per request on one mirror before the next mirror
BACKOFF = 2.0                           # seconds before the 2nd try, doubled for each further try
MAX_RETRY_AFTER = 30.0
MAX_DOWNLOAD = 1.5e9                    # bytes of records for one field: more is not a normal field
MERGE_GAP = 512 * 1024                  # fetch across gaps up to this size rather than make another request
COVERAGE = "coverage.json"
DIR_NAME = "gaia-xpsampled"


def chunk_name(chunk: int) -> str:
    return f"siril_cat{CHUNK_LEVEL}_healpix{LEVEL}_xpsamp_{chunk}.dat"


def catalogue_dir() -> Path:
    """The local catalogue folder: <darkroom cache>/gaia-xpsampled."""
    return tools.cache_dir() / DIR_NAME


# ------------------------------------------------------------------ HEALPix

def _spread_bits(v: np.ndarray) -> np.ndarray:
    """Put bit i of v at bit 2i (for v < 2**16)."""
    v = v.astype(np.int64) & 0xFFFF
    v = (v | (v << 8)) & 0x00FF00FF
    v = (v | (v << 4)) & 0x0F0F0F0F
    v = (v | (v << 2)) & 0x33333333
    v = (v | (v << 1)) & 0x55555555
    return v


def ang2pix(ra, dec, nside: int = NSIDE) -> np.ndarray:
    """NESTED HEALPix pixel of each (ra, dec) in degrees (the scheme Siril's catalogue uses)."""
    ra = np.atleast_1d(np.asarray(ra, dtype=np.float64))
    dec = np.atleast_1d(np.asarray(dec, dtype=np.float64))
    z = np.sin(np.radians(dec))
    za = np.abs(z)
    tt = np.mod(np.radians(ra), 2 * np.pi) / (np.pi / 2)        # in [0, 4)
    tt = np.where(tt >= 4.0, 0.0, tt)
    face = np.empty(ra.shape, dtype=np.int64)
    ix = np.empty(ra.shape, dtype=np.int64)
    iy = np.empty(ra.shape, dtype=np.int64)

    eq = za <= 2.0 / 3.0
    if eq.any():
        t1 = nside * (0.5 + tt[eq])
        t2 = nside * z[eq] * 0.75
        jp = (t1 - t2).astype(np.int64)                         # ascending edge line
        jm = (t1 + t2).astype(np.int64)                         # descending edge line
        ifp, ifm = jp // nside, jm // nside
        face[eq] = np.where(ifp == ifm, ifp | 4, np.where(ifp < ifm, ifp, ifm + 8))
        ix[eq] = jm & (nside - 1)
        iy[eq] = nside - (jp & (nside - 1)) - 1
    pol = ~eq
    if pol.any():
        ntt = np.minimum(3, tt[pol].astype(np.int64))
        tp = tt[pol] - ntt
        tmp = nside * np.sqrt(3 * (1 - za[pol]))
        jp = np.minimum((tp * tmp).astype(np.int64), nside - 1)
        jm = np.minimum(((1 - tp) * tmp).astype(np.int64), nside - 1)
        north = z[pol] >= 0
        face[pol] = np.where(north, ntt, ntt + 8)
        ix[pol] = np.where(north, nside - jm - 1, jp)
        iy[pol] = np.where(north, nside - jp - 1, jm)
    return face * nside * nside + _spread_bits(ix) + (_spread_bits(iy) << 1)


def chunk_of(pixel: int) -> int:
    return int(pixel) >> (2 * (LEVEL - CHUNK_LEVEL))


def disc_pixels(ra: float, dec: float, radius_deg: float) -> set[int]:
    """Every level-8 pixel that has some part within ``radius_deg`` of (ra, dec), and a
    few just outside. Found by sampling a fine grid on the tangent plane out to one pixel
    beyond the radius, so a pixel that only touches the edge of the disc is still caught."""
    if not (0 < radius_deg <= 30):
        raise ValueError(f"field radius {radius_deg:g} deg is out of range (0..30)")
    step = math.radians(PIXEL_DEG / 8)
    outer = math.radians(radius_deg + PIXEL_DEG)
    half = math.tan(outer)
    n = int(math.ceil(half / step))
    g = np.arange(-n, n + 1) * step
    x, y = np.meshgrid(g, g)
    rho = np.hypot(x, y)
    keep = np.arctan(rho) <= outer
    x, y, rho = x[keep], y[keep], rho[keep]
    c = np.arctan(rho)
    ra0, dec0 = math.radians(ra), math.radians(dec)
    with np.errstate(invalid="ignore", divide="ignore"):
        sin_dec = np.cos(c) * math.sin(dec0) + np.where(rho > 0, y * np.sin(c) * math.cos(dec0) / rho, 0.0)
        d = np.arcsin(np.clip(sin_dec, -1, 1))
        a = ra0 + np.arctan2(x * np.sin(c), rho * math.cos(dec0) * np.cos(c) - y * math.sin(dec0) * np.sin(c))
    return set(int(p) for p in np.unique(ang2pix(np.degrees(a), np.degrees(d))))


def by_chunk(pixels: Iterable[int]) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for p in sorted(pixels):
        out.setdefault(chunk_of(p), []).append(p)
    return out


def runs(values: list[int]) -> list[tuple[int, int]]:
    """Sorted integers as inclusive (first, last) runs of consecutive values."""
    out: list[tuple[int, int]] = []
    for v in sorted(values):
        if out and v == out[-1][1] + 1:
            out[-1] = (out[-1][0], v)
        else:
            out.append((v, v))
    return out


# ------------------------------------------------------------------ file layout

@dataclass(frozen=True)
class Header:
    raw: bytes
    title: str
    gaia_version: int
    level: int
    cat_type: int
    chunked: int
    chunk_level: int
    chunk: int
    first: int
    last: int


def parse_header(raw: bytes, chunk: int | None = None) -> Header:
    """Parse and check a chunk header; ValueError when it is not the file Siril expects."""
    if len(raw) != HEADER_SIZE:
        raise ValueError(f"header is {len(raw)} bytes, not {HEADER_SIZE}")
    title = raw[:48].split(b"\0", 1)[0].decode("ascii", errors="replace")
    gv, level, ctype, chunked, clevel = raw[48:53]
    c, first, last = (int.from_bytes(raw[53 + 4 * i:57 + 4 * i], "little") for i in range(3))
    h = Header(bytes(raw), title, gv, level, ctype, chunked, clevel, c, first, last)
    if (level, ctype, chunked, clevel) != (LEVEL, CAT_TYPE_XP_SAMPLED, 1, CHUNK_LEVEL):
        raise ValueError(f"not a Siril Gaia XP sampled chunk (level {level}, type {ctype}, "
                         f"chunked {chunked}, chunk level {clevel})")
    if first != c * PIX_PER_CHUNK or last != first + PIX_PER_CHUNK - 1:
        raise ValueError(f"header pixel range {first}..{last} does not match chunk {c}")
    if chunk is not None and c != chunk:
        raise ValueError(f"header is for chunk {c}, not {chunk}")
    return h


def parse_index(raw: bytes) -> np.ndarray:
    if len(raw) != INDEX_SIZE:
        raise ValueError(f"index is {len(raw)} bytes, not {INDEX_SIZE}")
    idx = np.frombuffer(raw, dtype="<u4").astype(np.int64)
    if np.any(np.diff(idx) < 0):
        raise ValueError("index is not cumulative")
    return idx


def pixel_span(index: np.ndarray, local: int) -> tuple[int, int]:
    """(first record, number of records) of chunk-local pixel ``local``."""
    start = 0 if local == 0 else int(index[local - 1])
    return start, int(index[local]) - start


EDGE_TOLERANCE_DEG = 0.02   # the catalogue files stars by Gaia source_id, whose HEALPix number
                            # comes from a slightly different position: a few stars sit just
                            # across the edge of their pixel


def check_records(data: bytes, pixel: int) -> None:
    """Every record must lie in ``pixel`` (or within EDGE_TOLERANCE_DEG of it): catches a
    wrong byte range or a corrupt download."""
    if len(data) % RECORD_SIZE:
        raise ValueError(f"{len(data)} bytes is not a whole number of records")
    if not data:
        return
    rec = np.frombuffer(data, dtype=RECORD)
    ra = rec["ra"] * COORD_SCALE
    dec = rec["dec"] * COORD_SCALE
    bad = ang2pix(ra, dec) != pixel
    if bad.any():
        t = EDGE_TOLERANCE_DEG
        ra_b, dec_b = ra[bad], dec[bad]
        near = np.zeros(ra_b.shape, dtype=bool)
        cosd = np.maximum(np.cos(np.radians(dec_b)), 1e-3)
        for da in (-t, 0, t):
            for dd in (-t, 0, t):
                near |= ang2pix(ra_b + da / cosd, np.clip(dec_b + dd, -90, 90)) == pixel
        if not near.all():
            raise ValueError(f"{int((~near).sum())} of {len(rec)} records do not lie in pixel {pixel}")


def build_chunk(header: bytes, records: dict[int, bytes]) -> bytes:
    """A chunk file holding only ``records`` (chunk-local pixel -> its records)."""
    counts = np.zeros(PIX_PER_CHUNK, dtype=np.int64)
    for local, data in records.items():
        counts[local] = len(data) // RECORD_SIZE
    index = np.cumsum(counts).astype("<u4")
    return header + index.tobytes() + b"".join(records[k] for k in sorted(records))


def read_chunk(path: Path) -> tuple[Header, dict[int, bytes]]:
    """(header, chunk-local pixel -> records) of a local chunk file."""
    raw = path.read_bytes()
    h = parse_header(raw[:HEADER_SIZE])
    index = parse_index(raw[HEADER_SIZE:HEADER_SIZE + INDEX_SIZE])
    base = HEADER_SIZE + INDEX_SIZE
    if len(raw) != base + int(index[-1]) * RECORD_SIZE:
        raise ValueError(f"{path.name} is {len(raw)} bytes; its index says "
                         f"{base + int(index[-1]) * RECORD_SIZE}")
    out = {}
    for local in np.nonzero(np.diff(np.concatenate([[0], index])))[0]:
        start, n = pixel_span(index, int(local))
        out[int(local)] = raw[base + start * RECORD_SIZE: base + (start + n) * RECORD_SIZE]
    return h, out


def disc(ra: float, dec: float, radius_deg: float) -> set[int]:
    """disc_pixels, with a field the local copy cannot serve as a StageError (the color stage
    then lets Siril download the spectra itself)."""
    try:
        return disc_pixels(ra, dec, radius_deg)
    except ValueError as e:
        raise tools.StageError(f"the local SPCC catalogue cannot serve this field: {e}") from None


LOCK = ".lock"


class Store:
    """The local catalogue folder.

    ``coverage.json`` lists the pixels fetched and, per chunk file, its size and a digest of
    its index. A pixel counts only while its chunk file is whole and is the one recorded, so a
    damaged or truncated file, or one rewritten without its record, is fetched again instead
    of silently giving SPCC fewer stars. Adding to the store holds an exclusive lock on
    ``.lock`` in the folder, so two darkroom processes do not overwrite each other's pixels."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else catalogue_dir()

    def _fingerprint(self, chunk: int) -> dict | None:
        """Size and index digest of a chunk file that is whole (header, cumulative index, as many
        records as the index says), else None."""
        path = self.root / chunk_name(chunk)
        try:
            size = path.stat().st_size
            with open(path, "rb") as f:
                head = f.read(HEADER_SIZE + INDEX_SIZE)
            parse_header(head[:HEADER_SIZE], chunk)
            index = parse_index(head[HEADER_SIZE:])
        except (OSError, ValueError):
            return None
        if size != HEADER_SIZE + INDEX_SIZE + int(index[-1]) * RECORD_SIZE:
            return None
        return {"size": size, "index": hashlib.sha1(head[HEADER_SIZE:]).hexdigest()[:16]}

    def coverage(self) -> set[int]:
        try:
            data = json.loads((self.root / COVERAGE).read_text())
            if data.get("level") != LEVEL or data.get("chunk_level") != CHUNK_LEVEL:
                return set()
            have = set()
            for a, b in data.get("pixels", []):
                have.update(range(int(a), int(b) + 1))
            files = data.get("chunks")          # absent in records written before it existed
        except (OSError, ValueError, TypeError, AttributeError):
            return set()
        out = set()
        for chunk, pixels in by_chunk(have).items():
            fp = self._fingerprint(chunk)       # the chunk file must be there and whole ...
            if fp is None or (isinstance(files, dict) and files.get(str(chunk)) != fp):
                continue                        # ... and be the file this record describes
            out.update(pixels)
        return out

    def missing(self, pixels: Iterable[int]) -> set[int]:
        return set(pixels) - self.coverage()

    def covers(self, ra: float, dec: float, radius_deg: float) -> bool:
        return not self.missing(disc(ra, dec, radius_deg))

    def size(self) -> int:
        return sum(p.stat().st_size for p in self.root.glob(chunk_name(0).replace("_0.dat", "_*.dat")))

    @contextlib.contextmanager
    def locked(self):
        """An exclusive lock on the store, held across processes (flock; Linux and macOS)."""
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl
        except ImportError:                     # no flock on this platform: unlocked
            yield
            return
        with open(self.root / LOCK, "a+b") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except OSError:                     # a filesystem without locks: carry on unlocked
                yield
                return
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def add(self, chunk: int, header: bytes, records: dict[int, bytes]) -> None:
        """Merge fetched pixels (global pixel -> records) into the chunk file, then record them."""
        with self.locked():
            self._add(chunk, header, records)

    def _add(self, chunk: int, header: bytes, records: dict[int, bytes]) -> None:
        path = self.root / chunk_name(chunk)
        first = chunk * PIX_PER_CHUNK
        known = self.coverage()                 # read under the lock: what the file holds now
        old_pixels = {p for p in known if chunk_of(p) == chunk}
        merged: dict[int, bytes] = {}
        if path.is_file() and old_pixels:
            try:
                old_header, merged = read_chunk(path)
                if old_header.raw != header:    # a different release of the file: start over
                    merged, old_pixels = {}, set()
            except (OSError, ValueError):       # damaged: start over
                merged, old_pixels = {}, set()
        else:
            old_pixels = set()
        merged = {k: v for k, v in merged.items() if first + k in old_pixels}
        for p, data in records.items():
            merged[p - first] = data
        _write_atomic(path, build_chunk(header, merged))
        # the file first, then the record of what it holds: a crash in between only
        # means fetching that chunk's pixels again (its digest no longer matches)
        others = {p for p in known if chunk_of(p) != chunk}
        self._write_coverage(others | old_pixels | set(records))

    def _write_coverage(self, pixels: set[int]) -> None:
        files = {}
        for chunk in by_chunk(pixels):
            fp = self._fingerprint(chunk)
            if fp is not None:
                files[str(chunk)] = fp
        pixels = {p for p in pixels if str(chunk_of(p)) in files}
        data = {"level": LEVEL, "chunk_level": CHUNK_LEVEL,
                "pixels": [list(r) for r in runs(sorted(pixels))], "chunks": files}
        _write_atomic(self.root / COVERAGE, (json.dumps(data) + "\n").encode())


def _write_atomic(path: Path, data: bytes) -> None:
    """Write through a temporary file of this process's own, then rename it into place."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".darkroom-tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


# ------------------------------------------------------------------ download

class FetchError(Exception):
    """One request failed. ``retry`` says whether trying the same mirror again may help."""

    def __init__(self, msg: str, retry: bool = True, wait: float | None = None):
        super().__init__(msg)
        self.retry = retry
        self.wait = wait


class TooBig(Exception):
    pass


def http_range(url: str, start: int, length: int, timeout: float = TIMEOUT) -> bytes:
    """GET bytes [start, start + length) of ``url``. Only a 206 answer of the right size counts:
    a server that ignores Range would send the whole file (gigabytes)."""
    from .catalog import _ssl_context
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Range": f"bytes={start}-{start + length - 1}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
            if r.status != 206:
                raise FetchError(f"HTTP {r.status} instead of 206 (no range support)", retry=False)
            data = r.read(length + 1)
    except urllib.error.HTTPError as e:
        wait = None
        if e.headers and e.headers.get("Retry-After", "").strip().isdigit():
            wait = float(e.headers["Retry-After"])
        raise FetchError(f"HTTP {e.code}", retry=e.code == 429 or e.code >= 500, wait=wait) from None
    except (urllib.error.URLError, OSError) as e:          # timeouts, resets, DNS
        raise FetchError(f"{getattr(e, 'reason', e)}") from None
    except http.client.HTTPException as e:                  # a cut-off answer (IncompleteRead), a bad status line
        raise FetchError(f"{type(e).__name__}: {e}") from None
    if len(data) != length:
        raise FetchError(f"got {len(data)} bytes, wanted {length}")
    return data


def mirrors_file() -> Path:
    from . import siril
    return siril.config_dir() / "spcc_mirrors.json"


def mirrors(path: Path | None = None) -> list[str]:
    """Siril's own mirror list (its spcc_mirrors.json, which Siril updates), then the built-in one."""
    out: list[str] = []
    try:
        for m in json.loads((path or mirrors_file()).read_text()):
            u = str(m.get("url", "")).rstrip("/")
            if re.match(r"https?://", u):
                out.append(u)
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    for u in DEFAULT_MIRRORS:
        if u not in out:
            out.append(u)
    return out


class Fetcher:
    def __init__(self, mirror_list: list[str] | None = None, get: Callable = http_range,
                 sleep: Callable = time.sleep, attempts: int = ATTEMPTS, backoff: float = BACKOFF,
                 log: Callable = print):
        self.mirrors = list(mirror_list) if mirror_list is not None else mirrors()
        self.get, self.sleep, self.attempts, self.backoff, self.log = get, sleep, attempts, backoff, log
        self.used: set[str] = set()
        self.bytes = 0

    def _request(self, base: str, name: str, start: int, length: int) -> bytes:
        url = f"{base}/{name}"
        for i in range(self.attempts):
            try:
                data = self.get(url, start, length)
                self.bytes += len(data)
                return data
            except FetchError as e:
                if not e.retry or i == self.attempts - 1:
                    raise
                wait = self.backoff * (2 ** i)
                if e.wait is not None:
                    wait = max(wait, min(e.wait, MAX_RETRY_AFTER))
                self.log(f"spcc catalogue: {_host(base)}: {e}; trying again in {wait:g}s")
                self.sleep(wait)
        raise AssertionError("unreachable")

    def fetch_chunk(self, chunk: int, pixels: list[int], budget: float = MAX_DOWNLOAD
                    ) -> tuple[bytes, dict[int, bytes]]:
        """(header, global pixel -> records) for ``pixels`` of one chunk, all from one mirror
        (the first that answers), trying the mirrors in turn."""
        name = chunk_name(chunk)
        first = chunk * PIX_PER_CHUNK
        errors = []
        order = sorted(self.mirrors, key=lambda m: m not in self.used)   # a mirror that worked first
        for base in order:
            try:
                header = self._request(base, name, 0, HEADER_SIZE)
                parse_header(header, chunk)
                index = parse_index(self._request(base, name, HEADER_SIZE, INDEX_SIZE))
                spans = []
                for a, b in runs([p - first for p in pixels]):
                    s0, _ = pixel_span(index, a)
                    s1, n1 = pixel_span(index, b)
                    spans.append((a, b, s0, s1 + n1 - s0))
                need = sum(n for *_, n in spans) * RECORD_SIZE
                spans = merge_spans(spans)
                if need > budget:
                    raise TooBig(f"the field needs {need / 1e6:.0f} MB of star spectra from {name}, more "
                                 f"than the {budget / 1e6:.0f} MB darkroom allows for one field")
                out: dict[int, bytes] = {}
                for locals_, s, n in spans:
                    data = (self._request(base, name, HEADER_SIZE + INDEX_SIZE + s * RECORD_SIZE,
                                          n * RECORD_SIZE) if n else b"")
                    for local in locals_:
                        ps, pn = pixel_span(index, local)
                        rec = data[(ps - s) * RECORD_SIZE:(ps - s + pn) * RECORD_SIZE]
                        check_records(rec, first + local)
                        out[first + local] = rec
                self.used.add(base)
                return header, out
            except (FetchError, ValueError) as e:
                errors.append(f"{_host(base)}: {e}")
                self.log(f"spcc catalogue: {_host(base)} failed for {name} ({e}); trying the next mirror")
        raise FetchError("every mirror failed: " + "; ".join(errors), retry=False)


def merge_spans(spans: list[tuple[int, int, int, int]], gap: int = MERGE_GAP
                ) -> list[tuple[list[int], int, int]]:
    """Join record spans (first pixel, last pixel, first record, records) that are at most
    ``gap`` bytes apart into one request each: (pixels, first record, records). Fewer,
    slightly larger requests are much faster than many small ones."""
    out: list[tuple[list[int], int, int]] = []
    for a, b, s, n in spans:
        if out and (s - (out[-1][1] + out[-1][2])) * RECORD_SIZE <= gap:
            px, s0, _ = out[-1]
            out[-1] = (px + list(range(a, b + 1)), s0, s + n - s0)
        else:
            out.append((list(range(a, b + 1)), s, n))
    return out


def _host(url: str) -> str:
    return re.sub(r"^\w+://([^/]+).*", r"\1", url)


def ensure(ra: float, dec: float, radius_deg: float, store: Store | None = None,
           fetcher: Fetcher | None = None, log: Callable = print, budget: float = MAX_DOWNLOAD) -> Path:
    """Make the local catalogue cover the disc; fetch only the pixels not there yet.
    Returns the catalogue folder. Raises StageError when a fetch fails on every mirror."""
    store = store or Store()
    pixels = disc(ra, dec, radius_deg)
    todo = store.missing(pixels)
    if not todo:
        log(f"spcc catalogue: the local copy already covers this field "
            f"({len(pixels)} sky pixels, in {store.root})")
        return store.root
    fetcher = fetcher or Fetcher(log=log)
    t0 = time.time()
    left = budget
    try:
        for chunk, px in by_chunk(todo).items():
            header, records = fetcher.fetch_chunk(chunk, px, left)
            left -= sum(len(v) for v in records.values())
            store.add(chunk, header, records)
    except (FetchError, TooBig) as e:
        raise tools.StageError(f"could not download the SPCC star spectra: {e}") from None
    except OSError as e:                # an unwritable or full cache
        raise tools.StageError(f"cannot write the local SPCC catalogue in {store.root}: "
                               f"{e.strerror or e}") from None
    log(f"spcc catalogue: fetched {len(todo)} of {len(pixels)} sky pixels for this field "
        f"({fetcher.bytes / 1e6:.1f} MB in {time.time() - t0:.0f}s from "
        f"{', '.join(sorted(_host(u) for u in fetcher.used)) or 'no mirror'}) into {store.root}")
    return store.root


# ------------------------------------------------------------------ the field of an image

def _sexagesimal(text: str, hours: bool) -> float:
    parts = [float(x) for x in re.split(r"[\s:hdms]+", text.strip()) if x]
    sign = -1.0 if text.strip().startswith("-") else 1.0
    v = abs(parts[0]) + (parts[1] if len(parts) > 1 else 0) / 60 + (parts[2] if len(parts) > 2 else 0) / 3600
    return sign * v * (15.0 if hours else 1.0)


@dataclass(frozen=True)
class Field:
    ra: float
    dec: float
    radius_deg: float           # half the image diagonal
    solved: bool                # from a WCS (plate solve) rather than the commanded pointing


MIN_SCALE = 1e-5            # deg per pixel (0.036"): finer than any telescope and camera here
MAX_SCALE = 0.1             # deg per pixel (6'): coarser than a fisheye lens on a large pixel


def field_of(path: Path, focal_mm: float, pixel_um: float) -> Field | None:
    """The sky field of a FITS image from its header, or None when it does not say.
    Reads only the centre coordinates, the image size and the WCS scale."""
    from astropy.io import fits
    try:
        h = fits.getheader(path)
    except Exception:
        return None
    w, hgt = h.get("NAXIS1"), h.get("NAXIS2")
    if not (isinstance(w, int) and isinstance(hgt, int) and w > 0 and hgt > 0):
        return None
    scale = None                # degrees per pixel
    try:
        if "CTYPE1" in h and "CRVAL1" in h and "CRVAL2" in h:
            if "CDELT1" in h and "CDELT2" in h:
                scale = math.sqrt(abs(float(h["CDELT1"]) * float(h["CDELT2"])))
            elif "CD1_1" in h:
                scale = math.sqrt(abs(float(h["CD1_1"]) * float(h.get("CD2_2", 0))
                                      - float(h.get("CD1_2", 0)) * float(h.get("CD2_1", 0))))
    except (TypeError, ValueError):
        scale = None
    if scale is not None and not (math.isfinite(scale) and MIN_SCALE <= scale <= MAX_SCALE):
        scale = None            # a placeholder WCS (e.g. CDELT = 1), not a plate solve
    try:
        if scale:
            ra, dec, solved = float(h["CRVAL1"]), float(h["CRVAL2"]), True
            # CRPIX is not always the centre; move to the centre along the WCS
            from astropy.wcs import WCS
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                c = WCS(h, naxis=2).pixel_to_world_values((w - 1) / 2, (hgt - 1) / 2)
            ra, dec = float(c[0]) % 360, float(c[1])
        elif "RA" in h and "DEC" in h:
            ra, dec, solved = float(h["RA"]), float(h["DEC"]), False
        elif "OBJCTRA" in h and "OBJCTDEC" in h:
            ra = _sexagesimal(str(h["OBJCTRA"]), True)
            dec = _sexagesimal(str(h["OBJCTDEC"]), False)
            solved = False
        elif "CTYPE1" in h and "CRVAL1" in h and "CRVAL2" in h:     # a WCS with no usable scale
            ra, dec, solved = float(h["CRVAL1"]) % 360, float(h["CRVAL2"]), False
        else:
            return None
    except Exception:           # a header astropy's WCS cannot use: no field, not a crash
        return None
    if not (0 <= ra < 360 and -90 <= dec <= 90):
        return None
    if not scale:
        if not (focal_mm > 0 and pixel_um > 0):
            return None
        scale = pixel_um / focal_mm * 206.264806 / 3600
    if not math.isfinite(scale):
        return None
    return Field(ra, dec, scale * math.hypot(w, hgt) / 2, solved)


def search_radius(field: Field) -> float:
    """The radius to cover: SPCC asks for half the image diagonal around the solved centre.
    The commanded pointing in an unsolved header can be off by a fraction of a degree, so
    an unsolved field gets a wider margin."""
    if field.solved:
        return field.radius_deg * 1.02 + 0.05
    return field.radius_deg * 1.1 + 0.5


_CENTER = re.compile(r"Image center: alpha: (\d+) (\d+) ([\d.]+), delta: ([+-]?\d+) (\d+) ([\d.]+)")
_LOCAL_QUERY = re.compile(r"Getting stars from (local|online) catalogue .*?radius of ([\d.]+) degrees")


def field_from_log(text: str) -> tuple[float, float, float] | None:
    """(ra, dec, radius_deg) of the SPCC query in a Siril log: the plate-solved centre and
    the radius SPCC printed (Siril prints twice the radius)."""
    centers = _CENTER.findall(text)
    query = _LOCAL_QUERY.findall(text)
    if not centers or not query:
        return None
    h, m, s, dd, dm, ds = centers[-1]
    ra = (int(h) + int(m) / 60 + float(s) / 3600) * 15
    sign = -1 if dd.startswith("-") else 1
    dec = sign * (abs(int(dd)) + int(dm) / 60 + float(ds) / 3600)
    return ra, dec, float(query[-1][1]) / 2 + 0.01


WORK_IMAGES = ("spcc.fit", "bge.fits", "stack.fit")     # plate-solved first


def work_field(work: Path, focal_mm: float, pixel_um: float) -> tuple[Path, Field] | None:
    """(image, field) for a work directory: from spcc.fit when it exists (plate-solved),
    else from bge.fits or stack.fit (the commanded pointing)."""
    for name in WORK_IMAGES:
        p = Path(work) / name
        if p.is_file():
            f = field_of(p, focal_mm, pixel_um)
            if f is not None:
                return p, f
    return None


# ------------------------------------------------------------------ `darkroom catalog`

CATALOG_HELP = "keep a local copy of the star spectra SPCC needs (the color stage does this by itself)"
CATALOG_EPILOG = """\
The color stage's SPCC needs Gaia DR3 XP sampled spectra for the stars in the field.
darkroom keeps a local copy of the sky it has processed, fetched once from the same
mirrors Siril uses (with retries and fallback to the next mirror), and runs SPCC on it
offline. The color stage fetches what a field needs by itself; `catalog fetch` does it
ahead of time, while online. A field takes a few to a few tens of MB.

  darkroom catalog fetch --work DIR            the field of DIR's image
  darkroom catalog fetch --ra 10.68 --dec 41.27 --radius 2.5
  darkroom catalog status [--work DIR]         what is stored; whether it covers DIR

environment: DARKROOM_CACHE (the copy is in <cache>/gaia-xpsampled)
"""


def add_parser(subparsers) -> None:
    import argparse
    p = subparsers.add_parser("catalog", help=CATALOG_HELP, description=CATALOG_HELP, epilog=CATALOG_EPILOG,
                              formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="action", required=True, metavar="ACTION")
    for name, help_ in (("fetch", "fetch the star spectra for a field"),
                        ("status", "show what the local copy holds")):
        q = sp.add_parser(name, help=help_, description=help_)
        q.add_argument("--work", type=Path, metavar="DIR", help="work directory: use the field of its image "
                       "(spcc.fit, else bge.fits, else stack.fit) and the focal length and pixel size of its "
                       "[color] settings")
        q.add_argument("--config", type=Path, metavar="FILE", help="settings file (default: <work>/darkroom.toml)")
        if name == "fetch":
            q.add_argument("--ra", type=float, metavar="DEG", help="field centre RA in degrees")
            q.add_argument("--dec", type=float, metavar="DEG", help="field centre Dec in degrees")
            q.add_argument("--radius", type=float, metavar="DEG", help="field radius in degrees")
    p.set_defaults(func=run_catalog)


def _field_for(a) -> tuple[float, float, float, str]:
    if a.work is not None:
        from . import config
        c = config.resolve(a.work.expanduser(), a.config)["color"]
        found = work_field(a.work.expanduser(), float(c["focal"]), float(c["pixel"]))
        if found is None:
            raise tools.StageError(f"no image with a sky position in {a.work} (looked for "
                                   + ", ".join(WORK_IMAGES) + ")")
        image, f = found
        return f.ra, f.dec, search_radius(f), f"{image.name}{'' if f.solved else ' (commanded pointing)'}"
    if None in (getattr(a, "ra", None), getattr(a, "dec", None), getattr(a, "radius", None)):
        raise tools.StageError("give --work DIR, or all of --ra, --dec and --radius")
    if not (0 <= a.ra < 360 and -90 <= a.dec <= 90 and 0 < a.radius <= 30):
        raise tools.StageError("--ra must be in 0..360, --dec in -90..90 and --radius in 0..30")
    return a.ra, a.dec, a.radius, "--ra/--dec/--radius"


def run_catalog(a) -> int:
    store = Store()
    if a.action == "fetch":
        ra, dec, radius, src = _field_for(a)
        print(f"field: RA {ra:.4f}, Dec {dec:+.4f}, radius {radius:.2f} deg (from {src})")
        ensure(ra, dec, radius, store=store)
        return 0
    have = store.coverage()
    print(f"local SPCC catalogue: {store.root}")
    print(f"  {len(have)} of {NPIX} sky pixels, {store.size() / 1e6:.1f} MB"
          + (f", in {len(by_chunk(have))} of {N_CHUNKS} files" if have else ""))
    if a.work is not None:
        ra, dec, radius, src = _field_for(a)
        pixels = disc(ra, dec, radius)
        miss = store.missing(pixels)
        print(f"  field of {a.work} (from {src}): "
              + ("covered" if not miss else f"{len(miss)} of {len(pixels)} sky pixels missing; "
                 f"run `darkroom catalog fetch --work {a.work}`"))
        return 0 if not miss else 1
    return 0
