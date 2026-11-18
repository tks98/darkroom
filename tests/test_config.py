"""darkroom.toml, presets and precedence (no external tools are run)."""
import tomllib
from pathlib import Path

import pytest

from darkroom import cli, config, siril
from darkroom.finish import FinishParams


def write(tmp_path, text):
    (tmp_path / "darkroom.toml").write_text(text)
    return tmp_path


def settings(tmp_path, *argv):
    a = cli.build_parser().parse_args([argv[0], "--work", str(tmp_path), *argv[1:]])
    return cli._settings(a)


def test_builtin_defaults_equal_the_presets():
    """Without any config the behaviour must be what the shipped presets describe."""
    plain = config.resolve(Path("/nonexistent-work"))
    preset = config.resolve(Path("/nonexistent-work"), None,
                            {("target", "camera"): "seestar-s30-pro", ("target", "look"): "galaxy"})
    for section in config.SCHEMA:
        if section != "target":
            assert plain[section] == preset[section], section
    f = FinishParams()
    assert cli.stages.finish_params(plain["finish"]) == f


def test_camera_preset_contents():
    settings_, meta, _ = config.load_camera("seestar-s30-pro")
    assert settings_["color"] == {"sensor": "Sony IMX585", "filter": "UV/IR Block", "focal": 160, "pixel": 2.9}
    assert meta["camera"]["bayer"] == "GRBG" and meta["camera"]["sub_glob"] == "Light_*.fit"
    assert meta["filters"] == {"IRCUT": "UV/IR Block", "LP": "ZWO Seestar LP"}
    assert meta["darks"] == config.DEFAULT_CAMERA_META["darks"]
    assert config.preset_names("cameras") == ["seestar-s30", "seestar-s30-pro"]
    assert config.preset_names("looks") == ["cluster", "galaxy", "nebula"]


def test_precedence_flag_config_look_camera_builtin(tmp_path):
    look = tmp_path / "mylook.toml"
    look.write_text('[stretch]\nasinh = 50\nsat = 0.5\n[color]\npixel = 3.3\n')
    cam = tmp_path / "mycam.toml"
    cam.write_text('[camera]\nname = "x"\n[color]\npixel = 4.4\nfocal = 250\nsensor = "S"\n')
    write(tmp_path, '[target]\ncamera = "mycam.toml"\nlook = "mylook.toml"\n[stretch]\nsat = 0.7\n'
                    '[color]\nsensor = "T"\n')
    s = settings(tmp_path, "stretch", "--shadows", "-3")
    assert s["stretch"] == {"asinh": 50, "shadows": -3.0, "target_bg": 0.10, "sat": 0.7}
    assert s.sources[("stretch", "asinh")] == "look preset mylook"
    assert s.sources[("stretch", "shadows")] == "flag"
    assert s.sources[("stretch", "sat")] == "darkroom.toml"
    assert s.sources[("stretch", "target_bg")] == "built-in"
    assert s["color"]["pixel"] == 3.3            # look beats camera
    assert s["color"]["focal"] == 250            # camera beats built-in
    assert s["color"]["sensor"] == "T"           # config beats camera
    s = settings(tmp_path, "stretch", "--sat", "0.9")
    assert s["stretch"]["sat"] == 0.9            # flag beats config


def test_flag_defaults_do_not_override_the_config(tmp_path):
    write(tmp_path, '[finish]\ngamma = 2.0\nneutral = false\ncrop = "2:3"\n[denoise]\ncpu = true\n'
                    '[color]\ncatalog_fallback = false\n')
    s = settings(tmp_path, "finish")
    assert s["finish"]["gamma"] == 2.0 and s["finish"]["neutral"] is False and s["finish"]["crop"] == "2:3"
    assert settings(tmp_path, "denoise")["denoise"]["cpu"] is True
    assert settings(tmp_path, "color")["color"]["catalog_fallback"] is False
    s = settings(tmp_path, "finish", "--gamma", "1.6", "--crop", "1,2,3,4")
    assert s["finish"]["gamma"] == 1.6 and s["finish"]["crop"] == "1,2,3,4"
    assert settings(tmp_path, "finish", "--crop", "none")["finish"]["crop"] == "none"
    p = cli.stages.finish_params(settings(tmp_path, "finish")["finish"])
    assert p.crop is None and p.crop_aspect == (2, 3)


def test_config_flag_points_elsewhere(tmp_path):
    other = tmp_path / "elsewhere" / "x.toml"
    other.parent.mkdir()
    other.write_text('[bge]\nsmoothing = 0.5\n[target]\nsubs = "lights"\n')
    s = settings(tmp_path, "bge", "--config", str(other))
    assert s["bge"]["smoothing"] == 0.5
    assert s["target"]["subs"] == str(other.parent / "lights")      # relative to the config file
    with pytest.raises(config.ConfigError, match="not found"):
        settings(tmp_path, "bge", "--config", str(tmp_path / "missing.toml"))


@pytest.mark.parametrize("text,match", [
    ('[stack]\nframing = "wide"\n', "framing"),
    ('[stack]\nkeep_fwhm = 0\n', "keep_fwhm"),
    ('[stack]\nkeep_fwhm = "90"\n', "number"),
    ('[finish]\ncrop = "2x3"\n', "crop"),
    ('[finish]\ncrop_center = "5"\n', "centre"),
    ('[finish]\ngama = 1\n', "unknown key"),
    ('[finnish]\ngamma = 1\n', "unknown section"),
    ('[finish]\nneutral = 1\n', "bool"),
    ('[target]\ncamera = "nikon-z"\n', "unknown camera preset"),
    ('[stack\n', "not valid TOML"),
])
def test_config_errors_are_clear(tmp_path, text, match):
    write(tmp_path, text)
    with pytest.raises(config.ConfigError, match=match):
        config.resolve(tmp_path)


def test_presets_cannot_set_target(tmp_path):
    (tmp_path / "l.toml").write_text('[target]\nsubs = "/x"\n')
    write(tmp_path, '[target]\nlook = "l.toml"\n')
    with pytest.raises(config.ConfigError, match="not allowed"):
        config.resolve(tmp_path)


def test_dark_values(tmp_path):
    write(tmp_path, '[target]\ndark = "AUTO"\n')
    assert config.resolve(tmp_path)["target"]["dark"] == "auto"
    write(tmp_path, '[target]\ndark = "masters/d.fit"\n')
    assert config.resolve(tmp_path)["target"]["dark"] == str(tmp_path / "masters" / "d.fit")


def test_dark_flags(tmp_path):
    s = settings(tmp_path, "stack", "--no-dark")
    assert s["target"]["dark"] == "none"
    s = settings(tmp_path, "stack", "--dark-library", "/lib")
    assert (s["target"]["dark"], s["target"]["dark_library"]) == ("auto", "/lib")
    write(tmp_path, '[target]\ndark = "/a/dark.fit"\n')
    assert settings(tmp_path, "stack")["target"]["dark"] == "/a/dark.fit"
    assert settings(tmp_path, "stack", "--dark-library", "/lib")["target"]["dark"] == "auto"
    with pytest.raises(SystemExit):
        settings(tmp_path, "stack", "--no-dark", "--dark", "x.fit")


def test_toml_value_round_trip():
    for v in ['plain', 'with "quotes" and \\ backslash', "M 31", True, False, 3, 2.9, 160.0, -2.5,
              "tab\there", "/path/with spaces/x"]:
        back = tomllib.loads(f"k = {config.toml_value(v)}")["k"]
        assert back == v and type(back) is (int if isinstance(v, float) and v.is_integer() else type(v))


def test_render_config_is_valid_toml_and_resolves_the_same(tmp_path):
    explicit = {("target", "name"): "M 31", ("target", "subs"): "/data/M 31_sub", ("target", "dark"): "auto",
                ("target", "camera"): "seestar-s30-pro", ("target", "look"): "galaxy",
                ("color", "filter"): "UV/IR Block"}
    eff = config.resolve(tmp_path, None, dict(explicit), use_file=False)
    text = config.render_config(explicit, eff, ["test"])
    data = tomllib.loads(text)
    assert data["target"]["name"] == "M 31" and data["color"] == {"filter": "UV/IR Block"}
    assert set(data) == set(config.SCHEMA)            # every section present, even if all commented
    for section, keys in config.SCHEMA.items():
        for key in keys:
            assert f"{key} = " in text
    write(tmp_path, text)
    s = config.resolve(tmp_path)
    plain = config.resolve(Path("/nonexistent-work"))
    for section in config.SCHEMA:
        if section != "target":
            assert s[section] == plain[section]


# --- Siril scripts rendered from settings

def stack_script(**values):
    base = {"SUBS": "/s", "PROCESS": "/w/process", "DARK": "/d.fit", "STACK": "/w/stack"}
    return siril.render((siril.template_dir() / "stack.ssf").read_text(), base | values)


def commands(text):
    return [l for l in text.splitlines() if l and not l.startswith("#")]


PHASE1_STACK = [
    "requires 1.4.0", "setext fit", "set16bits", 'cd "/s"', 'link light "-out=/w/process"', 'cd "/w/process"',
    'calibrate light "-dark=/d.fit" -cc=dark 3 3 -cfa -equalize_cfa -debayer',
    "register pp_light -2pass -maxstars=1000",
    "seqapplyreg pp_light -filter-wfwhm=90% -filter-round=90% -framing=min",
    'stack r_pp_light rej w 3 3 -norm=addscale -output_norm -weight=wfwhm -32b "-out=/w/stack"',
    "close",
]


def test_default_stack_script_is_the_phase1_script():
    assert commands(stack_script()) == PHASE1_STACK
    assert commands(stack_script(FRAMING="min", KEEP_FWHM="90", KEEP_ROUND="90", MAXIMIZE="")) == PHASE1_STACK


def test_stack_script_framing_and_filters():
    c = commands(stack_script(FRAMING="cog", KEEP_FWHM="80", KEEP_ROUND="95", MAXIMIZE=""))
    assert "seqapplyreg pp_light -filter-wfwhm=80% -filter-round=95% -framing=cog" in c
    assert not any("-maximize" in l for l in c)
    c = commands(stack_script(FRAMING="max", MAXIMIZE="1"))
    assert "seqapplyreg pp_light -filter-wfwhm=90% -filter-round=90% -framing=max" in c
    stack = [l for l in c if l.startswith("stack ")]
    assert len(stack) == 1 and "-maximize" in stack[0] and stack[0].endswith('"-out=/w/stack"')


def test_stack_script_without_dark():
    c = commands(stack_script(DARK=""))
    cal = [l for l in c if l.startswith("calibrate")]
    assert cal == ["calibrate light -cfa -equalize_cfa -debayer"]


def test_restack_script():
    t = (siril.template_dir() / "restack.ssf").read_text()
    c = commands(siril.render(t, {"PROCESS": "/w/process", "STACK": "/w/stack_cog", "FRAMING": "cog",
                                  "KEEP_FWHM": "90", "KEEP_ROUND": "90", "MAXIMIZE": ""}))
    assert c[3:] == ['cd "/w/process"', PHASE1_STACK[8].replace("=min", "=cog"),
                     PHASE1_STACK[9].replace("/w/stack", "/w/stack_cog"), "close"]
    assert not any(l.startswith(("link", "calibrate", "register")) for l in c)


def test_color_script_whiteref():
    t = (siril.template_dir() / "color.ssf").read_text()
    v = {"WORK": "/w", "FOCAL": "160", "PIXEL": "2.9", "CATALOG": "gaia", "SENSOR": "Sony IMX585",
         "FILTER": "UV/IR Block"}
    line = 'spcc "-oscsensor=Sony IMX585" "-oscfilter=UV/IR Block" "-whiteref=Average Spiral Galaxy"'
    assert line in siril.render(t, v).splitlines()
    assert '"-whiteref=Flat Photon Flux"' in siril.render(t, v | {"WHITEREF": "Flat Photon Flux"})


def test_template_conditionals():
    t = "{{if A}}yes {{A}}\n{{ifnot A}}no\nopt={{B=dflt}}\n"
    assert siril.render(t, {"A": "1"}) == "yes 1\nopt=dflt\n"
    assert siril.render(t, {"A": "", "B": "x"}) == "no\nopt=x\n"
    with pytest.raises(KeyError):
        siril.render(t, {"B": "x"})            # A is still required


def test_finish_core_recorded_only_when_on():
    from darkroom import stages
    base = {"gamma": 1.6, "star_strength": 0.95, "mid": 0.45, "sat": 1.2, "yb": 1.8, "sharpen": 0.8,
            "sharpen_radius": 2.0, "black": 0.02, "erode": 0, "neutral": True, "crop": "none",
            "crop_center": None}
    assert "core" not in stages.result_params("finish", base | {"core": 0.0})
    assert stages.result_params("finish", base | {"core": 0.3})["core"] == 0.3


def test_nebula_look_resolves(tmp_path):
    """The nebula look turns the yellow/blue push off, keeps StarNet and brightens the background."""
    write(tmp_path, '[target]\nlook = "nebula"\n')
    s = settings(tmp_path, "finish")
    assert s["finish"]["yb"] == 1.0 and s["finish"]["stars"] == "reduce"
    assert s["finish"]["sat"] == 1.5 and s.sources[("finish", "sat")] == "look preset nebula"
    assert settings(tmp_path, "stretch")["stretch"]["target_bg"] == 0.15
