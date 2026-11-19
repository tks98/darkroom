"""Fallback star catalogue for Siril's plate solve.

Siril fetches its online Gaia DR3 catalogue through VizieR. When that server
cannot be reached the plate solve fails, and the log names the cache file Siril
wanted to create, e.g.

    log: URL retrieval failed. libcurl error: [28]
    log: Cannot create catalogue file /.../download_cache/cat_02_011.0750_+41.3994_125.07_13.1.csv (generic error)

The name holds everything needed to fetch the same list elsewhere:
``cat_02`` = Gaia DR3, then RA and Dec in degrees, the radius in arcminutes and
the limiting magnitude. This module fetches that star list from ESA's Gaia
archive and writes it where Siril expects it, in the format of Siril's own
cached files. The only data sent is the ADQL query (sky coordinates).
"""
from __future__ import annotations

import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import tools

TAP_URL = "https://gea.esac.esa.int/tap-server/tap/sync"
USER_AGENT = "darkroom"
TIMEOUT = 180
HEADER = "ra,dec,pmra,pmdec,mag,bmag,teff"
GAIA_DR3 = 2                    # Siril's catalogue number in the cache file name

_FAILED = re.compile(r"Cannot create catalogue file (.+?\.csv)")
_NAME = re.compile(r"cat_(\d+)_(\d+\.\d+)_([+-]?\d+\.\d+)_(\d+\.\d+)_(-?\d+\.\d+)\.csv")


@dataclass(frozen=True)
class Request:
    path: Path                  # where Siril looks for the catalogue
    catalog: int
    ra: float                   # degrees
    dec: float                  # degrees
    radius_arcmin: float
    maglim: float

    @property
    def radius_deg(self) -> float:
        return self.radius_arcmin / 60.0


def parse_name(name: str):
    """(catalog, ra, dec, radius_arcmin, maglim) from a Siril cache file name, or None."""
    m = _NAME.fullmatch(name)
    if not m:
        return None
    return int(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4)), float(m.group(5))


def failed_requests(log_text: str) -> list[Request]:
    """Catalogue files a Siril log says it could not create (in order, without duplicates)."""
    out, seen = [], set()
    for m in _FAILED.finditer(log_text):
        path = Path(m.group(1).strip())
        parsed = parse_name(path.name)
        if parsed is None or path.name in seen:
            continue
        seen.add(path.name)
        override = os.environ.get("DARKROOM_SIRIL_CACHE")
        if override:
            path = Path(override).expanduser() / path.name
        out.append(Request(path, *parsed))
    return out


_SPCC_HTTP = re.compile(r"HTTP range request failed with code (\d+) for URL (\S+)")


def spcc_server_failure(log_text: str) -> str | None:
    """A plain explanation when Siril failed to download its SPCC star spectra, else None.

    SPCC reads Gaia DR3 XP spectra from Siril's own mirrors (Zenodo first). When the first
    mirror answers with an error, Siril 1.4.5 logs "Query failed on mirror ..., trying
    alternatives..." and then crashes instead of trying the next one, so the log simply ends."""
    hits = _SPCC_HTTP.findall(log_text)
    if not hits or "Query failed on mirror" not in log_text:
        return None
    codes = sorted({int(c) for c, _ in hits})
    host = urllib.parse.urlparse(hits[-1][1]).netloc or "its catalogue server"
    if 429 in codes:
        why = f"{host} refused with HTTP 429 (too many requests): it is rate-limiting this machine"
    elif any(c >= 500 for c in codes):
        why = f"{host} answered with HTTP {'/'.join(map(str, codes))}: the server is having problems"
    else:
        why = f"{host} answered with HTTP {'/'.join(map(str, codes))}"
    return (f"Siril could not download the star spectra that colour calibration (SPCC) needs: {why}. "
            "Siril 1.4.5 then stops (it crashes instead of trying its other mirrors). This is not a "
            "problem with the image. Wait a few minutes (longer after HTTP 429) and run the stage again.")


def adql(ra: float, dec: float, radius_deg: float, maglim: float) -> str:
    """The query Siril sends to VizieR, rewritten for the Gaia archive's own table. Like
    Siril, the radius is given in degrees to two decimals."""
    return ("SELECT ra,dec,pmra,pmdec,phot_g_mean_mag AS mag,phot_bp_mean_mag AS bmag,teff_gspphot AS teff "
            "FROM gaiadr3.gaia_source "
            f"WHERE 1=CONTAINS(POINT('ICRS',ra,dec),CIRCLE('ICRS',{ra:.4f},{dec:.4f},{radius_deg:.2f})) "
            f"AND phot_g_mean_mag<={maglim:g}")


def validate_csv(text: str, min_stars: int = 1) -> int:
    """Check a catalogue in Siril's cached Gaia format; return the number of stars."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != HEADER:
        first = lines[0][:80] if lines else ""
        raise ValueError(f"unexpected catalogue header {first!r} (wanted {HEADER!r})")
    n = 0
    for i, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        cells = line.split(",")
        if len(cells) != 7:
            raise ValueError(f"catalogue line {i} has {len(cells)} fields, not 7")
        try:
            ra, dec = float(cells[0]), float(cells[1])
            for c in cells[2:]:
                if c.strip():
                    float(c)
        except ValueError:
            raise ValueError(f"catalogue line {i} is not numeric: {line[:80]!r}") from None
        if not (0 <= ra <= 360 and -90 <= dec <= 90):
            raise ValueError(f"catalogue line {i} has coordinates out of range")
        n += 1
    if n < min_stars:
        raise ValueError(f"catalogue has {n} stars (wanted at least {min_stars})")
    return n


def _ssl_context() -> ssl.SSLContext:
    """A verifying TLS context. Some Python installs (python.org builds on macOS before
    "Install Certificates" is run) have an empty trust store; fall back to certifi if it is
    installed, then to the system bundle. Verification is never turned off."""
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509_ca", 0) == 0:
        candidates = []
        try:
            import certifi
            candidates.append(certifi.where())
        except ImportError:
            pass
        candidates += ["/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"]
        for c in candidates:
            if os.path.isfile(c):
                ctx.load_verify_locations(c)
                break
    return ctx


def fetch(ra: float, dec: float, radius_deg: float, maglim: float, url: str = TAP_URL,
          timeout: float = TIMEOUT) -> str:
    """Query the ESA Gaia archive; return validated CSV text with Siril's header line."""
    body = urllib.parse.urlencode({"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv",
                                   "QUERY": adql(ra, dec, radius_deg, maglim)}).encode()
    req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
            text = r.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as e:
        raise tools.StageError(f"Gaia archive request failed ({url}): {e}") from None
    text = text.replace("\r\n", "\n")
    try:
        validate_csv(text)
    except ValueError as e:
        raise tools.StageError(f"Gaia archive returned something unexpected: {e}") from None
    return text


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".darkroom-tmp")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def recover(log_text: str, log=print, fetcher=None) -> list[Path]:
    """Fetch every Gaia catalogue the log says Siril could not download.

    Returns the files written; empty when the log shows no catalogue failure this
    module can repair. Raises StageError when a fetch fails.
    """
    written = []
    for r in failed_requests(log_text):
        if r.catalog != GAIA_DR3:
            log(f"catalogue fallback: {r.path.name} is not a Gaia DR3 catalogue; cannot fetch it elsewhere")
            continue
        log(f"catalogue fallback: Siril could not download its catalogue; fetching Gaia DR3 from the ESA "
            f"archive (RA {r.ra:.4f}, Dec {r.dec:+.4f}, radius {r.radius_deg:.2f} deg, G <= {r.maglim:g})")
        text = (fetcher or fetch)(r.ra, r.dec, r.radius_deg, r.maglim)
        try:
            n = validate_csv(text)
        except ValueError as e:
            raise tools.StageError(f"the fetched catalogue is not usable: {e}") from None
        write_atomic(r.path, text if text.endswith("\n") else text + "\n")
        log(f"catalogue fallback: wrote {n} stars to {r.path}")
        written.append(r.path)
    return written
