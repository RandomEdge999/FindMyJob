from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from findmyjob.filefirst.modes import ModeRunner
from findmyjob.filefirst.models import FileFact, InboxJob, ScreeningDecision, utcnow_iso
from findmyjob.filefirst.prompt_budget import compact_fact_payload, compact_profile_payload, json_chars, trim_text
from findmyjob.filefirst.text_utils import strip_html_tags
from findmyjob.filefirst.workspace import FileWorkspace
from findmyjob.sources.normalizer import infer_experience_level

_SCREEN_PROMPT_CHAR_BUDGET = 9500
_SCREENING_AUTO_APPROVE_CONFIDENCE_FLOOR = 0.4
_SCREEN_CONTEXT_TIERS = (
    {
        "job_chars": 2600,
        "cv_chars": 1200,
        "fact_limit": 12,
        "detail": "normal",
    },
    {
        "job_chars": 2000,
        "cv_chars": 800,
        "fact_limit": 10,
        "detail": "tight",
    },
    {
        "job_chars": 1500,
        "cv_chars": 0,
        "fact_limit": 8,
        "detail": "tight",
    },
)
_SENIOR_TITLE_REJECT_PATTERN = re.compile(r"\b(?:senior|sr\.?|staff|principal|lead|architect|director)\b", re.IGNORECASE)


def _profile_for_llm(profile) -> dict[str, Any]:
    return compact_profile_payload(profile)


def _fact_text(fact: FileFact) -> str:
    return ' '.join(str(value) for value in fact.payload.values() if str(value or '').strip())


def _fact_score(fact: FileFact, title_terms: set[str]) -> int:
    haystack = re.sub(r'[^a-z0-9]+', ' ', _fact_text(fact).casefold())
    return len({token for token in haystack.split() if len(token) > 2} & title_terms)


def _facts_for_screen_context(facts: list[FileFact], job: InboxJob) -> list[FileFact]:
    title_terms = {token for token in re.sub(r'[^a-z0-9]+', ' ', job.title.casefold()).split() if len(token) > 2}
    always = [fact for fact in facts if fact.kind in {'contact', 'authorization', 'location', 'education'} and not fact.disallowed]
    ranked = sorted(
        [fact for fact in facts if fact.kind in {'work', 'project', 'skill'} and not fact.disallowed],
        key=lambda item: (_fact_score(item, title_terms), item.fact_id),
        reverse=True,
    )
    return [*always, *ranked]


def _fact_for_llm(fact: FileFact, *, detail: str) -> dict[str, Any]:
    return compact_fact_payload(fact, detail=detail)


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or '').strip().casefold()
    return text in {'1', 'true', 'yes', 'y', 'approved', 'allow'}


def _reasons_from_payload(payload: dict[str, Any]) -> list[str]:
    raw = payload.get('reasons')
    if isinstance(raw, list):
        reasons = [str(item).strip() for item in raw if str(item).strip()]
    elif isinstance(raw, str):
        reasons = [raw.strip()] if raw.strip() else []
    else:
        reasons = []
    fallback = payload.get('reason')
    if not reasons and str(fallback or '').strip():
        reasons = [str(fallback).strip()]
    return reasons


def normalize_screening_payload(payload: dict[str, Any]) -> ScreeningDecision:
    if not isinstance(payload, dict):
        raise ValueError('Screening payload must be a JSON object.')
    confidence_raw = payload.get('confidence', 0.0)
    try:
        confidence = float(confidence_raw or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    notes = payload.get('notes')
    if isinstance(notes, list):
        notes = '\n'.join(str(item).strip() for item in notes if str(item).strip()) or None
    elif notes is not None:
        notes = str(notes).strip() or None
    reasons = _reasons_from_payload(payload)
    approved = _parse_bool(payload.get('approved'))
    note_parts = [notes] if notes else []
    if not approved and confidence < _SCREENING_AUTO_APPROVE_CONFIDENCE_FLOOR:
        if 'low_confidence_held_for_review' not in reasons:
            reasons.append('low_confidence_held_for_review')
        note_parts.append(f'low_confidence_rejection<{_SCREENING_AUTO_APPROVE_CONFIDENCE_FLOOR:.1f}; held_for_review')
    return ScreeningDecision(
        approved=approved,
        reasons=reasons,
        confidence=confidence,
        internship_like=_parse_bool(payload.get('internship_like', payload.get('is_internship'))),
        seniority_too_high=_parse_bool(payload.get('seniority_too_high', payload.get('seniority_high'))),
        years_experience_signal=str(payload.get('years_experience_signal') or payload.get('years_experience') or '').strip() or None,
        notes='\n'.join(note_parts) or None,
        screened_at=utcnow_iso(),
    )


def _workflow_state_for(screening: ScreeningDecision) -> str:
    if screening.approved:
        return 'pending'
    if 'low_confidence_held_for_review' in (screening.reasons or []):
        return 'needs_review'
    return 'screened_out'


def _seniority_title_signal(title: str) -> str | None:
    normalized = re.sub(r"\s+", " ", str(title or "").strip())
    if not normalized:
        return None
    match = _SENIOR_TITLE_REJECT_PATTERN.search(normalized)
    if match is None:
        return None
    return re.sub(r"[^a-z0-9]+", "", match.group(0).casefold()) or None


def _deterministic_screening_decision(*, reasons: list[str], strategy: str, seniority_signal: str | None = None, notes: str | None = None) -> ScreeningDecision:
    note_parts = [f"deterministic_strategy={strategy}"]
    if seniority_signal:
        note_parts.append(f"matched_title_token={seniority_signal}")
    if notes:
        note_parts.append(notes)
    return ScreeningDecision(
        approved=False,
        reasons=reasons,
        confidence=1.0,
        internship_like=False,
        seniority_too_high=bool(seniority_signal),
        years_experience_signal=None,
        notes="; ".join(note_parts),
        screened_at=utcnow_iso(),
    )


def _hard_reject_screening(job: InboxJob) -> ScreeningDecision | None:
    reasons: list[str] = []
    seniority_signal = _seniority_title_signal(job.title)
    if seniority_signal:
        reasons.append(f"Deterministic seniority title filter matched: {seniority_signal}")
    if job.auth_reject_reason:
        reasons.append(f"Authorization filter: {job.auth_reject_reason.replace('_', ' ')}")
    if job.hard_reject_reason and not job.hard_reject_reason.startswith('seniority_title:'):
        reasons.append(f"Hard filter: {job.hard_reject_reason.replace('_', ' ')}")
    if not reasons:
        return None
    notes: list[str] = []
    if job.ats_family and job.ats_family != 'unknown':
        notes.append(f"ATS family: {job.ats_family}")
    if job.login_wall_detected:
        notes.append('Login or account wall detected on the apply surface.')
    return _deterministic_screening_decision(
        reasons=reasons,
        strategy='deterministic_title_fast_path' if seniority_signal else 'deterministic_hard_filter',
        seniority_signal=seniority_signal,
        notes=' '.join(notes) or 'Rejected by deterministic hard rules.',
    )


def _screen_context(ws: FileWorkspace, job: InboxJob) -> dict[str, Any]:
    profile = ws.load_profile()
    facts = ws.load_facts()
    sanitized_description = strip_html_tags(job.description)
    experience = infer_experience_level(job.title, sanitized_description)
    ranked_facts = _facts_for_screen_context(facts, job)
    last_context: dict[str, Any] | None = None
    for tier in _SCREEN_CONTEXT_TIERS:
        context = {
            'profile': _profile_for_llm(profile),
            'facts': [_fact_for_llm(fact, detail=str(tier["detail"])) for fact in ranked_facts[: int(tier["fact_limit"])]],
            'cv_markdown': trim_text(ws.load_cv(), limit=int(tier["cv_chars"])),
            'job': {
                'job_id': job.job_id,
                'company': job.company,
                'title': job.title,
                'location': job.location,
                'source': job.source,
                'url': job.url,
                'description': trim_text(sanitized_description, limit=int(tier["job_chars"])),
            },
            'signals': {
                'experience_level_inference': getattr(experience.level, 'value', str(experience.level)),
                'metadata_quality': experience.metadata_quality,
            },
        }
        last_context = context
        if json_chars(context) <= _SCREEN_PROMPT_CHAR_BUDGET:
            return context
    assert last_context is not None
    return last_context


def screen_job(workspace: Path | FileWorkspace, target: str | InboxJob, *, force: bool = False) -> tuple[InboxJob, ScreeningDecision]:
    ws = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
    ws.ensure()
    job = target if isinstance(target, InboxJob) else ws.load_job(target)
    if job is None:
        raise ValueError(f'Unknown screening target: {target}')
    if job.screening is not None and not force:
        return job, job.screening
    hard_screening = _hard_reject_screening(job)
    if hard_screening is not None:
        notes = dict(job.notes or {})
        notes.setdefault('screening_strategy', 'deterministic_title_fast_path' if hard_screening.seniority_too_high else 'deterministic_hard_filter')
        updated_job = job.model_copy(update={'screening': hard_screening, 'workflow_state': _workflow_state_for(hard_screening), 'notes': notes})
        ws.save_job(updated_job)
        ws.upsert_inbox_jobs([updated_job])
        return updated_job, hard_screening

    context = _screen_context(ws, job)
    runner = ModeRunner(ws)
    payload = runner.run_json('screen', context)
    model_profile = runner.last_profile_name
    screening = normalize_screening_payload(payload)
    notes = dict(job.notes or {})
    notes['screening_strategy'] = 'llm_classifier'
    if model_profile:
        notes['classifier_profile'] = model_profile
        if model_profile not in (screening.notes or ''):
            note = f"classifier_profile={model_profile}"
            screening = screening.model_copy(update={'notes': f"{screening.notes} | {note}" if screening.notes else note})
    updated_job = job.model_copy(update={'screening': screening, 'workflow_state': _workflow_state_for(screening), 'notes': notes})
    ws.save_job(updated_job)
    ws.upsert_inbox_jobs([updated_job])
    return updated_job, screening


def override_screening(workspace: Path | FileWorkspace, job_id: str, *, approved: bool = True, note: str | None = None) -> tuple[InboxJob, ScreeningDecision]:
    ws = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
    ws.ensure()
    job = ws.load_job(job_id)
    if job is None:
        raise ValueError(f'Unknown job for screening override: {job_id}')
    current = job.screening or ScreeningDecision(approved=approved)
    reasons = list(current.reasons)
    if note and note.strip():
        reasons.append(note.strip())
    if not reasons:
        reasons = ['Operator override']
    screening = current.model_copy(
        update={
            'approved': approved,
            'reasons': reasons,
            'notes': note or current.notes,
            'overridden': True,
            'screened_at': utcnow_iso(),
        }
    )
    updated_job = job.model_copy(update={'screening': screening, 'workflow_state': _workflow_state_for(screening)})
    ws.save_job(updated_job)
    ws.upsert_inbox_jobs([updated_job])
    return updated_job, screening


def reset_screening(workspace: Path | FileWorkspace, job_id: str) -> InboxJob:
    ws = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
    ws.ensure()
    job = ws.load_job(job_id)
    if job is None:
        raise ValueError(f'Unknown job for screening reset: {job_id}')
    notes = dict(job.notes or {})
    notes.pop('screening_strategy', None)
    notes.pop('classifier_profile', None)
    updated_job = job.model_copy(update={'screening': None, 'workflow_state': 'pending', 'notes': notes})
    ws.save_job(updated_job)
    jobs = ws.load_inbox()
    for index, inbox_job in enumerate(jobs):
        if inbox_job.job_id == job_id:
            jobs[index] = updated_job
            break
    else:
        jobs.append(updated_job)
    ws.save_inbox(jobs)
    return updated_job


def screening_payload(job: InboxJob | None) -> dict[str, Any] | None:
    if job is None or job.screening is None:
        return None
    payload = job.screening.model_dump(mode='json')
    payload['status'] = job.screening.status
    notes = dict(job.notes or {})
    if notes.get('classifier_profile'):
        payload['classifier_profile'] = notes['classifier_profile']
    if notes.get('screening_strategy'):
        payload['screening_strategy'] = notes['screening_strategy']
    return payload


__all__ = ['normalize_screening_payload', 'override_screening', 'reset_screening', 'screen_job', 'screening_payload']
