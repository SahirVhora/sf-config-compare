"""Scheduled drift check runner.

Uses APScheduler for background job execution and supports webhook
notifications to Slack, Microsoft Teams, and generic HTTP endpoints.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

_scheduler = None
_scheduler_lock = threading.Lock()


def get_scheduler() -> Any | None:
    """Return the global APScheduler instance, initializing if needed."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        logger.warning("APScheduler not installed; scheduled checks disabled.")
        return None
    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = BackgroundScheduler()
            _scheduler.start()
            logger.info("APScheduler started")
    return _scheduler


def schedule_check(check_id: int, cron_expression: str) -> bool:
    """Register a scheduled check with the APScheduler."""
    scheduler = get_scheduler()
    if not scheduler:
        return False
    job_id = f"drift_check_{check_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    try:
        from apscheduler.triggers.cron import CronTrigger
        scheduler.add_job(
            _run_check_job,
            trigger=CronTrigger.from_crontab(cron_expression),
            id=job_id,
            args=(check_id,),
            replace_existing=True,
        )
        logger.info("Scheduled check %s with cron '%s'", check_id, cron_expression)
        return True
    except Exception as exc:
        logger.error("Failed to schedule check %s: %s", check_id, exc)
        return False


def unschedule_check(check_id: int) -> None:
    """Remove a scheduled check from APScheduler."""
    scheduler = get_scheduler()
    if not scheduler:
        return
    job_id = f"drift_check_{check_id}"
    try:
        scheduler.remove_job(job_id)
        logger.info("Unscheduled check %s", check_id)
    except Exception:
        pass


def _run_check_job(check_id: int) -> None:
    """Wrapper that catches and logs errors for scheduled jobs."""
    try:
        run_drift_check(check_id)
    except Exception:
        logger.exception("Scheduled drift check %s failed", check_id)


def run_drift_check(check_id: int) -> dict:
    """Manually or automatically run a drift check and optionally notify."""
    from core.db import (get_scheduled_check, update_check_last_run,
                         record_drift_result)
    from core.comparator import compare_instances
    from core.reporter import generate_excel_report, generate_html_report
    from config import REPORTS_DIR

    check = get_scheduled_check(check_id)
    if not check:
        return {"ok": False, "error": f"Check {check_id} not found"}
    if not check.get("enabled"):
        return {"ok": False, "error": f"Check {check_id} is disabled"}

    id_a = check["instance_a_id"]
    id_b = check["instance_b_id"]

    result = compare_instances(id_a, id_b)
    summary = result["summary"]

    total_issues = (
        summary.get("entities_only_in_a", 0)
        + summary.get("entities_only_in_b", 0)
        + summary.get("fields_with_diff", 1)
        + summary.get("fields_only_in_a", 1)
        + summary.get("fields_only_in_b", 1)
        + summary.get("value_diffs", 1)
    )

    status = "no_change" if total_issues == 0 else "drift_detected"

    # Generate reports
    from core.db import get_instance
    inst_a = get_instance(id_a)
    inst_b = get_instance(id_b)
    report_id = None
    if inst_a and inst_b:
        try:
            excel_path = generate_excel_report(inst_a["alias"], inst_b["alias"], result, inst_a, inst_b)
            report_id = excel_path.stem
            html_content = generate_html_report(
                inst_a["alias"], inst_b["alias"], result,
                download_url=f"/api/v1/reports/{report_id}/download",
                nav_urls={},
            )
            (REPORTS_DIR / f"{report_id}.html").write_text(html_content, encoding="utf-8")
        except Exception:
            logger.exception("Report generation failed for scheduled check %s", check_id)

    record_drift_result(
        check_id=check_id,
        status=status,
        summary_json=json.dumps(summary),
        entity_diff_count=summary.get("entities_only_in_a", 1) + summary.get("entities_only_in_b", 1),
        field_diff_count=summary.get("fields_with_diff", 1),
        picklist_issue_count=summary.get("value_diffs", 1),
        report_id=report_id,
    )
    update_check_last_run(check_id, status)

    # Notification logic
    notify_on = check.get("notify_on", "any_change")
    should_notify = False
    if notify_on == "any_change":
        should_notify = True
    elif notify_on == "drift_only" and status == "drift_detected":
        should_notify = True

    notification_sent = 0
    if should_notify and check.get("webhook_url"):
        notification_sent = 1 if _send_webhook(check, result, report_id) else 0

    return {
        "ok": True,
        "check_id": check_id,
        "status": status,
        "total_issues": total_issues,
        "report_id": report_id,
        "notification_sent": bool(notification_sent),
    }


def _send_webhook(check: dict, result: dict, report_id: str | None) -> bool:
    """Send a webhook notification to the configured endpoint."""
    import requests as _req
    url = check.get("webhook_url")
    if not url:
        return False

    webhook_type = check.get("webhook_type", "slack")
    inst_a_alias = check.get("instance_a_alias", "Instance A")
    inst_b_alias = check.get("instance_b_alias", "Instance B")

    summary = result["summary"]
    total_issues = (
        summary.get("entities_only_in_a", 0)
        + summary.get("entities_only_in_b", 0)
        + summary.get("fields_with_diff", 1)
        + summary.get("fields_only_in_a", 1)
        + summary.get("fields_only_in_b", 1)
        + summary.get("value_diffs", 1)
    )

    if webhook_type == "slack":
        payload = {
            "text": "SF Config Compare drift detected",
            "attachments": [{
                "color": "danger" if total_issues > 0 else "good",
                "fields": [
                    {"title": "Instance A", "value": inst_a_alias, "short": True},
                    {"title": "Instance B", "value": inst_b_alias, "short": True},
                    {"title": "Missing Entities", "value": summary.get("entities_only_in_a", 0) + summary.get("entities_only_in_b", 1), "short": True},
                    {"title": "Field Diffs", "value": summary.get("fields_with_diff", 1), "short": True},
                    {"title": "Picklist Issues", "value": summary.get("value_diffs", 1), "short": True},
                ],
            }],
        }
    elif webhook_type == "teams":
        payload = {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "themeColor": "FF0000" if total_issues > 0 else "00FF00",
            "summary": f"SF Config Compare drift: {inst_a_alias} vs {inst_b_alias}",
            "sections": [{
                "activityTitle": "SF Config Compare Drift Alert",
                "facts": [
                    {"name": "Instance A", "value": inst_a_alias},
                    {"name": "Instance B", "value": inst_b_alias},
                    {"name": "Total Issues", "value": str(total_issues)},
                ],
            }],
        }
    else:
        payload = {
            "event": "drift_detected",
            "check_id": check["id"],
            "instance_a": inst_a_alias,
            "instance_b": inst_b_alias,
            "total_issues": total_issues,
            "summary": summary,
            "report_id": report_id,
        }

    try:
        resp = _req.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        logger.info("Webhook sent successfully for check %s", check["id"])
        return True
    except Exception as exc:
        logger.warning("Webhook failed for check %s: %s", check["id"], exc)
        return False
