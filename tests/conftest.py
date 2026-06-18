"""Pytest fixtures for the SF Config Compare test suite.

Provides reusable fixtures so tests don't duplicate DB setup, instance creation,
or HTTP mocking boilerplate.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# ── Patch DB_PATH BEFORE any module that imports it ────────────────────────
# core.db does `from config import DB_PATH` at import time. We must patch
# config.DB_PATH before anything else imports core.db, then also patch
# the already-imported module-level reference in core.db.
TEST_DB = Path(tempfile.mkdtemp()) / "test.db"

# Ensure project root is on sys.path for imports
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config as _config_mod

_config_mod.DB_PATH = TEST_DB

# Now it's safe to import modules that reference DB_PATH
import core.db as _db_mod

_db_mod.DB_PATH = TEST_DB

from app import app as _flask_app
from core.db import get_conn, init_db, upsert_instance
import pytest


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_db():
    """Reset the temp DB schema before every test."""
    TEST_DB.unlink(missing_ok=True)
    init_db()
    yield


@pytest.fixture
def client():
    """Flask test client with CSRF disabled via TESTING mode."""
    _flask_app.config["TESTING"] = True
    with _flask_app.test_client() as c:
        yield c


@pytest.fixture
def sample_instances():
    """Create two sample instances (DEV and PROD) with metadata entities,
    fields, and picklist values for comparison tests.
    """
    id_a = upsert_instance({
        "alias": "DEV",
        "base_url": "https://dev.example.com",
        "company_id": "DEV001",
        "auth_type": "basic",
        "username": "admin",
        "client_id": None,
        "token_url": None,
    })
    id_b = upsert_instance({
        "alias": "PROD",
        "base_url": "https://prod.example.com",
        "company_id": "PROD001",
        "auth_type": "basic",
        "username": "admin",
        "client_id": None,
        "token_url": None,
    })
    with get_conn() as conn:
        for inst_id, suffix in [(id_a, "_A"), (id_b, "_B")]:
            conn.execute(
                "INSERT INTO metadata_entities "
                "(instance_id, entity_name, entity_label, element_name, pull_timestamp) "
                "VALUES (?, ?, ?, ?, datetime('now'))",
                (inst_id, f"JobInfo{suffix}", f"Job Info{suffix}", f"JobInfo{suffix}"),
            )
            conn.execute(
                "INSERT INTO metadata_entities "
                "(instance_id, entity_name, entity_label, element_name, pull_timestamp) "
                "VALUES (?, ?, ?, ?, datetime('now'))",
                (inst_id, "SharedEntity", "Shared Entity", "SharedEntity"),
            )
        rows = conn.execute(
            "SELECT id, instance_id FROM metadata_entities WHERE entity_name = ?",
            ("SharedEntity",),
        ).fetchall()
        shared_entity_ids = {r["instance_id"]: r["id"] for r in rows}
        for inst_id, val in [(id_a, "A"), (id_b, "B")]:
            eid = shared_entity_ids[inst_id]
            conn.execute(
                "INSERT INTO metadata_fields "
                "(entity_id, field_id, field_label, field_type, required, visibility,"
                " max_length, picklist_id, is_custom, raw_attributes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (eid, "field1", "Field 1", "Edm.String", "false", "true", "255", "", 0, None),
            )
            conn.execute(
                "INSERT INTO metadata_fields "
                "(entity_id, field_id, field_label, field_type, required, visibility,"
                " max_length, picklist_id, is_custom, raw_attributes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (eid, f"field_{val}", f"Field {val}", "Edm.String", "false", "true", "255", "", 0, None),
            )
        for inst_id, code, label in [(id_a, "PL1", "Label A"), (id_b, "PL1", "Label B")]:
            conn.execute(
                "INSERT INTO picklist_values "
                "(instance_id, picklist_id, option_id, external_code, parent_picklist_id,"
                " status, label_en, all_labels, pull_timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                (inst_id, "status", "OPT1", code, None, "ACTIVE", label, json.dumps({"en_US": label})),
            )
    return id_a, id_b


@pytest.fixture
def mock_auth_password():
    """Patch get_password in all modules that import it directly."""
    with patch("core.auth.get_password", return_value="dummy_password_123"):
        with patch("core.odata_metadata_pull.get_password", return_value="dummy_password_123"):
            with patch("core.picklist_pull.get_password", return_value="dummy_password_123"):
                yield


@pytest.fixture
def mock_oauth_secret():
    """Patch get_client_secret and fetch_oauth_token in all relevant modules."""
    with patch("core.auth.get_client_secret", return_value="dummy_secret_456"):
        with patch("core.odata_metadata_pull.get_client_secret", return_value="dummy_secret_456"):
            with patch("core.odata_metadata_pull.fetch_oauth_token", return_value="mock_token_abc"):
                with patch("core.picklist_pull.get_client_secret", return_value="dummy_secret_456"):
                    with patch("core.picklist_pull.fetch_oauth_token", return_value="mock_token_abc"):
                        yield
