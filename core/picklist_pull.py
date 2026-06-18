import json
import logging
import time
import requests
from requests.auth import HTTPBasicAuth
from datetime import date, datetime

from sapsf_shared.utils import parse_sf_date

from core.auth import (fetch_oauth_token, format_basic_username,
                       get_client_secret, get_password)
from core.db import get_conn, record_pull_history

logger = logging.getLogger(__name__)

ODATA_PICKLIST_PATH = "/odata/v2/PickListValueV2"
PAGE_SIZE = 1000

# Label columns to collect into all_labels
_LABEL_PREFIX = "label_"


def pull_picklist(instance: dict, emit_fn=None) -> dict:
    """
    Pull picklist values directly from the SuccessFactors OData API.
    Writes results straight to the DB - no browser, no file download.
    Returns { success, total_values, total_picklists, duration_seconds } or { success: False, error }.
    """
    start = time.time()
    alias = instance["alias"]
    base_url = instance["base_url"].rstrip("/")

    def emit(step, status, message, pct=0):
        logger.info("[%s] %s: %s", step, status, message)
        if emit_fn:
            emit_fn(step, status, message, pct)

    history_id = record_pull_history(instance['id'], 'picklist', 'pending')
    emit("init", "in-progress", "Starting picklist pull via OData API", 5)

    try:
        auth_headers, auth_obj = _build_auth(instance, alias, emit)

        emit("fetch", "in-progress", "Fetching PickListValueV2 from OData API", 20)
        all_results = _fetch_all_pages(base_url, auth_headers, auth_obj, emit)

        emit("store", "in-progress", f"Storing {len(all_results)} picklist values", 80)
        stats = _write_to_db(all_results, instance["id"], datetime.now().isoformat())

        record_pull_history(instance['id'], 'picklist', 'success',
                            picklists_count=stats['total_picklists'],
                            values_count=stats['total_values'],
                            history_id=history_id)
        duration = round(time.time() - start, 2)
        emit("done", "success",
             f"Stored {stats['total_values']} values across {stats['total_picklists']} picklists", 100)
        return {"success": True, "duration_seconds": duration, **stats}

    except Exception as exc:
        record_pull_history(instance['id'], 'picklist', 'error',
                            error=str(exc), history_id=history_id)
        duration = round(time.time() - start, 2)
        emit("error", "error", str(exc), 0)
        logger.exception("Picklist pull failed for %s", alias)
        return {"success": False, "error": str(exc), "duration_seconds": duration}


def _build_auth(instance: dict, alias: str, emit):
    """Return (headers_dict, requests_auth_or_None) for the instance auth type."""
    if instance["auth_type"] == "oauth2":
        secret = get_client_secret(alias)
        if not secret:
            raise RuntimeError("OAuth client secret not found in keyring")
        emit("auth", "in-progress", "Fetching OAuth token", 10)
        token = fetch_oauth_token(
            instance["token_url"], instance["client_id"], secret, instance["company_id"]
        )
        return {"Authorization": f"Bearer {token}"}, None
    else:
        password = get_password(alias)
        if not password:
            raise RuntimeError("Password not found in keyring")
        username = format_basic_username(instance["username"], instance["company_id"])
        emit("auth", "in-progress", f"Using basic auth user {username}", 10)
        return {}, HTTPBasicAuth(username, password)


def _fetch_all_pages(base_url: str, headers: dict, auth, emit) -> list:
    all_results = []
    endpoint = f"{base_url}{ODATA_PICKLIST_PATH}"
    skip = 0
    url = _build_picklist_url(endpoint, skip)
    page = 0
    while url:
        page += 1
        emit("fetch", "in-progress", f"Fetching page {page}…", 20 + min(page * 5, 55))
        resp = requests.get(url, headers=headers, auth=auth, verify=True, timeout=60)
        resp.raise_for_status()
        data = resp.json().get("d", {})
        results = data.get("results", [])
        all_results.extend(results)
        next_url = data.get("__next")  # OData next-page link
        if next_url:
            url = next_url
        elif len(results) == PAGE_SIZE:
            skip += PAGE_SIZE
            url = _build_picklist_url(endpoint, skip)
        else:
            url = None
    return all_results


def _build_picklist_url(endpoint: str, skip: int) -> str:
    return f"{endpoint}?$format=json&$top={PAGE_SIZE}&$skip={skip}"


def _write_to_db(results: list, instance_id: int, pull_timestamp: str) -> dict:
    today = date.today()
    picklist_ids: set[str] = set()
    active = 0
    inactive = 0

    with get_conn() as conn:
        conn.execute("DELETE FROM picklist_values WHERE instance_id = ?", (instance_id,))

        for item in results:
            valid_from = parse_sf_date(item.get("validFrom"))
            valid_to = parse_sf_date(item.get("validTo"))
            if valid_from and valid_from > today:
                continue
            if valid_to and valid_to < today:
                continue

            picklist_id = item.get("PickListV2_id") or item.get("picklistId") or ""
            option_id = item.get("optionId")
            external_code = item.get("externalCode")
            parent_picklist_id = item.get("parentPicklistId") or item.get("PickListV2_parentPicklistId")
            status = item.get("status") or "ACTIVE"
            if str(status).upper() not in ("ACTIVE", "A", "1", "TRUE"):
                inactive += 1
                continue
            label_en = item.get("label_en_US") or item.get("label_en")

            all_labels = {
                k[len(_LABEL_PREFIX):]: v
                for k, v in item.items()
                if k.startswith(_LABEL_PREFIX) and v
            }

            conn.execute(
                "INSERT INTO picklist_values "
                "(instance_id, picklist_id, option_id, external_code, parent_picklist_id,"
                " status, label_en, all_labels, pull_timestamp)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    instance_id, picklist_id, option_id, external_code, parent_picklist_id,
                    status, label_en, json.dumps(all_labels), pull_timestamp,
                ),
            )
            picklist_ids.add(picklist_id)
            active += 1

    return {
        "total_picklists": len(picklist_ids),
        "total_values": active,
        "active_values": active,
        "inactive_values": inactive,
    }
