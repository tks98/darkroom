"""Local copy of SPCC's Gaia XP sampled catalogue: HEALPix maths, Siril's file layout,
mirror and retry logic, and how the color stage wires it into Siril (no network)."""
import json
import math
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from darkroom import cli, doctor, gaia_xp as g, siril, stages, tools

M31 = (10.6847, 41.2690)


# ------------------------------------------------------------------ HEALPix

def test_ang2pix_base_pixels():
    """nside 1: faces 0-3 north, 4-7 equator (centred on RA 0, 90, ...), 8-11 south."""
    ra = [45, 135, 225, 315, 0, 90, 180, 270, 45, 135, 225, 315]
    dec = [60] * 4 + [0] * 4 + [-60] * 4
    assert g.ang2pix(ra, dec, nside=1).tolist() == list(range(12))
    assert g.ang2pix(0, 90).tolist() == [65535]             # north pole: last pixel of face 0
    assert g.ang2pix(0, -90).tolist() == [8 * 65536]         # south pole: first pixel of face 8


def test_ang2pix_is_nested():
    """A level-n pixel number shifted right by 2 bits is its parent at level n-1."""
    rng = np.random.default_rng(1)
    ra = rng.uniform(0, 360, 20000)
    dec = np.degrees(np.arcsin(rng.uniform(-1, 1, 20000)))
    for level in range(1, 9):
        fine = g.ang2pix(ra, dec, nside=1 << level)
        coarse = g.ang2pix(ra, dec, nside=1 << (level - 1))
        assert np.array_equal(fine >> 2, coarse)


def test_ang2pix_equal_area():
    rng = np.random.default_rng(2)
    n = 192 * 2000
    ra = rng.uniform(0, 360, n)
    dec = np.degrees(np.arcsin(rng.uniform(-1, 1, n)))
    counts = np.bincount(g.ang2pix(ra, dec, nside=4), minlength=192)
    assert counts.min() > 1750 and counts.max() < 2250


def test_m31_is_in_chunk_2():
    (p,) = g.ang2pix(*M31)
    assert g.chunk_of(p) == 2 and g.chunk_name(2) == "siril_cat1_healpix8_xpsamp_2.dat"


def _random_points_in_disc(ra, dec, r_deg, n, seed=3):
    rng = np.random.default_rng(seed)
    rho = np.radians(r_deg) * np.sqrt(rng.uniform(0, 1, n))
    th = rng.uniform(0, 2 * np.pi, n)
    d0, a0 = math.radians(dec), math.radians(ra)
    sd = np.sin(d0) * np.cos(rho) + np.cos(d0) * np.sin(rho) * np.cos(th)
    d = np.arcsin(sd)
    a = a0 + np.arctan2(np.sin(th) * np.sin(rho) * np.cos(d0), np.cos(rho) - np.sin(d0) * sd)
    return np.degrees(a) % 360, np.degrees(d)


@pytest.mark.parametrize("ra, dec, r", [(*M31, 2.1), (83.8, -5.4, 0.7), (0.2, 0.1, 1.5),
                                        (280.0, 89.5, 1.0), (45.0, -88.0, 3.0)])
def test_disc_pixels_cover_the_disc(ra, dec, r):
    px = g.disc_pixels(ra, dec, r)
    a, d = _random_points_in_disc(ra, dec, r, 50000)
    assert set(g.ang2pix(a, d).tolist()) <= px
    # and not much more than the disc plus a ring of pixels
    area = 2 * math.pi * (1 - math.cos(math.radians(r + 2 * g.PIXEL_DEG)))
    assert len(px) <= area / (4 * math.pi / g.NPIX) * 1.1


def test_disc_pixels_include_a_pixel_that_only_touches_the_edge():
    ra, dec, r = 10.0, 30.0, 1.0
    px = g.disc_pixels(ra, dec, r)
    a, d = _random_points_in_disc(ra, dec, r + 0.002, 200000, seed=4)
    assert set(g.ang2pix(a, d).tolist()) <= px


def test_runs_and_chunks():
    assert g.runs([5, 1, 2, 3, 7, 8]) == [(1, 3), (5, 5), (7, 8)]
    assert g.by_chunk([16384 * 2 + 1, 5, 16384 * 2]) == {0: [5], 2: [32768, 32769]}


# ------------------------------------------------------------------ file layout

def header_for(chunk: int, title: bytes = b"Siril Gaia DR3 photometric extract chunk") -> bytes:
    first = chunk * g.PIX_PER_CHUNK
    raw = title.ljust(48, b"\0") + bytes([3, g.LEVEL, 2, 1, g.CHUNK_LEVEL])
    raw += chunk.to_bytes(4, "little") + first.to_bytes(4, "little")
    raw += (first + g.PIX_PER_CHUNK - 1).to_bytes(4, "little")
    return raw + b"\0" * (128 - len(raw))


def records_in(pixel: int, n: int, seed: int = 0) -> bytes:
    """``n`` records whose coordinates lie in ``pixel`` (found by sampling around it)."""
    if n == 0:
        return b""
    rng = np.random.default_rng(seed + pixel)
    # a point in the pixel: search the neighbourhood of M31 and of chunk 0 for samples
    centre = _pixel_point(pixel)
    out = []
    while len(out) < n:
        a = centre[0] + rng.normal(0, 0.05, 2000)
        d = centre[1] + rng.normal(0, 0.05, 2000)
        ok = g.ang2pix(a, d) == pixel
        out += list(zip(a[ok], d[ok]))
    rec = np.zeros(n, dtype=g.RECORD)
    rec["ra"] = np.round(np.array([p[0] for p in out[:n]]) % 360 / g.COORD_SCALE).astype(np.int64)
    rec["dec"] = np.round(np.array([p[1] for p in out[:n]]) / g.COORD_SCALE)
    rec["mag"] = 12000
    rec["flux"] = pixel % 1000
    return rec.tobytes()


_POINTS = {}


def _pixel_point(pixel):
    """A point inside a level-8 pixel: narrow down through its parents, level by level."""
    if pixel not in _POINTS:
        rng = np.random.default_rng(pixel)
        a, d = (x.ravel() for x in np.meshgrid(np.arange(0.5, 360, 1.0), np.arange(-89.5, 90, 1.0)))
        for level in range(1, g.LEVEL + 1):
            ok = g.ang2pix(a, d, nside=1 << level) == pixel >> (2 * (g.LEVEL - level))
            a, d = a[ok][:200], d[ok][:200]
            spread = 58.6 / (1 << level) / 2
            a = np.concatenate([a, (a[:, None] + rng.normal(0, spread, (len(a), 50))).ravel()])
            d = np.clip(np.concatenate([d, (d[:, None] + rng.normal(0, spread, (len(d), 50))).ravel()]),
                        -89.99, 89.99)
        ok = g.ang2pix(a, d) == pixel
        _POINTS[pixel] = (a[ok][0], d[ok][0])
    return _POINTS[pixel]


def remote_file(chunk: int, counts: dict[int, int]) -> bytes:
    """A whole chunk file as a mirror serves it (global pixel -> number of stars)."""
    first = chunk * g.PIX_PER_CHUNK
    recs = {p - first: records_in(p, n) for p, n in counts.items()}
    return g.build_chunk(header_for(chunk), recs)


def siril_query(path: Path, pixels: list[int]) -> list[bytes]:
    """What Siril 1.4.5's query_catalog (src/io/healpix/healpix.cpp) reads for these pixels:
    header, index entry before the first pixel and at the last pixel of each run, file size
    check, then the records."""
    raw = path.read_bytes()
    h = g.parse_header(raw[:128])
    out = []
    for a, b in g.runs([p - h.first for p in pixels]):
        idx = lambda i: int.from_bytes(raw[128 + 4 * i:132 + 4 * i], "little")
        start = 0 if a == 0 else idx(a - 1)
        end = idx(b)
        pos = 128 + g.INDEX_SIZE + start * g.RECORD_SIZE
        assert end >= start and pos + (end - start) * g.RECORD_SIZE <= len(raw), "Siril: index corrupted"
        out.append(raw[pos:pos + (end - start) * g.RECORD_SIZE])
    return out


def test_record_is_sirils_struct():
    assert g.RECORD_SIZE == 4 + 4 + 2 + 2 + 2 + 1 + 343 * 2 == 701


def test_parse_header_checks_the_catalogue():
    h = g.parse_header(header_for(2), 2)
    assert (h.level, h.chunk_level, h.chunk, h.first, h.last) == (8, 1, 2, 32768, 49151)
    with pytest.raises(ValueError, match="chunk 2, not 3"):
        g.parse_header(header_for(2), 3)
    bad = bytearray(header_for(2))
    bad[49] = 9
    with pytest.raises(ValueError, match="not a Siril Gaia XP"):
        g.parse_header(bytes(bad))
    with pytest.raises(ValueError):
        g.parse_header(b"<html>")


def test_partial_file_reads_like_the_full_one(tmp_path):
    chunk, first = 2, 2 * g.PIX_PER_CHUNK
    counts = {first + 10: 3, first + 11: 0, first + 12: 5, first + 500: 2, first + 16383: 1}
    full = tmp_path / "full.dat"
    full.write_bytes(remote_file(chunk, counts))
    keep = [first + 11, first + 12, first + 16383]
    _, recs = g.read_chunk(full)
    part = tmp_path / g.chunk_name(chunk)
    part.write_bytes(g.build_chunk(header_for(chunk), {p - first: recs.get(p - first, b"") for p in keep}))
    assert siril_query(part, keep) == siril_query(full, keep)
    assert siril_query(part, [first + 10]) == [b""]               # not kept: no stars, no error
    assert part.stat().st_size == 128 + g.INDEX_SIZE + 6 * 701


def test_check_records_rejects_records_from_another_pixel():
    p = int(g.ang2pix(*M31)[0])
    g.check_records(records_in(p, 4), p)
    with pytest.raises(ValueError, match="do not lie in pixel"):
        g.check_records(records_in(p + 5000, 2), p)
    with pytest.raises(ValueError, match="whole number"):
        g.check_records(b"x" * 700, p)


def test_store_merges_and_records_coverage(tmp_path):
    store = g.Store(tmp_path)
    first = 2 * g.PIX_PER_CHUNK
    store.add(2, header_for(2), {first + 1: records_in(first + 1, 2), first + 3: b""})
    store.add(2, header_for(2), {first + 2: records_in(first + 2, 1)})
    assert store.coverage() == {first + 1, first + 2, first + 3}
    _, recs = g.read_chunk(tmp_path / g.chunk_name(2))
    assert sorted(recs) == [1, 2] and len(recs[1]) == 2 * 701
    assert store.missing([first + 3, first + 4]) == {first + 4}
    data = json.loads((tmp_path / "coverage.json").read_text())
    assert data["pixels"] == [[first + 1, first + 3]]
    # a new release of the file on the mirrors: start the chunk over
    store.add(2, header_for(2, b"Siril Gaia DR3 v2"), {first + 9: b""})
    assert store.coverage() == {first + 9}
    # coverage only counts while the file is there
    (tmp_path / g.chunk_name(2)).unlink()
    assert store.coverage() == set()


# ------------------------------------------------------------------ download

class Mirrors:
    """Fake HTTP: base URL -> file bytes, or a list of errors to raise first."""

    def __init__(self, files: dict[str, bytes], errors: dict[str, list] | None = None):
        self.files, self.errors, self.calls = files, errors or {}, []

    def __call__(self, url, start, length):
        base = url.rsplit("/", 1)[0]
        self.calls.append((base, start, length))
        errs = self.errors.get(base)
        if errs:
            e = errs.pop(0)
            if e is not None:
                raise e
        data = self.files[base][start:start + length]
        if len(data) != length:
            raise g.FetchError("HTTP 416", retry=False)
        return data


def fetcher(m, mirror_list, sleeps=None):
    return g.Fetcher(mirror_list, get=m, sleep=(sleeps.append if sleeps is not None else lambda s: None),
                     log=lambda s: None)


FIRST = 2 * g.PIX_PER_CHUNK
COUNTS = {FIRST + i: (i % 4) for i in range(0, 40)}


def test_fetch_chunk_takes_only_the_needed_records():
    f = remote_file(2, COUNTS)
    m = Mirrors({"A": f})
    want = [FIRST + 2, FIRST + 3, FIRST + 30]
    header, recs = fetcher(m, ["A"]).fetch_chunk(2, want)
    assert header == header_for(2) and sorted(recs) == want
    assert len(recs[FIRST + 3]) == 3 * 701 and recs[FIRST + 30] == records_in(FIRST + 30, 2)
    assert m.calls[0][1:] == (0, 128) and m.calls[1][1:] == (128, g.INDEX_SIZE)


def test_far_apart_pixels_are_separate_requests():
    counts = {FIRST + 1: 1, FIRST + 5000: 800, FIRST + 9000: 1}
    m = Mirrors({"A": remote_file(2, counts)})
    fetcher(m, ["A"]).fetch_chunk(2, [FIRST + 1, FIRST + 9000])
    assert len(m.calls) == 4                    # header, index, two data ranges (800 stars between)
    m2 = Mirrors({"A": remote_file(2, COUNTS)})
    fetcher(m2, ["A"]).fetch_chunk(2, [FIRST + 1, FIRST + 7, FIRST + 20])
    assert len(m2.calls) == 3                   # small gaps: one data request


def test_retries_with_backoff_then_succeeds():
    m = Mirrors({"A": remote_file(2, COUNTS)},
                {"A": [g.FetchError("HTTP 429", wait=7), g.FetchError("HTTP 503")]})
    sleeps = []
    _, recs = fetcher(m, ["A"], sleeps).fetch_chunk(2, [FIRST + 1])
    assert sleeps == [7, 4] and recs[FIRST + 1] == records_in(FIRST + 1, 1)


def test_falls_back_to_the_next_mirror():
    f = remote_file(2, COUNTS)
    m = Mirrors({"A": f, "B": f, "C": f},
                {"A": [g.FetchError("HTTP 502")] * 3, "B": [g.FetchError("HTTP 404", retry=False)]})
    fx = fetcher(m, ["A", "B", "C"])
    fx.fetch_chunk(2, [FIRST + 1])
    assert [c[0] for c in m.calls].count("A") == 3 and [c[0] for c in m.calls].count("B") == 1
    assert fx.used == {"C"}
    m.calls.clear()
    fx.fetch_chunk(2, [FIRST + 2])                  # the mirror that worked goes first next time
    assert m.calls[0][0] == "C"


def test_a_mirror_with_wrong_data_is_skipped():
    good = remote_file(2, COUNTS)
    shifted = remote_file(2, {p + 1: n for p, n in COUNTS.items()})
    bad = good[:128 + g.INDEX_SIZE] + shifted[128 + g.INDEX_SIZE:]     # right index, records one pixel off
    m = Mirrors({"A": bad, "B": good})
    fx = fetcher(m, ["A", "B"])
    _, recs = fx.fetch_chunk(2, [FIRST + 1, FIRST + 2])
    assert fx.used == {"B"} and recs[FIRST + 2] == records_in(FIRST + 2, 2)


def test_every_mirror_failing_is_a_stage_error(tmp_path):
    m = Mirrors({"A": b"", "B": b""}, {"A": [g.FetchError("HTTP 500")] * 9, "B": [g.FetchError("HTTP 429")] * 9})
    with pytest.raises(tools.StageError, match="(?s)could not download.*A: HTTP 500.*B: HTTP 429"):
        g.ensure(*M31, 0.3, store=g.Store(tmp_path), fetcher=fetcher(m, ["A", "B"]), log=lambda s: None)
    assert not (tmp_path / "coverage.json").exists()


def test_a_huge_field_is_refused(tmp_path):
    m = Mirrors({"A": remote_file(2, {p: 5 for p in g.disc_pixels(*M31, 0.3)})})
    with pytest.raises(tools.StageError, match="more than the 0 MB"):
        g.ensure(*M31, 0.3, store=g.Store(tmp_path), fetcher=fetcher(m, ["A"]), log=lambda s: None,
                 budget=1000)


def test_ensure_fetches_once(tmp_path):
    ra, dec, r = M31[0], M31[1], 0.4
    need = g.disc_pixels(ra, dec, r)
    assert set(g.by_chunk(need)) == {2}
    m = Mirrors({"A": remote_file(2, {p: 1 for p in need})})
    store = g.Store(tmp_path)
    logs = []
    assert g.ensure(ra, dec, r, store=store, fetcher=fetcher(m, ["A"]), log=logs.append) == tmp_path
    assert store.covers(ra, dec, r) and "fetched" in logs[-1]
    n = len(m.calls)
    g.ensure(ra, dec, r, store=store, fetcher=fetcher(m, ["A"]), log=logs.append)
    assert len(m.calls) == n and "already covers" in logs[-1]
    hit = siril_query(tmp_path / g.chunk_name(2), sorted(need))
    assert sum(len(b) for b in hit) == len(need) * 701


class FakeResponse:
    def __init__(self, status, body):
        self.status, self.body = status, body

    def read(self, n):
        return self.body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_http_range_needs_a_206(monkeypatch):
    seen = {}

    def urlopen(req, timeout, context):
        seen["range"] = req.get_header("Range")
        return FakeResponse(200, b"x" * 10_000)
    monkeypatch.setattr(g.urllib.request, "urlopen", urlopen)
    with pytest.raises(g.FetchError, match="no range support") as e:
        g.http_range("https://example.invalid/f.dat", 128, 64)
    assert not e.value.retry and seen["range"] == "bytes=128-191"
    monkeypatch.setattr(g.urllib.request, "urlopen", lambda req, timeout, context: FakeResponse(206, b"y" * 64))
    assert g.http_range("https://example.invalid/f.dat", 128, 64) == b"y" * 64
    monkeypatch.setattr(g.urllib.request, "urlopen", lambda req, timeout, context: FakeResponse(206, b"y" * 10))
    with pytest.raises(g.FetchError, match="got 10 bytes") as e:
        g.http_range("https://example.invalid/f.dat", 128, 64)
    assert e.value.retry


def test_http_errors_say_whether_to_retry(monkeypatch):
    import urllib.error

    def raiser(code, headers=None):
        def urlopen(req, timeout, context):
            raise urllib.error.HTTPError(req.full_url, code, "x", headers or {}, None)
        return urlopen
    for code, retry in ((429, True), (500, True), (503, True), (404, False), (403, False)):
        monkeypatch.setattr(g.urllib.request, "urlopen", raiser(code, {"Retry-After": "12"}))
        with pytest.raises(g.FetchError) as e:
            g.http_range("https://example.invalid/f.dat", 0, 128)
        assert e.value.retry is retry and e.value.wait == 12


def test_mirrors_come_from_sirils_list_first(tmp_path):
    p = tmp_path / "spcc_mirrors.json"
    p.write_text(json.dumps([{"url": "https://mirror.example/files/", "description": "x"},
                             {"url": "ftp://nope"}, {"description": "no url"}]))
    out = g.mirrors(p)
    assert out[0] == "https://mirror.example/files" and "ftp://nope" not in out
    assert out[1:] == g.DEFAULT_MIRRORS
    assert g.mirrors(tmp_path / "missing.json") == g.DEFAULT_MIRRORS
    p.write_text("<html>")
    assert g.mirrors(p) == g.DEFAULT_MIRRORS


# ------------------------------------------------------------------ field of an image and of a log

def write_fits(path: Path, **cards) -> Path:
    h = fits.Header()
    for k, v in cards.items():
        h[k] = v
    fits.PrimaryHDU(np.zeros((30, 40), dtype=np.float32), header=h).writeto(path)
    return path


def test_field_of_unsolved_image(tmp_path):
    f = g.field_of(write_fits(tmp_path / "a.fits", RA=M31[0], DEC=M31[1]), 160, 2.9)
    scale = 2.9 / 160 * 206.264806 / 3600
    assert (f.ra, f.dec, f.solved) == (M31[0], M31[1], False)
    assert f.radius_deg == pytest.approx(scale * 25)
    assert g.search_radius(f) == pytest.approx(f.radius_deg * 1.1 + 0.5)
    f = g.field_of(write_fits(tmp_path / "b.fits", OBJCTRA="00 42 44.3", OBJCTDEC="-41 16 08"), 160, 2.9)
    assert f.ra == pytest.approx(10.6846, abs=1e-4) and f.dec == pytest.approx(-41.2689, abs=1e-4)
    assert g.field_of(write_fits(tmp_path / "c.fits"), 160, 2.9) is None
    (tmp_path / "d.fits").write_bytes(b"bge")
    assert g.field_of(tmp_path / "d.fits", 160, 2.9) is None


def test_field_of_solved_image(tmp_path):
    p = write_fits(tmp_path / "s.fits", CTYPE1="RA---TAN", CTYPE2="DEC--TAN", CRVAL1=M31[0], CRVAL2=M31[1],
                   CRPIX1=20.5, CRPIX2=15.5, CDELT1=-0.001, CDELT2=0.001, RA=11.0, DEC=40.0)
    f = g.field_of(p, 999, 9)
    assert f.solved and f.ra == pytest.approx(M31[0]) and f.dec == pytest.approx(M31[1])
    assert f.radius_deg == pytest.approx(0.001 * 25)


SOLVED_LOG = """\
log: Image center: alpha: 00 43 24.707, delta: +41 15 7.634
log: Running command: spcc
log: Getting stars from local catalogue Gaia DR3 xp_sampled for SPCC, with a radius of 4.10 degrees and limit magnitude 13.95
"""


def test_field_from_log():
    ra, dec, r = g.field_from_log(SOLVED_LOG)
    assert ra == pytest.approx(10.85295, abs=1e-4) and dec == pytest.approx(41.25212, abs=1e-4)
    assert r == pytest.approx(2.06)
    south = SOLVED_LOG.replace("+41 15", "-00 15")
    assert g.field_from_log(south)[1] == pytest.approx(-(15 / 60 + 7.634 / 3600))
    assert g.field_from_log("log: nothing") is None


# ------------------------------------------------------------------ Siril wiring

def test_scratch_config_copies_and_cleans_up(tmp_path):
    user = tmp_path / "config.1.4.ini"
    user.write_text("[core]\nwd=/somewhere\n")
    with siril.scratch_config(user) as cfg:
        assert cfg != user and cfg.read_text() == user.read_text()
        cfg.write_text("[core]\ncatalogue_gaia_photo=/x\n")         # what siril-cli saves at the end
    assert not cfg.exists() and user.read_text() == "[core]\nwd=/somewhere\n"
    with siril.scratch_config(tmp_path / "none.ini") as cfg:
        assert cfg.read_text() == "[core]\n"


def test_run_script_passes_the_initfile(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(siril.tools, "require_executable", lambda p, v: Path("/bin/siril-cli"))
    monkeypatch.setattr(siril.tools, "run_tool", lambda cmd, log: calls.append(cmd))
    s = tmp_path / "x.ssf"
    s.write_text("")
    siril.run_script(s, tmp_path / "log", initfile=tmp_path / "c.ini")
    siril.run_script(s, tmp_path / "log")
    assert calls[0] == ["/bin/siril-cli", "-i", str(tmp_path / "c.ini"), "-s", str(s.resolve())]
    assert calls[1] == ["/bin/siril-cli", "-s", str(s.resolve())]


def test_config_dir_follows_the_platform(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "is_macos", lambda: False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert siril.config_dir() == tmp_path / "siril"
    assert g.mirrors_file() == tmp_path / "siril" / "spcc_mirrors.json"
    monkeypatch.setattr(tools, "is_macos", lambda: True)
    assert siril.config_dir().parts[-3:] == ("Application Support", "org.siril.Siril", "siril")


OK_LOG = SOLVED_LOG + "log: Spectrophotometric Color Calibration succeeded.\n"


def fake_color_run(monkeypatch, tmp_path, outcomes, logs=None):
    """Replace siril.run_script; record (script text, initfile) of each call."""
    calls = []

    def run_script(script, log_path, initfile=None):
        i = len(calls)
        calls.append((script.read_text(), initfile, initfile.read_text() if initfile else None))
        log_path.write_text((logs or {}).get(i, OK_LOG))
        if outcomes[i] == "fail":
            raise tools.StageError("Siril script color.ssf failed")
        (tmp_path / stages.SPCC).write_bytes(b"spcc %d" % i)
    monkeypatch.setattr(stages.siril, "run_script", run_script)
    return calls


@pytest.fixture
def m31_work(tmp_path, monkeypatch):
    write_fits(tmp_path / stages.BGE, RA=M31[0], DEC=M31[1])
    user = tmp_path / "siril-user" / "config.1.4.ini"
    user.parent.mkdir()
    user.write_text("[core]\nwd=/user\n")
    monkeypatch.setattr(siril, "config_dir", lambda: user.parent)
    return tmp_path


def test_color_runs_spcc_on_the_local_copy(monkeypatch, m31_work, capsys):
    seen = []

    def ensure(ra, dec, radius, log=print, **k):
        seen.append((ra, dec, radius))
        return Path("/cache dir/gaia-xpsampled")
    monkeypatch.setattr(g, "ensure", ensure)
    monkeypatch.setattr(g.Store, "covers", lambda self, *a: True)
    calls = fake_color_run(monkeypatch, m31_work, ["ok"])
    stages.color(m31_work)
    script, initfile, cfg = calls[0]
    assert 'set "core.catalogue_gaia_photo=/cache dir/gaia-xpsampled"' in script
    assert script.index("set \"core.catalogue_gaia_photo") < script.index("spcc ")
    assert '"-whiteref=Average Spiral Galaxy" -catalog=localgaia\n' in script
    assert cfg == "[core]\nwd=/user\n" and initfile.parent != m31_work / "siril-user"
    assert not initfile.exists()                                     # the copy is gone
    assert (m31_work / "siril-user" / "config.1.4.ini").read_text() == "[core]\nwd=/user\n"
    assert seen[0][:2] == M31 and seen[0][2] > 0.5
    assert "local catalogue in /cache dir" in (m31_work / "logs" / "color.log").read_text()


def test_color_narrowband_also_uses_the_local_copy(monkeypatch, m31_work):
    monkeypatch.setattr(g, "ensure", lambda *a, **k: Path("/c"))
    monkeypatch.setattr(g.Store, "covers", lambda self, *a: True)
    calls = fake_color_run(monkeypatch, m31_work, ["ok"])
    stages.color(m31_work, narrowband="656.28/12,500.7/12,500.7/12")
    assert "-bbw=12 \"-whiteref=Average Spiral Galaxy\" -catalog=localgaia" in calls[0][0]


def test_color_goes_online_when_the_fetch_fails(monkeypatch, m31_work, capsys):
    def ensure(*a, **k):
        raise tools.StageError("could not download the SPCC star spectra: every mirror failed: zenodo.org: HTTP 503")
    monkeypatch.setattr(g, "ensure", ensure)
    calls = fake_color_run(monkeypatch, m31_work, ["ok"], {0: "log: online\n"})
    stages.color(m31_work)
    script, initfile, _ = calls[0]
    assert "catalogue_gaia_photo" not in script and "localgaia" not in script and initfile is None
    assert "Siril will try to download them itself" in capsys.readouterr().out


def test_color_without_spcc_local_never_fetches(monkeypatch, m31_work):
    monkeypatch.setattr(g, "ensure", lambda *a, **k: pytest.fail("must not fetch"))
    calls = fake_color_run(monkeypatch, m31_work, ["ok"], {0: "log: online\n"})
    stages.color(m31_work, spcc_local=False)
    assert "localgaia" not in calls[0][0] and calls[0][1] is None


def test_color_fetches_more_when_the_solved_field_reaches_further(monkeypatch, m31_work, capsys):
    fetched = []
    monkeypatch.setattr(g, "ensure", lambda *a, **k: fetched.append(a) or Path("/c"))
    covered = iter([False, True])
    monkeypatch.setattr(g.Store, "covers", lambda self, *a: next(covered))
    calls = fake_color_run(monkeypatch, m31_work, ["ok", "ok"])
    stages.color(m31_work)
    assert len(calls) == 2 and len(fetched) == 2
    assert fetched[1][0] == pytest.approx(10.85295, abs=1e-4)          # the solved centre from the log
    assert (m31_work / stages.SPCC).read_bytes() == b"spcc 1"
    assert "ran again" in (m31_work / "logs" / "color.log").read_text()


def test_color_retries_online_when_spcc_fails_on_the_local_copy(monkeypatch, m31_work, capsys):
    monkeypatch.setattr(g, "ensure", lambda *a, **k: Path("/c"))
    monkeypatch.setattr(g.Store, "covers", lambda self, *a: True)
    failed = SOLVED_LOG + "log: Chunk file not found: /c/siril_cat1_healpix8_xpsamp_3.dat\n"
    calls = fake_color_run(monkeypatch, m31_work, ["fail", "ok"], {0: failed, 1: "log: online\n"})
    stages.color(m31_work)
    assert "localgaia" in calls[0][0] and "localgaia" not in calls[1][0] and calls[1][1] is None
    assert "running it again with Siril downloading them itself" in capsys.readouterr().out
    assert (m31_work / "logs" / "color.attempt1.log").read_text() == failed


def test_color_keeps_the_server_explanation_when_online_fails_too(monkeypatch, m31_work):
    monkeypatch.setattr(g, "ensure", lambda *a, **k: Path("/c"))
    monkeypatch.setattr(g.Store, "covers", lambda self, *a: True)
    server = ("log: HTTP range request failed with code 429 for URL https://zenodo.org/records/1/files/x.dat\n"
              "log: Query failed on mirror https://zenodo.org/records/1/files, trying alternatives...\n")
    fake_color_run(monkeypatch, m31_work, ["fail", "fail"], {0: SOLVED_LOG, 1: server})
    with pytest.raises(tools.StageError, match="(?s)star spectra.*HTTP 429"):
        stages.color(m31_work)


def test_config_and_flag_reach_the_stage(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(stages, "color", lambda *a: seen.setdefault("args", a))
    assert cli.main(["color", "--work", str(tmp_path)]) == 0
    assert seen.pop("args")[-1] is True
    assert cli.main(["color", "--work", str(tmp_path), "--no-spcc-local"]) == 0
    assert seen.pop("args")[-1] is False
    (tmp_path / "darkroom.toml").write_text("[color]\nspcc_local = false\n")
    assert cli.main(["color", "--work", str(tmp_path)]) == 0
    assert seen.pop("args")[-1] is False


# ------------------------------------------------------------------ catalog command and doctor

def test_catalog_fetch_command(monkeypatch, tmp_path, capsys):
    seen = []
    monkeypatch.setattr(g, "ensure", lambda ra, dec, r, store=None: seen.append((ra, dec, r)))
    assert cli.main(["catalog", "fetch", "--ra", "10.68", "--dec", "41.27", "--radius", "2.5"]) == 0
    assert seen == [(10.68, 41.27, 2.5)]
    assert cli.main(["catalog", "fetch", "--ra", "10.68"]) == 1
    assert "give --work DIR" in capsys.readouterr().err
    write_fits(tmp_path / stages.BGE, RA=M31[0], DEC=M31[1])
    assert cli.main(["catalog", "fetch", "--work", str(tmp_path)]) == 0
    assert seen[-1][:2] == M31


def test_catalog_status_command(monkeypatch, tmp_path, capsys):
    write_fits(tmp_path / stages.BGE, RA=M31[0], DEC=M31[1])
    assert cli.main(["catalog", "status", "--work", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "0 of 786432 sky pixels" in out and "missing" in out


def test_doctor_reports_coverage(monkeypatch, tmp_path):
    assert doctor.check_spcc_catalogue(None) is None
    assert doctor.check_spcc_catalogue(tmp_path) is None                 # no image yet
    write_fits(tmp_path / stages.BGE, RA=M31[0], DEC=M31[1])
    store = g.Store(tmp_path / "cat")
    r = doctor.check_spcc_catalogue(tmp_path, store)
    assert r.status == doctor.WARN and "catalog fetch --work" in r.fix
    monkeypatch.setattr(g.Store, "missing", lambda self, px: set())
    assert doctor.check_spcc_catalogue(tmp_path, store).status == doctor.OK
    (tmp_path / "darkroom.toml").write_text("[color]\nspcc_local = false\n")
    assert doctor.check_spcc_catalogue(tmp_path, store) is None
