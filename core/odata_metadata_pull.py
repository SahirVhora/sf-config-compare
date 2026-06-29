import json
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime

import requests
from defusedxml.ElementTree import fromstring as _safe_fromstring

from core.auth import build_instance_auth
from core.db import record_pull_history

logger = logging.getLogger(__name__)

_EDM_NS = "http://schemas.microsoft.com/ado/2008/09/edm"
_SAP_NS = "http://www.successfactors.com/edm/sap"
_EDMX_NS = "http://schemas.microsoft.com/ado/2007/06/edmx"

_SAP = f"{{{_SAP_NS}}}"
_EDM = f"{{{_EDM_NS}}}"


def pull_odata_metadata(instance: dict, emit_fn=None) -> dict:
    """
    Pull OData entity/field metadata directly from SuccessFactors.
    Fetches /odata/v2/$metadata (one call, all entities), parses EntityType nodes,
    and writes into metadata_entities + metadata_fields - same tables as the admin
    XML pull, so the comparator and reporter work unchanged.
    Returns { success, entities_count, fields_count, duration_seconds }.
    """
    start = time.time()
    alias = instance["alias"]
    base_url = instance["base_url"].rstrip("/")

    def emit(step, status, message, pct=0):
        logger.info("[%s] %s: %s", step, status, message)
        if emit_fn:
            emit_fn(step, status, message, pct)

    emit("init", "in-progress", "Starting OData metadata pull", 5)

    history_id = record_pull_history(instance["id"], "metadata", "pending")
    try:
        auth_headers, auth_obj = _build_auth(instance, alias, emit)

        emit("fetch", "in-progress", "Fetching /odata/v2/$metadata from API", 20)
        url = f"{base_url}/odata/v2/$metadata"
        resp = requests.get(
            url, headers=auth_headers, auth=auth_obj, verify=True, timeout=120
        )
        resp.raise_for_status()

        emit("parse", "in-progress", "Parsing metadata XML", 55)
        logger.debug(
            "Response status: %s, Content-Type: %s",
            resp.status_code,
            resp.headers.get("Content-Type"),
        )
        logger.debug("Response snippet: %s", resp.text[:500])
        try:
            root = _safe_fromstring(resp.content)
        except ET.ParseError as parse_err:
            snippet = resp.text[:800].replace("\n", " ")
            raise RuntimeError(
                f"Metadata response is not valid XML ({parse_err}). "
                f"This usually means an auth failure or redirect. "
                f"Response snippet: {snippet}"
            ) from parse_err
        entity_labels = _collect_entity_labels(root)
        entities, fields = _parse_entity_types(root, entity_labels)

        emit(
            "store",
            "in-progress",
            f"Storing {len(entities)} entities, {sum(len(f) for f in fields.values())} fields",
            75,
        )
        pull_ts = datetime.now().isoformat()
        stats = _write_to_db(instance["id"], pull_ts, entities, fields)
        record_pull_history(
            instance["id"],
            "metadata",
            "success",
            entities_count=stats["entities_count"],
            fields_count=stats["fields_count"],
            history_id=history_id,
        )

        duration = round(time.time() - start, 2)
        emit(
            "done",
            "success",
            f"Stored {stats['entities_count']} entities, {stats['fields_count']} fields",
            100,
        )
        return {"success": True, "duration_seconds": duration, **stats}

    except Exception as exc:
        record_pull_history(
            instance["id"], "metadata", "error", error=str(exc), history_id=history_id
        )
        duration = round(time.time() - start, 2)
        emit("error", "error", str(exc), 0)
        logger.exception("OData metadata pull failed for %s", alias)
        return {"success": False, "error": str(exc), "duration_seconds": duration}


def _build_auth(instance: dict, alias: str, emit):
    auth_type = instance["auth_type"]
    if auth_type == "oauth2":
        emit("auth", "in-progress", "Fetching OAuth token", 10)
    else:
        emit("auth", "in-progress", f"Using basic auth for {alias}", 10)
    return build_instance_auth(instance, alias)


def _collect_entity_labels(root: ET.Element) -> dict[str, str]:
    """Build a map of entity_name -> label from the SFODataSet EntitySet nodes."""
    labels: dict[str, str] = {}
    for schema in root.iter(f"{_EDM}Schema"):
        if schema.get("Namespace") != "SFODataSet":
            continue
        for es in schema.iter(f"{_EDM}EntitySet"):
            name = es.get("Name") or ""
            label = es.get(f"{_SAP}label") or es.get("label") or ""
            if name:
                labels[name] = label
    return labels


def _parse_entity_types(root: ET.Element, entity_labels: dict) -> tuple[list, dict]:
    """
    Parse SFOData EntityType nodes.
    Returns (entities_list, fields_dict) where fields_dict maps entity_name -> list of field dicts.
    """
    entities = []
    fields: dict[str, list] = {}

    for schema in root.iter(f"{_EDM}Schema"):
        if schema.get("Namespace") != "SFOData":
            continue

        for et in schema.iter(f"{_EDM}EntityType"):
            entity_name = et.get("Name") or ""
            if not entity_name:
                continue

            key_names = {
                pr.get("Name") for pr in et.findall(f".//{_EDM}Key/{_EDM}PropertyRef")
            }

            entities.append(
                {
                    "entity_name": entity_name,
                    "entity_label": entity_labels.get(entity_name, ""),
                    "element_name": entity_name,
                }
            )

            entity_fields = []

            for prop in et.findall(f"{_EDM}Property"):
                field_id = prop.get("Name") or ""
                field_label = prop.get(f"{_SAP}label") or prop.get("label") or ""
                field_type = prop.get("Type") or ""
                # Nullable=false means required; sap:required may also be set
                nullable = prop.get("Nullable", "true").lower()
                sap_required = prop.get(f"{_SAP}required") or ""
                required = (
                    "true"
                    if nullable == "false" or sap_required.lower() == "true"
                    else "false"
                )
                visibility = prop.get(f"{_SAP}visible") or prop.get("visible") or ""
                max_length = prop.get("MaxLength") or ""
                picklist_id = prop.get(f"{_SAP}picklist") or prop.get("picklist") or ""
                is_custom = (
                    1
                    if field_id.startswith("cust_") or entity_name.startswith("cust_")
                    else 0
                )

                known = {
                    "Name",
                    "Type",
                    "Nullable",
                    "MaxLength",
                    f"{_SAP}label",
                    f"{_SAP}visible",
                    f"{_SAP}required",
                    f"{_SAP}picklist",
                }
                raw_attrs = {
                    _clean_attr(k): v
                    for k, v in prop.attrib.items()
                    if k not in known and v
                }
                if field_id in key_names:
                    raw_attrs["key"] = "true"

                entity_fields.append(
                    {
                        "field_id": field_id,
                        "field_label": field_label,
                        "field_type": field_type,
                        "required": required,
                        "visibility": visibility,
                        "max_length": max_length,
                        "picklist_id": picklist_id,
                        "is_custom": is_custom,
                        "raw_attributes": json.dumps(raw_attrs) if raw_attrs else None,
                        "nav": False,
                    }
                )

            for nav in et.findall(f"{_EDM}NavigationProperty"):
                field_id = nav.get("Name") or ""
                entity_fields.append(
                    {
                        "field_id": field_id,
                        "field_label": nav.get(f"{_SAP}label") or "",
                        "field_type": "NavigationProperty",
                        "required": "false",
                        "visibility": "",
                        "max_length": "",
                        "picklist_id": "",
                        "is_custom": 0,
                        "raw_attributes": None,
                        "nav": True,
                    }
                )

            fields[entity_name] = entity_fields

    return entities, fields


def _write_to_db(
    instance_id: int, pull_timestamp: str, entities: list, fields: dict
) -> dict:
    """Persist parsed metadata to SQLite inside a single transaction for speed and consistency.

    Deletes stale data first, then inserts new entities and fields atomically.
    If any step fails, the entire pull is rolled back so the DB is never left
    in a half-written state.
    """
    from core.db import transaction

    entities_count = 0
    fields_count = 0

    with transaction() as conn:
        conn.execute(
            "DELETE FROM metadata_fields WHERE entity_id IN "
            "(SELECT id FROM metadata_entities WHERE instance_id = ?)",
            (instance_id,),
        )
        conn.execute(
            "DELETE FROM metadata_entities WHERE instance_id = ?", (instance_id,)
        )

        for entity in entities:
            cur = conn.execute(
                "INSERT INTO metadata_entities "
                "(instance_id, entity_name, entity_label, element_name, pull_timestamp)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    instance_id,
                    entity["entity_name"],
                    entity["entity_label"],
                    entity["element_name"],
                    pull_timestamp,
                ),
            )
            entity_id = cur.lastrowid
            entities_count += 1

            for f in fields.get(entity["entity_name"], []):
                conn.execute(
                    "INSERT INTO metadata_fields "
                    "(entity_id, field_id, field_label, field_type, required, visibility,"
                    " max_length, picklist_id, is_custom, raw_attributes)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entity_id,
                        f["field_id"],
                        f["field_label"],
                        f["field_type"],
                        f["required"],
                        f["visibility"],
                        f["max_length"],
                        f["picklist_id"],
                        f["is_custom"],
                        f["raw_attributes"],
                    ),
                )
                fields_count += 1

    return {"entities_count": entities_count, "fields_count": fields_count}


def _clean_attr(attr: str) -> str:
    if attr.startswith("{"):
        attr = attr.split("}", 1)[-1]
    return attr
