import logging
import re
from core.db import get_conn

logger = logging.getLogger(__name__)

FIELD_ATTRS = ["field_type", "required", "visibility", "max_length", "picklist_id", "is_custom"]
ACTIVE_STATUSES = {"ACTIVE", "A", "1", "TRUE"}


def _clean_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _norm_status(value) -> str:
    raw = _clean_text(value).upper()
    return "ACTIVE" if raw in ACTIVE_STATUSES else raw


def compare_instances(instance_a_id: int, instance_b_id: int, picklist_fields: set | None = None, entity_filter: set | None = None) -> dict:
    entities_a = _load_entities(instance_a_id, entity_filter)
    entities_b = _load_entities(instance_b_id, entity_filter)

    names_a = set(entities_a)
    names_b = set(entities_b)

    only_in_a = sorted(names_a - names_b)
    only_in_b = sorted(names_b - names_a)
    in_both = sorted(names_a & names_b)

    entity_diffs = []
    for name in only_in_a:
        entity_diffs.append({"entity_name": name, "diff_type": "only_in_a", "details": entities_a[name]})
    for name in only_in_b:
        entity_diffs.append({"entity_name": name, "diff_type": "only_in_b", "details": entities_b[name]})

    field_diffs = []
    fields_matched = 0
    fields_with_diff = 0
    fields_only_in_a = 0
    fields_only_in_b = 0
    fields_by_entity_a = _load_fields_by_entity(instance_a_id, entity_filter)
    fields_by_entity_b = _load_fields_by_entity(instance_b_id, entity_filter)

    for entity_name in in_both:
        ent_a = entities_a[entity_name]
        ent_b = entities_b[entity_name]
        fields_a = {f["field_id"]: f for f in fields_by_entity_a.get(ent_a["id"], [])}
        fields_b = {f["field_id"]: f for f in fields_by_entity_b.get(ent_b["id"], [])}

        fids_a = set(fields_a)
        fids_b = set(fields_b)

        for fid in sorted(fids_a - fids_b):
            f = fields_a[fid]
            field_diffs.append({
                "entity_name": entity_name,
                "field_id": fid,
                "field_label": f.get("field_label"),
                "diff_type": "only_in_a",
                "attribute": None,
                "value_a": None,
                "value_b": None,
            })
            fields_only_in_a += 1

        for fid in sorted(fids_b - fids_a):
            f = fields_b[fid]
            field_diffs.append({
                "entity_name": entity_name,
                "field_id": fid,
                "field_label": f.get("field_label"),
                "diff_type": "only_in_b",
                "attribute": None,
                "value_a": None,
                "value_b": None,
            })
            fields_only_in_b += 1

        for fid in sorted(fids_a & fids_b):
            fa = fields_a[fid]
            fb = fields_b[fid]
            diffs_found = False
            for attr in FIELD_ATTRS:
                va = str(fa.get(attr) or "")
                vb = str(fb.get(attr) or "")
                if va != vb:
                    field_diffs.append({
                        "entity_name": entity_name,
                        "field_id": fid,
                        "field_label": fa.get("field_label"),
                        "diff_type": "attribute_mismatch",
                        "attribute": attr,
                        "value_a": va,
                        "value_b": vb,
                    })
                    diffs_found = True
            if diffs_found:
                fields_with_diff += 1
            else:
                fields_matched += 1

    picklist_result, picklist_summary = _compare_picklists(instance_a_id, instance_b_id, picklist_fields)

    return {
        "summary": {
            "entities_only_in_a": len(only_in_a),
            "entities_only_in_b": len(only_in_b),
            "entities_in_both": len(in_both),
            "fields_matched": fields_matched,
            "fields_with_diff": fields_with_diff,
            "fields_only_in_a": fields_only_in_a,
            "fields_only_in_b": fields_only_in_b,
            **picklist_summary,
        },
        "entity_diffs": entity_diffs,
        "field_diffs": field_diffs,
        "picklist_result": picklist_result,
    }


def get_all_picklist_locales(instance_ids: list[int]) -> list[str]:
    """Return sorted list of all locale keys found in all_labels for the given instances."""
    import json as _json
    locales: set[str] = set()
    with get_conn() as conn:
        placeholders = ",".join("?" * len(instance_ids))
        rows = conn.execute(
            f"SELECT all_labels FROM picklist_values WHERE instance_id IN ({placeholders})",
            instance_ids,
        ).fetchall()
    for row in rows:
        try:
            labels = _json.loads(row["all_labels"] or "{}")
            locales.update(k for k in labels if k)
        except Exception:
            pass
    return sorted(locales)


def _compare_picklists(instance_a_id: int, instance_b_id: int, fields: set | None) -> tuple[dict, dict]:
    import json as _json

    compare_label_en = fields is None or "label_en" in fields
    compare_status = fields is None or "status" in fields
    locale_filter = None if fields is None else {
        f[7:] for f in fields if f.startswith("locale:")
    }

    with get_conn() as conn:
        rows_a = conn.execute(
            "SELECT picklist_id, external_code, option_id, label_en, all_labels, status"
            " FROM picklist_values WHERE instance_id = ?",
            (instance_a_id,),
        ).fetchall()
        rows_b = conn.execute(
            "SELECT picklist_id, external_code, option_id, label_en, all_labels, status"
            " FROM picklist_values WHERE instance_id = ?",
            (instance_b_id,),
        ).fetchall()

    def index(rows):
        idx = {}
        for r in rows:
            code = r["external_code"] or r["option_id"]
            key = (r["picklist_id"], code)
            all_labels = {}
            try:
                raw_labels = _json.loads(r["all_labels"] or "{}")
                all_labels = {
                    _clean_text(k): _clean_text(v)
                    for k, v in raw_labels.items()
                    if _clean_text(k) and _clean_text(v)
                }
            except (_json.JSONDecodeError, TypeError):
                all_labels = {}
            idx[key] = {
                "label_en": _clean_text(r["label_en"]),
                "status": _norm_status(r["status"]),
                "all_labels": all_labels,
            }
        return idx

    idx_a = index(rows_a)
    idx_b = index(rows_b)

    pids_a = {k[0] for k in idx_a}
    pids_b = {k[0] for k in idx_b}
    pids_only_a = pids_a - pids_b
    pids_only_b = pids_b - pids_a
    pids_shared = pids_a & pids_b

    def counts_by_picklist(idx):
        c: dict[str, int] = {}
        for pl_id, _ in idx:
            c[pl_id] = c.get(pl_id, 0) + 1
        return c

    counts_a = counts_by_picklist(idx_a)
    counts_b = counts_by_picklist(idx_b)

    missing_picklists = []
    for pl_id in sorted(pids_only_a):
        missing_picklists.append({
            "picklist_id": pl_id,
            "diff_type": "only_in_a",
            "value_count": counts_a.get(pl_id, 0),
        })
    for pl_id in sorted(pids_only_b):
        missing_picklists.append({
            "picklist_id": pl_id,
            "diff_type": "only_in_b",
            "value_count": counts_b.get(pl_id, 0),
        })

    missing_values = []
    value_diffs = []

    for key in sorted(k for k in set(idx_a) | set(idx_b) if k[0] in pids_shared):
        pl_id, ext_code = key
        if key in idx_a and key not in idx_b:
            missing_values.append({
                "picklist_id": pl_id, "external_code": ext_code,
                "label": idx_a[key]["label_en"],
                "status": idx_a[key]["status"],
                "diff_type": "only_in_a",
            })
        elif key in idx_b and key not in idx_a:
            missing_values.append({
                "picklist_id": pl_id, "external_code": ext_code,
                "label": idx_b[key]["label_en"],
                "status": idx_b[key]["status"],
                "diff_type": "only_in_b",
            })
        else:
            va, vb = idx_a[key], idx_b[key]
            field_diffs: list[dict] = []

            if compare_label_en and va["label_en"] != vb["label_en"]:
                field_diffs.append({"field": "label_en", "value_a": va["label_en"], "value_b": vb["label_en"]})

            if compare_status and va["status"] != vb["status"]:
                field_diffs.append({"field": "status", "value_a": va["status"], "value_b": vb["status"]})

            locales_to_check = locale_filter if locale_filter is not None else (
                set(va["all_labels"]) | set(vb["all_labels"])
            )
            for locale in sorted(locales_to_check):
                lva = va["all_labels"].get(locale, "")
                lvb = vb["all_labels"].get(locale, "")
                if lva != lvb:
                    field_diffs.append({"field": f"locale:{locale}", "value_a": lva, "value_b": lvb})

            if field_diffs:
                value_diffs.append({
                    "picklist_id": pl_id, "external_code": ext_code,
                    "label_a": va["label_en"], "label_b": vb["label_en"],
                    "field_diffs": field_diffs,
                })

    summary = {
        "picklists_only_in_a": len(pids_only_a),
        "picklists_only_in_b": len(pids_only_b),
        "picklists_shared": len(pids_shared),
        "missing_values_in_a": sum(1 for v in missing_values if v["diff_type"] == "only_in_b"),
        "missing_values_in_b": sum(1 for v in missing_values if v["diff_type"] == "only_in_a"),
        "value_diffs": len(value_diffs),
    }
    return {
        "missing_picklists": missing_picklists,
        "missing_values": missing_values,
        "value_diffs": value_diffs,
    }, summary


def _load_entities(instance_id: int, entity_filter: set | None = None) -> dict:
    sql = "SELECT * FROM metadata_entities WHERE instance_id = ?"
    params: list = [instance_id]
    if entity_filter:
        placeholders = ",".join("?" * len(entity_filter))
        sql += f" AND entity_name IN ({placeholders})"
        params.extend(sorted(entity_filter))
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {r["entity_name"]: dict(r) for r in rows}


def _load_fields_by_entity(instance_id: int, entity_filter: set | None = None) -> dict[int, list]:
    by_entity: dict[int, list] = {}
    sql = """
        SELECT f.*
        FROM metadata_fields f
        JOIN metadata_entities e ON e.id = f.entity_id
        WHERE e.instance_id = ?
    """
    params: list = [instance_id]
    if entity_filter:
        placeholders = ",".join("?" * len(entity_filter))
        sql += f" AND e.entity_name IN ({placeholders})"
        params.extend(sorted(entity_filter))
    sql += " ORDER BY f.entity_id, f.field_id"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    for row in rows:
        item = dict(row)
        by_entity.setdefault(item["entity_id"], []).append(item)
    return by_entity
