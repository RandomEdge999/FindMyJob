from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select

from findmyjob.core.config import AppConfig
from findmyjob.db.models import AnswerMemoryRecord
from findmyjob.filefirst.models import AnswerMemoryEntry, FileFact
from findmyjob.filefirst.workspace import FileWorkspace
from findmyjob.personal.onboarding import build_profile_facts, parse_personal_pack


def _build_cv_markdown(parsed) -> str:
    lines: list[str] = []
    name = str(parsed.contact.get("name") or "Candidate").strip()
    lines.append(f"# {name}")
    contact_bits = [
        parsed.contact.get("email"),
        parsed.contact.get("phone"),
        parsed.contact.get("linkedin"),
        parsed.contact.get("github"),
        parsed.contact.get("portfolio") or parsed.contact.get("website"),
    ]
    for bit in [str(item).strip() for item in contact_bits if str(item or "").strip()]:
        lines.append(f"- {bit}")
    if parsed.location:
        location_line = ", ".join(
            str(parsed.location.get(key) or "").strip()
            for key in ("city", "region_code", "country_code")
            if str(parsed.location.get(key) or "").strip()
        )
        if location_line:
            lines.append(f"- {location_line}")
    if parsed.skills:
        lines.append("")
        lines.append("## Skills")
        lines.append(", ".join(parsed.skills[:20]))
    if parsed.experiences:
        lines.append("")
        lines.append("## Experience")
        for item in parsed.experiences[:6]:
            title = str(item.get("title") or "").strip()
            company = str(item.get("company") or "").strip()
            header = " - ".join(bit for bit in (title, company) if bit)
            if header:
                lines.append(f"### {header}")
            summary = str(item.get("summary") or "").strip()
            if summary:
                lines.append(f"- {summary}")
            for bullet in list(item.get("bullets") or [])[:4]:
                cleaned = str(bullet).strip()
                if cleaned:
                    lines.append(f"- {cleaned}")
    if parsed.projects:
        lines.append("")
        lines.append("## Projects")
        for item in parsed.projects[:4]:
            project_name = str(item.get("name") or "").strip()
            if project_name:
                lines.append(f"### {project_name}")
            summary = str(item.get("summary") or item.get("description") or "").strip()
            if summary:
                lines.append(f"- {summary}")
    return "\n".join(lines).rstrip() + "\n"


def _candidate_summary(parsed) -> str | None:
    parts: list[str] = []
    if parsed.experiences:
        first = parsed.experiences[0]
        title = str(first.get("title") or "").strip()
        company = str(first.get("company") or "").strip()
        if title and company:
            parts.append(f"Most recent role: {title} at {company}.")
    if parsed.skills:
        parts.append("Core skills: " + ", ".join(parsed.skills[:8]) + ".")
    return " ".join(parts) or None


def _resolve_source_dir(workspace: Path, source_dir: Path | None) -> Path:
    if source_dir is not None:
        return source_dir.expanduser().resolve()
    candidate = (workspace / "my_personal_information").resolve()
    if candidate.exists():
        return candidate
    try:
        config = AppConfig.load(workspace)
        existing = config.personal_source_dir(workspace)
        if existing is not None and existing.exists():
            return existing.resolve()
    except Exception:
        pass
    raise ValueError("No personal source directory found. Pass --source-dir or place data in my_personal_information/.")


def _export_answer_memory(workspace: Path) -> list[AnswerMemoryEntry]:
    try:
        from findmyjob.core.runtime import AppRuntime

        runtime = AppRuntime.bootstrap(workspace)
    except Exception:
        return []
    with runtime.session_scope() as session:
        records = session.scalars(
            select(AnswerMemoryRecord).where(AnswerMemoryRecord.approved.is_(True)).order_by(AnswerMemoryRecord.created_at.desc())
        ).all()
    return [
        AnswerMemoryEntry(
            canonical_question=record.canonical_question,
            context_constraints=dict(record.context_constraints or {}),
            answer_text=record.answer_text,
            grounded_fact_ids=list(record.grounded_fact_ids or []),
            approved=bool(record.approved),
            created_at=record.created_at.isoformat() if record.created_at else "",
        )
        for record in records
    ]


def export_legacy_personal_material(workspace: Path, source_dir: Path | None = None) -> dict[str, Any]:
    root = workspace.resolve()
    ws = FileWorkspace(root)
    ws.ensure()

    resolved_source = _resolve_source_dir(root, source_dir)
    parsed = parse_personal_pack(resolved_source)
    facts = build_profile_facts(parsed)
    exported_facts = [
        FileFact(
            fact_id=fact.fact_id,
            kind=fact.kind.value if hasattr(fact.kind, "value") else str(fact.kind),
            payload=dict(fact.payload),
            sensitivity=fact.sensitivity.value if hasattr(fact.sensitivity, "value") else str(fact.sensitivity),
            allowed_for_generation=bool(fact.allowed_for_generation),
            disallowed=bool(fact.disallowed),
            provenance=fact.provenance,
            confirmed=bool(fact.confirmed),
        )
        for fact in facts
    ]
    ws.save_facts(exported_facts)
    ws.save_cv(_build_cv_markdown(parsed))

    profile = ws.load_profile()
    profile.candidate.name = str(parsed.contact.get("name") or profile.candidate.name or "").strip()
    profile.candidate.email = parsed.contact.get("email") or profile.candidate.email
    profile.candidate.phone = parsed.contact.get("phone") or profile.candidate.phone
    profile.candidate.linkedin = parsed.contact.get("linkedin") or profile.candidate.linkedin
    profile.candidate.github = parsed.contact.get("github") or profile.candidate.github
    profile.candidate.website = parsed.contact.get("portfolio") or parsed.contact.get("website") or profile.candidate.website
    location = ", ".join(
        str(parsed.location.get(key) or "").strip()
        for key in ("city", "region_code", "country_code")
        if str(parsed.location.get(key) or "").strip()
    )
    if location:
        profile.candidate.location = location
    profile.candidate.summary = _candidate_summary(parsed)
    profile.candidate.target_roles = list(parsed.preferences.get("job_role_families") or profile.candidate.target_roles)
    country_code = str(parsed.location.get("country_code") or "").strip()
    if country_code:
        profile.targets.countries = [country_code]

    try:
        legacy = AppConfig.load(root)
    except Exception:
        legacy = None

    if legacy is not None:
        search = getattr(legacy, "search", None)
        if search is not None:
            profile.targets.title_keywords = list(getattr(search, "title_keywords", None) or profile.targets.title_keywords)
            profile.targets.countries = list(getattr(search, "countries", None) or profile.targets.countries)
            profile.targets.remote_only = bool(getattr(search, "remote_only", profile.targets.remote_only))
            posted_within_days = getattr(search, "posted_within_days", None)
            if posted_within_days is not None:
                profile.targets.posted_within_days = int(posted_within_days)

        legacy_sources = getattr(legacy, "sources", {}) or {}
        greenhouse = legacy_sources.get("greenhouse")
        if any(legacy_sources.get(source_name) is not None for source_name in ("greenhouse", "lever", "ashby")):
            portals = ws.load_portals()
            enabled_sources: list[str] = []
            for source_name in ("greenhouse", "lever", "ashby"):
                source_settings = legacy_sources.get(source_name)
                if source_name not in portals.sources or source_settings is None:
                    continue
                portals.sources[source_name].enabled = bool(getattr(source_settings, "enabled", portals.sources[source_name].enabled))
                portals.sources[source_name].boards = list(dict.fromkeys(getattr(source_settings, "boards", []) or []))
                if portals.sources[source_name].enabled:
                    enabled_sources.append(source_name)
            ws.save_portals(portals)

            autonomous = getattr(legacy, "autonomous", None)
            automation = profile.runtime.automation
            profile.runtime.automation = automation.model_copy(update={
                "enabled": bool(getattr(autonomous, "enabled", automation.enabled)),
                "submit_enabled": any(bool(getattr(legacy_sources.get(source_name), "submit_enabled", False)) for source_name in ("greenhouse", "lever", "ashby")),
                "production_sources": enabled_sources or list(automation.production_sources) or ["greenhouse", "lever", "ashby"],
                "ready_to_apply_threshold": int(getattr(autonomous, "ready_to_apply_threshold", automation.ready_to_apply_threshold)),
                "browser_mode": "attached" if bool(getattr(greenhouse, "browser_attach_enabled", False)) else automation.browser_mode,
                "browser_attach_enabled": bool(getattr(greenhouse, "browser_attach_enabled", automation.browser_attach_enabled)),
                "browser_cdp_url": getattr(greenhouse, "browser_cdp_url", automation.browser_cdp_url),
                "daily_submit_cap": int(getattr(autonomous, "daily_submit_cap", automation.daily_submit_cap)),
                "per_company_daily_cap": int(getattr(autonomous, "per_company_daily_cap", automation.per_company_daily_cap)),
            })

    ws.save_profile(profile)

    answers = _export_answer_memory(root)
    ws.save_answer_memory(answers)
    return {
        "source_dir": str(resolved_source),
        "facts_exported": len(exported_facts),
        "answer_memory_exported": len(answers),
        "cv_path": ws.relative_path(ws.cv_path),
        "facts_path": ws.relative_path(ws.facts_path),
        "profile_path": ws.relative_path(ws.profile_path),
        "answer_memory_path": ws.relative_path(ws.answer_memory_path),
    }
