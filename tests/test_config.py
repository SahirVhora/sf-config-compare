"""Config smoke tests — no SF connectivity required."""

import importlib
import sys
from pathlib import Path

import dotenv
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Avoid leaking env or developer .env between tests."""
    for key in ("SECRET_KEY", "FLASK_SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)


def _reload_config():
    """Reload config module after env changes (without reading .env)."""
    if "config" in sys.modules:
        del sys.modules["config"]
    sys.path.insert(0, str(ROOT))
    try:
        return importlib.import_module("config")
    finally:
        if str(ROOT) in sys.path:
            sys.path.remove(str(ROOT))


def test_secret_key_from_flask_secret_key():
    import os

    os.environ["FLASK_SECRET_KEY"] = "test-flask-secret"
    mod = _reload_config()
    assert mod.SECRET_KEY == "test-flask-secret"


def test_secret_key_from_secret_key():
    import os

    os.environ["SECRET_KEY"] = "test-secret-key"
    mod = _reload_config()
    assert mod.SECRET_KEY == "test-secret-key"


def test_secret_key_prefers_secret_key_over_flask():
    import os

    os.environ["SECRET_KEY"] = "primary"
    os.environ["FLASK_SECRET_KEY"] = "secondary"
    mod = _reload_config()
    assert mod.SECRET_KEY == "primary"


def test_runtime_paths_present():
    import os

    os.environ["FLASK_SECRET_KEY"] = "x" * 32
    mod = _reload_config()
    assert mod.DB_PATH.name == "vault.db"
    assert mod.REPORTS_DIR.name == "reports"
    assert mod.LOGS_DIR.name == "logs"


def test_basic_auth_username_adds_company_id():
    sys.path.insert(0, str(ROOT))
    try:
        from core.auth import format_basic_username
    finally:
        if str(ROOT) in sys.path:
            sys.path.remove(str(ROOT))

    assert format_basic_username("api.user", "ACME") == "api.user@ACME"
    assert format_basic_username("api.user@ACME", "ACME") == "api.user@ACME"
