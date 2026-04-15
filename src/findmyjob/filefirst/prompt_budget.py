from __future__ import annotations

import json
from typing import Any

from findmyjob.filefirst.models import FileFact, WorkspaceProfile


def json_chars(value: Any) -> int:
    return len(json.dumps(value, indent=2, sort_keys=True, default=str))


def trim_text(value: str | None, *, limit: int) -> str:
    text = str(value or "").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    clipped = text[:limit].rsplit(" ", 1)[0].strip()
    return (clipped or text[:limit].strip()) + "\n...[truncated]"


def compact_strings(items: list[Any] | None, *, limit: int, item_limit: int = 120) -> list[str]:
    cleaned: list[str] = []
    for item in list(items or []):
        value = trim_text(str(item or "").strip(), limit=item_limit)
        if value and value not in cleaned:
            cleaned.append(value)
        if len(cleaned) >= limit:
            break
    return cleaned


def compact_profile_payload(profile: WorkspaceProfile) -> dict[str, Any]:
    candidate = profile.candidate
    targets = profile.targets

    candidate_payload: dict[str, Any] = {}
    if candidate.name.strip():
        candidate_payload["name"] = candidate.name.strip()
    if str(candidate.location or "").strip():
        candidate_payload["location"] = str(candidate.location).strip()
    if str(candidate.summary or "").strip():
        candidate_payload["summary"] = trim_text(candidate.summary, limit=220)
    if candidate.target_roles:
        candidate_payload["target_roles"] = compact_strings(candidate.target_roles, limit=6, item_limit=60)

    targets_payload: dict[str, Any] = {}
    if targets.title_keywords:
        targets_payload["title_keywords"] = compact_strings(targets.title_keywords, limit=8, item_limit=40)
    if targets.locations:
        targets_payload["locations"] = compact_strings(targets.locations, limit=6, item_limit=60)
    if targets.countries:
        targets_payload["countries"] = compact_strings(targets.countries, limit=4, item_limit=30)
    if targets.regions:
        targets_payload["regions"] = compact_strings(targets.regions, limit=4, item_limit=40)
    if targets.cities:
        targets_payload["cities"] = compact_strings(targets.cities, limit=6, item_limit=40)
    targets_payload["remote_only"] = bool(targets.remote_only)
    if targets.employment_types:
        targets_payload["employment_types"] = compact_strings(targets.employment_types, limit=4, item_limit=40)
    if targets.posted_within_days is not None:
        targets_payload["posted_within_days"] = int(targets.posted_within_days)

    return {
        "candidate": candidate_payload,
        "targets": targets_payload,
    }


def _fact_label(payload: dict[str, Any], *keys: str) -> str | None:
    values: list[str] = []
    for key in keys:
        text = str(payload.get(key) or "").strip()
        if text and text not in values:
            values.append(text)
    if not values:
        return None
    return " | ".join(values[:3])


def _fact_bullets(payload: dict[str, Any], *, limit: int, item_limit: int) -> list[str]:
    return compact_strings(list(payload.get("bullets") or []), limit=limit, item_limit=item_limit)


def compact_fact_payload(fact: FileFact, *, detail: str = "normal") -> dict[str, Any]:
    payload = fact.payload or {}
    detail_normal = detail != "tight"
    summary_limit = 220 if detail_normal else 140
    bullet_limit = 2 if detail_normal else 1
    bullet_chars = 140 if detail_normal else 90

    result: dict[str, Any] = {
        "fact_id": fact.fact_id,
        "kind": fact.kind,
    }

    if fact.kind == "contact":
        label = _fact_label(payload, "name", "email", "phone")
        if label:
            result["label"] = label
        links = compact_strings(
            [payload.get("linkedin"), payload.get("github"), payload.get("website")],
            limit=3,
            item_limit=120,
        )
        if links:
            result["links"] = links
        location = str(payload.get("location") or "").strip()
        if location:
            result["location"] = location
        return result

    if fact.kind == "authorization":
        status_bits: list[str] = []
        if "is_authorized" in payload:
            status_bits.append(f"authorized={bool(payload.get('is_authorized'))}")
        if "requires_future_sponsorship" in payload:
            status_bits.append(f"future_sponsorship={bool(payload.get('requires_future_sponsorship'))}")
        if "requires_now_sponsorship" in payload:
            status_bits.append(f"now_sponsorship={bool(payload.get('requires_now_sponsorship'))}")
        if status_bits:
            result["summary"] = "; ".join(status_bits)
        return result

    if fact.kind == "location":
        label = _fact_label(payload, "location", "city", "region", "country")
        if label:
            result["label"] = label
        return result

    if fact.kind == "education":
        label = _fact_label(payload, "school", "degree", "date_label")
        if label:
            result["label"] = label
        summary = str(payload.get("summary") or "").strip()
        if summary:
            result["summary"] = trim_text(summary, limit=summary_limit)
        return result

    if fact.kind in {"work", "project"}:
        if fact.kind == "work":
            label = _fact_label(payload, "title", "company", "date_label")
            if not label:
                label = _fact_label(payload, "title", "company", "start_date", "end_date")
        else:
            label = _fact_label(payload, "name", "date_label")
        if label:
            result["label"] = label
        summary = str(payload.get("summary") or payload.get("description") or "").strip()
        if summary:
            result["summary"] = trim_text(summary, limit=summary_limit)
        bullets = _fact_bullets(payload, limit=bullet_limit, item_limit=bullet_chars)
        if bullets:
            result["highlights"] = bullets
        skills = compact_strings(list(payload.get("skills") or []), limit=6 if detail_normal else 4, item_limit=40)
        if skills:
            result["skills"] = skills
        return result

    if fact.kind == "skill":
        label = _fact_label(payload, "name", "skill")
        if label:
            result["label"] = label
        category = str(payload.get("category") or "").strip()
        if category:
            result["category"] = category
        summary = str(payload.get("summary") or payload.get("description") or "").strip()
        if summary and summary.casefold() != str(label or "").casefold():
            result["summary"] = trim_text(summary, limit=90 if detail_normal else 60)
        return result

    label = _fact_label(payload, "name", "title", "value", "display")
    if label:
        result["label"] = label
    summary = str(payload.get("summary") or payload.get("description") or "").strip()
    if summary:
        result["summary"] = trim_text(summary, limit=summary_limit)
    return result

