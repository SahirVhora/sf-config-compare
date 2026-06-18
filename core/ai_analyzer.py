"""AI-powered analysis layer for SF Config Compare.

Generates natural-language summaries and risk assessments of comparison
results using OpenAI's API. Falls back gracefully if no API key is configured.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _get_openai_client() -> Any | None:
    """Return an OpenAI client if the API key is configured, else None."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        import openai
        return openai.OpenAI(api_key=api_key)
    except ImportError:
        logger.warning("openai package not installed; AI features disabled.")
        return None


def summarize_comparison(alias_a: str, alias_b: str, result: dict) -> dict:
    """Generate an AI summary of a comparison result.

    Returns a dict with:
      - overview: one-paragraph executive summary
      - risk_score: 0-100 score (higher = more risk)
      - top_concerns: list of the 3-5 most important findings
      - recommendations: list of actionable recommendations
    """
    client = _get_openai_client()
    if not client:
        return {
            "overview": "AI analysis unavailable. Set OPENAI_API_KEY to enable.",
            "risk_score": 0,
            "top_concerns": [],
            "recommendations": [],
            "ai_enabled": False,
        }

    s = result["summary"]
    entity_diffs = result["entity_diffs"]
    field_diffs = result["field_diffs"]
    picklist = result["picklist_result"]

    # Build a concise prompt
    prompt = _build_prompt(alias_a, alias_b, s, entity_diffs, field_diffs, picklist)

    try:
        response = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a principal SAP SuccessFactors engineer reviewing a "
                        "configuration diff between two SF tenants. Respond ONLY with valid JSON. "
                        "No markdown, no explanation, just JSON."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=1200,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        return {
            "overview": parsed.get("overview", ""),
            "risk_score": min(100, max(0, int(parsed.get("risk_score", 0)))),
            "top_concerns": parsed.get("top_concerns", []),
            "recommendations": parsed.get("recommendations", []),
            "ai_enabled": True,
            "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        }
    except Exception as exc:
        logger.exception("AI summary generation failed")
        return {
            "overview": f"AI analysis failed: {exc}",
            "risk_score": 0,
            "top_concerns": [],
            "recommendations": [],
            "ai_enabled": False,
            "error": str(exc),
        }


def _build_prompt(alias_a: str, alias_b: str, summary: dict, entity_diffs: list, field_diffs: list, picklist: dict) -> str:
    """Build a structured prompt for the AI summarizer."""
    missing_entities = [d["entity_name"] for d in entity_diffs if "only" in d["diff_type"]]
    attr_mismatches = [d for d in field_diffs if d["diff_type"] == "attribute_mismatch"]
    missing_fields = [d for d in field_diffs if "only" in d["diff_type"]]

    # Summarize attribute changes by type
    attr_summary: dict[str, int] = {}
    for d in attr_mismatches:
        attr = d.get("attribute", "unknown")
        attr_summary[attr] = attr_summary.get(attr, 0) + 1

    prompt_parts = [
        f"Compare SAP SuccessFactors tenants '{alias_a}' (source) vs '{alias_b}' (target).",
        "",
        "Summary statistics:",
        f"- Entities only in source: {summary.get('entities_only_in_a', 1)}",
        f"- Entities only in target: {summary.get('entities_only_in_b', 1)}",
        f"- Entities in both: {summary.get('entities_in_both', 1)}",
        f"- Fields matched: {summary.get('fields_matched', 1)}",
        f"- Fields with attribute differences: {summary.get('fields_with_diff', 1)}",
        f"- Fields only in source: {summary.get('fields_only_in_a', 1)}",
        f"- Fields only in target: {summary.get('fields_only_in_b', 1)}",
        f"- Missing picklists: {summary.get('picklists_only_in_a', 1) + summary.get('picklists_only_in_b', 1)}",
        f"- Missing picklist values: {summary.get('missing_values_in_a', 1) + summary.get('missing_values_in_b', 1)}",
        f"- Picklist value differences: {summary.get('value_diffs', 1)}",
        "",
    ]

    if missing_entities:
        prompt_parts.append(f"Missing entities: {', '.join(missing_entities[:20])}")
    if attr_summary:
        prompt_parts.append("Attribute changes: " + ", ".join(f"{k} ({v})" for k, v in sorted(attr_summary.items(), key=lambda x: -x[1])[:10]))
    if missing_fields:
        by_entity: dict[str, int] = {}
        for d in missing_fields:
            by_entity[d["entity_name"]] = by_entity.get(d["entity_name"], 0) + 1
        prompt_parts.append("Missing fields by entity: " + ", ".join(f"{k} ({v})" for k, v in sorted(by_entity.items(), key=lambda x: -x[1])[:10]))

    prompt_parts.extend([
        "",
        "Return a JSON object with exactly these keys:",
        '- "overview": string, one-paragraph executive summary',
        '- "risk_score": integer 1-100 (higher = more risky to deploy to target)',
        '- "top_concerns": array of strings, 3-5 most important findings',
        '- "recommendations": array of strings, 3-5 actionable next steps',
    ])

    return "\n".join(prompt_parts)
