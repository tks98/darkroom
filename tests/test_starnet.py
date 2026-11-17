"""StarNet backends (StarNet2 2.6 / v2.0.2), the starnet stage, recorded parameters, the StarNet
folder lookup, and staging read-only subs for Siril. Stub scripts stand in for StarNet."""
import json
import os
import re
import sys
from pathlib import Path

import numpy as np
import pytest
import tifffile

from darkroom import cli, config, finish as fin, stages, starnet, tools

from test_run import fake, names, run  # noqa: F401  (fake is a fixture)

REAL_STARNET_DIR = tools.starnet_dir

V2_NAME = "starnet2_linux_2.6.2-0242_ORT_x64_cli"


def stub(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def legacy_folder(root: Path) -> Path:
    """StarNet v2.0.2 stand-in: `./starnet++ IN OUT` copies IN to OUT and logs cwd + args."""
    d = root / "StarNetv2CLI"
    stub(d / "starnet++", '#!/bin/sh\necho "$PWD|$*" >> "$PWD/calls.txt"\ncp "$1" "$2"\n')
    (d / "starnet2_weights.pb").write_bytes(b"w")
    return d


def starnet2_folder(root: Path, name: str = V2_NAME, lzw: bool = True) -> Path:
    """StarNet2 stand-in: `./starnet2 --input IN --output OUT -q` rewrites IN as an LZW TIFF."""
    d = root / name
    code = f"""#!{sys.executable}
import os, sys
import tifffile
a = sys.argv[1:]
with open(os.path.join(os.getcwd(), "calls.txt"), "a") as f:
    f.write(os.getcwd() + "|" + " ".join(a) + "\\n")
src, dst = a[a.index("--input") + 1], a[a.index("--output") + 1]
tifffile.imwrite(dst, tifffile.imread(src), photometric="rgb"{", compression='lzw'" if lzw else ""})
"""
    stub(d / "starnet2", code)
    (d / "StarNet2_weights.onnx").write_bytes(b"w")
    return d


def calls(folder: Path) -> list[tuple[str, str]]:
    return [tuple(l.split("|", 1)) for l in (folder / "calls.txt").read_text().splitlines()]


def stretched(work: Path, h=40, w=30) -> np.ndarray:
    work.mkdir(parents=True, exist_ok=True)
    img = (np.random.default_rng(0).random((h, w, 3)) * 65535).astype(np.uint16)
    tifffile.imwrite(work / stages.STRETCHED_TIF, img, photometric="rgb")
    return img


# ---- detection and command lines

def test_detect_kinds(tmp_path):
    assert starnet.detect(tmp_path) is None
    old = starnet.detect(legacy_folder(tmp_path))
    assert old.kind == "starnet++" and old.params() == {}
    new = starnet.detect(starnet2_folder(tmp_path))
    assert (new.kind, new.version, new.build) == ("starnet2", "2.6.2", "0242")
    assert new.params() == {"product": "starnet2", "version": "2.6.2", "build": "0242"}
    both = legacy_folder(tmp_path / "both")
    stub(both / "starnet2", "#!/bin/sh\n")
    assert starnet.detect(both).kind == "starnet2"                       # starnet2 wins
    assert starnet.detect(both).params() == {"product": "starnet2", "version": "unknown"}


def test_detect_prefers_accepted_json_version(tmp_path):
    d = starnet2_folder(tmp_path, name="renamed")
    (d / "accepted.json").write_text(json.dumps({"version": "2.6.3", "build": "0250"}))
    assert starnet.detect(d).params() == {"product": "starnet2", "version": "2.6.3", "build": "0250"}


def test_command_lines(tmp_path):
    src, dst = Path("/w/stretched.tif"), Path("/w/starless.tif")
    assert starnet.Backend(tmp_path, "starnet++").command(src, dst) == ["./starnet++", "/w/stretched.tif",
                                                                       "/w/starless.tif"]
    assert starnet.Backend(tmp_path, "starnet2", "2.6.2").command(src, dst) == [
        "./starnet2", "--input", "/w/stretched.tif", "--output", "/w/starless.tif", "-q"]


# ---- the stage

def test_stage_with_legacy_starnet(tmp_path):
    work, d = tmp_path / "work", legacy_folder(tmp_path)
    img = stretched(work)
    stages.starnet(work, str(d))
    (cwd, args), = calls(d)
    assert cwd == str(d.resolve())
    assert args == f"{work.resolve() / 'stretched.tif'} {work.resolve() / 'starless.tif'}"
    np.testing.assert_array_equal(tifffile.imread(work / "starless.tif"), img)


def test_stage_with_starnet2_keeps_lzw_and_finish_reads_it(tmp_path):
    work, d = tmp_path / "work", starnet2_folder(tmp_path)
    img = stretched(work)
    stages.starnet(work, str(d))
    (cwd, args), = calls(d)
    assert cwd == str(d.resolve())
    assert args == (f"--input {work.resolve() / 'stretched.tif'} --output "
                    f"{work.resolve() / 'starless.starnet2-tmp.tif'} -q")
    out = work / "starless.tif"
    with tifffile.TiffFile(out) as t:
        assert t.pages[0].compression == tifffile.COMPRESSION.LZW
    assert not (work / "starless.starnet2-tmp.tif").exists()
    np.testing.assert_allclose(fin.load(out), img / 65535.0, atol=1e-6)     # imagecodecs decodes LZW
    assert "StarNet2 2.6.2 (build 0242)" in (work / "logs" / "starnet.log").read_text()


def test_starnet2_failure_keeps_no_stale_output(tmp_path):
    work = tmp_path / "work"
    stretched(work)
    d = tmp_path / V2_NAME
    stub(d / "starnet2", "#!/bin/sh\nexit 3\n")
    (work / "starless.tif").write_bytes(b"old")
    with pytest.raises(tools.StageError, match="code 3"):
        stages.starnet(work, str(d))
    assert not (work / "starless.starnet2-tmp.tif").exists()
    assert (work / "starless.tif").read_bytes() == b"old"                # not replaced by a failed run


def test_starnet2_output_with_wrong_shape_fails(tmp_path):
    work = tmp_path / "work"
    stretched(work)
    d = tmp_path / V2_NAME
    stub(d / "starnet2", f"""#!{sys.executable}
import sys, numpy, tifffile
a = sys.argv
tifffile.imwrite(a[a.index("--output") + 1], numpy.zeros((5, 5, 3), numpy.uint16), photometric="rgb")
""")
    with pytest.raises(tools.StageError, match="shape"):
        stages.starnet(work, str(d))
    assert not (work / "starless.tif").exists()


def test_finish_reads_lzw_tiff(tmp_path):
    img = (np.random.default_rng(1).random((20, 10, 3)) * 65535).astype(np.uint16)
    tifffile.imwrite(tmp_path / "s.tif", img, photometric="rgb", compression="lzw")
    np.testing.assert_allclose(fin.load(tmp_path / "s.tif"), img / 65535.0, atol=1e-6)


# ---- recorded parameters

def test_recorded_params_per_backend(tmp_path):
    work = tmp_path / "work"
    stretched(work)
    old, new = legacy_folder(tmp_path), starnet2_folder(tmp_path)
    assert cli.main(["starnet", "--work", str(work), "--starnet-dir", str(old)]) == 0
    assert stages.recorded_params(work, "starnet") == {}                 # as before StarNet2 support
    s = config.resolve(work, None, {("starnet", "dir"): str(old)})
    assert stages.up_to_date(work, "starnet", cli._params("starnet", s)) == (True, "")
    s2 = config.resolve(work, None, {("starnet", "dir"): str(new)})
    ok, why = stages.up_to_date(work, "starnet", cli._params("starnet", s2))
    assert not ok and why == "changed: build, product, version"           # switching re-runs starnet
    assert cli.main(["starnet", "--work", str(work), "--starnet-dir", str(new)]) == 0
    assert stages.recorded_params(work, "starnet") == {"product": "starnet2", "version": "2.6.2", "build": "0242"}
    ok, why = stages.up_to_date(work, "finish", cli._params("finish", s2))
    assert not ok                                                         # finish follows (no record / newer input)


def test_run_reruns_starnet_and_finish_when_starnet_changes(fake, monkeypatch, tmp_path):
    work, ran, _ = fake
    monkeypatch.setattr(tools, "starnet_dir", REAL_STARNET_DIR)     # the fixture fakes it; use the real lookup
    old, new = legacy_folder(tmp_path / "sn"), starnet2_folder(tmp_path / "sn")
    assert run(work, "--starnet-dir", str(old)) == 0
    names(ran)
    assert run(work, "--starnet-dir", str(old)) == 0 and names(ran) == []
    assert run(work, "--starnet-dir", str(new)) == 0 and names(ran) == ["starnet", "finish"]
    assert run(work, "--starnet-dir", str(new)) == 0 and names(ran) == []
    assert run(work, "--starnet-dir", str(old)) == 0 and names(ran) == ["starnet", "finish"]


# ---- where StarNet is looked for

def install(root: Path, name: str, version: str, build: str) -> Path:
    d = starnet2_folder(root, name=name)
    (d / "accepted.json").write_text(json.dumps({"product": "StarNet2", "version": version, "build": build}))
    return d


def test_starnet_folder_precedence(tmp_path, monkeypatch):
    cache_root = starnet.installs_root()
    with pytest.raises(tools.StageError) as e:
        tools.starnet_dir(None)
    for words in tools.STARNET_LOOKUP:
        assert words in str(e.value)
    assert "darkroom get-starnet" in str(e.value)

    install(cache_root, "starnet2_linux_2.6.1-0230_ORT_x64_cli", "2.6.1", "0230")
    newest = install(cache_root, "starnet2_linux_2.6.10-0300_ORT_x64_cli", "2.6.10", "0300")
    starnet2_folder(cache_root, name="starnet2_linux_9.0.0-0999_ORT_x64_cli")     # no accepted.json: ignored
    (cache_root / ".incoming-x").mkdir()
    assert tools.starnet_dir(None) == newest                                       # 4. get-starnet install

    env_dir = legacy_folder(tmp_path / "env")
    monkeypatch.setenv("DARKROOM_STARNET_DIR", str(env_dir))
    assert tools.starnet_dir(None) == env_dir.resolve()                           # 3. env beats the cache

    work = tmp_path / "work"
    work.mkdir()
    cfg_dir = legacy_folder(tmp_path / "cfg")
    (work / "darkroom.toml").write_text(f'[starnet]\ndir = "{cfg_dir}"\n')
    s = config.resolve(work)
    assert tools.starnet_dir(cli._starnet_dir(s)) == cfg_dir.resolve()             # 2. config beats env

    flag_dir = starnet2_folder(tmp_path / "flag")
    a = cli.build_parser().parse_args(["starnet", "--work", str(work), "--starnet-dir", str(flag_dir)])
    assert tools.starnet_dir(cli._starnet_dir(cli._settings(a))) == flag_dir.resolve()   # 1. flag beats all

    with pytest.raises(tools.StageError, match=r"neither starnet2 nor starnet\+\+ found .*DARKROOM_STARNET_DIR"):
        monkeypatch.setenv("DARKROOM_STARNET_DIR", str(tmp_path))
        tools.starnet_dir(None)


# ---- read-only subs are staged for Siril

def _fake_siril(monkeypatch, scripts):
    def run_script(script, log):
        scripts.append(script.read_text())
        log.write_text("log: 2 images have been stacked.\n")
        out = re.findall(r'"-out=([^"]+)"', scripts[-1])[-1]
        open(out + ".fit", "wb").write(b"s")
    monkeypatch.setattr(stages.siril, "run_script", run_script)


def _subs(tmp_path) -> Path:
    subs = tmp_path / "subs"
    subs.mkdir()
    for n in ("Light_1.fit", "Light_2.fit"):
        (subs / n).write_bytes(n.encode())
    (subs / "Light_1.jpg").write_bytes(b"j")
    return subs


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can write to a 555 folder")
def test_read_only_subs_are_staged(tmp_path, monkeypatch):
    scripts = []
    _fake_siril(monkeypatch, scripts)
    subs, work = _subs(tmp_path), tmp_path / "work"
    subs.chmod(0o555)
    try:
        stages.stack(work, subs, None)
    finally:
        subs.chmod(0o755)
    staged = work.resolve() / "process" / "subs"
    assert f'cd "{staged}"' in scripts[-1] and f'cd "{subs.resolve()}"' not in scripts[-1]
    assert sorted(p.name for p in staged.iterdir()) == ["Light_1.fit", "Light_2.fit"]
    assert all(p.is_symlink() and p.resolve() == (subs / p.name).resolve() for p in staged.iterdir())
    assert (staged / "Light_2.fit").read_bytes() == b"Light_2.fit"
    stages.remove_process(work)
    assert not staged.exists() and (subs / "Light_1.fit").exists()        # only the links are removed


def test_writable_subs_are_used_directly(tmp_path, monkeypatch):
    scripts = []
    _fake_siril(monkeypatch, scripts)
    subs, work = _subs(tmp_path), tmp_path / "work"
    stages.stack(work, subs, None)
    assert f'cd "{subs.resolve()}"' in scripts[-1]
    assert not (work / "process" / "subs").exists()
