"""Deterministic migration-readiness rules for configuration comparisons.

This module does not use statistical models or AI. It translates source-to-target
configuration drift into explainable, actionable findings for migration review.
"""

from __future__ import annotations

from typing import Any

SEVERITIES = ("critical", "high", "medium", "low", "info")
_SEVERITY_RANK = {severity: rank for rank, severity in enumerate(SEVERITIES)}
RULES_VERSION = "1.0.0"


def _finding(
    rule_id: str,
    severity: str,
    object_type: str,
    object_id: str,
    affected_field: str | None,
    source_value: Any,
    target_value: Any,
    reason: str,
    recommended_correction: str,
) -> dict:
    return {
        "rule_id": rule_id,
        "severity": severity,
        "object_type": object_type,
        "object_id": object_id,
        "affected_field": affected_field,
        "source_value": source_value,
        "target_value": target_value,
        "reason": reason,
        "recommended_correction": recommended_correction,
        "detection_method": "deterministic_rule",
    }


def _field_finding(source_alias: str, target_alias: str, diff: dict) -> dict:
    entity = diff["entity_name"]
    field_id = diff["field_id"]
    attribute = str(diff.get("attribute") or "")
    source_value = diff.get("value_a")
    target_value = diff.get("value_b")
    object_id = f"{entity}.{field_id}"

    if diff["diff_type"] == "only_in_a":
        return _finding(
            "CHK-CFG-003",
            "high",
            "field",
            object_id,
            field_id,
            "present",
            "missing",
            f"Field {object_id} exists in {source_alias} but is missing from {target_alias}; source data cannot be mapped to the same target field.",
            f"Confirm the field is in migration scope, then create or transport it to {target_alias} before loading data.",
        )
    if diff["diff_type"] == "only_in_b":
        return _finding(
            "CHK-CFG-004",
            "info",
            "field",
            object_id,
            field_id,
            "missing",
            "present",
            f"Field {object_id} exists only in {target_alias}; it has no equivalent in the compared {source_alias} configuration.",
            "Confirm whether the target-only field needs a default, derivation rule, or explicit exclusion from the migration mapping.",
        )

    rules = {
        "required": (
            "CHK-CFG-005",
            "high",
            f"Required-setting drift can make records accepted in {source_alias} fail validation or weaken controls in {target_alias}.",
            f"Align the required setting in {target_alias}, or document and test a deliberate target-specific rule before mock load.",
        ),
        "field_type": (
            "CHK-CFG-006",
            "high",
            f"Field type differs between {source_alias} and {target_alias}, which can reject or reinterpret migrated values.",
            f"Align the field type in {target_alias} or add an explicit, tested conversion to the migration mapping.",
        ),
        "picklist_id": (
            "CHK-CFG-008",
            "high",
            f"Picklist assignment differs between {source_alias} and {target_alias}, so valid source codes may not resolve consistently.",
            f"Align the picklist assignment in {target_alias} and reconcile source codes before loading data.",
        ),
        "visibility": (
            "CHK-CFG-009",
            "medium",
            f"Visibility differs between {source_alias} and {target_alias}, which can change who can review or maintain migrated values.",
            f"Confirm the intended visibility in {target_alias} with the process owner and test it with representative roles.",
        ),
        "is_custom": (
            "CHK-CFG-010",
            "medium",
            f"Custom-field classification differs between {source_alias} and {target_alias}, indicating a potentially non-equivalent field definition.",
            f"Verify that {object_id} is the intended target field and align its definition before finalising the mapping.",
        ),
    }

    if attribute == "max_length":
        severity = "medium"
        reason = f"Maximum length differs between {source_alias} and {target_alias}; migrated values need a length profile before load."
        try:
            if int(str(source_value)) > int(str(target_value)):
                severity = "high"
                reason = f"Target maximum length is smaller than the source for {object_id}; source values may be truncated or rejected."
        except (TypeError, ValueError):
            pass
        return _finding(
            "CHK-CFG-007",
            severity,
            "field_attribute",
            object_id,
            attribute,
            source_value,
            target_value,
            reason,
            f"Profile source value lengths, then increase {target_alias} capacity or add a documented transformation with rejection handling.",
        )

    rule_id, severity, reason, correction = rules.get(
        attribute,
        (
            "CHK-CFG-099",
            "low",
            f"Field attribute {attribute or 'unknown'} differs between {source_alias} and {target_alias}.",
            f"Review the difference and confirm the intended {target_alias} configuration before migration sign-off.",
        ),
    )
    return _finding(
        rule_id,
        severity,
        "field_attribute",
        object_id,
        attribute,
        source_value,
        target_value,
        reason,
        correction,
    )


def analyze_migration_readiness(
    source_alias: str, target_alias: str, comparison: dict
) -> dict:
    """Return deterministic source-to-target readiness findings.

    The output is deliberately separate from the optional AI summary. Every
    finding identifies its rule, evidence, rationale, and recommended correction.
    """
    findings: list[dict] = []

    for diff in comparison.get("entity_diffs", []):
        entity = diff["entity_name"]
        if diff["diff_type"] == "only_in_a":
            findings.append(
                _finding(
                    "CHK-CFG-001",
                    "high",
                    "entity",
                    entity,
                    None,
                    "present",
                    "missing",
                    f"Entity {entity} exists in {source_alias} but is missing from {target_alias}, blocking equivalent target mapping for its data.",
                    f"Confirm scope and transport or configure {entity} in {target_alias} before the next mock load.",
                )
            )
        elif diff["diff_type"] == "only_in_b":
            findings.append(
                _finding(
                    "CHK-CFG-002",
                    "info",
                    "entity",
                    entity,
                    None,
                    "missing",
                    "present",
                    f"Entity {entity} exists only in {target_alias} and has no equivalent in the compared {source_alias} configuration.",
                    "Confirm whether the target-only entity requires defaults, derived data, or exclusion from migration scope.",
                )
            )

    findings.extend(
        _field_finding(source_alias, target_alias, diff)
        for diff in comparison.get("field_diffs", [])
    )

    picklists = comparison.get("picklist_result", {})
    for diff in picklists.get("missing_picklists", []):
        picklist_id = diff["picklist_id"]
        source_only = diff["diff_type"] == "only_in_a"
        findings.append(
            _finding(
                "CHK-CFG-011" if source_only else "CHK-CFG-012",
                "high" if source_only else "info",
                "picklist",
                picklist_id,
                None,
                "present" if source_only else "missing",
                "missing" if source_only else "present",
                (
                    f"Picklist {picklist_id} exists in {source_alias} but is missing from {target_alias}; mapped source codes cannot resolve."
                    if source_only
                    else f"Picklist {picklist_id} exists only in {target_alias} and has no compared source equivalent."
                ),
                (
                    f"Create or transport {picklist_id} to {target_alias}, then reconcile its values before mock load."
                    if source_only
                    else "Confirm whether the target-only picklist needs defaults or should be excluded from migration scope."
                ),
            )
        )

    for diff in picklists.get("missing_values", []):
        picklist_id = diff["picklist_id"]
        code = diff["external_code"]
        source_only = diff["diff_type"] == "only_in_a"
        active = str(diff.get("status", "")).upper() == "ACTIVE"
        findings.append(
            _finding(
                "CHK-CFG-013" if source_only else "CHK-CFG-014",
                "high"
                if source_only and active
                else ("low" if source_only else "info"),
                "picklist_value",
                f"{picklist_id}.{code}",
                "external_code",
                "present" if source_only else "missing",
                "missing" if source_only else "present",
                (
                    f"{'Active ' if active else ''}source picklist code {code} in {picklist_id} is missing from {target_alias}; records using it may fail or lose meaning."
                    if source_only
                    else f"Picklist code {code} in {picklist_id} exists only in {target_alias}."
                ),
                (
                    f"Add or map code {code} in {target_alias}, then validate usage counts and dependent mappings before load."
                    if source_only
                    else "Confirm whether the target-only code needs a source mapping, default, or explicit exclusion."
                ),
            )
        )

    for value_diff in picklists.get("value_diffs", []):
        picklist_id = value_diff["picklist_id"]
        code = value_diff["external_code"]
        for diff in value_diff.get("field_diffs", []):
            field = diff["field"]
            status_change = field == "status"
            findings.append(
                _finding(
                    "CHK-CFG-015" if status_change else "CHK-CFG-016",
                    "medium" if status_change else "low",
                    "picklist_value",
                    f"{picklist_id}.{code}",
                    field,
                    diff.get("value_a"),
                    diff.get("value_b"),
                    (
                        f"Status for picklist code {code} in {picklist_id} differs between {source_alias} and {target_alias}; availability during load may differ."
                        if status_change
                        else f"Label data for picklist code {code} in {picklist_id} differs between {source_alias} and {target_alias}."
                    ),
                    (
                        f"Align or deliberately map the status in {target_alias}, then test records that use code {code}."
                        if status_change
                        else f"Confirm the approved label in {target_alias} with the data owner and update it if the difference is unintended."
                    ),
                )
            )

    findings.sort(
        key=lambda finding: (
            _SEVERITY_RANK[finding["severity"]],
            finding["rule_id"],
            finding["object_id"],
            finding["affected_field"] or "",
        )
    )
    counts = {severity: 0 for severity in SEVERITIES}
    for finding in findings:
        counts[finding["severity"]] += 1
    highest = next((severity for severity in SEVERITIES if counts[severity]), None)

    return {
        "analysis_type": "deterministic_rules",
        "uses_ai": False,
        "rules_version": RULES_VERSION,
        "direction": {"source": source_alias, "target": target_alias},
        "summary": {
            "total": len(findings),
            "highest_severity": highest,
            "by_severity": counts,
        },
        "findings": findings,
    }
