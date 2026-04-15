from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from anyio import run as run_async

from findmyjob.core.enums import ModelRole
from findmyjob.core.types import ArtifactDraft
from findmyjob.filefirst.advanced_models import load_model_router
from findmyjob.filefirst.dossier import dossier_excerpt
from findmyjob.filefirst.models import EvaluationResult, FileFact, InboxJob, ResumePlan, WorkspaceProfile
from findmyjob.filefirst.prompt_budget import compact_fact_payload, compact_profile_payload, compact_strings, json_chars, trim_text
from findmyjob.filefirst.workspace import FileWorkspace
from findmyjob.sources.normalizer import normalize_text

_DRAFT_PROMPT_CHAR_BUDGET = 10_500
_COVER_LETTER_PARAGRAPH_COUNT = 3
_DRAFT_CONTEXT_TIERS = (
    {
        "job_chars": 3000,
        "cv_chars": 1200,
        "dossier_chars": 1000,
        "work_limit": 5,
        "project_limit": 3,
        "skill_limit": 15,
        "detail": "normal",
        "summary_chars": 600,
    },
    {
        "job_chars": 2200,
        "cv_chars": 800,
        "dossier_chars": 700,
        "work_limit": 4,
        "project_limit": 2,
        "skill_limit": 15,
        "detail": "tight",
        "summary_chars": 450,
    },
    {
        "job_chars": 1600,
        "cv_chars": 0,
        "dossier_chars": 500,
        "work_limit": 3,
        "project_limit": 2,
        "skill_limit": 15,
        "detail": "tight",
        "summary_chars": 320,
    },
)
_PLACEHOLDER_RE = re.compile(r"(todo|lorem ipsum|\{[^\n]+\}|\[[^\n]*insert[^\n]*\]|\[redacted-[^\]]+\])", re.IGNORECASE)
_PLACEHOLDER_NAME_RE = re.compile(r"\b(Your Name|First Last|Candidate Name|Full Name|John Doe|Jane Doe)\b", re.IGNORECASE)
_MOJIBAKE_RE = re.compile(
    r"[\xc0-\xff]{2,}"           # consecutive high-byte Latin chars (common UTF-8-as-Latin1 artifact)
    r"|\u00e2\u0080[\u0090-\u009f]"  # â€x sequences (smart quotes/dashes decoded as CP1252)
    r"|\u00c3[\u00a0-\u00bf]"        # Ã followed by Latin-1 supplement (double-encoded UTF-8)
    r"|\ufffd"                       # Unicode replacement character
)


def _fact_text(fact: FileFact) -> str:
    payload = fact.payload or {}
    parts: list[str] = []
    for value in payload.values():
        if isinstance(value, list):
            parts.extend(str(item) for item in value if str(item).strip())
        else:
            text = str(value or "").strip()
            if text:
                parts.append(text)
    return " ".join(parts)


def _job_terms(job: InboxJob, evaluation: EvaluationResult) -> set[str]:
    text = " ".join([job.title, job.description or "", evaluation.summary, *evaluation.keywords, *evaluation.fit_reasons])
    normalized = re.sub(r"[^a-z0-9]+", " ", text.casefold())
    return {token for token in normalized.split() if len(token) > 2}


def _fact_score(fact: FileFact, terms: set[str]) -> int:
    haystack = re.sub(r"[^a-z0-9]+", " ", _fact_text(fact).casefold())
    return len({token for token in haystack.split() if len(token) > 2} & terms)


def _profile_payload(profile: WorkspaceProfile) -> dict[str, Any]:
    return compact_profile_payload(profile)


def _fact_payload(fact: FileFact, *, detail: str) -> dict[str, Any]:
    return compact_fact_payload(fact, detail=detail)


def _draft_facts(
    facts: list[FileFact],
    job: InboxJob,
    evaluation: EvaluationResult,
    *,
    work_limit: int,
    project_limit: int,
    skill_limit: int,
) -> list[FileFact]:
    terms = _job_terms(job, evaluation)
    keep: list[FileFact] = []
    seen: set[str] = set()

    def add(items: list[FileFact]) -> None:
        for item in items:
            if item.fact_id in seen or item.disallowed:
                continue
            seen.add(item.fact_id)
            keep.append(item)

    essentials = [fact for fact in facts if fact.kind in {"contact", "authorization", "location", "education"}]
    ranked_work = sorted([fact for fact in facts if fact.kind == "work"], key=lambda item: (_fact_score(item, terms), item.fact_id), reverse=True)
    ranked_projects = sorted([fact for fact in facts if fact.kind == "project"], key=lambda item: (_fact_score(item, terms), item.fact_id), reverse=True)
    ranked_skills = sorted([fact for fact in facts if fact.kind == "skill"], key=lambda item: (_fact_score(item, terms), item.fact_id), reverse=True)
    add(essentials)
    add(ranked_work[:work_limit])
    add(ranked_projects[:project_limit])
    add(ranked_skills[:skill_limit])
    return keep


def _clean_list(items: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    for item in list(items or []):
        value = str(item or "").strip()
        if value and value not in cleaned:
            cleaned.append(value)
    return cleaned


def _line_count(items: list[str] | None) -> int:
    count = 0
    for item in _clean_list(items):
        count += len([line for line in str(item).splitlines() if line.strip()]) or 1
    return count


def _strip_truncation_marker(text: str | None) -> str:
    return re.sub(r"\s*\.\.\.\[truncated\]\s*", " ", str(text or "")).strip()


def _preferred_skill_fact_ids(facts: list[FileFact], selected_ids: list[str]) -> list[str]:
    facts_by_id = {fact.fact_id: fact for fact in facts if not fact.disallowed}
    categorized = [
        fact_id
        for fact_id in selected_ids
        if (
            fact_id in facts_by_id
            and str(facts_by_id[fact_id].payload.get("category") or "").strip().casefold()
            not in {"", "technical", "general"}
        )
    ]
    if categorized:
        for fact in facts:
            if fact.kind != "skill" or fact.disallowed:
                continue
            category = str(fact.payload.get("category") or "").strip().casefold()
            if category in {"", "technical", "general"} or fact.fact_id in categorized:
                continue
            categorized.append(fact.fact_id)
        return categorized
    return selected_ids


def _build_writer_input(
    workspace: FileWorkspace,
    job: InboxJob,
    evaluation: EvaluationResult,
    profile: WorkspaceProfile,
    facts: list[FileFact],
    *,
    job_chars: int,
    cv_chars: int,
    dossier_chars: int,
    summary_chars: int,
    detail: str,
    repair_issues: list[str] | None = None,
) -> dict[str, Any]:
    payload = {
        "job": {
            "company": job.company,
            "title": job.title,
            "location": job.location,
            "source": job.source,
            "url": job.url,
            "description": trim_text(job.description, limit=job_chars),
        },
        "evaluation": {
            "score": evaluation.score,
            "grade": evaluation.grade,
            "summary": trim_text(evaluation.summary, limit=summary_chars),
            "keywords": compact_strings(list(evaluation.keywords), limit=8, item_limit=50),
            "fit_reasons": compact_strings(list(evaluation.fit_reasons), limit=4, item_limit=120),
            "gaps": compact_strings(list(evaluation.gaps), limit=3, item_limit=120),
        },
        "profile": _profile_payload(profile),
        "facts": [_fact_payload(fact, detail=detail) for fact in facts],
        "cv_markdown": trim_text(workspace.load_cv(), limit=cv_chars),
        "candidate_dossier_markdown": dossier_excerpt(workspace, limit=dossier_chars),
        "schema": {
            "resume_draft": ["headline", "summary_lines", "selected_work_fact_ids", "selected_project_fact_ids", "selected_skill_fact_ids", "custom_bullets"],
            "cover_letter_draft": ["salutation", "paragraphs_exactly_3", "closing", "signature_name"],
            "adaptation_summary": "string|null",
        },
    }
    if repair_issues:
        payload["repair_issues"] = list(repair_issues)
        payload["repair_instruction"] = "Revise the draft to fix every listed deterministic validation issue while staying grounded only in the supplied facts, dossier, CV, and job description."
    return payload


async def _generate_json_with_first_role(
    router,
    roles: list[ModelRole],
    prompt: str,
    *,
    system_prompt: str,
    json_schema: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    last_error: Exception | None = None
    for role in roles:
        try:
            router.get_profile(role=role)
        except Exception as exc:
            last_error = exc
            continue
        try:
            return await router.generate_json_with_profile(
                role,
                prompt,
                system_prompt=system_prompt,
                json_schema=json_schema,
            )
        except Exception as exc:
            last_error = exc
    raise RuntimeError(str(last_error) if last_error is not None else "No usable model role is configured.")


def _valid_fact_ids(facts: list[FileFact], kind: str) -> list[str]:
    return [fact.fact_id for fact in facts if fact.kind == kind and not fact.disallowed]


def _resolved_fact_ids(selected_ids: list[str] | None, valid_ids: list[str], *, default_limit: int) -> list[str]:
    valid_set = set(valid_ids)
    resolved = [fact_id for fact_id in _clean_list(selected_ids) if fact_id in valid_set]
    if len(resolved) >= default_limit:
        return resolved[:default_limit]
    for fact_id in valid_ids:
        if fact_id in resolved:
            continue
        resolved.append(fact_id)
        if len(resolved) >= default_limit:
            break
    return resolved[:default_limit]


def _apply_draft_defaults(draft: ArtifactDraft, facts: list[FileFact], profile: WorkspaceProfile, job: InboxJob) -> None:
    work_ids = _valid_fact_ids(facts, "work")
    project_ids = _valid_fact_ids(facts, "project")
    skill_ids = _valid_fact_ids(facts, "skill")
    draft.resume_draft.summary_lines = _clean_list(draft.resume_draft.summary_lines)
    draft.resume_draft.custom_bullets = _clean_list(draft.resume_draft.custom_bullets)
    draft.resume_draft.selected_work_fact_ids = _resolved_fact_ids(draft.resume_draft.selected_work_fact_ids, work_ids, default_limit=4)
    draft.resume_draft.selected_project_fact_ids = _resolved_fact_ids(draft.resume_draft.selected_project_fact_ids, project_ids, default_limit=3)
    resolved_skill_ids = _resolved_fact_ids(draft.resume_draft.selected_skill_fact_ids, skill_ids, default_limit=10)
    draft.resume_draft.selected_skill_fact_ids = _preferred_skill_fact_ids(facts, resolved_skill_ids)
    draft.cover_letter_draft.paragraphs = _clean_list(draft.cover_letter_draft.paragraphs)
    if not draft.cover_letter_draft.salutation:
        draft.cover_letter_draft.salutation = f"Dear {job.company} Hiring Team,"
    if not draft.cover_letter_draft.closing:
        draft.cover_letter_draft.closing = "Sincerely,"
    if not draft.cover_letter_draft.signature_name:
        draft.cover_letter_draft.signature_name = profile.candidate.name or "Candidate"


def _normalize_sentence(value: str | None, *, limit: int = 220) -> str:
    text = _strip_truncation_marker(trim_text(value, limit=limit)).replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return ""
    if text[-1] not in ".!?":
        text += "."
    return text


def _job_echo(value: str | None, job: InboxJob, *, include_title: bool = False) -> bool:
    normalized = normalize_text(str(value or "")).casefold()
    if not normalized:
        return False
    description = normalize_text(trim_text(job.description, limit=4000)).casefold()
    title = normalize_text(str(job.title or "")).casefold()
    company = normalize_text(str(job.company or "")).casefold()
    if description and normalized in description:
        return True
    if any(token in normalized for token in ("is seeking", "we are looking", "you might thrive", "about the role", "about the team")):
        return True
    if company and company in normalized and len(normalized.split()) > 4:
        return True
    if include_title and title and normalized == title:
        return True
    return False


def _cover_letter_job_echo(value: str | None, job: InboxJob) -> bool:
    normalized = normalize_text(str(value or "")).casefold()
    if not normalized:
        return False
    description = normalize_text(_strip_truncation_marker(trim_text(job.description, limit=4000))).casefold()
    if description and normalized in description:
        return True
    suspicious_phrases = (
        "tech stack",
        "responsibilities",
        "requirements",
        "qualifications",
        "about the role",
        "about the team",
        "what you'll do",
        "you will",
        "you'll",
        "we are looking",
        "we're looking",
    )
    return any(phrase in normalized for phrase in suspicious_phrases)


def _selected_fact(facts: list[FileFact], selected_ids: list[str] | None) -> FileFact | None:
    facts_by_id = {fact.fact_id: fact for fact in facts if not fact.disallowed}
    for fact_id in _clean_list(selected_ids):
        fact = facts_by_id.get(fact_id)
        if fact is not None:
            return fact
    return None


def _skill_label(fact: FileFact | None) -> str:
    if fact is None:
        return ""
    category = str(fact.payload.get("category") or "").strip()
    name = str(fact.payload.get("name") or fact.payload.get("skill") or "").strip()
    if category and category.casefold() not in {"technical", "general"}:
        return category
    return name


def _grounded_resume_headline(draft: ArtifactDraft, facts: list[FileFact], profile: WorkspaceProfile, job: InboxJob) -> str:
    title = re.split(r"\s*[|,-]\s*", str(job.title or "").strip(), maxsplit=1)[0].strip() or "Software Engineer"
    skills: list[str] = []
    facts_by_id = {fact.fact_id: fact for fact in facts if not fact.disallowed}
    for fact_id in _clean_list(draft.resume_draft.selected_skill_fact_ids):
        fact = facts_by_id.get(fact_id)
        if fact is None:
            continue
        skill = _skill_label(fact)
        if skill and skill not in skills:
            skills.append(skill)
    if not skills and str(profile.candidate.summary or "").strip():
        summary_tokens = re.findall(r"[A-Za-z][A-Za-z+./-]{2,}", str(profile.candidate.summary))
        for token in summary_tokens:
            if token.lower() in {"most", "recent", "role", "core", "skills", "local", "tooling", "experience"}:
                continue
            if token not in skills:
                skills.append(token)
            if len(skills) >= 3:
                break
    if skills:
        return f"{title} | {', '.join(skills[:3])}"
    return title


def _grounded_resume_summary_lines(draft: ArtifactDraft, facts: list[FileFact], profile: WorkspaceProfile) -> list[str]:
    lines: list[str] = []
    candidate_summary = _normalize_sentence(profile.candidate.summary, limit=180)
    if candidate_summary:
        lines.append(candidate_summary)
    selected_work = _normalize_sentence(_fact_excerpt(_selected_fact(facts, draft.resume_draft.selected_work_fact_ids)), limit=220)
    if selected_work and selected_work not in lines:
        lines.append(selected_work)
    selected_project = _normalize_sentence(_fact_excerpt(_selected_fact(facts, draft.resume_draft.selected_project_fact_ids)), limit=200)
    if selected_project and selected_project not in lines:
        lines.append(selected_project)
    skills: list[str] = []
    facts_by_id = {fact.fact_id: fact for fact in facts if not fact.disallowed}
    for fact_id in _clean_list(draft.resume_draft.selected_skill_fact_ids):
        fact = facts_by_id.get(fact_id)
        if fact is None:
            continue
        skill = _skill_label(fact)
        if skill and skill not in skills:
            skills.append(skill)
    if skills:
        lines.append(_normalize_sentence(f"Core skills include {', '.join(skills[:4])}.", limit=180))
    return _clean_list(lines)[:4]


def _display_company_name(value: str | None) -> str:
    company = str(value or "").strip()
    if not company:
        return "the company"
    if company == company.casefold():
        return company.title()
    return company


def _rewrite_summary_to_first_person(summary_sentence: str, profile: WorkspaceProfile) -> str:
    candidate_name = str(profile.candidate.name or "").strip()
    rewritten = str(summary_sentence or "").strip()
    if candidate_name and rewritten.lower().startswith(candidate_name.lower()):
        rewritten = re.sub(
            rf'^{re.escape(candidate_name)}\s+is\s+',
            'I am ',
            rewritten,
            count=1,
            flags=re.IGNORECASE,
        )
    rewritten = re.sub(r'\bhis\b', 'my', rewritten, flags=re.IGNORECASE)
    rewritten = re.sub(r'\bher\b', 'my', rewritten, flags=re.IGNORECASE)
    return rewritten


def _cover_letter_skill_pitch(
    facts: list[FileFact],
    selected_skill_fact_ids: list[str] | None,
    profile: WorkspaceProfile,
) -> str:
    facts_by_id = {fact.fact_id: fact for fact in facts if not fact.disallowed}
    labels: list[str] = []
    for fact_id in _clean_list(selected_skill_fact_ids):
        fact = facts_by_id.get(fact_id)
        if fact is None:
            continue
        category = str(fact.payload.get("category") or "").strip()
        name = str(fact.payload.get("name") or fact.payload.get("skill") or "").strip()
        label = category if category and category.casefold() not in {"technical", "general"} else name.split(",")[0].strip()
        if label and label not in labels:
            labels.append(label)
        if len(labels) >= 3:
            break
    if not labels:
        summary = _normalize_sentence(profile.candidate.summary, limit=180)
        if summary:
            return summary.rstrip(".")
        return "software engineering and machine learning"
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return f"{labels[0]}, {labels[1]}, and {labels[2]}"


def _job_focus_sentence(job: InboxJob) -> str:
    description = normalize_text(str(job.description or ""))
    lowered = description.casefold()
    focus_areas = [
        ("billing and payments systems", ("billing", "payments", "payment", "stripe", "subscription", "commerce")),
        ("backend systems and APIs", ("backend", "api", "service", "services", "platform", "distributed")),
        ("applied machine learning and evaluation", ("machine learning", "ml", "model", "evaluation", "llm", "nlp", "ai")),
        ("data pipelines and analytics", ("data pipeline", "etl", "analytics", "warehouse", "telemetry", "dashboard")),
        ("developer tooling and automation", ("automation", "developer experience", "tooling", "workflow", "ci", "runbook")),
        ("customer-facing technical support", ("support", "customer", "incident", "troubleshooting", "escalation")),
        ("security and access control", ("security", "auth", "rbac", "identity", "compliance")),
    ]
    matches = [label for label, terms in focus_areas if any(term in lowered for term in terms)]
    if not matches:
        return "pragmatic software engineering"
    if len(matches) == 1:
        return matches[0]
    return ", ".join(matches[:2])


def _artifact_draft_json_schema() -> dict[str, Any]:
    string_array = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "properties": {
            "resume_draft": {
                "type": "object",
                "properties": {
                    "headline": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "summary_lines": {**string_array, "maxItems": 4},
                    "selected_work_fact_ids": string_array,
                    "selected_project_fact_ids": string_array,
                    "selected_skill_fact_ids": string_array,
                    "custom_bullets": {**string_array, "maxItems": 4},
                },
                "required": [
                    "headline",
                    "summary_lines",
                    "selected_work_fact_ids",
                    "selected_project_fact_ids",
                    "selected_skill_fact_ids",
                    "custom_bullets",
                ],
                "additionalProperties": True,
            },
            "cover_letter_draft": {
                "type": "object",
                "properties": {
                    "salutation": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "paragraphs": {
                        **string_array,
                        "minItems": _COVER_LETTER_PARAGRAPH_COUNT,
                        "maxItems": _COVER_LETTER_PARAGRAPH_COUNT,
                    },
                    "closing": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "signature_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["salutation", "paragraphs", "closing", "signature_name"],
                "additionalProperties": True,
            },
            "adaptation_summary": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["resume_draft", "cover_letter_draft"],
        "additionalProperties": True,
    }


def _fact_excerpt(fact: FileFact | None) -> str:
    if fact is None:
        return ""
    payload = fact.payload or {}
    if fact.kind == "work":
        title = str(payload.get("title") or "").strip()
        company = str(payload.get("company") or "").strip()
        summary_source = str(payload.get("summary") or payload.get("description") or "")
        first_line = next((line.strip() for line in summary_source.splitlines() if line.strip()), summary_source)
        summary = _normalize_sentence(first_line, limit=220)
        label = " at ".join(part for part in [title, company] if part).strip()
        if label and summary:
            return f"{label}: {summary}"
        return label or summary
    if fact.kind == "project":
        name = str(payload.get("name") or "Project").strip()
        summary_source = str(payload.get("summary") or payload.get("description") or "")
        first_line = next((line.strip() for line in summary_source.splitlines() if line.strip()), summary_source)
        summary = _normalize_sentence(first_line, limit=220)
        if name and summary:
            return f"{name}: {summary}"
        return name or summary
    if fact.kind == "skill":
        return str(payload.get("name") or payload.get("skill") or "").strip()
    return _normalize_sentence(payload.get("summary") or payload.get("description"), limit=180)


def _deterministic_cover_letter_paragraphs(
    draft: ArtifactDraft,
    facts: list[FileFact],
    profile: WorkspaceProfile,
    job: InboxJob,
    evaluation: EvaluationResult,
) -> list[str]:
    company = _display_company_name(job.company or evaluation.company or "the company")
    role = str(job.title or evaluation.role or "the role").strip()
    facts_by_id = {fact.fact_id: fact for fact in facts if not fact.disallowed}
    selected_work = next((facts_by_id.get(fact_id) for fact_id in _clean_list(draft.resume_draft.selected_work_fact_ids)), None)
    selected_project = next((facts_by_id.get(fact_id) for fact_id in _clean_list(draft.resume_draft.selected_project_fact_ids)), None)
    skill_pitch = _cover_letter_skill_pitch(facts, draft.resume_draft.selected_skill_fact_ids, profile)
    summary_sentence = _normalize_sentence(profile.candidate.summary or evaluation.summary, limit=320)
    summary_sentence = _rewrite_summary_to_first_person(summary_sentence, profile)
    fit_sentence = _normalize_sentence(next(iter(_clean_list(evaluation.fit_reasons)), ""), limit=220)
    work_sentence = _normalize_sentence(_fact_excerpt(selected_work), limit=240)
    project_sentence = _normalize_sentence(_fact_excerpt(selected_project), limit=220)
    focus_sentence = _job_focus_sentence(job)

    paragraphs: list[str] = []
    intro_parts = []
    if summary_sentence:
        intro_parts.append(summary_sentence)
    intro_parts.append(
        f"The {role} opportunity at {company} stands out to me because it aligns with how I approach {focus_sentence}."
    )
    paragraphs.append(" ".join(part.strip() for part in intro_parts if part.strip()))

    experience_parts = []
    if work_sentence:
        experience_parts.append(work_sentence)
    if project_sentence:
        experience_parts.append(project_sentence)
    if skill_pitch:
        experience_parts.append(f"My strongest supporting background is in {skill_pitch}.")
    if not experience_parts and fit_sentence:
        experience_parts.append(fit_sentence)
    paragraphs.append(
        " ".join(part.strip() for part in experience_parts if part.strip())
        or f"I would bring a grounded software engineering approach to {company}'s {role} work."
    )

    closing_parts = [
        f"I am drawn to {company}'s mission and believe my background in {skill_pitch} positions me well to contribute to the team.",
        "I am eager to contribute meaningfully from day one and grow alongside the team.",
        f"Thank you for your time and consideration for the {role} role.",
    ]
    paragraphs.append(" ".join(part.strip() for part in closing_parts if part.strip()))
    return [
        _normalize_sentence(paragraph, limit=600)
        for paragraph in paragraphs[:_COVER_LETTER_PARAGRAPH_COUNT]
        if paragraph.strip()
    ]


def _apply_grounded_repairs(
    draft: ArtifactDraft,
    facts: list[FileFact],
    profile: WorkspaceProfile,
    job: InboxJob,
    evaluation: EvaluationResult,
) -> None:
    company = _display_company_name(job.company or evaluation.company or "")
    headline = str(draft.resume_draft.headline or "").strip()
    if not headline or str(profile.candidate.name or "").strip().casefold() in headline.casefold() or _job_echo(headline, job, include_title=True):
        draft.resume_draft.headline = _grounded_resume_headline(draft, facts, profile, job)
    summary_lines = _clean_list(draft.resume_draft.summary_lines)
    if summary_lines and any(_job_echo(line, job) for line in summary_lines):
        fallback_summary = _grounded_resume_summary_lines(draft, facts, profile)
        if fallback_summary:
            draft.resume_draft.summary_lines = fallback_summary
    elif summary_lines:
        draft.resume_draft.summary_lines = summary_lines[:4]
    else:
        fallback_summary = _grounded_resume_summary_lines(draft, facts, profile)
        if fallback_summary:
            draft.resume_draft.summary_lines = fallback_summary
    cover_paragraphs = _clean_list(draft.cover_letter_draft.paragraphs)
    cover_body = "\n".join(cover_paragraphs)
    candidate_name = str(profile.candidate.name or "").strip()
    needs_cover_fallback = (
        len(cover_paragraphs) != _COVER_LETTER_PARAGRAPH_COUNT
        or any(_cover_letter_job_echo(paragraph, job) for paragraph in cover_paragraphs)
        or (candidate_name and candidate_name.casefold() in cover_body.casefold())
        or bool(re.search(r"\b(he|she|his|her)\b", cover_body, flags=re.IGNORECASE))
    )
    if needs_cover_fallback:
        draft.cover_letter_draft.paragraphs = _deterministic_cover_letter_paragraphs(draft, facts, profile, job, evaluation)
    if not str(draft.cover_letter_draft.salutation or "").strip() or (company and company.casefold() not in str(draft.cover_letter_draft.salutation or "").casefold()):
        draft.cover_letter_draft.salutation = f"Dear {company or 'Hiring'} Hiring Team,"
    draft.cover_letter_draft.closing = "Sincerely,"
    signature_name = str(draft.cover_letter_draft.signature_name or "").strip()
    if (
        not signature_name
        or "\n" in signature_name
        or re.search(r"\b(sincerely|regards|thanks|best)\b", signature_name, flags=re.IGNORECASE)
    ):
        draft.cover_letter_draft.signature_name = profile.candidate.name or "Candidate"


def _deterministic_draft_issues(draft: ArtifactDraft, facts: list[FileFact], profile: WorkspaceProfile, job: InboxJob) -> list[str]:
    issues: list[str] = []
    if not draft.cover_letter_draft.paragraphs or len(draft.cover_letter_draft.paragraphs) != _COVER_LETTER_PARAGRAPH_COUNT:
        issues.append(f"Cover letter must contain exactly {_COVER_LETTER_PARAGRAPH_COUNT} paragraphs.")
    if _line_count(draft.resume_draft.summary_lines) > 4:
        issues.append("Resume summary exceeds 4 lines.")
    if _line_count(draft.resume_draft.custom_bullets) > 4:
        issues.append("Custom resume bullets exceed 4 lines.")
    valid_fact_ids = {fact.fact_id for fact in facts if not fact.disallowed}
    for label, fact_ids in (
        ("selected_work_fact_ids", draft.resume_draft.selected_work_fact_ids),
        ("selected_project_fact_ids", draft.resume_draft.selected_project_fact_ids),
        ("selected_skill_fact_ids", draft.resume_draft.selected_skill_fact_ids),
    ):
        unresolved = [fact_id for fact_id in _clean_list(fact_ids) if fact_id not in valid_fact_ids]
        if unresolved:
            issues.append(f"{label} contains unresolved fact ids: {', '.join(unresolved)}.")
    if not str(draft.cover_letter_draft.salutation or "").strip():
        issues.append("Cover letter salutation is missing after defaults.")
    if not str(draft.cover_letter_draft.closing or "").strip():
        issues.append("Cover letter closing is missing after defaults.")
    if not str(draft.cover_letter_draft.signature_name or "").strip():
        issues.append("Cover letter signature is missing after defaults.")
    if str(profile.candidate.name or "").strip() and str(profile.candidate.name or "").strip().casefold() in str(draft.resume_draft.headline or "").casefold():
        issues.append("Resume headline must not duplicate the candidate name.")
    if _job_echo(draft.resume_draft.headline, job, include_title=True):
        issues.append("Resume headline echoes the job posting instead of candidate-grounded positioning.")
    if any(_job_echo(line, job) for line in _clean_list(draft.resume_draft.summary_lines)):
        issues.append("Resume summary lines echo the job posting instead of candidate-grounded evidence.")
    if any(_cover_letter_job_echo(paragraph, job) for paragraph in _clean_list(draft.cover_letter_draft.paragraphs)):
        issues.append("Cover letter paragraphs echo raw job-description text.")
    aggregate = "\n".join(
        [
            draft.resume_draft.headline or "",
            *draft.resume_draft.summary_lines,
            *draft.resume_draft.custom_bullets,
            draft.cover_letter_draft.salutation or "",
            *draft.cover_letter_draft.paragraphs,
            draft.cover_letter_draft.closing or "",
            draft.cover_letter_draft.signature_name or "",
        ]
    )
    if _PLACEHOLDER_RE.search(aggregate):
        issues.append("Draft still contains placeholder or redaction text.")
    if _PLACEHOLDER_NAME_RE.search(aggregate):
        issues.append("Draft contains a placeholder name (e.g. 'Your Name').")
    if _MOJIBAKE_RE.search(aggregate):
        issues.append("Draft contains mojibake or encoding artifacts.")
    candidate_name = str(profile.candidate.name or "").strip().casefold()
    cover_body = "\n".join(_clean_list(draft.cover_letter_draft.paragraphs)).casefold()
    if candidate_name and candidate_name in cover_body:
        issues.append("Cover letter body must stay in first person and avoid the candidate name.")
    if re.search(r"\b(he|she|his|her)\b", cover_body):
        issues.append("Cover letter body uses third-person voice.")
    # Detect duplicate salutations in cover letter paragraphs.
    salutation = str(draft.cover_letter_draft.salutation or "").strip().rstrip(",").lower()
    if salutation:
        for paragraph in draft.cover_letter_draft.paragraphs:
            if paragraph.strip().lower().startswith(salutation):
                issues.append("Cover letter paragraph repeats the salutation.")
                break
    # Detect duplicate URLs across resume and cover letter.
    url_pattern = re.compile(r"https?://\S+", re.IGNORECASE)
    all_urls = url_pattern.findall(aggregate)
    if len(all_urls) != len(set(url.lower().rstrip("/") for url in all_urls)):
        issues.append("Draft contains duplicate URLs.")
    return list(dict.fromkeys(issues))


def _apply_validation_metadata(draft: ArtifactDraft, issues: list[str], *, repair_attempted: bool = False, repair_writer_profile: str | None = None) -> ArtifactDraft:
    validation_profile = "deterministic_validation"
    deduped = list(dict.fromkeys(_clean_list(issues)))
    return draft.model_copy(
        update={
            "validation_profile": validation_profile,
            "validation_issues": deduped,
            "repair_attempted": repair_attempted,
            "repair_writer_profile": repair_writer_profile,
            "verified": not deduped,
        }
    )


async def _build_artifact_draft_async(workspace: FileWorkspace, job: InboxJob, evaluation: EvaluationResult) -> ArtifactDraft:
    router = load_model_router(workspace)
    if router is None:
        raise RuntimeError("No advanced model router is configured for drafting.")
    profile = workspace.load_profile()
    all_facts = workspace.load_facts()

    def _writer_input_for_tier(*, repair_issues: list[str] | None = None) -> dict[str, Any]:
        last_payload: dict[str, Any] | None = None
        for tier in _DRAFT_CONTEXT_TIERS:
            facts = _draft_facts(
                all_facts,
                job,
                evaluation,
                work_limit=int(tier["work_limit"]),
                project_limit=int(tier["project_limit"]),
                skill_limit=int(tier["skill_limit"]),
            )
            payload = _build_writer_input(
                workspace,
                job,
                evaluation,
                profile,
                facts,
                job_chars=int(tier["job_chars"]),
                cv_chars=int(tier["cv_chars"]),
                dossier_chars=int(tier["dossier_chars"]),
                summary_chars=int(tier["summary_chars"]),
                detail=str(tier["detail"]),
                repair_issues=repair_issues,
            )
            last_payload = payload
            if json_chars(payload) <= _DRAFT_PROMPT_CHAR_BUDGET:
                return payload
        assert last_payload is not None
        return last_payload

    async def writer_request(repair_issues: list[str] | None = None) -> tuple[ArtifactDraft, str]:
        writer_input = _writer_input_for_tier(repair_issues=repair_issues)
        prompt = json.dumps(writer_input, indent=2, sort_keys=True)
        system_prompt = (
            "/no-think\n"
            "You are a professional resume writer and career coach generating tailored job application documents.\n\n"
            "STRICT ANTI-HALLUCINATION RULES - violating ANY of these makes the output invalid:\n"
            "1. Every skill, project, employer, metric, date, and technology you mention MUST appear in the supplied facts, dossier, CV excerpt, or job description.\n"
            "2. Do NOT use placeholders or redaction text. No TODO, lorem ipsum, template markers, bracket variables, or invented company/project names.\n"
            "3. Do NOT fabricate numbers, percentages, timelines, universities, titles, visas, or work authorization details.\n"
            "4. The real job description is authoritative. Tailor directly to it. Mention the exact company and role where appropriate.\n"
            "5. Keep the resume ATS-safe, concise, and one-page friendly.\n\n"
            "COVER LETTER REQUIREMENTS:\n"
            "- Return exactly 3 body paragraphs.\n"
            "- CRITICAL: Write all cover letter content in FIRST PERSON (I/my/me). Never use third person or the candidate's name within body paragraphs.\n"
            "- Paragraph 1: direct hook tied to the company and role, without generic 'I am applying' boilerplate.\n"
            "- Paragraph 2: strongest technical fit from the candidate facts.\n"
            "- Paragraph 3: close with specific enthusiasm, likely contribution, and thanks.\n"
            "- No raw job-description text pasted into the cover letter.\n"
            "- No self-deprecating language about gaps or 'room to grow.'\n\n"
            "RESUME REQUIREMENTS:\n"
            "- summary_lines: 2-4 concise lines.\n"
            "- The resume summary must describe the CANDIDATE's actual experience, NOT the job requirements.\n"
            "- Never mention technologies or achievements the candidate doesn't have.\n"
            "- custom_bullets: 0-4 concrete bullets grounded in supplied facts only.\n"
            "- selected_*_fact_ids must only reference provided fact IDs.\n\n"
            "Return ONLY valid JSON matching the schema."
        )
        try:
            payload, profile_name = await _generate_json_with_first_role(
                router,
                [ModelRole.WRITER],
                prompt,
                system_prompt=system_prompt,
                json_schema=_artifact_draft_json_schema(),
            )
        except TypeError as exc:
            if "json_schema" not in str(exc):
                raise
            payload, profile_name = await _generate_json_with_first_role(
                router,
                [ModelRole.WRITER],
                prompt,
                system_prompt=system_prompt,
            )
        draft = ArtifactDraft.model_validate(payload)
        draft.writer_profile = profile_name
        current_facts = _draft_facts(
            all_facts,
            job,
            evaluation,
            work_limit=5,
            project_limit=3,
            skill_limit=15,
        )
        _apply_draft_defaults(draft, current_facts, profile, job)
        _apply_grounded_repairs(draft, current_facts, profile, job, evaluation)
        return draft, profile_name

    draft, _writer_profile = await writer_request()
    validation_facts = _draft_facts(
        all_facts,
        job,
        evaluation,
        work_limit=5,
        project_limit=3,
        skill_limit=15,
    )
    issues = _deterministic_draft_issues(draft, validation_facts, profile, job)
    if not issues:
        return _apply_validation_metadata(draft, [])

    repaired, repair_writer_profile = await writer_request(issues)
    repaired = _apply_validation_metadata(
        repaired,
        _deterministic_draft_issues(repaired, validation_facts, profile, job),
        repair_attempted=True,
        repair_writer_profile=repair_writer_profile,
    )
    if repaired.validation_issues:
        log = logging.getLogger("findmyjob.drafting")
        log.warning("Draft repair still has validation issues: %s", "; ".join(repaired.validation_issues))
    return repaired


def build_resume_plan_with_router(
    workspace: FileWorkspace | Path,
    job: InboxJob,
    evaluation: EvaluationResult,
) -> tuple[ResumePlan, dict[str, Any]]:
    ws = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
    ws.ensure()
    draft = run_async(_build_artifact_draft_async, ws, job, evaluation)
    plan = ResumePlan(
        headline=draft.resume_draft.headline,
        summary_lines=_clean_list(draft.resume_draft.summary_lines),
        selected_work_fact_ids=_clean_list(draft.resume_draft.selected_work_fact_ids),
        selected_project_fact_ids=_clean_list(draft.resume_draft.selected_project_fact_ids),
        selected_skill_fact_ids=_clean_list(draft.resume_draft.selected_skill_fact_ids),
        custom_bullets=_clean_list(draft.resume_draft.custom_bullets),
        cover_letter_paragraphs=_clean_list(draft.cover_letter_draft.paragraphs),
    )
    metadata = {
        "writer_profile": draft.writer_profile,
        "validation_profile": draft.validation_profile or "deterministic_validation",
        "validation_issues": list(draft.validation_issues),
        "repair_attempted": bool(draft.repair_attempted),
        "repair_writer_profile": draft.repair_writer_profile,
        "verified": draft.verified,
        "adaptation_summary": draft.adaptation_summary,
        "signature_name": draft.cover_letter_draft.signature_name,
        "salutation": draft.cover_letter_draft.salutation,
        "closing": draft.cover_letter_draft.closing,
    }
    return plan, metadata
