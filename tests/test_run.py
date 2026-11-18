"""Aspect-ratio crop, recorded parameters and `darkroom run` skipping (stages are faked)."""
import os
import re

import numpy as np
import pytest

from darkroom import cli, finish as fin, stages, tools
from darkroom.finish import FinishParams


# --- crop

def test_aspect_box_m31_numbers():
    # starless image 1710 x 3632, target at x=977, y=1794 -> the crop chosen by hand
    assert fin.aspect_box(1710, 3632, (2, 3), (977, 1794)) == (0, 512, 1710, 2565)


@pytest.mark.parametrize("size,aspect,center,want", [
    ((1710, 3632), (2, 3), (100, 100), (0, 0, 1710, 2565)),          # clamped at the top
    ((1710, 3632), (2, 3), (100, 3600), (0, 1067, 1710, 2565)),      # clamped at the bottom
    ((1710, 3632), (1, 1), (855, 1816), (0, 961, 1710, 1710)),
    ((1710, 3632), (3, 2), (855, 1816), (0, 1246, 1710, 1140)),
    ((4000, 1000), (2, 3), (2000, 500), (1667, 0, 666, 1000)),       # height is the limiting side
    ((4000, 1000), (2, 3), (3990, 500), (3334, 0, 666, 1000)),
    ((300, 200), (3, 2), (0, 0), (0, 0, 300, 200)),                  # already that shape
])
def test_aspect_box(size, aspect, center, want):
    box = fin.aspect_box(*size, aspect, center)
    assert box == want
    x, y, w, h = box
    assert x >= 0 and y >= 0 and x + w <= size[0] and y + h <= size[1]


def test_parse_crop_spec():
    assert fin.parse_crop_spec(None) == ("none", None)
    assert fin.parse_crop_spec("None") == ("none", None)
    assert fin.parse_crop_spec("0,512,1710,2565") == ("box", (0, 512, 1710, 2565))
    assert fin.parse_crop_spec(" 2:3 ") == ("aspect", (2, 3))
    for bad in ["2:0", "a:b", "1,2,3", "2:3:4", "-1,0,5,5", "0,0,0,5", "wide"]:
        with pytest.raises(ValueError):
            fin.parse_crop_spec(bad)


def blob_image(h=300, w=200, cx=120, cy=210):
    y, x = np.mgrid[:h, :w]
    g = 0.05 + 0.5 * np.exp(-((x - cx) ** 2 + (y - cy) ** 2) / (2 * 25.0 ** 2))
    img = np.repeat(g[..., None], 3, -1).astype(np.float32)
    img[20, 30] = 1.0                      # a leftover star: brighter, but tiny
    return img


def test_target_center_ignores_a_single_bright_pixel():
    assert fin.target_center(blob_image()) == (120, 210)


def test_recombine_aspect_crop_and_center_override():
    sl = blob_image()
    msgs = []
    p = FinishParams(black=0.0, mid=0.5, sat=1.0, yb=1.0, core=0.0, sharpen=0.0, neutral=False, crop_aspect=(1, 1))
    out = fin.recombine(sl, sl, p, log=msgs.append)
    assert out.shape == (200, 200, 3)                       # box 0,110,200,200
    np.testing.assert_allclose(out, sl[100:300], atol=1e-6)     # 210 - 100 = 110, clamped to 300 - 200
    assert any("0,100,200,200" in m and "x=120, y=210" in m for m in msgs)
    p.crop_center = (100, 100)
    out = fin.recombine(sl, sl, p, log=msgs.append)
    np.testing.assert_allclose(out, sl[0:200], atol=1e-6)
    assert "(given)" in msgs[-1]


def test_crop_outside_image_is_an_error():
    sl = blob_image()
    p = FinishParams(crop=(0, 0, 500, 500), neutral=False)
    with pytest.raises(ValueError, match="does not fit"):
        fin.recombine(sl, sl, p, log=lambda m: None)


def test_crop_flag_forms():
    a = cli.build_parser().parse_args(["finish", "--work", "w", "--crop", "2:3", "--crop-center", "10,20"])
    assert a.crop == cli.Aspect(2, 3) and a.crop_center == "10,20"
    p = cli._finish_params(a)
    assert p.crop is None and p.crop_aspect == (2, 3) and p.crop_center == (10, 20)
    assert cli._overrides(a)[("finish", "crop")] == "2:3"
    a = cli.build_parser().parse_args(["finish", "--work", "w", "--crop", "none"])
    assert a.crop is None and cli._overrides(a)[("finish", "crop")] == "none"
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["finish", "--work", "w", "--crop", "2:x"])


# --- skip logic

@pytest.fixture
def fake(monkeypatch, tmp_path):
    """A work dir with config, subs and a dark; every stage is replaced by one that just
    writes its outputs. Returns (work, list of stages that ran, kwargs of the last stack)."""
    work = tmp_path / "work"
    subs = tmp_path / "subs"
    subs.mkdir()
    work.mkdir()
    (subs / "Light_1.fit").write_bytes(b"x")
    (tmp_path / "dark.fit").write_bytes(b"d")
    (work / "darkroom.toml").write_text(
        f'[target]\nsubs = "{subs}"\ndark = "{tmp_path / "dark.fit"}"\n[finish]\ngamma = 1.6\n')
    ran, clock = [], [1_000_000_000]

    def make(stage):
        def run(w, *args, **kw):
            ran.append((stage, args, kw))
            for p in stages.outputs(w, stage):
                p.write_bytes(stage.encode())
                clock[0] += 10                       # strictly increasing mtimes, no sleeping
                os.utime(p, (clock[0], clock[0]))
        return run

    for st in stages.STAGES:
        monkeypatch.setattr(stages, st, make(st))
    monkeypatch.setattr(tools, "starnet_dir", lambda flag: tmp_path)
    for p in list(subs.iterdir()) + [tmp_path / "dark.fit"]:
        os.utime(p, (1000, 1000))
    return work, ran, clock


def names(ran):
    out = [r[0] for r in ran]
    ran.clear()
    return out


def run(work, *extra):
    return cli.main(["run", "--work", str(work), *extra])


DEFAULT = [s for s in stages.STAGES if s != "deconv"]       # the stages a default config runs


def mtimes(work):
    return {p.name: p.stat().st_mtime_ns for st in DEFAULT for p in stages.outputs(work, st)}


def test_second_run_skips_everything(fake, capsys):
    work, ran, _ = fake
    assert run(work) == 0
    assert names(ran) == DEFAULT
    before = mtimes(work)
    capsys.readouterr()
    assert run(work) == 0
    assert names(ran) == []
    assert mtimes(work) == before
    out = capsys.readouterr().out
    for st in DEFAULT:
        assert f"-- {st}: up to date, skipped" in out
    assert "-- deconv: off ([deconv] strength = 0)" in out


def test_changed_finish_setting_reruns_only_finish(fake):
    work, ran, _ = fake
    run(work); names(ran)
    cfg = work / "darkroom.toml"
    cfg.write_text(cfg.read_text().replace("gamma = 1.6", "gamma = 1.9"))
    assert run(work) == 0
    assert names(ran) == ["finish"]
    assert run(work) == 0 and names(ran) == []
    assert run(work, "--crop", "2:3") == 0 and names(ran) == ["finish"]       # a flag counts too


def test_changed_middle_stage_reruns_it_and_everything_after(fake):
    work, ran, _ = fake
    run(work); names(ran)
    cfg = work / "darkroom.toml"
    cfg.write_text(cfg.read_text() + "[denoise]\nstrength = 0.4\n")
    assert run(work) == 0
    assert names(ran) == ["denoise", "stretch", "starnet", "finish"]


def test_from_and_force(fake):
    work, ran, _ = fake
    run(work); names(ran)
    assert run(work, "--from", "stretch") == 0
    assert names(ran) == ["stretch", "starnet", "finish"]
    assert run(work, "--from", "stretch", "--to", "stretch") == 0 and names(ran) == ["stretch"]
    assert run(work) == 0 and names(ran) == ["starnet", "finish"]     # stretched.tif is newer now
    assert run(work, "--force") == 0
    assert [r[0] for r in ran] == DEFAULT
    assert ran[0][2]["force"] is True                                 # stack may clear process/
    names(ran)
    assert run(work, "--to", "color") == 0 and names(ran) == []


def test_missing_output_or_newer_input_reruns(fake):
    work, ran, clock = fake
    run(work); names(ran)
    (work / stages.SPCC).unlink()
    assert run(work) == 0 and names(ran) == ["color", "denoise", "stretch", "starnet", "finish"]
    clock[0] += 100
    os.utime(work / stages.STARLESS, (clock[0], clock[0]))            # as if starnet was re-run by hand
    assert run(work) == 0 and names(ran) == ["finish"]


def test_stage_commands_record_parameters(fake):
    work, ran, _ = fake
    run(work); names(ran)
    assert cli.main(["finish", "--work", str(work), "--gamma", "2.5"]) == 0
    assert stages.recorded_params(work, "finish")["gamma"] == 2.5
    names(ran)
    assert run(work) == 0 and names(ran) == ["finish"]                # config still says 1.6
    assert cli.main(["finish", "--work", str(work), "--out", "trial"]) == 0
    names(ran)
    assert run(work) == 0 and names(ran) == []                        # --out trial left final.* alone


def test_failed_stage_leaves_no_record(fake, monkeypatch):
    work, ran, _ = fake
    run(work); names(ran)

    def boom(*a, **k):
        raise tools.StageError("no")
    good = stages.stretch
    monkeypatch.setattr(stages, "stretch", boom)
    assert run(work, "--from", "stretch") == 1
    assert stages.recorded_params(work, "stretch") is None
    monkeypatch.setattr(stages, "stretch", good)
    assert run(work) == 0 and names(ran) == ["stretch", "starnet", "finish"]


def stretch_src(ran):
    (call,) = [r for r in ran if r[0] == "stretch"]
    return call[2]["src"]


def test_deconv_off_leaves_stretch_as_it_was(fake, capsys):
    work, ran, _ = fake
    assert run(work) == 0
    assert stretch_src(ran) == stages.DENOISED
    assert "-- deconv: off ([deconv] strength = 0)" in capsys.readouterr().out
    assert stages.recorded_params(work, "deconv") is None
    s = cli.config.resolve(work)
    assert stages.recorded_params(work, "stretch") == stages.result_params("stretch", s["stretch"])  # no new keys
    assert not (work / stages.DECONVOLVED).exists()


def test_switching_deconv_on_and_off(fake, capsys):
    work, ran, _ = fake
    run(work); names(ran)
    plain = stages.recorded_params(work, "stretch")
    cfg = work / "darkroom.toml"
    text = cfg.read_text()
    cfg.write_text(text + "[deconv]\nstrength = 0.5\n")
    assert run(work) == 0
    assert stretch_src(ran) == stages.DECONVOLVED
    assert names(ran) == ["deconv", "stretch", "starnet", "finish"]
    assert stages.recorded_params(work, "deconv") == {"strength": 0.5, "psf": "auto", "cpu": False}
    assert stages.recorded_params(work, "stretch") == plain | {"input": "deconvolved.fits"}
    assert run(work) == 0 and names(ran) == []
    cfg.write_text(text + "[deconv]\nstrength = 0.5\npsf = 3.2\n")
    assert run(work) == 0 and names(ran) == ["deconv", "stretch", "starnet", "finish"]
    assert run(work, "--to", "deconv") == 0 and names(ran) == []
    assert run(work, "--from", "deconv", "--to", "deconv") == 0 and names(ran) == ["deconv"]
    assert run(work) == 0 and names(ran) == ["stretch", "starnet", "finish"]  # deconvolved.fits is newer
    capsys.readouterr()
    cfg.write_text(text + "[deconv]\nstrength = 0\npsf = 3.2\n")             # off again
    assert run(work) == 0
    assert stretch_src(ran) == stages.DENOISED
    assert names(ran) == ["stretch", "starnet", "finish"]
    assert "-- stretch: has to run (changed: input)" in capsys.readouterr().out
    assert stages.recorded_params(work, "stretch") == plain
    assert run(work) == 0 and names(ran) == []


def test_denoise_change_reruns_deconv(fake):
    work, ran, _ = fake
    cfg = work / "darkroom.toml"
    cfg.write_text(cfg.read_text() + "[deconv]\nstrength = 0.5\n")
    assert run(work) == 0 and names(ran) == stages.STAGES
    cfg.write_text(cfg.read_text() + "[denoise]\nstrength = 0.4\n")
    assert run(work) == 0 and names(ran) == ["denoise", "deconv", "stretch", "starnet", "finish"]


def test_result_params_ignore_unused_crop_center():
    a = stages.result_params("finish", cli.config.resolve("/nonexistent-work")["finish"] | {"crop_center": "1,2"})
    assert a["crop_center"] is None
    assert stages.result_params("starnet", {"dir": "/x"}) == {}


def test_stack_refuses_to_mix_runs(tmp_path, monkeypatch):
    work, subs = tmp_path / "w", tmp_path / "s"
    (work / "process").mkdir(parents=True)
    subs.mkdir()
    (subs / "Light_1.fit").write_bytes(b"x")
    (tmp_path / "d.fit").write_bytes(b"x")
    (work / "process" / "pp_light_.seq").write_text("old")
    (work / "process" / "pp_light_00001.fit").write_bytes(b"old")
    scripts = []

    def run_script(script, log):
        scripts.append(script.read_text())
        log.write_text("log: Rejection stacking complete. 3 images have been stacked.\n")
        out = re.findall(r'"-out=([^"]+)"', scripts[-1])[-1]
        open(out + ".fit", "wb").write(b"s")

    monkeypatch.setattr(stages.siril, "run_script", run_script)
    with pytest.raises(tools.StageError, match="--force"):
        stages.stack(work, subs, tmp_path / "d.fit")
    assert (work / "process" / "pp_light_.seq").read_text() == "old" and not scripts
    stages.stack(work, subs, tmp_path / "d.fit", force=True)
    assert list((work / "process").iterdir()) == []                   # old frames gone before Siril ran
    assert "link light" in scripts[-1]
    (work / "process" / "pp_light_.seq").write_text("new")
    stages.stack(work, subs, None, framing="max", reuse=True, out="stack_max")
    assert "link light" not in scripts[-1] and "-framing=max" in scripts[-1] and "-maximize" in scripts[-1]
    assert (work / "process" / "pp_light_.seq").exists()
    assert (work / "stack_max.fit").is_file()
    stages.stack(work, subs, None, reuse=True, clean=True)
    assert not (work / "process").exists()
    with pytest.raises(tools.StageError, match="nothing to reuse"):
        stages.stack(work, subs, None, reuse=True)
