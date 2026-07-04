import logging
import re

from core.db import get_conn

logger = logging.getLogger(__name__)

FIELD_ATTRS = [
    "field_type",
    "required",
    "visibility",
    "max_length",
    "picklist_id",
    "is_custom",
]
ACTIVE_STATUSES = {"ACTIVE", "A", "1", "TRUE"}


def _clean_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _norm_status(value) -> str:
    raw = _clean_text(value).upper()
    return "ACTIVE" if raw in ACTIVE_STATUSES else raw


def compare_instances(
    instance_a_id: int,
    instance_b_id: int,
    picklist_fields: set | None = None,
    entity_filter: set | None = None,
) -> dict:
    entities_a = _load_entities(instance_a_id, entity_filter)
    entities_b = _load_entities(instance_b_id, entity_filter)

    names_a = set(entities_a)
    names_b = set(entities_b)

    only_in_a = sorted(names_a - names_b)
    only_in_b = sorted(names_b - names_a)
    in_both = sorted(names_a & names_b)

    entity_diffs = []
    for name in only_in_a:
        entity_diffs.append(
            {"entity_name": name, "diff_type": "only_in_a", "details": entities_a[name]}
        )
    for name in only_in_b:
        entity_diffs.append(
            {"entity_name": name, "diff_type": "only_in_b", "details": entities_b[name]}
        )

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
            field_diffs.append(
                {
                    "entity_name": entity_name,
                    "field_id": fid,
                    "field_label": f.get("field_label"),
                    "diff_type": "only_in_a",
                    "attribute": None,
                    "value_a": None,
                    "value_b": None,
                }
            )
            fields_only_in_a += 1

        for fid in sorted(fids_b - fids_a):
            f = fields_b[fid]
            field_diffs.append(
                {
                    "entity_name": entity_name,
                    "field_id": fid,
                    "field_label": f.get("field_label"),
                    "diff_type": "only_in_b",
                    "attribute": None,
                    "value_a": None,
                    "value_b": None,
                }
            )
            fields_only_in_b += 1

        for fid in sorted(fids_a & fids_b):
            fa = fields_a[fid]
            fb = fields_b[fid]
            diffs_found = False
            for attr in FIELD_ATTRS:
                va = str(fa.get(attr) or "")
                vb = str(fb.get(attr) or "")
                if va != vb:
                    field_diffs.append(
                        {
                            "entity_name": entity_name,
                            "field_id": fid,
                            "field_label": fa.get("field_label"),
                            "diff_type": "attribute_mismatch",
                            "attribute": attr,
                            "value_a": va,
                            "value_b": vb,
                        }
                    )
                    diffs_found = True
            if diffs_found:
                fields_with_diff += 1
            else:
                fields_matched += 1

    picklist_result, picklist_summary = _compare_picklists(
        instance_a_id, instance_b_id, picklist_fields
    )

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


def _compare_picklists(
    instance_a_id: int, instance_b_id: int, fields: set | None
) -> tuple[dict, dict]:
    import json as _json

    compare_label_en = fields is None or "label_en" in fields
    compare_status = fields is None or "status" in fields
    locale_filter = (
        None if fields is None else {f[7:] for f in fields if f.startswith("locale:")}
    )

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
        missing_picklists.append(
            {
                "picklist_id": pl_id,
                "diff_type": "only_in_a",
                "value_count": counts_a.get(pl_id, 0),
            }
        )
    for pl_id in sorted(pids_only_b):
        missing_picklists.append(
            {
                "picklist_id": pl_id,
                "diff_type": "only_in_b",
                "value_count": counts_b.get(pl_id, 0),
            }
        )

    missing_values = []
    value_diffs = []

    for key in sorted(k for k in set(idx_a) | set(idx_b) if k[0] in pids_shared):
        pl_id, ext_code = key
        if key in idx_a and key not in idx_b:
            missing_values.append(
                {
                    "picklist_id": pl_id,
                    "external_code": ext_code,
                    "label": idx_a[key]["label_en"],
                    "status": idx_a[key]["status"],
                    "diff_type": "only_in_a",
                }
            )
        elif key in idx_b and key not in idx_a:
            missing_values.append(
                {
                    "picklist_id": pl_id,
                    "external_code": ext_code,
                    "label": idx_b[key]["label_en"],
                    "status": idx_b[key]["status"],
                    "diff_type": "only_in_b",
                }
            )
        else:
            va, vb = idx_a[key], idx_b[key]
            field_diffs: list[dict] = []

            if compare_label_en and va["label_en"] != vb["label_en"]:
                field_diffs.append(
                    {
                        "field": "label_en",
                        "value_a": va["label_en"],
                        "value_b": vb["label_en"],
                    }
                )

            if compare_status and va["status"] != vb["status"]:
                field_diffs.append(
                    {
                        "field": "status",
                        "value_a": va["status"],
                        "value_b": vb["status"],
                    }
                )

            locales_to_check = (
                locale_filter
                if locale_filter is not None
                else (set(va["all_labels"]) | set(vb["all_labels"]))
            )
            for locale in sorted(locales_to_check):
                lva = va["all_labels"].get(locale, "")
                lvb = vb["all_labels"].get(locale, "")
                if lva != lvb:
                    field_diffs.append(
                        {"field": f"locale:{locale}", "value_a": lva, "value_b": lvb}
                    )

            if field_diffs:
                value_diffs.append(
                    {
                        "picklist_id": pl_id,
                        "external_code": ext_code,
                        "label_a": va["label_en"],
                        "label_b": vb["label_en"],
                        "field_diffs": field_diffs,
                    }
                )

    summary = {
        "picklists_only_in_a": len(pids_only_a),
        "picklists_only_in_b": len(pids_only_b),
        "picklists_shared": len(pids_shared),
        "missing_values_in_a": sum(
            1 for v in missing_values if v["diff_type"] == "only_in_b"
        ),
        "missing_values_in_b": sum(
            1 for v in missing_values if v["diff_type"] == "only_in_a"
        ),
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


def _load_fields_by_entity(
    instance_id: int, entity_filter: set | None = None
) -> dict[int, list]:
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


# ---------------------------------------------------------------------------
# N-tenant matrix comparison
# ---------------------------------------------------------------------------


def compare_instances_matrix(
    instance_ids: list[int],
    picklist_fields: set | None = None,
    entity_filter: set | None = None,
) -> dict:
    """
    Compare N instances (N >= 2) in a matrix view.

    Returns a structured result with per-instance values for every entity,
    field attribute, and picklist value - suitable for side-by-side matrix
    reports across DEV / QA / UAT / PROD.

    Zero changes to the existing compare_instances() function; this is a
    separate entry point that reuses the same low-level helpers.
    """
    if len(instance_ids) < 2:
        raise ValueError("compare_instances_matrix requires at least 2 instance IDs")

    # Load instance metadata for display
    from core.db import get_instance  # type: ignore[attr-defined]

    instances_meta: list[dict] = []
    for iid in instance_ids:
        inst = get_instance(iid)
        if inst is None:
            raise ValueError(f"Instance ID {iid} not found")
        instances_meta.append(
            {"id": iid, "alias": inst["alias"], "base_url": inst["base_url"]}
        )

    # --- Entity matrix ---
    all_entities: dict[int, dict] = {
        iid: _load_entities(iid, entity_filter) for iid in instance_ids
    }
    all_entity_names: set[str] = set()
    for emap in all_entities.values():
        all_entity_names.update(emap)

    entity_matrix: dict[str, dict] = {}
    for name in sorted(all_entity_names):
        present_in = [iid for iid in instance_ids if name in all_entities[iid]]
        missing_from = [iid for iid in instance_ids if name not in all_entities[iid]]
        entity_matrix[name] = {"present_in": present_in, "missing_from": missing_from}

    # --- Field matrix ---
    all_fields_by_instance: dict[int, dict[int, list]] = {
        iid: _load_fields_by_entity(iid, entity_filter) for iid in instance_ids
    }

    field_matrix: dict[str, dict] = {}
    fields_uniform = 0
    fields_with_diffs = 0

    for entity_name in sorted(all_entity_names):
        present_in = entity_matrix[entity_name]["present_in"]
        if len(present_in) < 2:
            continue

        # Build {field_id: {instance_id: field_row}} across all present instances
        field_union: dict[str, dict[int, dict]] = {}
        for iid in present_in:
            ent_row = all_entities[iid].get(entity_name)
            if ent_row is None:
                continue
            eid = ent_row["id"]
            for f in all_fields_by_instance[iid].get(eid, []):
                fid = f["field_id"]
                field_union.setdefault(fid, {})[iid] = f

        entity_fields: dict[str, dict] = {}
        for fid in sorted(field_union):
            by_instance = field_union[fid]
            field_label = next(iter(by_instance.values())).get("field_label", "")

            # Collect per-instance attribute values
            values: dict[int, dict] = {}
            for iid in instance_ids:
                if iid in by_instance:
                    values[iid] = {
                        attr: str(by_instance[iid].get(attr) or "")
                        for attr in FIELD_ATTRS
                    }
                else:
                    values[iid] = None  # field absent in this instance

            # Determine uniformity across instances that have the field
            present_values = [v for v in values.values() if v is not None]
            differing_attrs = []
            if len(present_values) >= 2:
                for attr in FIELD_ATTRS:
                    attr_vals = {pv[attr] for pv in present_values}
                    if len(attr_vals) > 1:
                        differing_attrs.append(attr)

            is_uniform = len(differing_attrs) == 0 and all(
                v is not None for v in values.values()
            )

            entity_fields[fid] = {
                "field_label": field_label,
                "values": values,
                "is_uniform": is_uniform,
                "differing_attrs": differing_attrs,
            }

            if is_uniform:
                fields_uniform += 1
            else:
                fields_with_diffs += 1

        if entity_fields:
            field_matrix[entity_name] = entity_fields

    # --- Picklist matrix ---
    picklist_matrix, pl_uniform, pl_diffs = _compare_picklists_matrix(
        instance_ids, picklist_fields
    )

    # --- Summary ---
    entities_in_all = sum(
        1 for v in entity_matrix.values() if len(v["missing_from"]) == 0
    )
    entities_with_gaps = len(entity_matrix) - entities_in_all

    return {
        "instances": instances_meta,
        "summary": {
            "total_instances": len(instance_ids),
            "entities_in_all": entities_in_all,
            "entities_with_gaps": entities_with_gaps,
            "fields_uniform": fields_uniform,
            "fields_with_diffs": fields_with_diffs,
            "picklist_values_uniform": pl_uniform,
            "picklist_values_with_diffs": pl_diffs,
        },
        "entity_matrix": entity_matrix,
        "field_matrix": field_matrix,
        "picklist_matrix": picklist_matrix,
    }


def _compare_picklists_matrix(
    instance_ids: list[int],
    fields: set | None,
) -> tuple[dict, int, int]:
    """
    Build per-instance picklist value matrix across N instances.

    Returns (picklist_matrix, uniform_count, diff_count).
    """
    import json as _json

    compare_label_en = fields is None or "label_en" in fields
    compare_status = fields is None or "status" in fields

    # Load all picklist rows for all instances in one query
    all_rows: dict[int, list] = {iid: [] for iid in instance_ids}
    if instance_ids:
        placeholders = ",".join("?" * len(instance_ids))
        with get_conn() as conn:
            rows = conn.execute(
                f"SELECT instance_id, picklist_id, external_code, option_id,"
                f" label_en, all_labels, status"
                f" FROM picklist_values WHERE instance_id IN ({placeholders})",
                instance_ids,
            ).fetchall()
        for row in rows:
            all_rows[row["instance_id"]].append(dict(row))

    # Index: (picklist_id, code) -> {instance_id: {label_en, status}}
    idx: dict[tuple, dict[int, dict]] = {}
    for iid, rows in all_rows.items():
        for r in rows:
            code = r["external_code"] or r["option_id"]
            key = (r["picklist_id"], code)
            try:
                all_labels = _json.loads(r["all_labels"] or "{}")
            except (_json.JSONDecodeError, TypeError):
                all_labels = {}
            idx.setdefault(key, {})[iid] = {
                "label_en": _clean_text(r["label_en"]),
                "status": _norm_status(r["status"]),
                "all_labels": {
                    _clean_text(k): _clean_text(v) for k, v in all_labels.items() if k
                },
            }

    picklist_matrix: dict[str, dict] = {}
    uniform_count = 0
    diff_count = 0

    for (pl_id, code), by_instance in sorted(idx.items()):
        entry: dict[str, dict] = {}

        if compare_label_en:
            label_vals = {
                iid: by_instance[iid]["label_en"] if iid in by_instance else None
                for iid in instance_ids
            }
            entry["label_en"] = label_vals

        if compare_status:
            status_vals = {
                iid: by_instance[iid]["status"] if iid in by_instance else None
                for iid in instance_ids
            }
            entry["status"] = status_vals

        # Uniformity: all present instances have same label_en and status
        present = [by_instance[iid] for iid in instance_ids if iid in by_instance]
        is_uniform = (
            len(present) == len(instance_ids)
            and (not compare_label_en or len({p["label_en"] for p in present}) == 1)
            and (not compare_status or len({p["status"] for p in present}) == 1)
        )
        entry["is_uniform"] = is_uniform

        picklist_matrix.setdefault(pl_id, {})[code] = entry

        if is_uniform:
            uniform_count += 1
        else:
            diff_count += 1

    return picklist_matrix, uniform_count, diff_count
