"""Comprehensive test suite for SF Config Compare Phases 1-5.

Run with: pytest tests/test_phases.py -v
"""

import json
import os
from unittest.mock import patch

import pytest
from werkzeug.exceptions import Forbidden

from app import app as flask_app
from app import check_csrf
from core import ai_analyzer
from core.comparator import compare_instances
from core.scheduler import run_drift_check

# ── Phase 1: Security + API + Caching ────────────────────────────────────


class TestPhase1SecurityAndAPI:
    def test_csrf_token_present(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_api_health(self, client):
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "healthy"
        assert data["database"] == "up"

    def test_api_list_instances_empty(self, client):
        resp = client.get("/api/v1/instances")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["instances"] == []

    def test_api_blueprint_is_csrf_exempt(self):
        prev_testing = flask_app.config.get("TESTING")
        flask_app.config["TESTING"] = False
        try:
            with flask_app.test_request_context("/api/v1/compare", method="POST"):
                assert check_csrf() is None
        finally:
            flask_app.config["TESTING"] = prev_testing

    def test_non_api_post_requires_csrf(self):
        prev_testing = flask_app.config.get("TESTING")
        flask_app.config["TESTING"] = False
        try:
            with flask_app.test_request_context("/", method="POST"):
                with pytest.raises(Forbidden):
                    check_csrf()
        finally:
            flask_app.config["TESTING"] = prev_testing

    def test_api_compare_validation(self, client):
        resp = client.post("/api/v1/compare", json={})
        assert resp.status_code == 422
        data = resp.get_json()
        assert "instance_a_id" in str(data["error"])

    def test_api_compare_same_instance(self, client, sample_instances):
        id_a, _ = sample_instances
        resp = client.post(
            "/api/v1/compare", json={"instance_a_id": id_a, "instance_b_id": id_a}
        )
        assert resp.status_code == 422

    def test_api_compare_success(self, client, sample_instances):
        id_a, id_b = sample_instances
        resp = client.post(
            "/api/v1/compare", json={"instance_a_id": id_a, "instance_b_id": id_b}
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "summary" in data
        assert "entity_diffs" in data
        assert "field_diffs" in data
        assert data["instance_a"] == "DEV"
        assert data["instance_b"] == "PROD"

    def test_api_compare_with_entity_filter(self, client, sample_instances):
        id_a, id_b = sample_instances
        resp = client.post(
            "/api/v1/compare",
            json={
                "instance_a_id": id_a,
                "instance_b_id": id_b,
                "entity_filter": ["SharedEntity"],
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["summary"]["entities_in_both"] == 1

    def test_api_compare_report(self, client, sample_instances):
        id_a, id_b = sample_instances
        resp = client.post(
            "/api/v1/compare/report",
            json={"instance_a_id": id_a, "instance_b_id": id_b},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "reports" in data
        assert "comparison" in data

    def test_report_access_token_guard(self, client):
        resp = client.get("/reports/test/view")
        assert resp.status_code in (400, 403, 404)


# ── Phase 2: Historical Drift Tracking ───────────────────────────────────


class TestPhase2PullHistory:
    def test_pull_history_table_exists(self):
        from core.db import get_conn

        with get_conn() as conn:
            conn.execute("SELECT 1 FROM pull_history LIMIT 1")

    def test_entity_snapshots_table_exists(self):
        from core.db import get_conn

        with get_conn() as conn:
            conn.execute("SELECT 1 FROM entity_snapshots LIMIT 1")

    def test_picklist_snapshots_table_exists(self):
        from core.db import get_conn

        with get_conn() as conn:
            conn.execute("SELECT 1 FROM picklist_snapshots LIMIT 1")

    def test_record_and_retrieve_pull_history(self, sample_instances):
        from core.db import get_pull_history, record_pull_history

        id_a, _ = sample_instances
        hist_id = record_pull_history(
            id_a, "metadata", "success", entities_count=10, fields_count=50
        )
        assert hist_id > 0
        history = get_pull_history(id_a)
        assert len(history) == 1
        assert history[0]["entities_count"] == 10
        assert history[0]["fields_count"] == 50
        assert history[0]["status"] == "success"

    def test_save_and_retrieve_entity_snapshots(self, sample_instances):
        from core.db import get_conn, record_pull_history, save_entity_snapshots

        id_a, _ = sample_instances
        hist_id = record_pull_history(id_a, "metadata", "success")
        entities = [
            {
                "entity_name": "JobInfo",
                "entity_label": "Job Info",
                "element_name": "JobInfo",
                "fields": [{"field_id": "field1", "field_type": "Edm.String"}],
            },
        ]
        save_entity_snapshots(hist_id, entities)
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM entity_snapshots WHERE history_id = ?", (hist_id,)
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["entity_name"] == "JobInfo"

    def test_save_and_retrieve_picklist_snapshots(self, sample_instances):
        from core.db import get_conn, record_pull_history, save_picklist_snapshots

        id_a, _ = sample_instances
        hist_id = record_pull_history(id_a, "picklist", "success")
        values = [
            {
                "picklist_id": "status",
                "external_code": "PL1",
                "option_id": "OPT1",
                "label_en": "Active",
                "status": "ACTIVE",
                "all_labels": '{"en_US": "Active"}',
            },
        ]
        save_picklist_snapshots(hist_id, values)
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM picklist_snapshots WHERE history_id = ?", (hist_id,)
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["picklist_id"] == "status"

    def test_api_pull_history(self, client, sample_instances):
        from core.db import record_pull_history

        id_a, _ = sample_instances
        record_pull_history(id_a, "metadata", "success", entities_count=5)
        resp = client.get(f"/api/v1/instances/{id_a}/history")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["instance_id"] == id_a
        assert len(data["history"]) >= 1

    def test_api_pull_history_detail(self, client, sample_instances):
        from core.db import record_pull_history

        id_a, _ = sample_instances
        hist_id = record_pull_history(id_a, "metadata", "success")
        resp = client.get(f"/api/v1/instances/{id_a}/history/{hist_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["history"]["id"] == hist_id


# ── Phase 3: Selective Entity Comparison ─────────────────────────────────


class TestPhase3EntityFilter:
    def test_compare_all_entities(self, sample_instances):
        id_a, id_b = sample_instances
        result = compare_instances(id_a, id_b)
        assert result["summary"]["entities_in_both"] == 1
        assert result["summary"]["entities_only_in_a"] == 1
        assert result["summary"]["entities_only_in_b"] == 1

    def test_compare_filtered_entities(self, sample_instances):
        id_a, id_b = sample_instances
        result = compare_instances(id_a, id_b, entity_filter={"SharedEntity"})
        assert result["summary"]["entities_in_both"] == 1
        assert result["summary"]["entities_only_in_a"] == 0
        assert result["summary"]["entities_only_in_b"] == 0

    def test_compare_nonexistent_filter(self, sample_instances):
        id_a, id_b = sample_instances
        result = compare_instances(id_a, id_b, entity_filter={"FakeEntity"})
        assert result["summary"]["entities_in_both"] == 0
        assert result["summary"]["entities_only_in_a"] == 0
        assert result["summary"]["entities_only_in_b"] == 0

    def test_compare_with_field_diffs(self, sample_instances):
        id_a, id_b = sample_instances
        result = compare_instances(id_a, id_b, entity_filter={"SharedEntity"})
        assert result["summary"]["fields_only_in_a"] == 1
        assert result["summary"]["fields_only_in_b"] == 1
        assert result["summary"]["entities_in_both"] == 1


# ── Phase 4: Scheduled Drift Checks ──────────────────────────────────────


class TestPhase4ScheduledChecks:
    def test_scheduled_checks_table_exists(self):
        from core.db import get_conn

        with get_conn() as conn:
            conn.execute("SELECT 1 FROM scheduled_checks LIMIT 1")

    def test_create_and_retrieve_scheduled_check(self, sample_instances):
        from core.db import create_scheduled_check, get_scheduled_check

        id_a, id_b = sample_instances
        check_id = create_scheduled_check(
            {
                "name": "Daily Dev vs Prod",
                "instance_a_id": id_a,
                "instance_b_id": id_b,
                "cron_expression": "0 0 * * *",
                "enabled": True,
                "webhook_url": "https://hooks.slack.com/test",
                "webhook_type": "slack",
                "notify_on": "drift_only",
            }
        )
        assert check_id >= 1
        check = get_scheduled_check(check_id)
        assert check["name"] == "Daily Dev vs Prod"
        assert check["enabled"] == 1
        assert check["cron_expression"] == "0 0 * * *"

    def test_list_scheduled_checks(self, sample_instances):
        from core.db import create_scheduled_check, get_scheduled_checks

        id_a, id_b = sample_instances
        create_scheduled_check(
            {
                "name": "Check 1",
                "instance_a_id": id_a,
                "instance_b_id": id_b,
                "cron_expression": "0 0 * * *",
                "enabled": True,
            }
        )
        checks = get_scheduled_checks()
        assert len(checks) >= 1

    def test_update_scheduled_check(self, sample_instances):
        from core.db import (
            create_scheduled_check,
            get_scheduled_check,
            update_scheduled_check,
        )

        id_a, id_b = sample_instances
        check_id = create_scheduled_check(
            {
                "name": "Original",
                "instance_a_id": id_a,
                "instance_b_id": id_b,
                "cron_expression": "0 0 * * *",
                "enabled": True,
            }
        )
        update_scheduled_check(
            check_id,
            {
                "name": "Updated",
                "cron_expression": "0 12 * * *",
                "enabled": False,
                "webhook_url": None,
                "webhook_type": "slack",
                "notify_on": "any_change",
            },
        )
        check = get_scheduled_check(check_id)
        assert check["name"] == "Updated"
        assert check["enabled"] == 0

    def test_delete_scheduled_check(self, sample_instances):
        from core.db import (
            create_scheduled_check,
            delete_scheduled_check,
            get_scheduled_check,
        )

        id_a, id_b = sample_instances
        check_id = create_scheduled_check(
            {
                "name": "ToDelete",
                "instance_a_id": id_a,
                "instance_b_id": id_b,
                "cron_expression": "0 0 * * *",
                "enabled": True,
            }
        )
        delete_scheduled_check(check_id)
        assert get_scheduled_check(check_id) is None

    def test_api_scheduled_checks_crud(self, client, sample_instances):
        id_a, id_b = sample_instances
        resp = client.post(
            "/api/v1/scheduled-checks",
            json={
                "name": "API Check",
                "instance_a_id": id_a,
                "instance_b_id": id_b,
                "cron_expression": "0 0 * * *",
            },
        )
        assert resp.status_code == 201
        check_id = resp.get_json()["id"]

        resp = client.get("/api/v1/scheduled-checks")
        assert resp.status_code == 200
        assert any(c["id"] == check_id for c in resp.get_json()["checks"])

        resp = client.get(f"/api/v1/scheduled-checks/{check_id}")
        assert resp.status_code == 200
        assert resp.get_json()["name"] == "API Check"

        resp = client.put(
            f"/api/v1/scheduled-checks/{check_id}",
            json={
                "name": "Updated API Check",
                "cron_expression": "0 12 * * *",
                "enabled": True,
            },
        )
        assert resp.status_code == 200

        resp = client.delete(f"/api/v1/scheduled-checks/{check_id}")
        assert resp.status_code == 200
        resp = client.get(f"/api/v1/scheduled-checks/{check_id}")
        assert resp.status_code == 404

    def test_record_and_retrieve_drift_result(self, sample_instances):
        from core.db import create_scheduled_check, get_conn, record_drift_result

        id_a, id_b = sample_instances
        check_id = create_scheduled_check(
            {
                "name": "Drift Test",
                "instance_a_id": id_a,
                "instance_b_id": id_b,
                "cron_expression": "0 0 * * *",
                "enabled": True,
            }
        )
        result_id = record_drift_result(
            check_id=check_id,
            status="drift_detected",
            summary_json=json.dumps({"entities_only_in_a": 1}),
            entity_diff_count=1,
            field_diff_count=2,
            picklist_issue_count=0,
            report_id="test_report",
        )
        assert result_id >= 1
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM drift_results WHERE check_id = ?", (check_id,)
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "drift_detected"

    def test_api_drift_results(self, client, sample_instances):
        from core.db import create_scheduled_check, record_drift_result

        id_a, id_b = sample_instances
        check_id = create_scheduled_check(
            {
                "name": "Drift API Test",
                "instance_a_id": id_a,
                "instance_b_id": id_b,
                "cron_expression": "0 0 * * *",
                "enabled": True,
            }
        )
        record_drift_result(check_id, "no_change", "{}")
        resp = client.get(f"/api/v1/scheduled-checks/{check_id}/drift-results")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["check_id"] == check_id
        assert len(data["results"]) == 1

    def test_run_drift_check(self, sample_instances):
        from core.db import create_scheduled_check, get_conn

        id_a, id_b = sample_instances
        check_id = create_scheduled_check(
            {
                "name": "Drift Check",
                "instance_a_id": id_a,
                "instance_b_id": id_b,
                "cron_expression": "0 0 * * *",
                "enabled": True,
            }
        )
        result = run_drift_check(check_id)
        assert result["ok"] is True
        assert result["status"] in ("no_change", "drift_detected")
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM drift_results WHERE check_id = ?", (check_id,)
            ).fetchall()
        assert len(rows) >= 1


# ── Phase 5: AI Analysis ───────────────────────────────────────────────────


class TestPhase5AIAnalyzer:
    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_ai_analyzer_fallback_without_api_key(self, sample_instances):
        id_a, id_b = sample_instances
        result = compare_instances(id_a, id_b)
        summary = ai_analyzer.summarize_comparison("DEV", "PROD", result)
        assert summary["ai_enabled"] is False
        assert "OPENAI_API_KEY" in summary["overview"]

    def test_build_prompt_structure(self, sample_instances):
        id_a, id_b = sample_instances
        result = compare_instances(id_a, id_b)
        prompt = ai_analyzer._build_prompt(
            "DEV",
            "PROD",
            result["summary"],
            result["entity_diffs"],
            result["field_diffs"],
            result["picklist_result"],
        )
        assert "DEV" in prompt
        assert "PROD" in prompt
        assert "risk_score" in prompt
        assert "top_concerns" in prompt

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_ai_summary_api_without_key(self, client, sample_instances):
        id_a, id_b = sample_instances
        resp = client.post(
            "/compare/ai-summary",
            data={
                "instance_a_id": id_a,
                "instance_b_id": id_b,
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ai_enabled"] is False

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_api_ai_summary_endpoint(self, client, sample_instances):
        id_a, id_b = sample_instances
        resp = client.post(
            "/api/v1/compare/ai-summary",
            json={
                "instance_a_id": id_a,
                "instance_b_id": id_b,
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "summary" in data


# ── Integration: Mock OData Server ──────────────────────────────────────


class TestIntegrationODataPull:
    """Integration tests for odata_metadata_pull against a mock OData server."""

    _MOCK_METADATA_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx"
           xmlns="http://schemas.microsoft.com/ado/2008/09/edm"
           xmlns:sap="http://www.successfactors.com/edm/sap">
  <edmx:DataServices>
    <Schema Namespace="SFODataSet">
      <EntityContainer Name="SFODataSet">
        <EntitySet Name="JobInfo" sap:label="Job Information"/>
        <EntitySet Name="CompInfo" sap:label="Compensation Information"/>
      </EntityContainer>
    </Schema>
    <Schema Namespace="SFOData">
      <EntityType Name="JobInfo">
        <Key>
          <PropertyRef Name="code"/>
        </Key>
        <Property Name="code" Type="Edm.String" Nullable="false"
                  sap:label="Code" sap:visible="true" sap:required="true" sap:picklist="jobType"/>
        <Property Name="name" Type="Edm.String" MaxLength="255" Nullable="true"
                  sap:label="Name" sap:visible="true"/>
        <NavigationProperty Name="userNav" sap:label="User Navigation"/>
      </EntityType>
      <EntityType Name="CompInfo">
        <Key>
          <PropertyRef Name="payComponent"/>
        </Key>
        <Property Name="payComponent" Type="Edm.String" Nullable="false"
                  sap:label="Pay Component" sap:visible="true"/>
      </EntityType>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>
"""

    def test_pull_odata_metadata_success(
        self, requests_mock, mock_auth_password, clean_db
    ):
        from core.db import get_conn, upsert_instance
        from core.odata_metadata_pull import pull_odata_metadata

        inst_id = upsert_instance(
            {
                "alias": "INT_DEV",
                "base_url": "https://int-dev.example.com",
                "company_id": "INT001",
                "auth_type": "basic",
                "username": "admin",
                "client_id": None,
                "token_url": None,
            }
        )
        instance = {
            "id": inst_id,
            "alias": "INT_DEV",
            "base_url": "https://int-dev.example.com",
            "company_id": "INT001",
            "auth_type": "basic",
            "username": "admin",
        }

        requests_mock.get(
            "https://int-dev.example.com/odata/v2/$metadata",
            text=self._MOCK_METADATA_XML,
            status_code=200,
        )

        result = pull_odata_metadata(instance)

        assert result["success"] is True
        assert result["entities_count"] == 2
        assert (
            result["fields_count"] == 4
        )  # code, name, userNav (JobInfo) + payComponent (CompInfo)

        with get_conn() as conn:
            entities = conn.execute(
                "SELECT * FROM metadata_entities WHERE instance_id = ?", (inst_id,)
            ).fetchall()
            assert len(entities) == 2
            jobinfo = [e for e in entities if e["entity_name"] == "JobInfo"][0]
            assert jobinfo["entity_label"] == "Job Information"

            fields = conn.execute(
                "SELECT * FROM metadata_fields WHERE entity_id = ?", (jobinfo["id"],)
            ).fetchall()
            assert len(fields) == 3
            field_ids = {f["field_id"] for f in fields}
            assert field_ids == {"code", "name", "userNav"}

    def test_pull_odata_metadata_auth_error(
        self, requests_mock, mock_auth_password, clean_db
    ):
        from core.db import upsert_instance
        from core.odata_metadata_pull import pull_odata_metadata

        inst_id = upsert_instance(
            {
                "alias": "INT_DEV2",
                "base_url": "https://int-dev2.example.com",
                "company_id": "INT002",
                "auth_type": "basic",
                "username": "admin",
                "client_id": None,
                "token_url": None,
            }
        )
        instance = {
            "id": inst_id,
            "alias": "INT_DEV2",
            "base_url": "https://int-dev2.example.com",
            "company_id": "INT002",
            "auth_type": "basic",
            "username": "admin",
        }

        requests_mock.get(
            "https://int-dev2.example.com/odata/v2/$metadata",
            status_code=401,
            reason="Unauthorized",
        )

        result = pull_odata_metadata(instance)
        assert result["success"] is False
        assert "401" in result["error"] or "Unauthorized" in result["error"]

    def test_pull_odata_metadata_oauth(self, requests_mock, clean_db):
        from core.db import upsert_instance
        from core.odata_metadata_pull import pull_odata_metadata

        inst_id = upsert_instance(
            {
                "alias": "INT_OAUTH",
                "base_url": "https://int-oauth.example.com",
                "company_id": "INT003",
                "auth_type": "oauth2",
                "username": None,
                "client_id": "my_client",
                "token_url": "https://int-oauth.example.com/oauth/token",
            }
        )
        instance = {
            "id": inst_id,
            "alias": "INT_OAUTH",
            "base_url": "https://int-oauth.example.com",
            "company_id": "INT003",
            "auth_type": "oauth2",
            "client_id": "my_client",
            "token_url": "https://int-oauth.example.com/oauth/token",
        }

        # Mock OAuth token endpoint
        requests_mock.post(
            "https://int-oauth.example.com/oauth/token",
            json={"access_token": "mock_token_123", "token_type": "Bearer"},
        )
        requests_mock.get(
            "https://int-oauth.example.com/odata/v2/$metadata",
            text=self._MOCK_METADATA_XML,
            status_code=200,
        )

        with patch("core.auth.get_client_secret", return_value="dummy_secret_456"):
            with patch(
                "sapsf_shared.auth.OAuth2Auth.fetch_token",
                return_value="mock_token_123",
            ):
                result = pull_odata_metadata(instance)
        assert result["success"] is True
        assert result["entities_count"] == 2


# ── Integration: Mock Picklist API ────────────────────────────────────────


class TestIntegrationPicklistPull:
    """Integration tests for picklist_pull against a mock Picklist Center API."""

    _MOCK_PICKLIST_JSON = {
        "d": {
            "results": [
                {
                    "__metadata": {
                        "uri": "https://example.com/odata/v2/PickListValueV2(1)"
                    },
                    "PickListV2_id": "status",
                    "optionId": "OPT1",
                    "externalCode": "ACTIVE",
                    "status": "ACTIVE",
                    "label_en_US": "Active",
                    "label_en_GB": "Active",
                    "validFrom": None,
                    "validTo": None,
                    "parentPicklistId": None,
                },
                {
                    "__metadata": {
                        "uri": "https://example.com/odata/v2/PickListValueV2(2)"
                    },
                    "PickListV2_id": "status",
                    "optionId": "OPT2",
                    "externalCode": "INACTIVE",
                    "status": "ACTIVE",
                    "label_en_US": "Inactive",
                    "label_en_GB": "Inactive",
                    "validFrom": None,
                    "validTo": None,
                    "parentPicklistId": None,
                },
                {
                    "__metadata": {
                        "uri": "https://example.com/odata/v2/PickListValueV2(3)"
                    },
                    "PickListV2_id": "country",
                    "optionId": "OPT3",
                    "externalCode": "USA",
                    "status": "ACTIVE",
                    "label_en_US": "United States",
                    "validFrom": None,
                    "validTo": None,
                    "parentPicklistId": None,
                },
            ],
            "__next": None,
        }
    }

    def test_pull_picklist_success(self, requests_mock, mock_auth_password, clean_db):
        from core.db import get_conn, upsert_instance
        from core.picklist_pull import pull_picklist

        inst_id = upsert_instance(
            {
                "alias": "INT_PL",
                "base_url": "https://int-pl.example.com",
                "company_id": "PL001",
                "auth_type": "basic",
                "username": "admin",
                "client_id": None,
                "token_url": None,
            }
        )
        instance = {
            "id": inst_id,
            "alias": "INT_PL",
            "base_url": "https://int-pl.example.com",
            "company_id": "PL001",
            "auth_type": "basic",
            "username": "admin",
        }

        requests_mock.get(
            "https://int-pl.example.com/odata/v2/PickListValueV2",
            json=self._MOCK_PICKLIST_JSON,
            status_code=200,
        )

        result = pull_picklist(instance)

        assert result["success"] is True
        assert result["total_values"] == 3
        assert result["total_picklists"] == 2  # status, country

        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM picklist_values WHERE instance_id = ?", (inst_id,)
            ).fetchall()
            assert len(rows) == 3
            picklist_ids = {r["picklist_id"] for r in rows}
            assert picklist_ids == {"status", "country"}

    def test_pull_picklist_pagination(
        self, requests_mock, mock_auth_password, clean_db
    ):
        from core.db import get_conn, upsert_instance
        from core.picklist_pull import pull_picklist

        inst_id = upsert_instance(
            {
                "alias": "INT_PL2",
                "base_url": "https://int-pl2.example.com",
                "company_id": "PL002",
                "auth_type": "basic",
                "username": "admin",
                "client_id": None,
                "token_url": None,
            }
        )
        instance = {
            "id": inst_id,
            "alias": "INT_PL2",
            "base_url": "https://int-pl2.example.com",
            "company_id": "PL002",
            "auth_type": "basic",
            "username": "admin",
        }

        page1 = {
            "d": {
                "results": [
                    {
                        "PickListV2_id": "status",
                        "optionId": "OPT1",
                        "externalCode": "A",
                        "status": "ACTIVE",
                        "label_en_US": "A",
                        "validFrom": None,
                        "validTo": None,
                    },
                ],
                "__next": "https://int-pl2.example.com/odata/v2/PickListValueV2?$format=json&$top=1000&$skip=1001",
            }
        }
        page2 = {
            "d": {
                "results": [
                    {
                        "PickListV2_id": "status",
                        "optionId": "OPT2",
                        "externalCode": "B",
                        "status": "ACTIVE",
                        "label_en_US": "B",
                        "validFrom": None,
                        "validTo": None,
                    },
                ],
                "__next": None,
            }
        }

        requests_mock.get(
            "https://int-pl2.example.com/odata/v2/PickListValueV2",
            json=page1,
            status_code=200,
        )
        requests_mock.get(
            "https://int-pl2.example.com/odata/v2/PickListValueV2?$format=json&$top=1000&$skip=1001",
            json=page2,
            status_code=200,
        )

        result = pull_picklist(instance)

        assert result["success"] is True
        assert result["total_values"] == 2

        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM picklist_values WHERE instance_id = ?", (inst_id,)
            ).fetchall()
            assert len(rows) == 2
            codes = {r["external_code"] for r in rows}
            assert codes == {"A", "B"}


# ── Cross-phase integration tests ────────────────────────────────────────


class TestIntegration:
    def test_end_to_end_instance_crud(self, client):
        # Add instance
        resp = client.post(
            "/instances/add",
            data={
                "csrf_token": "test",
                "alias": "INT_TEST",
                "base_url": "https://int.example.com",
                "company_id": "INT001",
                "auth_type": "basic",
                "username": "admin",
                "password": "secret123",
            },
        )
        assert resp.status_code in (200, 302)

        resp = client.get("/api/v1/instances")
        assert resp.status_code == 200
        instances = resp.get_json()["instances"]
        assert len(instances) == 1
        assert instances[0]["alias"] == "INT_TEST"

        inst_id = instances[0]["id"]
        resp = client.post(
            f"/instances/{inst_id}/edit",
            data={
                "csrf_token": "test",
                "alias": "INT_TEST_UPDATED",
                "base_url": "https://int.example.com",
                "company_id": "INT001",
                "auth_type": "basic",
                "username": "admin",
            },
        )
        assert resp.status_code in (200, 302)

        from core.db import get_instance

        inst = get_instance(inst_id)
        assert inst["alias"] == "INT_TEST_UPDATED"

        resp = client.post(f"/instances/{inst_id}/delete", data={"csrf_token": "test"})
        assert resp.status_code in (200, 302)
        assert get_instance(inst_id) is None

    def test_database_migrations_all_tables(self):
        from core.db import get_conn

        tables = [
            "instances",
            "metadata_entities",
            "metadata_fields",
            "picklist_values",
            "pull_jobs",
            "pull_history",
            "entity_snapshots",
            "picklist_snapshots",
            "scheduled_checks",
            "drift_results",
        ]
        with get_conn() as conn:
            for table in tables:
                conn.execute(f"SELECT 1 FROM {table} LIMIT 1")

    def test_caching_layer_present(self):
        from app import cache

        assert cache is not None

    def test_rate_limiter_present(self):
        from app import limiter

        assert limiter is not None
