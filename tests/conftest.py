"""Shared fixtures. Every network call goes to an in-process fake."""

import importlib.util
from pathlib import Path
from types import MappingProxyType

import pytest

from hal.leaderboard import Leaderboard
from hal.ratelimit import RateLimiter
from hal.storage import LocalStore
from tests.fakes import FakeOpenRouter

ROOT = Path(__file__).parents[1]


@pytest.fixture()
def store(tmp_path):
    """Return an empty local store."""
    return LocalStore(tmp_path / "experiments")


# The two models the tests run as the small newest set: one can fail while the other works.
TWO_NEWEST = ("openai/gpt-6-astra", "anthropic/claude-fable-5.1")


@pytest.fixture()
def two_newest(monkeypatch):
    """Make two pinned models the newest set, so a test can mix a failing and a working model.

    ``model_set`` reads ``hal.catalog.MODELS`` at call time, so every module sees the
    patched sets; the pinned entries themselves (``MODELS_BY_ID``) are unchanged.
    """
    import hal.catalog as catalog

    def generation(model):
        if model["id"] in TWO_NEWEST:
            return "newest"
        return "current" if model["generation"] == "newest" else model["generation"]

    models = tuple(MappingProxyType({**m, "generation": generation(m)}) for m in catalog.MODELS)
    monkeypatch.setattr(catalog, "MODELS", models)


@pytest.fixture()
def fake():
    """Return a fresh fake OpenRouter."""
    return FakeOpenRouter()


@pytest.fixture()
def app_module(tmp_path, monkeypatch, fake):
    """Import the hyphenated Flask entry point wired to fakes and a temp store."""
    monkeypatch.setenv("EXPERIMENTS_DIR", str(tmp_path / "boot"))
    spec = importlib.util.spec_from_file_location("flask_app", ROOT / "flask-app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.STORE = LocalStore(tmp_path / "experiments")
    module.LEADERBOARD = Leaderboard(module.STORE, ttl=0)
    module.HTTP_TRANSPORT = fake.transport()
    module.RUN_LIMITER = RateLimiter(100, 3600)
    module.FLAG_LIMITER = RateLimiter(100, 3600)
    module.FAKE = fake
    return module


@pytest.fixture()
def client(app_module):
    """Return a Flask test client."""
    return app_module.app.test_client()
