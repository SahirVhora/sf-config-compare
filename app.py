import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime
from logging.handlers import RotatingFileHandler

from config import LOG_LEVEL, LOGS_DIR, REPORT_ACCESS_TOKEN, REPORTS_DIR, SECRET_KEY
from core.api import api as _api_blueprint
from core.auth import (
    delete_credentials,
    get_client_secret,
    get_password,
    store_client_secret,
    store_password,
)
from core.comparator import compare_instances
from core.db import (
    create_scheduled_check,
    delete_instance,
    delete_scheduled_check,
    get_all_instances,
    get_conn,
    get_entities_for_instance,
    get_fields_for_entities,
    get_instance,
    get_picklists_for_instance,
    get_pull_history,
    get_scheduled_check,
    get_scheduled_checks,
    init_db,
    update_pull_timestamp,
    update_scheduled_check,
    upsert_instance,
)
from core.reporter import generate_excel_report, generate_html_report
from core.scheduler import schedule_check
from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)

LOGS_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    handlers=[
        RotatingFileHandler(LOGS_DIR / "app.log", maxBytes=5_000_000, backupCount=3),
        logging.StreamHandler(),
    ],
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── Caching layer ─────────────────────────────────────────────────────────
try:
    from flask_caching import Cache

    cache = Cache(app, config={"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 300})
except ImportError:
    logger.debug("flask_caching not installed; response caching disabled")
    cache = None

# ── Rate limiting (enterprise hardening) ──────────────────────────────────
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address

    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=["200 per day", "50 per hour"],
        storage_uri="memory://",
        strategy="fixed-window",
    )
except ImportError:
    logger.debug("flask_limiter not installed; rate limiting disabled")
    limiter = None
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
)


def _get_csrf_token():
    if "csrf_token" not in session:
        import secrets

        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = _get_csrf_token


@app.before_request
def check_csrf():
    if app.config.get("TESTING") is True:
        return
    if request.method == "POST":
        token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        if not token or token != session.get("csrf_token"):
            abort(403, "CSRF token missing or invalid")


init_db()

# ── Wire up scheduled checks on startup (Phase 4) ────────────────────────
try:
    for check in get_scheduled_checks():
        if check.get("enabled"):
            schedule_check(check["id"], check.get("cron_expression", "0 0 * * *"))
except Exception:
    logger.exception("Failed to load scheduled checks on startup")

# ── Register REST API blueprint ─────────────────────────────────────────────
app.register_blueprint(_api_blueprint)

_jobs: dict[str, dict] = {}
_job_queues: dict[str, queue.Queue] = {}
_jobs_lock = threading.Lock()
_pull_semaphore = threading.Semaphore(3)


def _run_pull(
    job_id: str,
    instance: dict,
    pull_type: str,
):
    with _pull_semaphore:
        _run_pull_inner(job_id, instance, pull_type)


def _run_pull_inner(
    job_id: str,
    instance: dict,
    pull_type: str,
):
    q = _job_queues[job_id]
    _jobs[job_id]["status"] = "running"

    def emit(step, status, message, pct=0):
        event = {
            "step": step,
            "status": status,
            "message": message,
            "percent_complete": pct,
            "timestamp": datetime.now().isoformat(),
        }
        _jobs[job_id]["last_event"] = event
        q.put(event)

    try:
        if pull_type == "picklist":
            from core.picklist_pull import pull_picklist

            result = pull_picklist(instance, emit_fn=emit)
            if result["success"]:
                total = result.get("total_values", 0)
                update_pull_timestamp(instance["id"], "picklist")
                emit("parse_picklist", "success", f"Stored {total} picklist values", 100)
            else:
                _jobs[job_id]["status"] = "error"
                _jobs[job_id]["error"] = result.get("error")
                q.put(None)
                return

        elif pull_type == "odata_metadata":
            from core.odata_metadata_pull import pull_odata_metadata

            result = pull_odata_metadata(instance, emit_fn=emit)
            if result["success"]:
                update_pull_timestamp(instance["id"], "metadata")
                emit(
                    "done",
                    "success",
                    f"Stored {result['entities_count']} entities, {result['fields_count']} fields",
                    100,
                )
            else:
                _jobs[job_id]["status"] = "error"
                _jobs[job_id]["error"] = result.get("error")
                q.put(None)
                return

        else:
            raise ValueError(f"Unknown pull_type: {pull_type}")

        _jobs[job_id]["status"] = "done"
        _jobs[job_id]["finished_at"] = datetime.now().isoformat()
    except Exception as exc:
        logger.exception("Job %s failed", job_id)
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = str(exc)
        emit("error", "error", str(exc), 0)
    finally:
        q.put(None)


@app.route("/")
def index():
    if cache:
        cache_key = "instances_list"
        cached = cache.get(cache_key)
        if cached:
            return render_template("index.html", instances=cached)
    instances = get_all_instances()
    if cache:
        cache.set(cache_key, instances, timeout=60)
    return render_template("index.html", instances=instances)


@app.route("/instances/add", methods=["GET", "POST"])
def add_instance():
    if request.method == "POST":
        try:
            data = _form_to_instance(request.form)
            _validate_instance_form(data, request.form)
            _save_credentials(data, request.form)
            upsert_instance(data)
            flash("Instance added.", "success")
            return redirect(url_for("index"))
        except Exception as exc:
            logger.exception("Failed to add instance")
            flash(f"Error saving instance: {exc}", "error")
    return render_template("instance_form.html", instance=None, action="Add")


@app.route("/instances/<int:instance_id>/edit", methods=["GET", "POST"])
def edit_instance(instance_id):
    instance = get_instance(instance_id)
    if not instance:
        abort(404)
    if request.method == "POST":
        try:
            data = _form_to_instance(request.form)
            data["id"] = instance_id
            _validate_instance_form(data, request.form, existing_alias=instance["alias"])
            _save_credentials(data, request.form, existing_alias=instance["alias"])
            upsert_instance(data)
            flash("Instance updated.", "success")
            return redirect(url_for("index"))
        except Exception as exc:
            logger.exception("Failed to update instance")
            flash(f"Error saving instance: {exc}", "error")
    return render_template("instance_form.html", instance=instance, action="Edit")


@app.route("/instances/<int:instance_id>/delete", methods=["POST"])
def del_instance(instance_id):
    instance = get_instance(instance_id)
    if instance:
        delete_credentials(instance["alias"])
        delete_instance(instance_id)
        flash("Instance deleted.", "info")
    return redirect(url_for("index"))


@app.route("/instances/<int:instance_id>/pull", methods=["POST"])
def trigger_pull(instance_id):
    instance = get_instance(instance_id)
    if not instance:
        abort(404)
    pull_type = request.form.get("pull_type", "both")
    if pull_type not in {"odata_metadata", "picklist"}:
        flash("Please choose an API metadata or picklist pull.", "error")
        return redirect(url_for("index"))
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        # Prune stale completed jobs older than 10 minutes
        now = time.time()
        stale = [
            jid
            for jid, j in _jobs.items()
            if j.get("status") in {"done", "error"} and now - j.get("started_at", now) > 600
        ]
        for jid in stale:
            _jobs.pop(jid, None)
            _job_queues.pop(jid, None)
        _jobs[job_id] = {
            "status": "pending",
            "instance_id": instance_id,
            "pull_type": pull_type,
            "started_at": time.time(),
        }
        _job_queues[job_id] = queue.Queue()
    t = threading.Thread(
        target=_run_pull,
        args=(job_id, instance, pull_type),
        daemon=True,
    )
    t.start()
    return redirect(url_for("pull_status", job_id=job_id))


@app.route("/pull/<job_id>")
def pull_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        abort(404)
    instance = get_instance(job["instance_id"])
    return render_template("pull_status.html", job_id=job_id, job=job, instance=instance)


@app.route("/pull/stream/<job_id>")
def pull_stream(job_id):
    with _jobs_lock:
        exists = job_id in _job_queues
    if not exists:
        abort(404)

    @stream_with_context
    def generate():
        with _jobs_lock:
            q = _job_queues[job_id]
        while True:
            event = q.get()
            if event is None:
                yield 'data: {"done": true}\n\n'
                break
            yield f"data: {json.dumps(event)}\n\n"
        with _jobs_lock:
            _job_queues.pop(job_id, None)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/instances/<int:instance_id>/browse")
def browse(instance_id):
    instance = get_instance(instance_id)
    if not instance:
        abort(404)
    raw_entities = get_entities_for_instance(instance_id)
    with get_conn() as conn:
        entity_ids = [e["id"] for e in raw_entities]
        fields_by_entity = get_fields_for_entities(conn, entity_ids)
    for entity in raw_entities:
        entity["fields"] = fields_by_entity.get(entity["id"], [])
    picklist_rows = get_picklists_for_instance(instance_id)
    picklists: dict = {}
    for row in picklist_rows:
        picklists.setdefault(row["picklist_id"], []).append(row)
    total_fields = sum(len(e["fields"]) for e in raw_entities)
    return render_template(
        "browse.html",
        instance=instance,
        entities=raw_entities,
        picklists=picklists,
        total_fields=total_fields,
        total_picklists=len(picklists),
        total_picklist_values=len(picklist_rows),
    )


@app.route("/compare", methods=["GET", "POST"])
def compare():
    instances = get_all_instances()
    if request.method == "POST":
        id_a = int(request.form.get("instance_a", 0))
        id_b = int(request.form.get("instance_b", 0))
        if id_a == id_b:
            flash("Please select two different instances.", "error")
            return render_template("compare.html", instances=instances)
        inst_a = get_instance(id_a)
        inst_b = get_instance(id_b)
        picklist_fields = set(request.form.getlist("compare_fields")) or None
        entity_filter = set(request.form.getlist("entity_filter")) or None
        result = compare_instances(
            id_a, id_b, picklist_fields=picklist_fields, entity_filter=entity_filter
        )
        excel_path = generate_excel_report(inst_a["alias"], inst_b["alias"], result, inst_a, inst_b)
        report_id = excel_path.stem
        html_content = generate_html_report(
            inst_a["alias"],
            inst_b["alias"],
            result,
            download_url=url_for("download_report", report_id=report_id),
            nav_urls={
                "dashboard": url_for("index"),
                "compare": url_for("compare"),
            },
        )
        (REPORTS_DIR / f"{report_id}.html").write_text(html_content, encoding="utf-8")
        return redirect(url_for("view_report", report_id=report_id))
    return render_template("compare.html", instances=instances)


@app.route("/reports/<report_id>/view")
def view_report(report_id):
    if not re.match(r"^[A-Za-z0-9_\-]{3,160}$", report_id):
        abort(400, "Invalid report ID")
    if REPORT_ACCESS_TOKEN and request.args.get("token") != REPORT_ACCESS_TOKEN:
        abort(403, "Missing or invalid report access token")
    report_path = REPORTS_DIR / f"{report_id}.html"
    try:
        resolved = report_path.resolve()
        reports_resolved = REPORTS_DIR.resolve()
        if not str(resolved).startswith(str(reports_resolved)):
            abort(400, "Invalid report path")
    except Exception:
        abort(400, "Invalid report path")
    html_file = report_path
    if not html_file.exists():
        abort(404)
    return html_file.read_text(encoding="utf-8")


@app.route("/reports/<report_id>/download")
def download_report(report_id):
    if REPORT_ACCESS_TOKEN and request.args.get("token") != REPORT_ACCESS_TOKEN:
        abort(403, "Missing or invalid report access token")
    from flask import send_file

    xlsx = REPORTS_DIR / f"{report_id}.xlsx"
    if not xlsx.exists():
        abort(404)
    return send_file(str(xlsx), as_attachment=True, download_name=xlsx.name)


@app.route("/instances/<int:instance_id>/test", methods=["POST"])
def test_connection(instance_id):
    """Lightweight connectivity test - fetches $metadata with the stored credentials."""
    import requests as _req

    instance = get_instance(instance_id)
    if not instance:
        return {"ok": False, "error": "Instance not found"}, 404

    try:
        metadata_url = f"{instance['base_url']}/odata/v2/$metadata"
        headers = {"Accept": "application/xml"}
        timeout = 15

        if instance.get("auth_type") == "oauth2":
            from core.auth import fetch_oauth_token
            from core.auth import get_client_secret as _gcs

            secret = _gcs(instance["alias"])
            if not secret:
                return {"ok": False, "error": "No client secret stored"}, 400
            token = fetch_oauth_token(
                instance["token_url"], instance["client_id"], secret, instance["company_id"]
            )
            headers["Authorization"] = f"Bearer {token}"
            username = pwd = None
        else:
            from core.auth import format_basic_username
            from core.auth import get_password as _gp

            pwd = _gp(instance["alias"])
            if not pwd:
                return {"ok": False, "error": "No password stored"}, 400
            username = format_basic_username(instance.get("username"), instance.get("company_id"))

        resp = _req.get(
            metadata_url,
            auth=(username, pwd) if username and pwd else None,
            headers=headers,
            timeout=timeout,
        )
        if resp.status_code == 200:
            return {
                "ok": True,
                "message": f"Connected. HTTP {resp.status_code}, {len(resp.content)} bytes",
            }, 200
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.reason}"}, 400

    except Exception as exc:
        logger.warning("Connection test failed for instance %s: %s", instance.get("alias"), exc)
        return {"ok": False, "error": str(exc)}, 400


def _form_to_instance(form) -> dict:
    return {
        "alias": form["alias"].strip(),
        "base_url": form["base_url"].strip().rstrip("/"),
        "company_id": form["company_id"].strip(),
        "auth_type": form.get("auth_type", "basic"),
        "username": form.get("username", "").strip() or None,
        "client_id": form.get("client_id", "").strip() or None,
        "token_url": form.get("token_url", "").strip() or None,
    }


def _validate_instance_form(data: dict, form, existing_alias: str | None = None):
    if not data["base_url"].startswith(("https://", "http://")):
        raise ValueError("Base URL must start with https:// or http://")

    if data["auth_type"] == "basic":
        if not data.get("username"):
            raise ValueError("Username is required for basic authentication.")
        if not form.get("password", "").strip() and not (
            existing_alias and get_password(existing_alias)
        ):
            raise ValueError("Password is required for basic authentication.")
    elif data["auth_type"] == "oauth2":
        if not data.get("client_id"):
            raise ValueError("Client ID is required for OAuth 2.0.")
        if not data.get("token_url"):
            raise ValueError("Token URL is required for OAuth 2.0.")
        if not form.get("client_secret", "").strip() and not (
            existing_alias and get_client_secret(existing_alias)
        ):
            raise ValueError("Client secret is required for OAuth 2.0.")
    else:
        raise ValueError("Unsupported authentication type.")


def _save_credentials(data: dict, form, existing_alias: str | None = None):
    alias = data["alias"]

    if data["auth_type"] == "basic":
        pwd = form.get("password", "").strip()
        if not pwd and existing_alias:
            pwd = get_password(existing_alias) or ""
        store_password(alias, pwd)
    else:
        secret = form.get("client_secret", "").strip()
        if not secret and existing_alias:
            secret = get_client_secret(existing_alias) or ""
        store_client_secret(alias, secret)

    if existing_alias and existing_alias != alias:
        delete_credentials(existing_alias)


if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    port = int(os.getenv("PORT", "5050"))
    app.run(port=port, debug=debug_mode)


# ── Pull History (Phase 2) ───────────────────────────────────────────────
@app.route("/instances/<int:instance_id>/history")
def instance_history(instance_id):
    instance = get_instance(instance_id)
    if not instance:
        abort(404)
    history = get_pull_history(instance_id)
    return render_template("history.html", instance=instance, history=history)


# ── Scheduled Checks (Phase 4) ───────────────────────────────────────────
@app.route("/scheduled-checks", methods=["GET"])
def scheduled_checks_list():
    checks = get_scheduled_checks()
    instances = get_all_instances()
    instance_map = {i["id"]: i for i in instances}
    return render_template("scheduled_checks.html", checks=checks, instance_map=instance_map)


@app.route("/scheduled-checks/add", methods=["POST"])
def scheduled_check_add():
    try:
        data = {
            "name": request.form["name"],
            "instance_a_id": int(request.form["instance_a_id"]),
            "instance_b_id": int(request.form["instance_b_id"]),
            "cron_expression": request.form.get("cron_expression", "0 0 * * *"),
            "enabled": bool(request.form.get("enabled")),
            "webhook_url": request.form.get("webhook_url") or None,
            "webhook_type": request.form.get("webhook_type", "slack"),
            "notify_on": request.form.get("notify_on", "any_change"),
        }
        check_id = create_scheduled_check(data)
        if data["enabled"]:
            from core.scheduler import schedule_check

            schedule_check(check_id, data["cron_expression"])
        flash("Scheduled check created.", "success")
    except Exception as exc:
        logger.exception("Failed to create scheduled check")
        flash(f"Error: {exc}", "error")
    return redirect(url_for("scheduled_checks_list"))


@app.route("/scheduled-checks/<int:check_id>/toggle", methods=["POST"])
def scheduled_check_toggle(check_id):
    check = get_scheduled_check(check_id)
    if not check:
        abort(404)
    new_state = not bool(check.get("enabled", 1))
    update_scheduled_check(check_id, {**check, "enabled": new_state, "name": check["name"]})
    from core.scheduler import schedule_check, unschedule_check

    if new_state:
        schedule_check(check_id, check.get("cron_expression", "0 0 * * *"))
    else:
        unschedule_check(check_id)
    flash(f"Check {'enabled' if new_state else 'disabled'}.", "success")
    return redirect(url_for("scheduled_checks_list"))


@app.route("/scheduled-checks/<int:check_id>/delete", methods=["POST"])
def scheduled_check_delete(check_id):
    from core.scheduler import unschedule_check

    unschedule_check(check_id)
    delete_scheduled_check(check_id)
    flash("Scheduled check deleted.", "info")
    return redirect(url_for("scheduled_checks_list"))


# ── AI Summary (Phase 5) ────────────────────────────────────────────────
@app.route("/compare/ai-summary", methods=["POST"])
def ai_summary():
    try:
        id_a = int(request.form.get("instance_a_id", 0))
        id_b = int(request.form.get("instance_b_id", 1))
        inst_a = get_instance(id_a)
        inst_b = get_instance(id_b)
        if not inst_a or not inst_b:
            return jsonify({"error": "Instance not found"}), 404
        result = compare_instances(id_a, id_b)
        from core.ai_analyzer import summarize_comparison

        summary = summarize_comparison(inst_a["alias"], inst_b["alias"], result)
        return jsonify(summary)
    except Exception as exc:
        logger.exception("AI summary failed")
        return jsonify({"error": str(exc)}), 500
