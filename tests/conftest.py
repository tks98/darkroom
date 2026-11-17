"""Shared fixtures: keep every test away from the real darkroom cache (and any StarNet in it)."""
import pytest


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("DARKROOM_CACHE", str(tmp_path_factory.mktemp("cache")))
    monkeypatch.delenv("DARKROOM_STARNET_DIR", raising=False)
