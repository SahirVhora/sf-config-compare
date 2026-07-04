"""REST API blueprint for programmatic access to SF Config Compare.

Provides JSON endpoints for CI/CD integration, automated drift detection,
and third-party tool consumption (e.g., Slack bots, monitoring dashboards).

All endpoints return standard JSON. Errors use RFC 7807-inspired shape:
  { "error": str, "detail": str | None, "status": int }
"""

from __future__ import annotations

import logging

from config import REPORT_ACCESS_TOKEN, REPORTS_DIR
from flask import Blueprint, abort, jsonify, request

from core.comparator import compare_instances, compare_instances_matrix
from core.db import get_all_instances, get_instance
from core.reporter import (
    generate_excel_report,
    generate_html_report,
    generate_matrix_excel_report,
    generate_matrix_html_report,
)

logger = logging.getLogger(__name__)
api = Blueprint("api", __name__, url_prefix="/api/v1")


def _json_err(
    message: str, detail: str | None = None, status: int = 400
) -> tuple[dict, int]:
    return {"error": message, "detail": detail, "status": status}, status


def _check_report_token() -> None:
    if REPORT_ACCESS_TOKEN and request.args.get("token") != REPORT_ACCESS_TOKEN:
        abort(403)


# ── Instances ──────────────────────────────────────────────────────────────


@api.route("/instances", methods=["GET"])
def list_instances():
    """Return all configured instances with last-pull timestamps."""
    instances = get_all_instances()
    for inst in instances:
        inst.pop("password", None)
        inst.pop("client_secret", None)
    return jsonify({"instances": instances})


@api.route("/instances/<int:instance_id>", methods=["GET"])
def get_instance_detail(instance_id: int):
    """Return a single instance by ID."""
    inst = get_instance(instance_id)
    if not inst:
        return _json_err("Instance not found", status=404)
    inst.pop("password", None)
    inst.pop("client_secret", None)
    return jsonify(inst)


@api.route("/instances/<int:instance_id>/test", methods=["POST"])
def api_test_connection(instance_id: int):
    """Lightweight connectivity test — same logic as web test_connection."""
    import requests as _req

    inst = get_instance(instance_id)
    if not inst:
        return _json_err("Instance not found", status=404)

    try:
        metadata_url = f"{inst['base_url']}/odata/v2/$metadata"
        headers = {"Accept": "application/xml"}
        timeout = 15

        if inst.get("auth_type") == "oauth2":
            from core.auth import fetch_oauth_token
            from core.auth import get_client_secret as _gcs

            secret = _gcs(inst["alias"])
            if not secret:
                return _json_err("No client secret stored", status=400)
            token = fetch_oauth_token(
                inst["token_url"], inst["client_id"], secret, inst["company_id"]
            )
            headers["Authorization"] = f"Bearer {token}"
            auth = None
        else:
            from core.auth import format_basic_username
            from core.auth import get_password as _gp

            pwd = _gp(inst["alias"])
            if not pwd:
                return _json_err("No password stored", status=400)
            username = format_basic_username(
                inst.get("username"), inst.get("company_id")
            )
            auth = (username, pwd)

        resp = _req.get(metadata_url, auth=auth, headers=headers, timeout=timeout)
        if resp.status_code == 200:
            return jsonify(
                {
                    "ok": True,
                    "instance_id": instance_id,
                    "alias": inst["alias"],
                    "http_status": resp.status_code,
                    "bytes": len(resp.content),
                }
            )
        return _json_err(
            "Connection test failed",
            detail=f"HTTP {resp.status_code}: {resp.reason}",
            status=400,
        )
    except Exception as exc:
        logger.warning("API connection test failed for %s: %s", inst.get("alias"), exc)
        return _json_err("Connection test failed", detail=str(exc), status=400)


# ── Comparison ─────────────────────────────────────────────────────────────


@api.route("/compare", methods=["POST"])
def api_compare():
    """Run a comparison between two instances and return JSON results.

    Body (JSON):
      {
        "instance_a_id": int,
        "instance_b_id": int,
        "picklist_fields": ["label_en", "status", "locale:en_US"],
        "entity_filter": ["JobInfo", "CompInfo"]  // optional
      }
    """
    body: dict = request.get_json(silent=True) or {}
    id_a = body.get("instance_a_id")
    id_b = body.get("instance_b_id")
    if not isinstance(id_a, int) or not isinstance(id_b, int):
        return _json_err(
            "instance_a_id and instance_b_id are required integers", status=422
        )
    if id_a == id_b:
        return _json_err(
            "instance_a_id and instance_b_id must be different", status=422
        )

    inst_a = get_instance(id_a)
    inst_b = get_instance(id_b)
    if not inst_a or not inst_b:
        return _json_err("One or both instances not found", status=404)

    picklist_fields = set(body.get("picklist_fields", [])) or None
    entity_filter = set(body.get("entity_filter", [])) or None
    result = compare_instances(
        id_a, id_b, picklist_fields=picklist_fields, entity_filter=entity_filter
    )

    return jsonify(
        {
            "instance_a": inst_a["alias"],
            "instance_b": inst_b["alias"],
            "summary": result["summary"],
            "entity_diffs": result["entity_diffs"],
            "field_diffs": result["field_diffs"],
            "picklist_result": result["picklist_result"],
        }
    )


@api.route("/compare/report", methods=["POST"])
def api_compare_report():
    """Run a comparison and return both JSON + generated report URLs.

    Body (JSON): same as /api/v1/compare
    Response includes:
      - comparison JSON
      - report_html_url: path to generated HTML report
      - report_xlsx_url: path to generated Excel report
    """
    body: dict = request.get_json(silent=True) or {}
    id_a = body.get("instance_a_id")
    id_b = body.get("instance_b_id")
    if not isinstance(id_a, int) or not isinstance(id_b, int):
        return _json_err(
            "instance_a_id and instance_b_id are required integers", status=422
        )

    inst_a = get_instance(id_a)
    inst_b = get_instance(id_b)
    if not inst_a or not inst_b:
        return _json_err("One or both instances not found", status=404)

    picklist_fields = set(body.get("picklist_fields", [])) or None
    entity_filter = set(body.get("entity_filter", [])) or None
    result = compare_instances(
        id_a, id_b, picklist_fields=picklist_fields, entity_filter=entity_filter
    )

    excel_path = generate_excel_report(
        inst_a["alias"], inst_b["alias"], result, inst_a, inst_b
    )
    report_id = excel_path.stem
    html_content = generate_html_report(
        inst_a["alias"],
        inst_b["alias"],
        result,
        download_url=f"/api/v1/reports/{report_id}/download",
        nav_urls={},
    )
    html_path = REPORTS_DIR / f"{report_id}.html"
    html_path.write_text(html_content, encoding="utf-8")

    token_q = f"?token={REPORT_ACCESS_TOKEN}" if REPORT_ACCESS_TOKEN else ""
    return jsonify(
        {
            "comparison": {
                "summary": result["summary"],
                "entity_diffs": result["entity_diffs"],
                "field_diffs": result["field_diffs"],
                "picklist_result": result["picklist_result"],
            },
            "reports": {
                "html": f"/api/v1/reports/{report_id}/view{token_q}",
                "xlsx": f"/api/v1/reports/{report_id}/download{token_q}",
            },
        }
    )


# ── Reports ────────────────────────────────────────────────────────────────


@api.route("/reports/<report_id>/view", methods=["GET"])
def api_view_report(report_id: str):
    """View a generated HTML report (same guard as web view_report)."""
    import re as _re

    if not _re.match(r"^[A-Za-z0-9_\-]{3,160}$", report_id):
        return _json_err("Invalid report ID", status=400)
    _check_report_token()
    report_path = REPORTS_DIR / f"{report_id}.html"
    if not report_path.exists():
        return _json_err("Report not found", status=404)
    from flask import Response

    return Response(report_path.read_text(encoding="utf-8"), mimetype="text/html")


@api.route("/reports/<report_id>/download", methods=["GET"])
def api_download_report(report_id: str):
    """Download a generated Excel report."""
    import re as _re

    if not _re.match(r"^[A-Za-z0-9_\-]{3,160}$", report_id):
        return _json_err("Invalid report ID", status=400)
    _check_report_token()
    xlsx = REPORTS_DIR / f"{report_id}.xlsx"
    if not xlsx.exists():
        return _json_err("Report not found", status=404)
    from flask import send_file

    return send_file(str(xlsx), as_attachment=True, download_name=xlsx.name)


# ── Pull History (Phase 2) ─────────────────────────────────────────────────


@api.route("/instances/<int:instance_id>/history", methods=["GET"])
def api_pull_history(instance_id: int):
    """Return pull history for an instance."""
    from core.db import get_pull_history

    pull_type = request.args.get("pull_type")
    limit = request.args.get("limit", 100, type=int)
    history = get_pull_history(instance_id, pull_type, limit)
    return jsonify({"instance_id": instance_id, "history": history})


@api.route("/instances/<int:instance_id>/history/<int:history_id>", methods=["GET"])
def api_pull_history_detail(instance_id: int, history_id: int):
    """Return detailed snapshot for a specific pull history record."""
    from core.db import (
        get_entity_snapshots,
        get_picklist_snapshots,
        get_pull_history_by_id,
    )

    hist = get_pull_history_by_id(history_id)
    if not hist or hist["instance_id"] != instance_id:
        return _json_err("History record not found", status=404)
    entities = get_entity_snapshots(history_id)
    picklists = get_picklist_snapshots(history_id)
    return jsonify(
        {
            "history": hist,
            "entity_snapshots": entities,
            "picklist_snapshots": picklists,
        }
    )


# ── Scheduled Checks (Phase 4) ─────────────────────────────────────────────


@api.route("/scheduled-checks", methods=["GET", "POST"])
def api_scheduled_checks():
    from core.db import create_scheduled_check, get_scheduled_checks

    if request.method == "GET":
        checks = get_scheduled_checks()
        return jsonify({"checks": checks})

    body = request.get_json(silent=True) or {}
    required = ["name", "instance_a_id", "instance_b_id"]
    for field in required:
        if field not in body:
            return _json_err(f"{field} is required", status=422)
    check_id = create_scheduled_check(body)
    return jsonify({"id": check_id, "created": True}), 201


@api.route("/scheduled-checks/<int:check_id>", methods=["GET", "PUT", "DELETE"])
def api_scheduled_check_detail(check_id: int):
    from core.db import (
        delete_scheduled_check,
        get_scheduled_check,
        update_scheduled_check,
    )

    if request.method == "GET":
        check = get_scheduled_check(check_id)
        if not check:
            return _json_err("Check not found", status=404)
        return jsonify(check)
    if request.method == "PUT":
        body = request.get_json(silent=True) or {}
        update_scheduled_check(check_id, body)
        return jsonify({"updated": True})
    if request.method == "DELETE":
        delete_scheduled_check(check_id)
        return jsonify({"deleted": True})


@api.route("/scheduled-checks/<int:check_id>/drift-results", methods=["GET"])
def api_drift_results(check_id: int):
    from core.db import get_drift_results

    limit = request.args.get("limit", 50, type=int)
    results = get_drift_results(check_id, limit)
    return jsonify({"check_id": check_id, "results": results})


@api.route("/scheduled-checks/<int:check_id>/run", methods=["POST"])
def api_run_scheduled_check(check_id: int):
    """Manually trigger a scheduled drift check."""
    from core.scheduler import run_drift_check

    result = run_drift_check(check_id)
    return jsonify(result)


# ── AI Analysis (Phase 5) ─────────────────────────────────────────────────


@api.route("/compare/ai-summary", methods=["POST"])
def api_ai_summary():
    """Generate an AI-powered summary of a comparison result.

    Body (JSON):
      {
        "instance_a_id": int,
        "instance_b_id": int,
        "picklist_fields": [...],
        "entity_filter": [...]  // optional
      }
    """
    body: dict = request.get_json(silent=True) or {}
    id_a = body.get("instance_a_id")
    id_b = body.get("instance_b_id")
    if not isinstance(id_a, int) or not isinstance(id_b, int):
        return _json_err(
            "instance_a_id and instance_b_id are required integers", status=422
        )

    inst_a = get_instance(id_a)
    inst_b = get_instance(id_b)
    if not inst_a or not inst_b:
        return _json_err("One or both instances not found", status=404)

    picklist_fields = set(body.get("picklist_fields", [])) or None
    entity_filter = set(body.get("entity_filter", [])) or None
    result = compare_instances(
        id_a, id_b, picklist_fields=picklist_fields, entity_filter=entity_filter
    )

    try:
        from core.ai_analyzer import summarize_comparison

        summary = summarize_comparison(inst_a["alias"], inst_b["alias"], result)
        return jsonify(
            {
                "summary": summary,
                "comparison": {
                    "summary": result["summary"],
                    "entity_diffs_count": len(result["entity_diffs"]),
                    "field_diffs_count": len(result["field_diffs"]),
                },
            }
        )
    except Exception as exc:
        logger.exception("AI summary failed")
        return _json_err("AI summary generation failed", detail=str(exc), status=500)


# ── Matrix comparison ───────────────────────────────────────────────────────


@api.route("/matrix", methods=["POST"])
def api_matrix():
    """Run an N-tenant matrix comparison and return JSON + report URLs.

    Body (JSON):
      {
        "instance_ids": [int, int, ...],      // min 2 required
        "picklist_fields": ["label_en"],       // optional
        "entity_filter": ["JobInfo"]           // optional
      }
    """
    body: dict = request.get_json(silent=True) or {}
    instance_ids = body.get("instance_ids", [])
    if not isinstance(instance_ids, list) or len(instance_ids) < 2:
        return _json_err(
            "instance_ids must be a list of at least 2 integers", status=422
        )
    try:
        instance_ids = [int(i) for i in instance_ids]
    except (TypeError, ValueError):
        return _json_err("instance_ids must all be integers", status=422)

    selected_instances = [get_instance(iid) for iid in instance_ids]
    if any(inst is None for inst in selected_instances):
        return _json_err("One or more instance IDs not found", status=404)

    picklist_fields = set(body.get("picklist_fields", [])) or None
    entity_filter = set(body.get("entity_filter", [])) or None

    result = compare_instances_matrix(
        instance_ids, picklist_fields=picklist_fields, entity_filter=entity_filter
    )

    include_reports = body.get("include_reports", False)
    response: dict = {
        "instances": result["instances"],
        "summary": result["summary"],
        "entity_matrix": result["entity_matrix"],
        "field_matrix": {
            entity: {
                fid: {
                    "field_label": finfo["field_label"],
                    "is_uniform": finfo["is_uniform"],
                    "differing_attrs": finfo["differing_attrs"],
                }
                for fid, finfo in fields.items()
            }
            for entity, fields in result["field_matrix"].items()
        },
        "picklist_matrix": result["picklist_matrix"],
    }

    if include_reports:
        aliases = [inst["alias"] for inst in selected_instances]
        excel_path = generate_matrix_excel_report(aliases, result, selected_instances)
        report_id = excel_path.stem
        html_content = generate_matrix_html_report(aliases, result, selected_instances)
        (REPORTS_DIR / f"{report_id}.html").write_text(html_content, encoding="utf-8")
        response["report_id"] = report_id

    return jsonify(response)


# ── Health ─────────────────────────────────────────────────────────────────


@api.route("/health", methods=["GET"])
def health():
    """Liveness + readiness probe for container orchestration."""
    import time as _time

    from core.db import get_conn

    db_ok = False
    try:
        with get_conn() as conn:
            conn.execute("SELECT 1")
            db_ok = True
    except Exception:
        pass
    return jsonify(
        {
            "status": "healthy" if db_ok else "unhealthy",
            "database": "up" if db_ok else "down",
            "timestamp": _time.time(),
        }
    ), (200 if db_ok else 503)
