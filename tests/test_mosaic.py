"""Mosaic mode: pointing spread, catalogue prefill, crop with WCS, plumbing (synthetic data only)."""
import re
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS

from darkroom import catalog, cli, config, darks, mosaic, siril, stages, tools

# the test subs are headers only
pytestmark = pytest.mark.filterwarnings("ignore:File may have been truncated")


def sub(path, ra, dec, w=1080, h=1920, **extra):
    """A tiny FITS whose header claims a full-size Seestar frame at (ra, dec)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    hdr = fits.PrimaryHDU(np.zeros((h, w), np.uint8)).header       # SIMPLE, BITPIX, NAXIS1/2 = w, h
    hdr["RA"], hdr["DEC"] = ra, dec
    for k, v in extra.items():
        hdr[k.replace("_", "-")] = v
    hdr.tofile(path)            # header only: the pixels are never read
    return path


def code(script):
    """A Siril script without its comments."""
    return "\n".join(l for l in script.splitlines() if l.strip() and not l.startswith("#"))


def frames(points, w=1080, h=1920):
    return [mosaic.Frame(ra, dec, w, h) for ra, dec in points]


# ------------------------------------------------------------- geometry

def test_frame_radius_matches_siril_for_the_s30():
    assert round(mosaic.frame_radius_arcmin(1080, 1920, 150, 2.9), 2) == 73.21


def test_separation_and_mean_pointing_across_ra_zero():
    assert mosaic.separation(10, 0, 11, 0) == pytest.approx(1.0)
    assert mosaic.separation(10, 60, 12, 60) == pytest.approx(1.0, rel=2e-3)     # cos(60) shrinks RA
    ra, dec = mosaic.mean_pointing([359.5, 0.5], [0, 0])
    assert min(ra, 360 - ra) == pytest.approx(0, abs=1e-9) and dec == pytest.approx(0, abs=1e-9)


def test_spread_tells_a_mosaic_from_one_pointing():
    one = mosaic.spread(frames([(98.35, 4.94)] * 5 + [(98.36, 4.95)]), 150, 2.9)
    assert one.pointings == 2 and not one.is_mosaic
    assert one.field_deg == pytest.approx(1080 * 3.988 / 3600, rel=1e-3)          # 1.20 deg
    grid = [(98 + 0.6 * i, 4 + 0.6 * j) for i in range(3) for j in range(4)]
    many = mosaic.spread(frames(grid), 150, 2.9)
    assert many.pointings == 12 and many.is_mosaic and many.spread_deg > 1
    assert "12 pointings" in many.describe()
    assert mosaic.spread([], 150, 2.9) is None and mosaic.spread(frames(grid), None, 2.9) is None


def test_detect_reads_headers_and_skips_unreadable_files(tmp_path):
    files = [sub(tmp_path / f"Light_{i}.fit", 98 + 0.7 * i, 4.0) for i in range(4)]
    (tmp_path / "Light_bad.fit").write_bytes(b"not fits")
    files.append(tmp_path / "Light_bad.fit")
    fr = mosaic.read_frames(files)
    assert [(f.ra, f.width, f.height) for f in fr] == [(98 + 0.7 * i, 1080, 1920) for i in range(4)]
    assert mosaic.detect(files, 150, 2.9).is_mosaic
    assert mosaic.detect([tmp_path / "Light_bad.fit"], 150, 2.9) is None


# ------------------------------------------------------------- prefill

def test_cache_name_is_what_siril_asks_for():
    assert mosaic.cache_name(98.35, 4.9444, 73.21, 13) == "cat_02_098.3500_+04.9444_73.21_13.0.csv"
    assert mosaic.cache_name(5.5, -0.25, 73.21, 13.5) == "cat_02_005.5000_-00.2500_73.21_13.5.csv"
    # the name parses back with the fallback's own parser
    assert catalog.parse_name(mosaic.cache_name(98.35, 4.9444, 73.21, 13)) == (2, 98.35, 4.9444, 73.21, 13.0)


def test_requests_round_header_coordinates_like_siril():
    req = mosaic.requests(frames([(98.350005, 4.944444), (98.350004, 4.944441), (99.0, 5.0)]), 150, 2.9, 13)
    assert sorted(req) == ["cat_02_098.3500_+04.9444_73.21_13.0.csv", "cat_02_099.0000_+05.0000_73.21_13.0.csv"]
    assert req["cat_02_098.3500_+04.9444_73.21_13.0.csv"] == (98.35, 4.9444, 73.21)


def star_csv(points):
    return catalog.HEADER + "\n" + "".join(f"{ra:.6f},{dec:.6f},1.0,-2.0,12.1,12.6,5000\n" for ra, dec in points)


def test_split_catalog_by_angular_distance():
    stars = [(10.0, 0.0), (10.5, 0.0), (11.9, 0.0), (12.0, 1.0), (30.0, 0.0)]
    cones = {"a": (10.0, 0.0, 60.0), "b": (12.0, 0.0, 73.21)}
    out = mosaic.split_catalog(star_csv(stars), cones)
    assert out["a"][1] == 2 and out["b"][1] == 2            # (12, 1) is 60' from b's centre: inside
    for text, n in out.values():
        assert catalog.validate_csv(text, min_stars=0) == n
        assert text.startswith(catalog.HEADER + "\n") and text.endswith("\n")
    assert "30.000000" not in out["a"][0] + out["b"][0]
    with pytest.raises(ValueError):
        mosaic.split_catalog("nope\n1,2\n", cones)


def test_prefill_one_query_for_all_pointings(tmp_path):
    files = [sub(tmp_path / "subs" / f"Light_{i}.fit", 98.0 + 0.5 * i, 4.0) for i in range(3)]
    files.append(sub(tmp_path / "subs" / "Light_9.fit", 98.0, 4.0))     # a second frame, same pointing
    calls, msgs = [], []

    def fetcher(ra, dec, radius_deg, mag):
        calls.append((ra, dec, radius_deg, mag))
        return star_csv([(98.0 + 0.01 * i, 4.0) for i in range(200)])

    cache = tmp_path / "cache"
    written = mosaic.prefill(files, 150, 2.9, 13, cache=cache, fetcher=fetcher, log=msgs.append)
    assert len(calls) == 1
    ra, dec, radius, mag = calls[0]
    assert ra == pytest.approx(98.5, abs=1e-6) and mag == 13
    far = float(mosaic.separation(98.0, 4.0, ra, dec))
    assert far == pytest.approx(0.4988, abs=1e-3)
    assert radius == pytest.approx(far + 73.21 / 60 + 0.01, abs=1e-6)       # every cone fits in the query
    assert sorted(p.name for p in written) == sorted(mosaic.requests(mosaic.read_frames(files), 150, 2.9, 13))
    assert len(written) == 3 and all(p.parent == cache for p in written)
    assert not list(cache.glob("*.darkroom-tmp"))
    n = catalog.validate_csv((cache / mosaic.cache_name(98.0, 4.0, 73.21, 13)).read_text())
    assert n == 123                                         # 98.00 .. 99.22, radius 73.21'
    # a second run finds everything cached and does not query again
    assert mosaic.prefill(files, 150, 2.9, 13, cache=cache, fetcher=fetcher, log=msgs.append) == []
    assert len(calls) == 1 and "already in" in msgs[-1]


def test_siril_cache_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DARKROOM_SIRIL_CACHE", str(tmp_path))
    assert mosaic.siril_cache_dir() == tmp_path
    monkeypatch.delenv("DARKROOM_SIRIL_CACHE")
    monkeypatch.setattr(tools, "is_macos", lambda: False)
    # Linux: Siril keeps its download cache in its config folder, not in XDG_CACHE_HOME
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdgcache"))
    assert mosaic.siril_cache_dir() == tmp_path / "home" / ".config" / "siril" / "download_cache"
    monkeypatch.setenv("HOME", "/cache")                     # the Docker image
    assert mosaic.siril_cache_dir() == Path("/cache/.config/siril/download_cache")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert mosaic.siril_cache_dir() == tmp_path / "xdg" / "siril" / "download_cache"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/ignored")
    assert mosaic.siril_cache_dir() == Path("/cache/.config/siril/download_cache")
    monkeypatch.setattr(tools, "is_macos", lambda: True)
    assert mosaic.siril_cache_dir().parts[-4:] == ("Application Support", "org.siril.Siril", "siril",
                                                   "download_cache")


# ------------------------------------------------------------- crop

def ragged(h=300, w=200, seed=0):
    """A mosaic-like coverage: zero outside a ragged, stair-stepped region."""
    rng = np.random.default_rng(seed)
    valid = np.zeros((h, w), bool)
    valid[40:260, 30:180] = True
    valid[10:60, 60:120] = True                 # a tile sticking out at the top
    valid[200:290, 0:50] = True                 # and one at the bottom left
    valid[100:103, 175:182] = False             # a notch in the right edge
    return valid, rng


def brute_force_area(valid):
    best = 0
    H, W = valid.shape
    cum = valid.cumsum(0).cumsum(1)
    cum = np.pad(cum, ((1, 0), (1, 0)))
    for y0 in range(H):
        for y1 in range(y0 + 1, H + 1):
            # widest run of columns that are valid over rows y0..y1
            col = (cum[y1, 1:] - cum[y0, 1:] - cum[y1, :-1] + cum[y0, :-1]) == (y1 - y0)
            run = m = 0
            for c in col:
                run = run + 1 if c else 0
                m = max(m, run)
            best = max(best, m * (y1 - y0))
    return best


def test_largest_rectangle_is_largest():
    valid, _ = ragged(60, 40)
    valid = valid[::5, ::5]
    y, x, h, w = mosaic._largest_rectangle(valid)
    assert valid[y:y + h, x:x + w].all()
    assert h * w == brute_force_area(valid)


@pytest.mark.parametrize("coarse", [400, 20, 7])
def test_valid_box_is_all_valid_and_near_largest(coarse):
    valid, _ = ragged()
    x, y, w, h = mosaic.valid_box(valid, coarse=coarse)
    assert valid[y:y + h, x:x + w].all()
    # the true largest: rows 40..260 x columns 30..175 (the notch cuts the right edge)
    assert w * h >= 0.97 * (220 * 145)
    # no side can grow by a pixel and stay valid
    assert not (y > 0 and valid[y - 1, x:x + w].all()) and not (x > 0 and valid[y:y + h, x - 1].all())


def test_valid_box_edge_cases():
    assert mosaic.valid_box(np.zeros((5, 5), bool)) is None
    full = np.ones((50, 30), bool)
    assert mosaic.valid_box(full) == (0, 0, 30, 50)
    one = np.zeros((9, 9), bool)
    one[4, 6] = True
    assert mosaic.valid_box(one, coarse=3) == (6, 4, 1, 1)


def test_valid_mask_needs_every_channel():
    d = np.ones((3, 4, 5), np.float32)
    d[1, 0, 0] = 0
    d[2, 3, 4] = np.nan
    m = mosaic.valid_mask(d)
    assert m.shape == (4, 5) and not m[0, 0] and not m[3, 4] and m.sum() == 18
    assert mosaic.valid_mask(np.ones((4, 5))).all()


def wcs_header(h, w):
    hdr = fits.Header()
    hdr.update({"CTYPE1": "RA---TAN", "CTYPE2": "DEC--TAN", "CRVAL1": 98.0, "CRVAL2": 5.0,
                "CRPIX1": w / 2 + 0.5, "CRPIX2": h / 2 + 0.5, "CD1_1": -1.1e-3, "CD1_2": 1e-5,
                "CD2_1": 1e-5, "CD2_2": 1.1e-3, "STACKCNT": 127})
    return hdr


def test_crop_to_data_keeps_the_sky_position_of_every_pixel(tmp_path):
    valid, rng = ragged()
    H, W = valid.shape
    data = (rng.random((3, H, W)).astype(np.float32) + 0.1) * valid
    data[:, 150, 100] = 5.0                      # a "star" inside the valid area
    p = tmp_path / "stack.fit"
    fits.PrimaryHDU(data, header=wcs_header(H, W)).writeto(p)
    before = WCS(fits.getheader(p), naxis=2).pixel_to_world_values(100, 150)   # numpy col 100, row 150
    msgs = []
    x, y, w, h = mosaic.crop_to_data(p, tmp_path / "stack_full.fit", log=msgs.append)
    full = fits.getdata(tmp_path / "stack_full.fit")
    np.testing.assert_array_equal(full, data)                                  # uncropped file kept as is
    hdr = fits.getheader(p)
    out = fits.getdata(p)
    assert out.shape == (3, h, w) and (out != 0).all() and out.dtype.name == "float32"
    assert hdr["STACKCNT"] == 127 and hdr["NAXIS1"] == w and hdr["NAXIS2"] == h
    # the star moved by the crop offset, and the WCS still puts it at the same sky position
    sy, sx = np.argwhere(out[0] == 5.0)[0]
    assert (sx, sy) == (100 - x, 150 - y)
    after = WCS(hdr, naxis=2).pixel_to_world_values(sx, sy)
    np.testing.assert_allclose(after, before, atol=1e-9)
    assert any(f"{W}x{H} to {w}x{h}" in m and "stack_full.fit" in m for m in msgs)


def test_crop_to_data_full_frame_is_untouched(tmp_path):
    p = tmp_path / "stack.fit"
    fits.PrimaryHDU(np.ones((3, 8, 6), np.float32), header=wcs_header(8, 6)).writeto(p)
    assert mosaic.crop_to_data(p, tmp_path / "stack_full.fit", log=lambda m: None) == (0, 0, 6, 8)
    assert not (tmp_path / "stack_full.fit").exists() and fits.getheader(p)["CRPIX1"] == 3.5


def test_shift_wcs_without_wcs_is_harmless():
    hdr = fits.Header()
    mosaic.shift_wcs(hdr, 3, 4)
    assert "CRPIX1" not in hdr


# ------------------------------------------------------------- settings, flags, presets

def test_mosaic_setting_flag_and_framing(tmp_path, capsys):
    subs = tmp_path / "subs"
    subs.mkdir()
    base = f'[target]\nsubs = "{subs}"\ndark = "none"\n'
    (tmp_path / "darkroom.toml").write_text(base)
    a = cli.build_parser().parse_args(["stack", "--work", str(tmp_path)])
    t = cli._stack_table(cli._settings(a))
    assert t["mosaic"] is False and t["framing"] == "min"
    assert "mosaic" not in stages.result_params("stack", t)             # old work dirs stay up to date
    a = cli.build_parser().parse_args(["stack", "--work", str(tmp_path), "--mosaic"])
    t = cli._stack_table(cli._settings(a))
    assert t["mosaic"] is True and t["framing"] == "max"                 # forced, silently (default)
    assert "WARNING" not in capsys.readouterr().out
    p = stages.result_params("stack", t)
    assert p["mosaic"] is True and p["framing"] == "max"
    (tmp_path / "darkroom.toml").write_text(base + '[stack]\nmosaic = true\nframing = "cog"\n')
    a = cli.build_parser().parse_args(["stack", "--work", str(tmp_path)])
    t = cli._stack_table(cli._settings(a))
    assert t["framing"] == "max" and "framing = cog" in capsys.readouterr().out
    a = cli.build_parser().parse_args(["stack", "--work", str(tmp_path), "--no-mosaic"])
    assert cli._stack_table(cli._settings(a))["mosaic"] is False
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["stack", "--work", str(tmp_path), "--mosaic", "--no-mosaic"])
    capsys.readouterr()
    assert config.SCHEMA["stack"]["mosaic"][:2] == ("bool", False)


def test_presets_s30_and_s30_pro_match_their_own_subs(tmp_path):
    tables = {n: config.load_camera(n)[1]["camera"] for n in config.preset_names("cameras")}
    assert {"seestar-s30", "seestar-s30-pro"} <= set(tables)
    s30 = sub(tmp_path / "a" / "Light_1.fit", 98, 4, CREATOR="ZWO Seestar S30", INSTRUME="Seestar S30")
    pro = sub(tmp_path / "b" / "Light_1.fit", 98, 4, CREATOR="ZWO Seestar S30 Pro", INSTRUME="Seestar S30 Pro")
    assert darks.detect_camera(s30, tables) == "seestar-s30"
    assert darks.detect_camera(pro, tables) == "seestar-s30-pro"
    # whichever order the presets come in
    assert darks.detect_camera(pro, dict(reversed(list(tables.items())))) == "seestar-s30-pro"


def test_s30_preset_contents():
    settings_, meta, _ = config.load_camera("seestar-s30")
    assert settings_["color"] == {"sensor": "ZWO Seestar S30", "filter": "UV/IR Block", "focal": 150, "pixel": 2.9}
    assert meta["camera"]["bayer"] == "GRBG" and meta["camera"]["sub_glob"] == "Light_*.fit"
    assert meta["filters"] == {"IRCUT": "UV/IR Block", "LP": "ZWO Seestar LP"}
    assert meta["darks"] == config.load_camera("seestar-s30-pro")[1]["darks"]


def test_init_detects_a_mosaic_and_the_s30(tmp_path, capsys):
    hdr = dict(CREATOR="ZWO Seestar S30", FILTER="LP", EXPTIME=60.0, FOCALLEN=150.0, XPIXSZ=2.9,
               BAYERPAT="GRBG", OBJECT="NGC 2244")
    for i in range(6):
        sub(tmp_path / "rosette" / f"Light_mosaic_{i}.fit", 98 + 0.5 * (i % 3), 4 + 0.6 * (i // 3), **hdr)
    for i in range(3):
        sub(tmp_path / "single" / f"Light_{i}.fit", 98, 4, **hdr)
    assert cli.main(["init", "--work", str(tmp_path / "w1"), "--subs", str(tmp_path / "rosette"), "--no-dark"]) == 0
    out = capsys.readouterr().out
    s = config.resolve(tmp_path / "w1")
    assert s["stack"]["mosaic"] is True and s["target"]["camera"] == "seestar-s30"
    assert s["color"]["filter"] == "ZWO Seestar LP" and s["color"]["sensor"] == "ZWO Seestar S30"
    assert "mosaic = true" in (tmp_path / "w1" / "darkroom.toml").read_text()
    assert "look like a mosaic" in out and "6 pointings" in out
    assert cli.main(["init", "--work", str(tmp_path / "w2"), "--subs", str(tmp_path / "single"), "--no-dark"]) == 0
    assert "look like a mosaic" not in capsys.readouterr().out
    assert config.resolve(tmp_path / "w2")["stack"]["mosaic"] is False


# ------------------------------------------------------------- scripts and the stack stage

def test_mosaic_scripts_render():
    t = siril.template_dir()
    first = siril.render((t / "mosaic_solve.ssf").read_text(), {
        "PROCESS": "/w/process", "SUBS": "/s/rosette subs", "DARK": "", "NODARK": "1", "FORCE": "-force ", "FRAMES": "",
        "MAG": "13", "FOCAL": "150", "PIXEL": "2.9"})
    lines = [l for l in first.splitlines() if l and not l.startswith("#")]
    assert 'cd "/s/rosette subs"' in lines and 'link light "-out=/w/process"' in lines
    assert "calibrate light -cfa -equalize_cfa -debayer" in lines
    assert ("seqplatesolve pp_light -catalog=gaia -nocache -force -limitmag=13 -focal=150 -pixelsize=2.9 "
            "-order=2") in lines
    again = siril.render((t / "mosaic_solve.ssf").read_text(), {
        "PROCESS": "/w/process", "SUBS": "", "DARK": "", "NODARK": "", "FRAMES": "151", "MAG": "13", "FOCAL": "150",
        "PIXEL": "2.9"})
    again = code(again)
    assert "link" not in again and "calibrate" not in again and "-force" not in again
    assert 'cd "/w/process"' in again and "select pp_light 1 151" in again
    assert again.index("select pp_light") < again.index("seqplatesolve pp_light")
    assert "select" not in code(first)
    dark = siril.render((t / "mosaic_solve.ssf").read_text(), {
        "PROCESS": "/w/process", "SUBS": "/s", "DARK": "/d/dark.fit", "NODARK": "", "FORCE": "-force ", "FRAMES": "",
        "MAG": "13", "FOCAL": "150", "PIXEL": "2.9"})
    assert 'calibrate light "-dark=/d/dark.fit" -cc=dark 3 3 -cfa -equalize_cfa -debayer' in dark
    stack = siril.render((t / "mosaic_stack.ssf").read_text(), {
        "PROCESS": "/w/process", "STACK": "/w/stack", "KEEP_FWHM": "90", "KEEP_ROUND": "90"})
    lines = [l for l in stack.splitlines() if l and not l.startswith("#")]
    assert lines.index("set32bits") < next(i for i, l in enumerate(lines) if l.startswith("seqapplyreg"))
    assert "seqapplyreg pp_light -filter-wfwhm=90% -filter-round=90% -framing=max" in lines
    assert ('stack r_pp_light rej w 3 3 -norm=addscale -overlap_norm -output_norm -weight=wfwhm -feather=40 '
            '-maximize -32b "-out=/w/stack"') in lines


def test_solved_count():
    assert stages.solved_count("log: 139 images successfully platesolved out of 151 included\n") == (139, 151)
    # a rerun: Siril's count includes the frames solved before
    assert stages.solved_count("log: 139 images successfully platesolved out of 151 included\n"
                               "log: 142 images successfully platesolved out of 151 included\n"
                               "log: (139 were already solved and skipped)\n") == (142, 151)
    assert stages.solved_count("nothing") is None


class FakeSiril:
    """Stands in for siril.run_script: records scripts and writes what Siril would."""

    def __init__(self, work, cache, solve_logs):
        self.work, self.cache, self.solve_logs, self.scripts = work, cache, list(solve_logs), []

    def __call__(self, script, log):
        text = script.read_text()
        self.scripts.append(text)
        if "seqplatesolve" in text:
            (self.work / "process").mkdir(exist_ok=True)
            (self.work / "process" / "pp_light_.seq").write_text(
                "#S 'sequence_name' start_index nb_images ...\nS 'pp_light_' 1 4 3 5 0 6 0 0 0\n")
            log.write_text(self.solve_logs.pop(0))
            return
        log.write_text("log: Rejection stacking complete. 5 images have been stacked.\n")
        out = re.findall(r'"-out=([^"]+)"', text)[-1]
        data = np.zeros((3, 40, 30), np.float32)
        data[:, 5:35, 4:28] = 1.0
        fits.PrimaryHDU(data, header=wcs_header(40, 30)).writeto(out + ".fit", overwrite=True)


def test_mosaic_stack_prefills_solves_recovers_and_crops(tmp_path, monkeypatch, capsys):
    work, subs, cache = tmp_path / "w", tmp_path / "subs", tmp_path / "cache"
    for i in range(4):
        sub(subs / f"Light_{i}.fit", 98 + 0.6 * i, 4.0)
    monkeypatch.setenv("DARKROOM_SIRIL_CACHE", str(cache))
    monkeypatch.setattr(catalog, "fetch", lambda ra, dec, r, m: star_csv([(98.0 + 0.1 * i, 4.0) for i in range(30)]))
    missing = cache / "cat_02_120.0000_+04.0000_73.21_13.0.csv"          # a name the prediction missed
    fake = FakeSiril(work, cache, [
        f"log: Cannot create catalogue file {missing} (generic error)\n"
        "log: 3 images successfully platesolved out of 4 included\n",
        "log: 4 images successfully platesolved out of 4 included\nlog: (3 were already solved and skipped)\n"])
    monkeypatch.setattr(stages.siril, "run_script", fake)
    stages.stack(work, subs, None, mosaic=True, focal=150, pixel=2.9)
    out = capsys.readouterr().out
    assert len(list(cache.glob("cat_02_*.csv"))) == 5                    # 4 prefilled + 1 recovered
    solve1, solve2, stack = map(code, fake.scripts)
    assert "-force" in solve1 and "link light" in solve1
    assert "-force" not in solve2 and "calibrate" not in solve2 and "select pp_light 1 4" in solve2
    assert "set32bits" in stack and "-overlap_norm" in stack and "-framing=max" in stack
    assert "4 of 4 frames plate-solved" in out and "5 frames stacked" in out
    assert fits.getdata(work / "stack.fit").shape == (3, 30, 24)
    assert fits.getdata(work / "stack_full.fit").shape == (3, 40, 30)
    assert fits.getheader(work / "stack.fit")["CRPIX1"] == pytest.approx(15.5 - 4)
    assert (work / "logs" / "stack_solve.attempt1.log").is_file()
    # re-stack the solved sequence
    stages.stack(work, subs, None, mosaic=True, reuse=True, out="stack_b")
    assert "seqplatesolve" not in fake.scripts[-1] and "-overlap_norm" in fake.scripts[-1]
    assert (work / "stack_b.fit").is_file() and (work / "stack_b_full.fit").is_file()


def test_mosaic_stack_fails_when_nothing_solves(tmp_path, monkeypatch):
    work, subs = tmp_path / "w", tmp_path / "subs"
    sub(subs / "Light_1.fit", 98, 4.0)
    monkeypatch.setenv("DARKROOM_SIRIL_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(catalog, "fetch", lambda *a: star_csv([(98.0, 4.0)]))
    fake = FakeSiril(work, None, ["log: 0 images successfully platesolved out of 1 included\n"])
    monkeypatch.setattr(stages.siril, "run_script", fake)
    with pytest.raises(tools.StageError, match="no frame was plate-solved"):
        stages.stack(work, subs, None, mosaic=True, focal=150, pixel=2.9)
    assert len(fake.scripts) == 1


def test_ordinary_stack_warns_about_a_mosaic(tmp_path, monkeypatch, capsys):
    work, subs = tmp_path / "w", tmp_path / "subs"
    for i in range(4):
        sub(subs / f"Light_{i}.fit", 98 + 0.6 * i, 4.0)
    fake = FakeSiril(work, None, [])
    monkeypatch.setattr(stages.siril, "run_script", fake)
    stages.stack(work, subs, None, focal=150, pixel=2.9)
    assert "look like a mosaic" in capsys.readouterr().out
    assert "register pp_light" in fake.scripts[-1] and not (work / "stack_full.fit").exists()
