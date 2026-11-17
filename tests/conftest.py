"""Shared fixtures: keep every test away from the real darkroom cache (and any StarNet in it) and
from the real tools folder of `darkroom setup`."""
import pytest


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("DARKROOM_CACHE", str(tmp_path_factory.mktemp("cache")))
    monkeypatch.delenv("DARKROOM_STARNET_DIR", raising=False)
    monkeypatch.setenv("DARKROOM_TOOLS_DIR", str(tmp_path_factory.mktemp("tools")))
