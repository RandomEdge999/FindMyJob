"""Greenhouse training-mode orchestration.

This module implements the "sample, inspect, draft, review" workflow
for the logged-in My Greenhouse browser path.  It never submits.

The workflow:
1. Attach to the user's Chrome over CDP
2. Navigate to https://my.greenhouse.io/jobs
3. Set the posted-date filter
4. Harvest visible jobs, sample up to batch_size
5. For each sampled job:
   a. Navigate to the job view page
   b. Find and navigate to the company job page
   c. Find and navigate to the apply form
   d. Capture screenshots, DOM snapshots, and form fields
   e. Use the configured writer model to generate a structured draft
   f. Render resume and cover-letter artifacts via the existing pipeline
   g. Prompt the user for per-job approval or rejection
6. Persist feedback as audit events
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import select

from findmyjob.apply.cdp_session import CDPAttachError, cdp_browser_context, find_or_open_tab
from findmyjob.apply.greenhouse_training import VALID_POSTED_WINDOWS, harvest_visible_jobs, inspect_training_job_path, set_posted_window
from findmyjob.apply.service import ApplicationService
from findmyjob.core.enums import ApplicationMode, ArtifactKind, JobLifecycleStatus, ModelRole, PolicyMode, ReviewStatus, RunStatus, VerificationStatus
from findmyjob.core.policies import SENSITIVE_QUESTION_KEYWORDS
from findmyjob.core.types import (
    ApplicationQuestion,
    ArtifactBinding,
    ArtifactDraft,
    CoverLetterDraft,
    GreenhouseTrainingJobSummary,
    GroundedAnswer,
    JobSearchQuery,
    NormalizedJobPosting,
    ProfileFact,
    ResumeDraft,
    ReviewPacket,
    TrainingPromotionResultSummary,
    TrainingReviewOutcome,
    TrainingRunSummary,
    TrainingSampleSummary,
    ValidationReport,
)
from findmyjob.db.models import AuditEventRecord, JobPosting
from findmyjob.db.repositories import ApplicationRepository, AuditRepository, JobRepository, ProfileRepository, RunRepository, TrainingSampleRepository, hash_content
from findmyjob.qualification.rules import qualification_for_job
from findmyjob.sources.contracts import FormFieldSpec
from findmyjob.sources.greenhouse_scale import extract_greenhouse_board_tokens
from findmyjob.sources.normalizer import build_normalized_job, slugify


TRAINING_RUN_TYPE = "training"
TRAINING_REJECTION_REASON_LABELS = {
    "job_fit": "Job fit",
    "company_fit": "Company fit",
    "missing_evidence": "Missing evidence",
    "tone": "Tone",
    "formatting": "Formatting",
    "navigation_issue": "Navigation issue",
    "other": "Other",
    "noninteractive_default": "Non-interactive default",
    "rejected_by_user": "Rejected by user",
}
_DRAFTER_SYSTEM_PROMPT = (
    "You are a professional resume and cover-letter writer. "
    "Given a job description, a candidate profile, and prior operator feedback, produce a "
    "structured JSON object with keys `resume_draft`, `cover_letter_draft`, and `adaptation_summary`. "
    "The resume_draft should include: headline (string), summary_lines (list of 2-3 short sentences), "
    "selected_work_fact_ids (list of fact_id strings to include), selected_project_fact_ids, "
    "selected_skill_fact_ids, and custom_bullets (list). "
    "The cover_letter_draft should include: salutation (string), paragraphs (list of 3-4 paragraph strings), "
    "closing (string), and signature_name (string). "
    "The adaptation_summary must briefly explain how the draft responded to prior approvals or rejections. "
    "Use ATS-friendly plain language, keep the resume to one page worth of content, and do not invent facts."
)

class TrainingModeError(RuntimeError):
    """Raised for training-mode workflow failures."""


# ---------------------------------------------------------------------------
# Writer-model-backed drafting
# ---------------------------------------------------------------------------


async def generate_training_draft(
    runtime,
    job_description: str,
    job_title: str | None,
    company_name: str | None,
) -> ArtifactDraft:
    """Use the configured writer model to produce a structured draft.

    Raises :class:`TrainingModeError` if no healthy writer model is available.
    """
    try:
        writer = runtime.model_router.get_profile(role=ModelRole.WRITER)
    except ValueError:
        # Fallback: try cover_letter_writer or resume_writer
        try:
            writer = runtime.model_router.get_profile(role=ModelRole.COVER_LETTER_WRITER)
        except ValueError:
            raise TrainingModeError(
                "No writer model profile is configured.  "
                "A healthy writer-role model is required for training-mode drafting."
            )

    # Gather profile facts
    facts_text = _format_profile_facts(runtime)

    prompt = (
        f"Job title: {job_title or 'Unknown'}\n"
        f"Company: {company_name or 'Unknown'}\n\n"
        f"Job description:\n{job_description[:4000]}\n\n"
        f"Candidate profile:\n{facts_text}\n\n"
        "Produce a structured JSON draft with `resume_draft` and `cover_letter_draft` keys."
    )

    try:
        payload = await runtime.model_router.generate_json(
            ModelRole.WRITER,
            prompt,
            system_prompt=_DRAFTER_SYSTEM_PROMPT,
        )
    except Exception as exc:
        raise TrainingModeError(f"Writer-model drafting failed: {exc}") from exc

    resume_raw = payload.get("resume_draft", {})
    cover_raw = payload.get("cover_letter_draft", {})

    return ArtifactDraft(
        resume_draft=ResumeDraft(
            headline=resume_raw.get("headline"),
            summary_lines=resume_raw.get("summary_lines", []),
            selected_work_fact_ids=resume_raw.get("selected_work_fact_ids", []),
            selected_project_fact_ids=resume_raw.get("selected_project_fact_ids", []),
            selected_skill_fact_ids=resume_raw.get("selected_skill_fact_ids", []),
            custom_bullets=resume_raw.get("custom_bullets", []),
        ),
        cover_letter_draft=CoverLetterDraft(
            salutation=cover_raw.get("salutation"),
            paragraphs=cover_raw.get("paragraphs", []),
            closing=cover_raw.get("closing"),
            signature_name=cover_raw.get("signature_name"),
        ),
        writer_profile=writer.name,
    )


def _format_profile_facts(runtime) -> str:
    """Format profile facts into a text block for the drafter prompt."""
    from findmyjob.db.repositories import ProfileRepository
    from findmyjob.db.models import ProfileFactRecord

    with runtime.session_scope() as session:
        repo = ProfileRepository(session)
        facts = repo.list_facts()
        if not facts:
            return "(No profile facts available.)"
        lines = []
        for fact in facts:
            if fact.disallowed:
                continue
            lines.append(f"- [{fact.kind.value}] {json.dumps(fact.payload, default=str)}")
        return "\n".join(lines) if lines else "(No shareable profile facts.)"


# ---------------------------------------------------------------------------
# Artifact rendering
# ---------------------------------------------------------------------------


def render_training_artifacts(
    runtime,
    job_summary: GreenhouseTrainingJobSummary,
    artifact_draft: ArtifactDraft,
) -> dict[str, str | None]:
    """Render resume and cover-letter artifacts using the existing pipeline.

    Returns dict with keys: resume_pdf_path, resume_text_path,
    cover_letter_pdf_path, cover_letter_text_path.
    """
    company = slugify(job_summary.company_name or "unknown")
    title = slugify(job_summary.job_title or "job")
    base_name = f"training-{company}-{title}-{uuid4().hex[:8]}"

    # Build a synthetic NormalizedJobPosting for the pipeline
    job_model = NormalizedJobPosting(
        company_name=job_summary.company_name or "Unknown",
        company_key=company,
        title=job_summary.job_title or "Unknown",
        source="greenhouse_training",
        source_kind="greenhouse",
        source_job_id=uuid4().hex[:12],
        posting_url=job_summary.job_url,
        description=job_summary.description_snippet or "",
        normalized_description=job_summary.description_snippet or "",
        discovered_at=datetime.now(timezone.utc),
        job_identity_key=uuid4().hex[:16],
        duplicate_cluster_key=uuid4().hex[:16],
        location_raw=job_summary.location,
    )

    # Load facts
    from findmyjob.db.repositories import ProfileRepository

    with runtime.session_scope() as session:
        repo = ProfileRepository(session)
        facts = repo.list_facts()
        active_facts = [f for f in facts if not f.disallowed]

    pipeline = runtime.documents
    context = pipeline.build_resume_context(job_model, active_facts, artifact_draft)
    context["cover_letter"] = pipeline.build_cover_letter_payload(context)

    result: dict[str, str | None] = {}

    # Resume
    if pipeline.resume_renderer == "latex" and pipeline.template_config.resume_template_path:
        resume_pdf = pipeline.render_latex_resume(base_name, context)
        resume_text = pipeline.write_resume_text_from_pdf(base_name, resume_pdf, context)
    else:
        resume_text = pipeline.write_resume_text(base_name, context)
        resume_pdf = pipeline.render_typst("resume.typ", base_name, context)

    result["resume_pdf_path"] = str(resume_pdf.path)
    result["resume_text_path"] = str(resume_text.path)

    # Cover letter
    cover_letter_text = pipeline.write_cover_letter_text(base_name, context)
    cover_letter_pdf = pipeline.render_typst("cover_letter.typ", base_name, context)

    result["cover_letter_text_path"] = str(cover_letter_text.path)
    result["cover_letter_pdf_path"] = str(cover_letter_pdf.path)

    return result


# ---------------------------------------------------------------------------
# Feedback persistence
# ---------------------------------------------------------------------------


def persist_training_feedback(
    runtime,
    review: TrainingReviewOutcome,
    run_id: str,
) -> None:
    """Store per-job training feedback as an audit event."""
    with runtime.session_scope() as session:
        audit = AuditRepository(session)
        audit.emit(
            event_type="training.review",
            entity_type="training_job",
            entity_id=review.job_url,
            run_id=run_id,
            payload={
                "job_url": review.job_url,
                "job_title": review.job_title,
                "company_name": review.company_name,
                "approved": review.approved,
                "rejection_note": review.rejection_note,
                "resume_artifact_path": review.resume_artifact_path,
                "cover_letter_artifact_path": review.cover_letter_artifact_path,
                "reviewed_at": (review.reviewed_at or datetime.now(timezone.utc)).isoformat(),
            },
        )


def load_prior_training_feedback(runtime, limit: int = 100) -> list[dict[str, Any]]:
    """Load prior training feedback for improving future drafts."""
    with runtime.session_scope() as session:
        audit = AuditRepository(session)
        events = audit.list_events(event_type="training.review", limit=limit)
        return [
            {
                "job_url": event.payload.get("job_url"),
                "job_title": event.payload.get("job_title"),
                "company_name": event.payload.get("company_name"),
                "approved": event.payload.get("approved"),
                "rejection_note": event.payload.get("rejection_note"),
                "reviewed_at": event.payload.get("reviewed_at"),
            }
            for event in events
        ]


# ---------------------------------------------------------------------------
# Main training run
# ---------------------------------------------------------------------------


def _stable_job_sample_key(job_data: dict[str, Any]) -> tuple[str, str, str, str]:
    """Keep training samples reproducible across runs."""
    return (
        str(job_data.get("company") or "").casefold(),
        str(job_data.get("title") or "").casefold(),
        str(job_data.get("posted_text") or "").casefold(),
        str(job_data.get("url") or ""),
    )


def _clean_text(value: Any) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _normalize_rejection_reason_code(reason_code: str | None, rejection_note: str | None) -> str:
    aliases = {"fit": "job_fit", "job": "job_fit", "job_fit": "job_fit", "job-fit": "job_fit", "company": "company_fit", "company_fit": "company_fit", "company-fit": "company_fit", "evidence": "missing_evidence", "missing_evidence": "missing_evidence", "missing-evidence": "missing_evidence", "tone": "tone", "format": "formatting", "formatting": "formatting", "navigation": "navigation_issue", "navigation_issue": "navigation_issue", "navigation-issue": "navigation_issue", "other": "other", "rejected_by_user": "rejected_by_user", "noninteractive_default": "noninteractive_default", "skip": "other", "skipped": "other"}
    normalized = str(reason_code or "").strip().lower().replace(" ", "_")
    if normalized in aliases:
        return aliases[normalized]
    note = str(rejection_note or "").lower()
    if any(token in note for token in ("format", "layout", "pdf")):
        return "formatting"
    if any(token in note for token in ("tone", "voice")):
        return "tone"
    if any(token in note for token in ("evidence", "specific", "bullet", "fact")):
        return "missing_evidence"
    if any(token in note for token in ("company", "mission", "team")):
        return "company_fit"
    if any(token in note for token in ("navigation", "apply", "form", "page")):
        return "navigation_issue"
    if any(token in note for token in ("fit", "role", "job")):
        return "job_fit"
    return "other"


def _reason_label(reason_code: str | None) -> str:
    normalized = _normalize_rejection_reason_code(reason_code, None) if reason_code else "other"
    return TRAINING_REJECTION_REASON_LABELS.get(normalized, "Other")


def _feedback_summary(approved: bool, reason_code: str | None, note: str | None) -> str:
    if approved:
        return "Approved by operator for review-first promotion."
    label = _reason_label(reason_code)
    detail = _clean_text(note)
    return f"Rejected: {label}." if not detail else f"Rejected: {label}. {detail}"


def _feedback_line(item: dict[str, Any]) -> str:
    status = "approved" if item.get("approved") else "rejected"
    title = _clean_text(item.get("job_title")) or "Unknown role"
    company = _clean_text(item.get("company_name")) or "Unknown company"
    summary = _clean_text(item.get("feedback_summary")) or _clean_text(item.get("rejection_note")) or "No free-text note recorded."
    if not item.get("approved"):
        label = _reason_label(item.get("rejection_reason_code"))
        if label not in summary:
            summary = f"{label}: {summary}"
    return f"- {status} | {title} @ {company} | {summary}"


def _format_feedback_context(prior_feedback: list[dict[str, Any]]) -> list[str]:
    approved_count = 0
    rejected_count = 0
    lines: list[str] = []
    for item in prior_feedback:
        is_approved = bool(item.get("approved"))
        if is_approved and approved_count >= 3:
            continue
        if not is_approved and rejected_count >= 5:
            continue
        lines.append(_feedback_line(item))
        approved_count += 1 if is_approved else 0
        rejected_count += 0 if is_approved else 1
        if approved_count >= 3 and rejected_count >= 5:
            break
    return lines


def _review_feedback_payload(review: TrainingReviewOutcome) -> dict[str, Any]:
    return {"job_url": review.job_url, "job_title": review.job_title, "company_name": review.company_name, "approved": review.approved, "rejection_note": review.rejection_note, "rejection_reason_code": review.rejection_reason_code, "feedback_summary": review.feedback_summary, "linked_job_id": review.linked_job_id, "linked_application_id": review.linked_application_id, "review_packet_path": review.review_packet_path, "reviewed_at": review.reviewed_at.isoformat() if review.reviewed_at is not None else None}


def _sample_feedback_payload(sample: TrainingSampleSummary) -> dict[str, Any]:
    return {"sample_id": sample.sample_id, "job_url": sample.view_page_url, "job_title": sample.job_title, "company_name": sample.company_name, "approved": sample.approved, "rejection_note": sample.review_note, "rejection_reason_code": sample.review_reason_code, "feedback_summary": sample.feedback_summary, "linked_job_id": sample.promoted_job_id, "linked_application_id": sample.promoted_application_id, "review_packet_path": sample.review_packet_path, "reviewed_at": sample.updated_at.isoformat() if sample.updated_at is not None else None}


def inspect_greenhouse_training_readiness(runtime) -> ValidationReport:
    report = ValidationReport(context="greenhouse_training", workspace=str(runtime.workspace))
    inspection = runtime.model_router.inspect_profiles()
    writer_profile = next((profile for profile in inspection.get("profiles", []) if profile.get("role") == ModelRole.WRITER.value), None)
    if writer_profile is None:
        report.add("blocked", "training.writer_role", "Writer role is not configured for training mode.", detail="Bind a `writer` model profile before running `fmj greenhouse train`.")
    elif writer_profile.get("status") == "blocked":
        report.add("blocked", "training.writer_role", "Writer role is configured but not healthy enough for training mode.", detail="; ".join(writer_profile.get("issues") or []), data={"profile": writer_profile.get("name"), "transport": writer_profile.get("transport"), "provider": writer_profile.get("provider"), "model": writer_profile.get("model")})
    else:
        report.add("ok", "training.writer_role", "Writer role is ready for training-mode drafting.", detail=f"profile={writer_profile.get('name')} | transport={writer_profile.get('transport')} | model={writer_profile.get('model')}")
    template_state = runtime.documents.inspect_template_state()
    missing_templates: list[str] = []
    if runtime.documents.resume_renderer in {"latex", "latex_direct"}:
        resume_template = runtime.documents.template_config.resume_template_path
        if resume_template is None or not resume_template.exists():
            missing_templates.append(str(resume_template or "missing_resume_template"))
    else:
        resume_template = runtime.documents.template_dir / "resume.typ"
        if not resume_template.exists():
            missing_templates.append(str(resume_template))
    if runtime.documents.resume_renderer != "latex_direct":
        cover_letter_template = runtime.documents.template_dir / "cover_letter.typ"
        if not cover_letter_template.exists():
            missing_templates.append(str(cover_letter_template))
    report.add("ok" if not missing_templates else "blocked", "training.templates", "Training artifact templates are ready." if not missing_templates else "Training artifact templates are missing.", detail=template_state.get("typst_template_dir") if not missing_templates else ", ".join(missing_templates), data=template_state)
    with runtime.session_scope() as session:
        fact_count = len([fact for fact in ProfileRepository(session).list_facts() if not fact.disallowed])
    report.add("ok" if fact_count else "warning", "training.profile_facts", f"Grounded profile facts available for drafting: {fact_count}.", detail=None if fact_count else "Training can start, but weak or missing facts will reduce draft quality.")
    return report


def _ensure_training_ready(runtime) -> None:
    report = inspect_greenhouse_training_readiness(runtime)
    if report.blocked_count == 0:
        return
    detail = "; ".join(f"{finding.key}: {finding.summary}" for finding in report.findings if finding.status == "blocked")
    raise TrainingModeError(f"Training mode is not ready: {detail}")


async def generate_training_draft(runtime, job_description: str, job_title: str | None, company_name: str | None, *, prior_feedback: list[dict[str, Any]] | None = None) -> ArtifactDraft:
    try:
        writer = runtime.model_router.get_profile(role=ModelRole.WRITER)
    except ValueError as exc:
        raise TrainingModeError("No writer model profile is configured. A healthy writer-role model is required for training-mode drafting.") from exc
    facts_text = _format_profile_facts(runtime)
    feedback_lines = _format_feedback_context(prior_feedback or [])
    prompt = f"Job title: {job_title or 'Unknown'}\nCompany: {company_name or 'Unknown'}\n\nJob description:\n{job_description[:4000]}\n\nCandidate profile:\n{facts_text}\n\nPrior training feedback:\n" + ("\n".join(feedback_lines) if feedback_lines else "(No prior training feedback recorded.)") + "\n\nProduce the requested JSON draft."
    try:
        payload = await runtime.model_router.generate_json(ModelRole.WRITER, prompt, system_prompt=_DRAFTER_SYSTEM_PROMPT)
    except Exception as exc:
        raise TrainingModeError(f"Writer-model drafting failed: {exc}") from exc
    resume_raw = payload.get("resume_draft", {}) or {}
    cover_raw = payload.get("cover_letter_draft", {}) or {}
    return ArtifactDraft(resume_draft=ResumeDraft(headline=resume_raw.get("headline"), summary_lines=list(resume_raw.get("summary_lines", []) or []), selected_work_fact_ids=list(resume_raw.get("selected_work_fact_ids", []) or []), selected_project_fact_ids=list(resume_raw.get("selected_project_fact_ids", []) or []), selected_skill_fact_ids=list(resume_raw.get("selected_skill_fact_ids", []) or []), custom_bullets=list(resume_raw.get("custom_bullets", []) or [])), cover_letter_draft=CoverLetterDraft(salutation=cover_raw.get("salutation"), paragraphs=list(cover_raw.get("paragraphs", []) or []), closing=cover_raw.get("closing"), signature_name=cover_raw.get("signature_name")), writer_profile=writer.name, adaptation_summary=_clean_text(payload.get("adaptation_summary")), feedback_context=feedback_lines)


def persist_training_feedback(runtime, review: TrainingReviewOutcome, run_id: str) -> None:
    with runtime.session_scope() as session:
        AuditRepository(session).emit(event_type="training.review", entity_type="training_job", entity_id=review.linked_application_id or review.job_url, run_id=run_id, payload={"run_id": run_id, "job_url": review.job_url, "job_title": review.job_title, "company_name": review.company_name, "approved": review.approved, "rejection_note": review.rejection_note, "rejection_reason_code": review.rejection_reason_code, "feedback_summary": review.feedback_summary, "resume_artifact_path": review.resume_artifact_path, "cover_letter_artifact_path": review.cover_letter_artifact_path, "linked_job_id": review.linked_job_id, "linked_application_id": review.linked_application_id, "review_packet_path": review.review_packet_path, "reviewed_at": (review.reviewed_at or datetime.now(timezone.utc)).isoformat()})


def load_prior_training_feedback(runtime, limit: int = 100) -> list[dict[str, Any]]:
    with runtime.session_scope() as session:
        sample_feedback = TrainingSampleRepository(session).list_samples(review_statuses=["approved", "rejected"], limit=limit)
        if sample_feedback:
            return [_sample_feedback_payload(sample) for sample in sample_feedback]
        events = session.execute(select(AuditEventRecord).where(AuditEventRecord.event_type == "training.review").order_by(AuditEventRecord.created_at.desc()).limit(limit)).scalars().all()
    feedback: list[dict[str, Any]] = []
    for event in events:
        payload = dict(event.payload or {})
        payload.setdefault("run_id", event.run_id)
        payload.setdefault("created_at", event.created_at.isoformat() if event.created_at is not None else None)
        feedback.append(payload)
    return feedback


def list_training_runs(runtime, limit: int = 20) -> list[dict[str, Any]]:
    with runtime.session_scope() as session:
        runs = RunRepository(session).list_runs_by_type(TRAINING_RUN_TYPE, limit=limit)
    return [{"run_id": run.id, "status": run.status.value, "mode": run.mode.value, "started_at": run.started_at.isoformat() if run.started_at is not None else None, "completed_at": run.completed_at.isoformat() if run.completed_at is not None else None, "checkpoint_state": dict(run.checkpoint_state or {})} for run in runs]


def list_training_history(runtime, *, limit: int = 50, run_id: str | None = None) -> list[TrainingSampleSummary]:
    with runtime.session_scope() as session:
        return TrainingSampleRepository(session).list_samples(run_id=run_id, limit=limit)


def list_training_review_history(runtime, *, limit: int = 50, run_id: str | None = None) -> list[dict[str, Any]]:
    history = list_training_history(runtime, limit=limit, run_id=run_id)
    if history:
        return [{"sample_id": item.sample_id, "run_id": item.run_id, "job_url": item.view_page_url, "job_title": item.job_title, "company_name": item.company_name, "approved": item.approved, "review_status": item.review_status, "rejection_reason_code": item.review_reason_code, "rejection_note": item.review_note, "feedback_summary": item.feedback_summary, "linked_job_id": item.promoted_job_id, "linked_application_id": item.promoted_application_id, "review_packet_path": item.review_packet_path, "recorded_at": item.updated_at.isoformat() if item.updated_at is not None else None} for item in history]
    feedback = load_prior_training_feedback(runtime, limit=limit)
    if run_id is not None:
        feedback = [item for item in feedback if item.get("run_id") == run_id]
    return [{"sample_id": item.get("sample_id"), "run_id": item.get("run_id"), "job_url": item.get("job_url"), "job_title": item.get("job_title"), "company_name": item.get("company_name"), "approved": bool(item.get("approved")), "review_status": "approved" if item.get("approved") else "rejected", "rejection_reason_code": item.get("rejection_reason_code"), "rejection_note": item.get("rejection_note"), "feedback_summary": item.get("feedback_summary"), "linked_job_id": item.get("linked_job_id"), "linked_application_id": item.get("linked_application_id"), "review_packet_path": item.get("review_packet_path"), "recorded_at": item.get("reviewed_at") or item.get("created_at")} for item in feedback]
def _profile_fact_model(record) -> ProfileFact:
    return ProfileFact(fact_id=record.fact_id, kind=record.kind, payload=record.payload, sensitivity=record.sensitivity, allowed_for_generation=record.allowed_for_generation, disallowed=record.disallowed, provenance=record.provenance, confirmed=record.confirmed)


def _sample_to_job_payload(sample: TrainingSampleSummary) -> dict[str, Any]:
    return GreenhouseTrainingJobSummary(job_url=sample.view_page_url, job_title=sample.job_title, company_name=sample.company_name, location=sample.location, posted_text=sample.posted_text, company_page_url=sample.company_page_url, apply_url=sample.apply_page_url, description_snippet=sample.description_excerpt, form_fields=list(sample.extracted_form_fields), screenshot_paths=list(sample.screenshot_paths), dom_snapshot_path=sample.dom_snapshot_paths[-1] if sample.dom_snapshot_paths else None, page_captures=list(sample.page_captures), layout_notes=list(sample.layout_notes), draft_change_summary=sample.draft_change_summary, linked_job_id=sample.promoted_job_id, linked_application_id=sample.promoted_application_id, review_packet_path=sample.review_packet_path).model_dump(mode="json")


def build_training_report(runtime, run_id: str | None = None) -> dict[str, Any]:
    with runtime.session_scope() as session:
        run_repo = RunRepository(session)
        run = run_repo.get_run(run_id) if run_id else next(iter(run_repo.list_runs_by_type(TRAINING_RUN_TYPE, limit=1)), None)
        if run is None:
            raise ValueError("No training run found.")
        samples = TrainingSampleRepository(session).list_samples(run_id=run.id, limit=200)
    checkpoint = dict(run.checkpoint_state or {})
    readiness = inspect_greenhouse_training_readiness(runtime)
    health = {"sample_count": len(samples), "with_company_page_url": sum(1 for sample in samples if sample.company_page_url), "with_apply_url": sum(1 for sample in samples if sample.apply_page_url), "with_form_fields": sum(1 for sample in samples if sample.extracted_form_fields), "samples_with_navigation_warnings": sum(1 for sample in samples if any("error" in note.lower() or "no apply" in note.lower() for note in sample.layout_notes))}
    return {"run_id": run.id, "status": run.status.value, "mode": run.mode.value, "started_at": run.started_at.isoformat() if run.started_at is not None else None, "completed_at": run.completed_at.isoformat() if run.completed_at is not None else None, "sampled_count": len(samples) or checkpoint.get("sampled_count") or 0, "approved_count": sum(1 for sample in samples if sample.review_status == "approved") if samples else checkpoint.get("approved_count") or 0, "rejected_count": sum(1 for sample in samples if sample.review_status == "rejected") if samples else checkpoint.get("rejected_count") or 0, "promoted_application_ids": [sample.promoted_application_id for sample in samples if sample.promoted_application_id] or list(checkpoint.get("promoted_application_ids") or []), "review_packet_paths": [sample.review_packet_path for sample in samples if sample.review_packet_path] or list(checkpoint.get("review_packet_paths") or []), "notes": list(checkpoint.get("notes") or []), "sampled_jobs": [_sample_to_job_payload(sample) for sample in samples] if samples else list(checkpoint.get("sampled_jobs") or []), "reviews": list_training_review_history(runtime, limit=200, run_id=run.id), "samples": [sample.model_dump(mode="json") for sample in samples], "readiness": readiness.model_dump(mode="json"), "health": health}


def _company_job_page_url(job_summary: GreenhouseTrainingJobSummary) -> str | None:
    return job_summary.company_page_url


def _training_posting_url(job_summary: GreenhouseTrainingJobSummary) -> str:
    return _company_job_page_url(job_summary) or job_summary.apply_url or job_summary.job_url


def _training_board_token(job_summary: GreenhouseTrainingJobSummary) -> str | None:
    haystack = "\n".join([value for value in [job_summary.job_url, job_summary.apply_url, _company_job_page_url(job_summary), *job_summary.layout_notes] if value])
    tokens = extract_greenhouse_board_tokens(haystack)
    return sorted(tokens)[0] if tokens else None


def _training_source_job_id(job_summary: GreenhouseTrainingJobSummary) -> str:
    for candidate in [job_summary.apply_url, _company_job_page_url(job_summary), job_summary.job_url]:
        if not candidate:
            continue
        match = re.search(r"/jobs/(\d+)", urlparse(candidate).path)
        if match:
            return match.group(1)
        segments = [segment for segment in urlparse(candidate).path.split("/") if segment]
        if segments:
            cleaned = slugify(segments[-1])
            if cleaned != "unknown":
                return cleaned
    return hash_content(job_summary.job_url)[:12]


def _training_questions_from_form_fields(runtime, form_fields: list[dict[str, Any]]) -> list[ApplicationQuestion]:
    questions: list[ApplicationQuestion] = []
    for field in form_fields:
        name = _clean_text(field.get("name")) or _clean_text(field.get("id"))
        prompt = _clean_text(field.get("label")) or name
        if not name or not prompt:
            continue
        raw_type = str(field.get("type") or "text").strip().lower()
        field_type = raw_type if raw_type in {"text", "textarea", "select", "radio", "checkbox", "file"} else "text"
        spec = FormFieldSpec(name=name, label=prompt, field_type=field_type, widget_type=field_type, required=bool(field.get("required")), options=[str(option).strip() for option in field.get("options", []) if str(option).strip()], prompt_text=prompt, normalized_key=slugify(prompt), sensitive=any(keyword in prompt.lower() for keyword in SENSITIVE_QUESTION_KEYWORDS), source_snapshot_ref=f"training:{_clean_text(field.get('id')) or name}")
        question = spec.to_question()
        if question.question_type.value == "unknown":
            question.question_type = runtime.grounding.classify_question(question.prompt_text, question.options)
        questions.append(question)
    return questions


def _answer_memory_context(question: ApplicationQuestion, job: JobPosting) -> dict[str, Any]:
    options = [str(option).strip().lower() for option in question.options if str(option).strip()]
    return {"question_type": question.question_type.value, "source_adapter": job.source_adapter, "option_signature": "|".join(sorted(options))}


def _file_answer_for_question(question: ApplicationQuestion, artifacts_by_kind: dict[str, str]) -> GroundedAnswer:
    prompt = question.prompt_text.lower()
    preferred = ArtifactKind.COVER_LETTER_PDF if "cover" in prompt else ArtifactKind.RESUME_PDF
    fallback = ArtifactKind.COVER_LETTER_TEXT if preferred == ArtifactKind.COVER_LETTER_PDF else ArtifactKind.RESUME_TEXT
    artifact_path = artifacts_by_kind.get(preferred.value) or artifacts_by_kind.get(fallback.value)
    if not artifact_path:
        return GroundedAnswer(question=question.prompt_text, question_type=question.question_type, canonical_question=question.normalized_key, verification_status=VerificationStatus.NEEDS_USER_INPUT)
    bound_kind = preferred if artifact_path.endswith(".pdf") else fallback
    mime_type = "application/pdf" if artifact_path.endswith(".pdf") else "text/plain"
    return GroundedAnswer(question=question.prompt_text, question_type=question.question_type, answer=artifact_path, provenance="artifact", canonical_question=question.normalized_key, artifact_binding=ArtifactBinding(artifact_kind=bound_kind, source_artifact_kind=preferred, path=artifact_path, mime_type=mime_type), verification_status=VerificationStatus.VERIFIED)


def _merge_flags(flags: list[str]) -> list[str]:
    seen: list[str] = []
    for flag in flags:
        cleaned = str(flag).strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen

def _iter_dom_snapshot_paths(job_summary: GreenhouseTrainingJobSummary) -> list[str]:
    paths: list[str] = []
    for capture in job_summary.page_captures:
        if capture.dom_snapshot_path:
            paths.append(capture.dom_snapshot_path)
    if job_summary.dom_snapshot_path and job_summary.dom_snapshot_path not in paths:
        paths.append(job_summary.dom_snapshot_path)
    return paths


def _store_training_artifacts(app_repo: ApplicationRepository, job: JobPosting, application_id: str, job_summary: GreenhouseTrainingJobSummary, artifact_paths: dict[str, str | None]) -> dict[str, str]:
    mapping = {"resume_pdf_path": ArtifactKind.RESUME_PDF, "resume_text_path": ArtifactKind.RESUME_TEXT, "cover_letter_pdf_path": ArtifactKind.COVER_LETTER_PDF, "cover_letter_text_path": ArtifactKind.COVER_LETTER_TEXT}
    stored: dict[str, str] = {}
    for key, artifact_kind in mapping.items():
        path = artifact_paths.get(key)
        if not path:
            continue
        app_repo.store_artifact(artifact_kind, path, hash_content(path), {"valid": True, "training": True}, job_posting_id=job.id, application_id=application_id)
        stored[artifact_kind.value] = path
    for index, screenshot_path in enumerate(job_summary.screenshot_paths, start=1):
        app_repo.store_artifact(ArtifactKind.SNAPSHOT, screenshot_path, hash_content(screenshot_path), {"valid": True, "training": True, "phase": f"training_screenshot_{index}"}, job_posting_id=job.id, application_id=application_id)
    for index, dom_path in enumerate(_iter_dom_snapshot_paths(job_summary), start=1):
        app_repo.store_artifact(ArtifactKind.SNAPSHOT, dom_path, hash_content(dom_path), {"valid": True, "training": True, "phase": f"training_dom_{index}"}, job_posting_id=job.id, application_id=application_id)
    return stored


def _sample_to_job_summary(sample: TrainingSampleSummary) -> GreenhouseTrainingJobSummary:
    return GreenhouseTrainingJobSummary(job_url=sample.view_page_url, job_title=sample.job_title, company_name=sample.company_name, location=sample.location, posted_text=sample.posted_text, company_page_url=sample.company_page_url, apply_url=sample.apply_page_url, description_snippet=sample.description_excerpt, form_fields=list(sample.extracted_form_fields), screenshot_paths=list(sample.screenshot_paths), dom_snapshot_path=sample.dom_snapshot_paths[-1] if sample.dom_snapshot_paths else None, page_captures=list(sample.page_captures), layout_notes=list(sample.layout_notes), draft_change_summary=sample.draft_change_summary, linked_job_id=sample.promoted_job_id, linked_application_id=sample.promoted_application_id, review_packet_path=sample.review_packet_path)


def _build_training_sample(*, run_id: str, jobs_page_url: str, job_summary: GreenhouseTrainingJobSummary, artifact_paths: dict[str, str | None], review: TrainingReviewOutcome) -> TrainingSampleSummary:
    return TrainingSampleSummary(sample_id=uuid4().hex, run_id=run_id, jobs_page_url=jobs_page_url, view_page_url=job_summary.job_url, company_page_url=job_summary.company_page_url, apply_page_url=job_summary.apply_url, job_title=job_summary.job_title, company_name=job_summary.company_name, location=job_summary.location, posted_text=job_summary.posted_text, description_excerpt=job_summary.description_snippet, extracted_form_fields=list(job_summary.form_fields), screenshot_paths=list(job_summary.screenshot_paths), dom_snapshot_paths=_iter_dom_snapshot_paths(job_summary), page_captures=list(job_summary.page_captures), layout_notes=list(job_summary.layout_notes), artifact_paths=dict(artifact_paths), draft_change_summary=job_summary.draft_change_summary, review_status="approved" if review.approved else "rejected", review_reason_code=review.rejection_reason_code, review_note=review.rejection_note, feedback_summary=review.feedback_summary)


def _save_training_sample(runtime, sample: TrainingSampleSummary) -> TrainingSampleSummary:
    with runtime.session_scope() as session:
        record = TrainingSampleRepository(session).save_sample(sample)
        return TrainingSampleRepository.to_model(record)


async def _promote_training_sample(runtime, sample: TrainingSampleSummary) -> TrainingPromotionResultSummary:
    if sample.review_status != "approved":
        return TrainingPromotionResultSummary(
            sample_id=sample.sample_id,
            run_id=sample.run_id,
            review_status=sample.review_status,
            promoted=False,
            notes=["Sample is not approved and cannot be promoted."],
        )

    with runtime.session_scope() as session:
        sample_repo = TrainingSampleRepository(session)
        app_repo = ApplicationRepository(session)
        record = sample_repo.get_record(sample.sample_id)
        if record is None:
            raise ValueError(f"Training sample not found: {sample.sample_id}")
        if record.promoted_application_id and app_repo.get_application(record.promoted_application_id) is not None:
            return TrainingPromotionResultSummary(
                sample_id=sample.sample_id,
                run_id=sample.run_id,
                review_status=sample.review_status,
                promoted=True,
                job_id=record.promoted_job_id,
                application_id=record.promoted_application_id,
                review_packet_path=record.review_packet_path,
                notes=["Sample was already promoted."],
            )

    job_summary = _sample_to_job_summary(sample)
    posting_url = _training_posting_url(job_summary)
    apply_url = job_summary.apply_url or posting_url
    board_token = _training_board_token(job_summary)
    source_job_id = _training_source_job_id(job_summary)
    query = JobSearchQuery.from_search_settings(runtime.config.search).to_discovery_query()
    training_notes = {
        "run_id": sample.run_id,
        "sample_id": sample.sample_id,
        "jobs_page_url": sample.jobs_page_url,
        "my_greenhouse_job_url": job_summary.job_url,
        "company_job_url": _company_job_page_url(job_summary),
        "apply_url": apply_url,
        "draft_change_summary": job_summary.draft_change_summary,
        "layout_notes": list(job_summary.layout_notes),
    }
    job_model = build_normalized_job(
        company_name=job_summary.company_name or "Unknown",
        title=job_summary.job_title or "Unknown",
        source="greenhouse",
        source_kind="greenhouse",
        source_job_id=source_job_id,
        posting_url=posting_url,
        apply_url=apply_url,
        location_raw=job_summary.location,
        employment_type=None,
        compensation=None,
        description=job_summary.description_snippet or "Training capture from My Greenhouse.",
        notes={"board": board_token, "training": training_notes},
    )

    with runtime.session_scope() as session:
        job_repo = JobRepository(session)
        app_repo = ApplicationRepository(session)
        profile_repo = ProfileRepository(session)
        audit_repo = AuditRepository(session)
        sample_repo = TrainingSampleRepository(session)

        job = job_repo.upsert_job(job_model, raw_payload={"training_sample": sample.model_dump(mode="json")})
        if board_token:
            job.board_token = board_token

        application = app_repo.ensure_application(job.id, ApplicationMode.DRY_RUN)
        application.status = JobLifecycleStatus.PREPARING
        application.review_status = ReviewStatus.PENDING
        application.review_flags = []
        application.handoff_reason = "Training-approved Greenhouse sample. Manual review/fill only."
        job.lifecycle_status = JobLifecycleStatus.PREPARING

        app_repo.clear_questions(application.id)
        questions = _training_questions_from_form_fields(runtime, job_summary.form_fields)
        stored_questions = [app_repo.store_question(application.id, question) for question in questions]
        facts = [_profile_fact_model(record) for record in profile_repo.list_facts()]
        artifacts_by_kind = _store_training_artifacts(app_repo, job, application.id, job_summary, sample.artifact_paths)

        grounded_answers: list[GroundedAnswer] = []
        for question_record, question in zip(stored_questions, questions, strict=False):
            memory_context = _answer_memory_context(question, job)
            answer_memory = [
                {
                    "canonical_question": memory.canonical_question,
                    "answer_text": memory.answer_text,
                    "grounded_fact_ids": list(memory.grounded_fact_ids or []),
                    "approved": memory.approved,
                    "context_constraints": dict(memory.context_constraints or {}),
                }
                for memory in app_repo.find_answer_memory(
                    question.normalized_key or runtime.grounding.canonicalize_question(question.prompt_text),
                    context_constraints=memory_context,
                )
            ]
            if question.question_type.value == "file":
                answer = _file_answer_for_question(question, artifacts_by_kind)
            else:
                answer = await runtime.grounding.answer_question(
                    question.prompt_text,
                    facts,
                    options=question.options,
                    normalized_key=question.normalized_key,
                    answer_memory=answer_memory,
                    memory_context=memory_context,
                )
            grounded_answers.append(answer)
            app_repo.store_answer(question_record.id, answer)
            if (
                answer.canonical_question
                and answer.verification_status == VerificationStatus.VERIFIED
                and not question.sensitive
                and question.question_type.value == "deterministic"
            ):
                app_repo.store_answer_memory(
                    answer.canonical_question,
                    answer,
                    approved=True,
                    context_constraints=memory_context,
                )

        app_service = ApplicationService(job_repo, app_repo)
        question_answers = app_repo.list_answers_for_application(application.id)
        missing_required = app_service.missing_required_questions(question_answers)
        ungrounded = app_service.ungrounded_question_prompts(question_answers)
        artifacts = list(app_repo.list_artifacts(application_id=application.id))
        artifact_kinds = {artifact.kind for artifact in artifacts}
        artifact_validation_failures = app_service.artifact_validation_failures(artifacts)
        sensitive_questions = [question.prompt_text for question in questions if question.sensitive]
        duplicate_siblings = [
            {
                "job_id": sibling.id,
                "source": sibling.source_adapter,
                "title": sibling.title,
                "status": sibling.lifecycle_status.value,
            }
            for sibling in job_repo.duplicate_siblings(job.duplicate_cluster_key, exclude_job_id=job.id)
        ]
        gate = app_service.submission_gate(
            job,
            application,
            artifact_kinds | {ArtifactKind.REVIEW_PACKET},
            ungrounded,
            PolicyMode.REVIEW_ONLY,
            missing_required_fields=missing_required,
            artifact_validation_failures=artifact_validation_failures,
        )
        packet = ReviewPacket(
            job_id=job.id,
            application_id=application.id,
            qualification=qualification_for_job(job_model, query, facts),
            source_policy=PolicyMode.REVIEW_ONLY,
            artifacts=list(
                dict.fromkeys(
                    [
                        *(path for path in artifacts_by_kind.values() if path),
                        *job_summary.screenshot_paths,
                        *_iter_dom_snapshot_paths(job_summary),
                    ]
                )
            ),
            artifact_validation_failures=artifact_validation_failures,
            questions=questions,
            answers=grounded_answers,
            sensitive_questions=sensitive_questions,
            duplicate_cluster_siblings=duplicate_siblings,
            submit_ready=False,
            handoff_url=apply_url,
        )
        packet_path = app_service.review_packet_path(runtime.workspace, job.id)
        packet_path.parent.mkdir(parents=True, exist_ok=True)
        packet_path.write_text(packet.model_dump_json(indent=2), encoding="utf-8")
        app_repo.store_artifact(
            ArtifactKind.REVIEW_PACKET,
            str(packet_path),
            hash_content(packet.model_dump(mode="json")),
            {
                "submit_ready": False,
                "valid": not artifact_validation_failures,
                "failure_reason": "; ".join(artifact_validation_failures),
                "training": True,
            },
            job_posting_id=job.id,
            application_id=application.id,
        )

        review_flags = _merge_flags(gate.missing_required_fields + gate.ungrounded_answers + sensitive_questions + ["training_approved"])
        if gate.is_ready:
            application = app_repo.mark_prepared(application.id, flags=review_flags)
            application.review_status = ReviewStatus.PENDING
        else:
            application.status = JobLifecycleStatus.NEEDS_USER_INPUT
            application.review_status = ReviewStatus.NEEDS_USER_INPUT
            application.review_flags = review_flags
        job.lifecycle_status = application.status

        sample_repo.attach_promotion(
            sample.sample_id,
            job_id=job.id,
            application_id=application.id,
            review_packet_path=str(packet_path),
        )
        audit_repo.emit(
            event_type="training.sample.promoted",
            entity_type="application",
            entity_id=application.id,
            run_id=sample.run_id,
            payload={
                "sample_id": sample.sample_id,
                "job_id": job.id,
                "application_id": application.id,
                "status": application.status.value,
                "review_status": application.review_status.value,
                "question_count": len(questions),
                "review_packet_path": str(packet_path),
            },
        )
        return TrainingPromotionResultSummary(
            sample_id=sample.sample_id,
            run_id=sample.run_id,
            review_status=sample.review_status,
            promoted=True,
            job_id=job.id,
            application_id=application.id,
            review_packet_path=str(packet_path),
            notes=["Promoted into the existing review/apply pipeline without submit."],
        )


async def promote_training_samples(runtime, *, sample_id: str | None = None, run_id: str | None = None, limit: int = 100) -> list[TrainingPromotionResultSummary]:
    with runtime.session_scope() as session:
        sample_repo = TrainingSampleRepository(session)
        if sample_id:
            sample = sample_repo.get(sample_id)
            if sample is None:
                raise ValueError(f"Training sample not found: {sample_id}")
            targets = [sample]
        else:
            target_run_id = run_id
            if target_run_id is None:
                latest = RunRepository(session).list_runs_by_type(TRAINING_RUN_TYPE, limit=1)
                target_run_id = latest[0].id if latest else None
            if target_run_id is None:
                raise ValueError("No training run found.")
            targets = sample_repo.list_samples(run_id=target_run_id, review_statuses=["approved"], limit=limit)
            if not targets:
                raise ValueError("No approved training samples found for promotion.")
    return [await _promote_training_sample(runtime, sample) for sample in targets]


def _normalize_prompt_result(result: Any) -> tuple[bool, str | None, str | None]:
    if isinstance(result, dict):
        return bool(result.get("approved")), _clean_text(result.get("reason_code") or result.get("rejection_reason_code")), _clean_text(result.get("note") or result.get("rejection_note"))
    if isinstance(result, tuple):
        if len(result) >= 3:
            return bool(result[0]), _clean_text(result[1]), _clean_text(result[2])
        if len(result) == 2:
            return bool(result[0]), _clean_text(result[1]), None
        if len(result) == 1:
            return bool(result[0]), None, None
    return bool(result), None, None


def _stable_job_sample_key(job_data: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (str(job_data.get("company") or "").casefold(), str(job_data.get("title") or "").casefold(), str(job_data.get("posted_text") or "").casefold(), str(job_data.get("url") or ""), str(job_data.get("row_text") or ""))

async def run_greenhouse_training(
    runtime,
    *,
    url: str = "https://my.greenhouse.io/jobs",
    posted_window: int = 10,
    batch_size: int = 5,
    cdp_url: str = "http://127.0.0.1:9222",
    keep_tabs_open: bool = False,
    prompt_fn=None,
    run_id: str | None = None,
) -> TrainingRunSummary:
    """Execute a full training run with durable persistence and review-first promotion."""
    if posted_window not in VALID_POSTED_WINDOWS:
        raise ValueError(f"posted_window must be one of {VALID_POSTED_WINDOWS}, got {posted_window}")

    _ensure_training_ready(runtime)
    started_at = datetime.now(timezone.utc)
    with runtime.session_scope() as session:
        run_repo = RunRepository(session)
        audit_repo = AuditRepository(session)
        if run_id:
            run = run_repo.get_run(run_id)
            if run is None:
                raise ValueError(f"Training run not found: {run_id}")
            run.status = RunStatus.RUNNING
            run.started_at = run.started_at or started_at
            run.checkpoint_state = {**(run.checkpoint_state or {}), "url": url, "posted_window": posted_window, "batch_size": batch_size, "cdp_url": cdp_url, "keep_tabs_open": keep_tabs_open}
        else:
            run = run_repo.create_run(TRAINING_RUN_TYPE, ApplicationMode.DRY_RUN, checkpoint_state={"started_at": started_at.isoformat(), "url": url, "posted_window": posted_window, "batch_size": batch_size, "cdp_url": cdp_url, "keep_tabs_open": keep_tabs_open})
        audit_repo.emit(event_type="training.run.started", entity_type="training_run", entity_id=run.id, run_id=run.id, payload={"url": url, "posted_window": posted_window, "batch_size": batch_size, "cdp_url": cdp_url, "keep_tabs_open": keep_tabs_open})
        active_run_id = run.id

    artifacts_dir = runtime.config.artifacts_dir(runtime.workspace) / "training" / active_run_id
    summary = TrainingRunSummary(run_id=active_run_id, started_at=started_at, start_url=url, posted_window=posted_window, batch_size=batch_size, cdp_url=cdp_url)
    prior_feedback = load_prior_training_feedback(runtime, limit=40)
    failed = False

    try:
        async with cdp_browser_context(cdp_url, keep_tabs_open=keep_tabs_open) as (_browser, context):
            jobs_page = await find_or_open_tab(context, url)
            await set_posted_window(jobs_page, posted_window)
            visible_jobs = await harvest_visible_jobs(jobs_page, max_jobs=max(batch_size * 4, batch_size))
            if not visible_jobs:
                summary.notes.append("No jobs found on the page.")
            else:
                sampled = sorted(visible_jobs, key=_stable_job_sample_key)[:batch_size]
                for index, job_data in enumerate(sampled, start=1):
                    inspection = {"page_captures": [], "company_page_url": None, "apply_url": None, "job_description_text": "", "form_fields": [], "screenshot_paths": [], "dom_snapshot_paths": [], "layout_notes": []}
                    try:
                        inspection = await inspect_training_job_path(jobs_page, job_data, artifacts_dir / f"sample-{index:02d}")
                    except Exception as exc:
                        inspection["layout_notes"].append(f"Navigation error: {exc}")

                    page_captures = list(inspection.get("page_captures") or [])
                    view_url = page_captures[0].url if page_captures else str(job_data.get("url") or jobs_page.url)
                    dom_paths = [path for path in inspection.get("dom_snapshot_paths") or [] if path]
                    job_summary = GreenhouseTrainingJobSummary(
                        job_url=view_url,
                        job_title=_clean_text(job_data.get("title")),
                        company_name=_clean_text(job_data.get("company")),
                        location=_clean_text(job_data.get("location")),
                        posted_text=_clean_text(job_data.get("posted_text")),
                        company_page_url=_clean_text(inspection.get("company_page_url")),
                        apply_url=_clean_text(inspection.get("apply_url")),
                        description_snippet=_clean_text((inspection.get("job_description_text") or "")[:2000]),
                        form_fields=list(inspection.get("form_fields") or []),
                        screenshot_paths=[str(path) for path in inspection.get("screenshot_paths") or []],
                        dom_snapshot_path=dom_paths[-1] if dom_paths else None,
                        page_captures=page_captures,
                        layout_notes=list(inspection.get("layout_notes") or []),
                    )

                    artifact_paths: dict[str, str | None] = {}
                    try:
                        draft = await generate_training_draft(runtime, job_description=job_summary.description_snippet or "", job_title=job_summary.job_title, company_name=job_summary.company_name, prior_feedback=prior_feedback)
                        job_summary.draft_change_summary = draft.adaptation_summary
                        artifact_paths = render_training_artifacts(runtime, job_summary, draft)
                    except TrainingModeError as exc:
                        job_summary.layout_notes.append(f"Drafting skipped: {exc}")
                    except Exception as exc:
                        job_summary.layout_notes.append(f"Artifact rendering error: {exc}")

                    approved = False
                    rejection_reason_code = None
                    rejection_note = None
                    if prompt_fn is not None:
                        approved, rejection_reason_code, rejection_note = _normalize_prompt_result(prompt_fn(job_summary, artifact_paths))
                    else:
                        rejection_reason_code = "noninteractive_default"

                    review = TrainingReviewOutcome(job_url=job_summary.job_url, job_title=job_summary.job_title, company_name=job_summary.company_name, approved=approved, rejection_note=None if approved else rejection_note, rejection_reason_code=None if approved else _normalize_rejection_reason_code(rejection_reason_code, rejection_note), resume_artifact_path=artifact_paths.get("resume_pdf_path"), cover_letter_artifact_path=artifact_paths.get("cover_letter_pdf_path"), reviewed_at=datetime.now(timezone.utc))
                    review.feedback_summary = _feedback_summary(review.approved, review.rejection_reason_code, review.rejection_note)
                    sample = _save_training_sample(runtime, _build_training_sample(run_id=active_run_id, jobs_page_url=url, job_summary=job_summary, artifact_paths=artifact_paths, review=review))

                    if review.approved:
                        promotion = await _promote_training_sample(runtime, sample)
                        if promotion.promoted:
                            sample.promoted_job_id = promotion.job_id
                            sample.promoted_application_id = promotion.application_id
                            sample.review_packet_path = promotion.review_packet_path
                            job_summary.linked_job_id = promotion.job_id
                            job_summary.linked_application_id = promotion.application_id
                            job_summary.review_packet_path = promotion.review_packet_path
                            review.linked_job_id = promotion.job_id
                            review.linked_application_id = promotion.application_id
                            review.review_packet_path = promotion.review_packet_path
                            if promotion.application_id:
                                summary.promoted_application_ids.append(promotion.application_id)
                            if promotion.review_packet_path:
                                summary.review_packet_paths.append(promotion.review_packet_path)

                    summary.sampled_jobs.append(job_summary)
                    summary.reviews.append(review)
                    summary.artifact_paths.extend(path for path in artifact_paths.values() if path)
                    summary.approved_count += 1 if review.approved else 0
                    summary.rejected_count += 0 if review.approved else 1
                    persist_training_feedback(runtime, review, active_run_id)
                    prior_feedback.insert(0, _sample_feedback_payload(sample))

                    if index < len(sampled) and jobs_page.url != url:
                        await jobs_page.goto(url, wait_until="domcontentloaded")
                        await set_posted_window(jobs_page, posted_window)
    except CDPAttachError:
        failed = True
        raise
    except Exception as exc:
        failed = True
        summary.notes.append(f"Training run error: {exc}")
        raise
    finally:
        summary.completed_at = datetime.now(timezone.utc)
        with runtime.session_scope() as session:
            RunRepository(session).complete_run(active_run_id, RunStatus.FAILED if failed else RunStatus.COMPLETED, checkpoint_state=summary.model_dump(mode="json"))
            AuditRepository(session).emit(event_type="training.run.failed" if failed else "training.run.completed", entity_type="training_run", entity_id=active_run_id, run_id=active_run_id, payload={"sampled_count": len(summary.sampled_jobs), "approved_count": summary.approved_count, "rejected_count": summary.rejected_count, "promoted_application_ids": list(summary.promoted_application_ids), "review_packet_paths": list(summary.review_packet_paths), "duration_seconds": (summary.completed_at - started_at).total_seconds() if summary.completed_at else None, "notes": list(summary.notes)})
    return summary











