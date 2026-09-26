"""Shared fixtures. Every network call goes to an in-process fake."""

import importlib.util
from pathlib import Path

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
