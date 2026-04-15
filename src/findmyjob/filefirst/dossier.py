from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from findmyjob.filefirst.models import FileFact
from findmyjob.filefirst.workspace import FileWorkspace

_MAX_SECTION_ITEMS = 12
_MAX_SOURCE_CHARS = 4000


def _trim_text(value: str | None, *, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    clipped = text[:limit].rsplit(" ", 1)[0].strip()
    return (clipped or text[:limit].strip()) + "\n...[truncated]"


def _fact_summary(fact: FileFact) -> str:
    payload = fact.payload or {}
    keys = [
        "name",
        "title",
        "company",
        "school",
        "degree",
        "summary",
        "description",
        "date_label",
        "start_date",
        "end_date",
        "location",
        "display",
        "category",
        "value",
    ]
    parts: list[str] = []
    for key in keys:
        value = payload.get(key)
        text = str(value or "").strip()
        if text and text not in parts:
            parts.append(text)
    bullets = payload.get("bullets") or []
    if isinstance(bullets, list):
        bullet_text = "; ".join(str(item).strip() for item in bullets[:3] if str(item).strip())
        if bullet_text:
            parts.append(bullet_text)
    skills = payload.get("skills") or []
    if isinstance(skills, list):
        skill_text = ", ".join(str(item).strip() for item in skills[:8] if str(item).strip())
        if skill_text:
            parts.append(f"Skills: {skill_text}")
    return " | ".join(parts[:6])


def _sort_facts(facts: list[FileFact]) -> list[FileFact]:
    return sorted(facts, key=lambda item: item.fact_id)


def _facts_by_kind(facts: list[FileFact], kind: str) -> list[FileFact]:
    return [fact for fact in facts if fact.kind == kind and not fact.disallowed]


def _markdown_list(title: str, rows: list[str]) -> list[str]:
    lines = [f"## {title}"]
    if rows:
        lines.extend(f"- {row}" for row in rows)
    else:
        lines.append("- None recorded.")
    lines.append("")
    return lines


def _fact_rows(facts: list[FileFact], *, limit: int = _MAX_SECTION_ITEMS) -> list[str]:
    rows: list[str] = []
    for fact in _sort_facts(facts)[:limit]:
        summary = _fact_summary(fact)
        refs = ", ".join(str(item).strip() for item in list(fact.payload.get("source_refs") or [])[:4] if str(item).strip())
        line = f"`{fact.fact_id}`: {summary}" if summary else f"`{fact.fact_id}`"
        if refs:
            line = f"{line} [sources: {refs}]"
        rows.append(line)
    return rows


def _profile_preferences(workspace: FileWorkspace) -> list[str]:
    profile = workspace.load_profile()
    targets = profile.targets
    runtime = profile.runtime.automation
    rows = [
        f"Candidate name: {profile.display_name}",
        f"Target roles: {', '.join(profile.candidate.target_roles) or 'Not set'}",
        f"Title keywords: {', '.join(targets.title_keywords) or 'Not set'}",
        f"Locations: {', '.join(targets.locations) or 'Not set'}",
        f"Countries: {', '.join(targets.countries) or 'Not set'}",
        f"Remote only: {'yes' if targets.remote_only else 'no'}",
        f"Default submit mode: {runtime.default_submit_mode}",
        f"Automation submit enabled: {'yes' if runtime.submit_enabled else 'no'}",
        f"Daily submit cap: {runtime.daily_submit_cap}",
        f"Per-company daily cap: {runtime.per_company_daily_cap}",
    ]
    return rows


def _onboarding_notes(workspace: FileWorkspace) -> list[str]:
    manifest_path = workspace.fmj_dir / "onboarding" / "personal_onboarding.json"
    if not manifest_path.exists():
        return []
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows: list[str] = []
    for key in (
        "source_dir",
        "resume_renderer",
        "resume_template",
        "cover_letter_template",
        "cover_letter_reference",
    ):
        value = str(payload.get(key) or "").strip()
        if value:
            rows.append(f"{key}: {value}")
    for key in ("flagged_items", "skipped_items"):
        for item in list(payload.get(key) or [])[:8]:
            text = str(item).strip()
            if text:
                rows.append(f"{key}: {text}")
    return rows


def _source_excerpt(workspace: FileWorkspace) -> list[str]:
    rows: list[str] = []
    cv_text = _trim_text(workspace.load_cv(), limit=_MAX_SOURCE_CHARS)
    if cv_text:
        rows.append("## CV Excerpt\n```md\n" + cv_text + "\n```")
    personal_info = workspace.root / "my_personal_information" / "personal_info.txt"
    if personal_info.exists():
        rows.append("## Personal Info Excerpt\n```txt\n" + _trim_text(personal_info.read_text(encoding="utf-8"), limit=_MAX_SOURCE_CHARS) + "\n```")
    return rows


def build_candidate_dossier(workspace: FileWorkspace | Path) -> str:
    ws = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
    ws.ensure()
    facts = ws.load_facts()
    lines = [
        "# Candidate Dossier",
        "",
        "This file is operator-owned grounding context for resume and cover-letter generation.",
        "It is dense on purpose. Use it alongside the canonical facts, CV, and live job description.",
        "Never invent employers, projects, metrics, dates, skills, or visa details beyond what appears here or in the supplied facts.",
        "",
    ]
    lines.extend(_markdown_list("Operator Preferences", _profile_preferences(ws)))
    lines.extend(_markdown_list("Contact", _fact_rows(_facts_by_kind(facts, "contact"), limit=3)))
    lines.extend(_markdown_list("Authorization", _fact_rows(_facts_by_kind(facts, "authorization"), limit=3)))
    lines.extend(_markdown_list("Location", _fact_rows(_facts_by_kind(facts, "location"), limit=3)))
    lines.extend(_markdown_list("Education", _fact_rows(_facts_by_kind(facts, "education"), limit=6)))
    lines.extend(_markdown_list("Work History", _fact_rows(_facts_by_kind(facts, "work"), limit=8)))
    lines.extend(_markdown_list("Projects", _fact_rows(_facts_by_kind(facts, "project"), limit=8)))
    lines.extend(_markdown_list("Skills", _fact_rows(_facts_by_kind(facts, "skill"), limit=32)))
    lines.extend(_markdown_list("Preferences And Personal Context", _fact_rows(_facts_by_kind(facts, "preference") + _facts_by_kind(facts, "personal"), limit=8)))
    lines.extend(_markdown_list("Onboarding Notes", _onboarding_notes(ws)))
    lines.extend(_source_excerpt(ws))
    return "\n".join(line.rstrip() for line in lines).rstrip() + "\n"


def regenerate_candidate_dossier(workspace: FileWorkspace | Path) -> dict[str, Any]:
    ws = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
    ws.ensure()
    body = build_candidate_dossier(ws)
    ws.save_candidate_dossier(body)
    return candidate_dossier_metadata(ws) | {"saved": True}


def candidate_dossier_metadata(workspace: FileWorkspace | Path) -> dict[str, Any]:
    ws = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
    ws.ensure()
    path = ws.candidate_dossier_path
    exists = path.exists()
    updated_at = None
    size_bytes = 0
    if exists:
        stat = path.stat()
        updated_at = stat.st_mtime
        size_bytes = stat.st_size
    return {
        "path": ws.relative_path(path),
        "exists": exists,
        "size_bytes": size_bytes,
        "updated_at_epoch": updated_at,
    }


def dossier_excerpt(workspace: FileWorkspace | Path, *, limit: int = 6000) -> str:
    ws = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
    existing = ws.load_candidate_dossier()
    body = existing if existing is not None else build_candidate_dossier(ws)
    return _trim_text(body, limit=limit)
