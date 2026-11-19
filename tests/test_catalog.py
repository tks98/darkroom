"""Catalogue fallback: log parsing, query construction, CSV validation (no network)."""
import pytest

from darkroom import catalog, stages, tools

LOG = """\
log: Running command: platesolve
log: Plate solving image from online Gaia DR3 (via Vizier) for a field of view of 4.17 degrees, using a limit magnitude of 13.10
log: URL retrieval failed. libcurl error: [28]
log: Cannot create catalogue file /home/someone/Library/Application Support/org.siril.Siril/siril/download_cache/cat_02_011.0750_+41.3994_125.07_13.1.csv (generic error)
log: Plate solving failed: The image could not be aligned with the reference stars
log: Script execution failed.
"""
NAME = "cat_02_011.0750_+41.3994_125.07_13.1.csv"
CSV = """\
ra,dec,pmra,pmdec,mag,bmag,teff
12.208212433480202,39.71879371111923,-8.087190557952873,-2.2155759674117954,12.0029,12.245572,6332.663
12.321934932416909,39.808468438363526,4.048041259654588,-4.221877207159093,10.857287,11.350146,
9.739855295847988,43.00892156570062,,,10.483221,,5806.5054
"""


def test_parse_failed_request_from_log(monkeypatch):
    monkeypatch.delenv("DARKROOM_SIRIL_CACHE", raising=False)
    (r,) = catalog.failed_requests(LOG + LOG)          # the same failure twice is one request
    assert str(r.path) == ("/home/someone/Library/Application Support/org.siril.Siril/siril/"
                           "download_cache/" + NAME)
    assert (r.catalog, r.ra, r.dec, r.radius_arcmin, r.maglim) == (2, 11.075, 41.3994, 125.07, 13.1)
    assert r.radius_deg == pytest.approx(2.0845)


def test_parse_name_variants():
    assert catalog.parse_name("cat_02_097.9941_-05.0218_44.20_15.1.csv") == (2, 97.9941, -5.0218, 44.2, 15.1)
    assert catalog.parse_name("cat_01_083.8182_-05.3902_38.18_15.7.csv")[0] == 1
    assert catalog.parse_name("something_else.csv") is None


def test_no_catalogue_failure_means_no_requests():
    assert catalog.failed_requests("log: Plate solving failed: not enough stars\n") == []
    assert catalog.recover("log: Siril solve succeeded.\n", log=lambda m: None) == []


def test_cache_directory_override(monkeypatch, tmp_path):
    monkeypatch.setenv("DARKROOM_SIRIL_CACHE", str(tmp_path))
    (r,) = catalog.failed_requests(LOG)
    assert r.path == tmp_path / NAME


def test_adql():
    q = catalog.adql(11.075, 41.3994, 125.07 / 60, 13.1)
    assert q == ("SELECT ra,dec,pmra,pmdec,phot_g_mean_mag AS mag,phot_bp_mean_mag AS bmag,teff_gspphot AS teff "
                 "FROM gaiadr3.gaia_source "
                 "WHERE 1=CONTAINS(POINT('ICRS',ra,dec),CIRCLE('ICRS',11.0750,41.3994,2.08)) "
                 "AND phot_g_mean_mag<=13.1")
    assert "CIRCLE('ICRS',97.9941,-5.0218," in catalog.adql(97.9941, -5.0218, 0.7, 15.1)


def test_validate_csv():
    assert catalog.validate_csv(CSV) == 3
    assert catalog.validate_csv(CSV.replace("\n", "\r\n")) == 3
    for bad, match in [
        ("", "header"),
        ("<?xml version='1.0'?><VOTABLE>error</VOTABLE>", "header"),
        (CSV.replace("ra,dec,pmra,pmdec,mag,bmag,teff", "ra,dec,pmra,pmdec,phot_g_mean_mag,bmag,teff"), "header"),
        (CSV + "1.0,2.0,3.0\n", "fields"),
        (CSV + "abc,2.0,,,1,2,3\n", "not numeric"),
        (CSV + "400.0,2.0,,,1,2,3\n", "out of range"),
        ("ra,dec,pmra,pmdec,mag,bmag,teff\n", "0 stars"),
    ]:
        with pytest.raises(ValueError, match=match):
            catalog.validate_csv(bad)


def test_recover_writes_the_file_atomically(monkeypatch, tmp_path):
    monkeypatch.setenv("DARKROOM_SIRIL_CACHE", str(tmp_path / "cache"))
    asked, msgs = [], []

    def fetcher(ra, dec, radius_deg, maglim):
        asked.append((ra, dec, round(radius_deg, 4), maglim))
        return CSV

    written = catalog.recover(LOG, log=msgs.append, fetcher=fetcher)
    assert written == [tmp_path / "cache" / NAME]
    assert written[0].read_text() == CSV
    assert asked == [(11.075, 41.3994, 2.0845, 13.1)]
    assert [p.name for p in (tmp_path / "cache").iterdir()] == [NAME]      # no temp file left
    assert any("catalogue fallback" in m and "3 stars" in m for m in msgs)


def test_recover_rejects_bad_data_and_other_catalogues(monkeypatch, tmp_path):
    monkeypatch.setenv("DARKROOM_SIRIL_CACHE", str(tmp_path))
    with pytest.raises(tools.StageError, match="not usable"):
        catalog.recover(LOG, log=lambda m: None, fetcher=lambda *a: "<html>busy</html>")
    assert list(tmp_path.iterdir()) == []
    other = LOG.replace("cat_02_", "cat_01_")
    assert catalog.recover(other, log=lambda m: None, fetcher=lambda *a: CSV) == []


def fake_siril(monkeypatch, tmp_path, outcomes):
    """Replace siril.run_script: each call pops an outcome ('fail' writes the failure log)."""
    calls = []

    def run_script(script, log_path):
        outcome = outcomes[len(calls)]
        calls.append(outcome)
        if outcome == "fail":
            log_path.write_text(LOG)
            raise tools.StageError("Siril script color.ssf failed")
        log_path.write_text("log: Siril solve succeeded.\n")
        (tmp_path / stages.SPCC).write_bytes(b"spcc")

    monkeypatch.setattr(stages.siril, "run_script", run_script)
    (tmp_path / stages.BGE).write_bytes(b"bge")
    return calls


def test_color_retries_once_after_fetching(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DARKROOM_SIRIL_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(catalog, "fetch", lambda *a, **k: CSV)
    calls = fake_siril(monkeypatch, tmp_path, ["fail", "ok"])
    stages.color(tmp_path)
    assert calls == ["fail", "ok"]
    assert (tmp_path / "cache" / NAME).read_text() == CSV
    assert "catalogue fallback was used" in capsys.readouterr().out
    assert "catalogue fallback used" in (tmp_path / "logs" / "color.log").read_text()
    assert "Cannot create catalogue file" in (tmp_path / "logs" / "color.attempt1.log").read_text()


def test_color_reports_both_errors_when_the_retry_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("DARKROOM_SIRIL_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(catalog, "fetch", lambda *a, **k: CSV)
    calls = fake_siril(monkeypatch, tmp_path, ["fail", "fail"])
    with pytest.raises(tools.StageError, match="(?s)First attempt.*Second attempt"):
        stages.color(tmp_path)
    assert calls == ["fail", "fail"]


def test_color_fallback_can_be_switched_off(monkeypatch, tmp_path):
    monkeypatch.setenv("DARKROOM_SIRIL_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(catalog, "fetch", lambda *a, **k: pytest.fail("must not fetch"))
    calls = fake_siril(monkeypatch, tmp_path, ["fail", "ok"])
    with pytest.raises(tools.StageError, match="switched off"):
        stages.color(tmp_path, catalog_fallback=False)
    assert calls == ["fail"] and not (tmp_path / "cache").exists()


def test_color_reports_a_failed_fetch(monkeypatch, tmp_path):
    monkeypatch.setenv("DARKROOM_SIRIL_CACHE", str(tmp_path / "cache"))

    def boom(*a, **k):
        raise tools.StageError("Gaia archive request failed: timed out")
    monkeypatch.setattr(catalog, "fetch", boom)
    fake_siril(monkeypatch, tmp_path, ["fail", "ok"])
    with pytest.raises(tools.StageError, match="(?s)color.ssf failed.*fallback also failed.*timed out"):
        stages.color(tmp_path)


SPCC_429 = """log: Getting stars from online catalogue Gaia DR3 xp_sampled for PCC, with a radius of 5.07 degrees and limit magnitude 12.49
log: HTTP range request failed with code 429 for URL https://zenodo.org/records/17988559/files/siril_cat1_healpix8_xpsamp_21.dat
log: Failed to read data entries via HTTP
log: Query failed on mirror https://zenodo.org/records/17988559/files, trying alternatives...
"""


def test_spcc_server_failure_rate_limit():
    msg = catalog.spcc_server_failure(SPCC_429)
    assert msg and "HTTP 429" in msg and "zenodo.org" in msg and "Wait" in msg


def test_spcc_server_failure_server_error():
    msg = catalog.spcc_server_failure(SPCC_429.replace("code 429", "code 502"))
    assert msg and "HTTP 502" in msg and "problems" in msg


def test_spcc_server_failure_none_without_mirror_failover():
    assert catalog.spcc_server_failure("log: Found a solution for color calibration using 1241 stars.") is None
    assert catalog.spcc_server_failure(SPCC_429.replace("Query failed on mirror", "x")) is None


def test_color_explains_an_spcc_server_failure(monkeypatch, tmp_path):
    """Siril crashes after the SPCC mirror refuses; the stage says why instead of retrying."""
    monkeypatch.setattr(catalog, "fetch", lambda *a, **k: pytest.fail("must not fetch"))

    def run_script(script, log_path):
        log_path.write_text(SPCC_429)
        raise tools.StageError("siril-cli exited with code -11")

    monkeypatch.setattr(stages.siril, "run_script", run_script)
    (tmp_path / stages.BGE).write_bytes(b"bge")
    with pytest.raises(tools.StageError, match="(?s)star spectra.*HTTP 429.*Wait"):
        stages.color(tmp_path)
