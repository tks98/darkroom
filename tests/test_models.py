"""The pinned GraXpert denoise model check and the cached fixed-batch copy (stand-in bytes only)."""
import hashlib
from pathlib import Path

import pytest

from darkroom import denoise, models, tools


@pytest.fixture
def model(tmp_path, monkeypatch):
    p = tmp_path / "model.onnx"
    p.write_bytes(b"stand-in model")
    monkeypatch.setattr(models, "DENOISE_BYTES", p.stat().st_size)
    monkeypatch.setattr(models, "DENOISE_SHA256", hashlib.sha256(p.read_bytes()).hexdigest())
    monkeypatch.setenv("DARKROOM_GRAXPERT_MODEL", str(p))
    return p


def test_pins_are_the_published_model():
    assert models.DENOISE_BYTES == 477703971 and len(models.DENOISE_SHA256) == 64


def test_verify_denoise_model(model):
    assert models.verify_denoise_model() == model
    model.write_bytes(b"stand-in modeX")                       # same size, other bytes
    models._SHA_CACHE.clear()
    with pytest.raises(tools.StageError, match="not its SHA-256.*darkroom fetch-models"):
        models.verify_denoise_model(model)
    model.write_bytes(b"short")
    with pytest.raises(tools.StageError, match="not the .* of denoise model 3.0.2"):
        models.verify_denoise_model(model)
    model.unlink()
    with pytest.raises(tools.StageError, match="not found"):
        models.verify_denoise_model(model)


def test_fixed_batch_path_matches_denoise(model, monkeypatch):
    def fake_build(cmd, log):
        Path(cmd[-1]).write_bytes(b"fixed")
    monkeypatch.setattr(tools, "run_tool", fake_build)
    assert denoise.fixed_batch_model(model.parent / "log", log=lambda m: None) == models.fixed_batch_path()


def test_cached_fixed_batch_copy_is_rebuilt_when_its_source_changed(model, monkeypatch):
    built = []

    def fake_build(cmd, log):
        built.append(cmd[-2])
        Path(cmd[-1]).write_bytes(b"fixed")
    monkeypatch.setattr(tools, "run_tool", fake_build)
    dst = models.fixed_batch_path()
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(b"built by an older darkroom from who knows what")
    msgs = []
    models.verify_denoise_model(log=msgs.append)                 # no record of its source: dropped
    assert not dst.exists() and any("rebuilding" in m for m in msgs)
    denoise.fixed_batch_model(model.parent / "log", log=lambda m: None)
    assert built == [str(model)] and dst.read_bytes() == b"fixed"
    models.verify_denoise_model()                                # same source: kept
    denoise.fixed_batch_model(model.parent / "log", log=lambda m: None)
    assert len(built) == 1
    model.write_bytes(b"stand-in modeY")                         # a different model, then pinned as the new one
    models._SHA_CACHE.clear()
    monkeypatch.setattr(models, "DENOISE_SHA256", hashlib.sha256(model.read_bytes()).hexdigest())
    models.verify_denoise_model()
    assert not dst.exists()
    denoise.fixed_batch_model(model.parent / "log", log=lambda m: None)
    assert len(built) == 2


def test_hash_is_computed_once_per_file_state(model, monkeypatch):
    opened = []
    real_open = open

    def counting_open(p, *a, **k):
        if Path(p) == model:
            opened.append(p)
        return real_open(p, *a, **k)
    monkeypatch.setattr("builtins.open", counting_open)
    models._SHA_CACHE.clear()
    models.sha256(model)
    models.sha256(model)
    assert len(opened) == 1
