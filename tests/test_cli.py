"""CLI parsing and defaults (no external tools are run)."""
import pytest

from darkroom import cli, stages, tools


def parse(*argv):
    return cli.build_parser().parse_args(list(argv))


def test_finish_defaults_are_final_settings():
    a = parse("finish", "--work", "w")
    p = cli._finish_params(a)
    assert (p.gamma, p.star_strength, p.mid, p.sat, p.yb, p.sharpen, p.sharpen_radius,
            p.black, p.erode, p.neutral, p.crop) == (1.6, 0.95, 0.45, 1.2, 1.8, 0.8, 2.0, 0.02, 0, True, None)
    assert a.out == "final"


def test_finish_flags():
    a = parse("finish", "--work", "w", "--no-neutral", "--crop", "0,512,1710,2565")
    assert a.neutral is False and a.crop == (0, 512, 1710, 2565)


def test_bad_crop_is_a_usage_error():
    with pytest.raises(SystemExit):
        parse("finish", "--work", "w", "--crop", "1,2")


def test_stage_defaults():
    a = parse("stretch", "--work", "w")
    assert (a.asinh, a.shadows, a.target_bg, a.sat) == (25.0, -2.5, 0.10, 0.35)
    a = parse("color", "--work", "w")
    assert (a.sensor, a.filt, a.focal, a.pixel, a.catalog) == ("Sony IMX585", "UV/IR Block", 160.0, 2.9, "gaia")
    a = parse("denoise", "--work", "w")
    assert (a.strength, a.cpu) == (0.6, False)
    assert parse("bge", "--work", "w").smoothing == 0.1


def test_run_stage_order():
    assert stages.STAGES == ["stack", "bge", "color", "denoise", "stretch", "starnet", "finish"]
    a = parse("run", "--work", "w", "--from", "stretch", "--to", "finish")
    assert (a.from_, a.to) == ("stretch", "finish")


def test_run_stack_needs_subs_and_dark(tmp_path, capsys):
    assert cli.main(["run", "--work", str(tmp_path), "--to", "stack"]) == 1
    assert "--subs and --dark" in capsys.readouterr().err


def test_starnet_dir_missing_is_clear(monkeypatch):
    monkeypatch.delenv("DARKROOM_STARNET_DIR", raising=False)
    with pytest.raises(tools.StageError, match="DARKROOM_STARNET_DIR"):
        tools.starnet_dir(None)


def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("DARKROOM_SIRIL", str(tmp_path / "siril-cli"))
    monkeypatch.setenv("DARKROOM_CACHE", "~/somewhere")
    assert tools.siril_cli() == tmp_path / "siril-cli"
    assert "~" not in str(tools.cache_dir())


def test_missing_input_fails_loudly(tmp_path, capsys):
    assert cli.main(["finish", "--work", str(tmp_path)]) == 1
    assert "stretched.tif" in capsys.readouterr().err
