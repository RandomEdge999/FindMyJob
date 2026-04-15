from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from findmyjob.apply.browser import analyze_dom_snapshot
from findmyjob.apply.service import ApplicationService
from findmyjob.core.enums import PolicyMode
from findmyjob.core.policies import resolve_source_policy
from findmyjob.core.types import NormalizedJobPosting
from findmyjob.db.repositories import ApplicationRepository, AuditRepository, JobRepository
from findmyjob.orchestrator.service import Orchestrator
from findmyjob.sources.classification import classify_job


def unique_strings(values: Sequence[Any]) -> list[str]:
    unique: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in unique:
            unique.append(item)
    return unique


def policy_for_source(source_kind: str, settings: Any | None = None, autonomous_settings: Any | None = None) -> PolicyMode:
    return resolve_source_policy(source_kind, settings, autonomous_settings)


def build_job_model(job: Any) -> NormalizedJobPosting:
    return NormalizedJobPosting(
        company_name=job.company.display_name,
        company_key=job.company.normalized_name,
        title=job.title,
        source=job.source_adapter,
        source_kind=job.source_kind,
        source_job_id=job.source_job_id,
        posting_url=job.posting_url,
        apply_url=job.apply_url,
        location_raw=job.location_raw,
        location_normalized=job.location_normalized,
        country_code=job.country_code,
        region_code=job.region_code,
        city=job.city,
        location_scope=job.location_scope,
        workplace_type=job.workplace_type,
        employment_type=job.employment_type,
        experience_level=job.experience_level,
        posted_at=job.posted_at,
        source_updated_at=job.source_updated_at,
        compensation=job.compensation,
        compensation_min=job.compensation_min,
        compensation_max=job.compensation_max,
        compensation_currency=job.compensation_currency,
        compensation_interval=job.compensation_interval,
        remote_country_codes=list(job.remote_country_codes or []),
        company_employee_count_min=job.company.employee_count_min,
        company_employee_count_max=job.company.employee_count_max,
        company_size_bucket=job.company.company_size_bucket,
        metadata_quality=dict(job.metadata_quality or {}),
        description=job.description,
        normalized_description=job.normalized_description,
        discovered_at=job.discovered_at,
        job_identity_key=job.job_identity_key,
        duplicate_cluster_key=job.duplicate_cluster_key,
        lifecycle_status=job.lifecycle_status,
        notes=dict(job.notes or {}),
    )


def classification_payload(job: Any | None) -> dict[str, Any] | None:
    if job is None:
        return None
    autonomous = dict((job.notes or {}).get("autonomous") or {})
    classification = classify_job(
        source_kind=job.source_kind,
        apply_url=job.apply_url,
        posting_url=job.posting_url,
        source_adapter=job.source_adapter,
    )
    return {
        "board_family": str(autonomous.get("board_family") or classification.board_family.value),
        "automation_tier": str(autonomous.get("automation_tier") or classification.automation_tier.value),
        "supports_auto_submit": bool(autonomous.get("supports_auto_submit", classification.supports_auto_submit)),
        "automation_skip_reason": autonomous.get("automation_skip_reason") or classification.automation_skip_reason,
        "detection_method": autonomous.get("classification_method") or classification.detection_method,
        "confidence": float(autonomous.get("confidence") or classification.confidence or 0.0),
    }


def autonomous_notes_payload(job: Any | None) -> dict[str, Any]:
    notes = dict((getattr(job, "notes", {}) or {}).get("autonomous") or {}) if job is not None else {}
    return {
        "matched_presets": list(notes.get("matched_presets") or []),
        "skip_reason": notes.get("skip_reason"),
        "submit_attempted": notes.get("submit_attempted"),
        "submit_result": notes.get("submit_result"),
        "submit_failure_reason": notes.get("submit_failure_reason"),
        "last_evaluated_at": notes.get("last_evaluated_at"),
    }


def submission_gate_payload(
    runtime: Any,
    job: Any | None,
    application: Any | None,
    job_repo: JobRepository,
    app_repo: ApplicationRepository,
) -> dict[str, Any] | None:
    if job is None or application is None:
        return None
    question_answers = app_repo.list_answers_for_application(application.id)
    artifacts = list(app_repo.list_artifacts(application_id=application.id))
    app_service = ApplicationService(job_repo, app_repo)
    plan_missing: list[str] = []
    try:
        adapter = Orchestrator(runtime).source_adapters().get(job.source_adapter)
        if adapter is not None:
            plan = adapter.bind_answers(
                build_job_model(job),
                question_answers,
                app_service.artifact_path_map(artifacts),
            )
            plan_missing = list(plan.missing_required_fields or [])
    except Exception:
        plan_missing = []
    gate = app_service.submission_gate(
        job,
        application,
        {artifact.kind for artifact in artifacts},
        app_service.ungrounded_question_prompts(question_answers),
        policy_for_source(job.source_kind, runtime.config.policy, runtime.config.autonomous),
        missing_required_fields=unique_strings(app_service.missing_required_questions(question_answers) + plan_missing),
        artifact_validation_failures=app_service.artifact_validation_failures(artifacts),
        low_confidence_answers=app_service.low_confidence_answers(question_answers),
        warnings=app_service.artifact_validation_warnings(artifacts),
    )
    payload = gate.model_dump(mode="json")
    payload["is_ready"] = gate.is_ready
    payload["blockers"] = (
        [{"category": "missing_required_field", "label": item} for item in gate.missing_required_fields]
        + [{"category": "missing_artifact", "label": item.value if hasattr(item, "value") else str(item)} for item in gate.missing_artifacts]
        + [{"category": "ungrounded_answer", "label": item} for item in gate.ungrounded_answers]
        + [{"category": "low_confidence_answer", "label": item} for item in gate.low_confidence_answers]
    )
    return payload


def dom_analysis_payload(snapshot_path: str | None) -> dict[str, Any] | None:
    path_value = str(snapshot_path or "").strip()
    if not path_value:
        return None
    try:
        html = Path(path_value).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return dict(analyze_dom_snapshot(html) or {})


def submit_attempt_history_payload(artifact_ref: Any, attempt: Any) -> dict[str, Any]:
    payload = dict(attempt.payload or {})
    evidence = dict(payload.get("evidence") or {})
    return {
        "attempt_id": attempt.id,
        "status": attempt.status,
        "source_policy": attempt.source_policy.value if hasattr(attempt.source_policy, "value") else str(attempt.source_policy),
        "created_at": attempt.created_at.isoformat() if attempt.created_at is not None else None,
        "message": payload.get("message"),
        "submitted": payload.get("submitted"),
        "uncertain": payload.get("uncertain"),
        "failure_reason": evidence.get("failure_reason"),
        "failure_classification": dict(payload.get("failure_classification") or {}) or None,
        "confirmation_text": evidence.get("confirmation_text"),
        "confirmation_strategy": evidence.get("confirmation_strategy"),
        "visible_validation_errors": list(evidence.get("visible_validation_errors") or []),
        "matched_confirmation_markers": list(evidence.get("matched_confirmation_markers") or []),
        "missing_required_controls": list(evidence.get("missing_required_controls") or []),
        "submit_button_present": evidence.get("submit_button_present"),
        "submit_button_enabled": evidence.get("submit_button_enabled"),
        "dom_analysis": dom_analysis_payload(evidence.get("post_submit_dom_snapshot_path") or evidence.get("dom_snapshot_path")),
        "artifacts": [
            item
            for item in [
                artifact_ref("submission_receipt", payload.get("snapshot_path")) if payload.get("snapshot_path") else None,
                artifact_ref("submission_trace", payload.get("trace_path")) if payload.get("trace_path") else None,
                artifact_ref("pre_submit_snapshot", evidence.get("pre_submit_snapshot_path")) if evidence.get("pre_submit_snapshot_path") else None,
                artifact_ref("final_snapshot", evidence.get("final_snapshot_path")) if evidence.get("final_snapshot_path") else None,
                artifact_ref("dom_snapshot", evidence.get("dom_snapshot_path")) if evidence.get("dom_snapshot_path") else None,
                artifact_ref("post_submit_dom_snapshot", evidence.get("post_submit_dom_snapshot_path")) if evidence.get("post_submit_dom_snapshot_path") else None,
            ]
            if item is not None
        ],
    }


def recent_activity_payload(
    audit_repo: AuditRepository,
    app_repo: ApplicationRepository,
    job_repo: JobRepository,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    events = audit_repo.list_events(limit=max(limit * 4, limit))
    allowed_types = {
        "application.submitted",
        "application.submit_blocked",
        "review.action",
        "task.failed",
        "job.discovered",
    }
    activities: list[dict[str, Any]] = []
    for event in events:
        if event.event_type not in allowed_types:
            continue
        payload = dict(event.payload or {})
        job = None
        if event.entity_type == "application" and event.entity_id:
            application = app_repo.get_application(event.entity_id)
            if application is not None:
                job = job_repo.get_job(application.job_posting_id)
        elif event.entity_type == "job_posting" and event.entity_id:
            job = job_repo.get_job(event.entity_id)
        automation_metadata = dict(payload.get("automation_metadata") or {})
        if not automation_metadata and job is not None:
            automation_metadata = classification_payload(job) or {}
        failure_classification = dict(payload.get("failure_classification") or {})
        low_confidence_answers = list(payload.get("low_confidence_answers") or [])
        summary = payload.get("message") or payload.get("reason") or payload.get("status") or event.event_type
        detail = None
        if low_confidence_answers:
            detail = "; ".join(low_confidence_answers[:3])
        elif failure_classification.get("failure_reason"):
            detail = str(failure_classification.get("failure_reason"))
        elif payload.get("error"):
            detail = str(payload.get("error"))
        elif payload.get("failure_reason"):
            detail = str(payload.get("failure_reason"))
        tone = "neutral"
        if event.event_type == "application.submit_blocked":
            tone = "warning"
        elif event.event_type == "task.failed":
            tone = "danger"
        elif failure_classification:
            category = str(failure_classification.get("failure_category") or "")
            if category in {"rate_limited", "timeout"}:
                tone = "warning"
            elif category:
                tone = "danger"
        elif "submitted" in str(summary):
            tone = "success"
        activities.append(
            {
                "event_id": event.id,
                "event_type": event.event_type,
                "event_label": event.event_type.replace(".", " ").replace("_", " ").title(),
                "summary": str(summary),
                "detail": detail,
                "tone": tone,
                "created_at": event.created_at.isoformat() if event.created_at is not None else None,
                "run_id": event.run_id,
                "task_id": event.task_id,
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
                "job_title": job.title if job is not None else None,
                "company": job.company.display_name if job is not None else None,
                "automation_metadata": automation_metadata or None,
                "failure_classification": failure_classification or None,
                "low_confidence_answers": low_confidence_answers,
            }
        )
        if len(activities) >= limit:
            break
    return activities


def classification_summary(daily: dict[str, Any], review: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    family_counts: Counter[str] = Counter()
    tier_counts: Counter[str] = Counter()
    seen_job_ids: set[str] = set()
    buckets = [
        "shortlisted",
        "watching",
        "new_matching",
        "ready_for_review",
        "needs_user_input",
        "approved_pending_submit",
        "suppressed",
    ]
    candidates: list[dict[str, Any]] = list(review.get("items") or [])
    for key in buckets:
        candidates.extend(list(daily.get(key) or []))
    for item in candidates:
        job_id = str(item.get("job_id") or "")
        if job_id and job_id in seen_job_ids:
            continue
        if job_id:
            seen_job_ids.add(job_id)
        classification = dict(item.get("classification") or {})
        family_counts[str(classification.get("board_family") or "unknown")] += 1
        tier_counts[str(classification.get("automation_tier") or "unsupported_high_friction")] += 1
    return {
        "families": [{"key": key, "count": count} for key, count in family_counts.most_common()],
        "tiers": [{"key": key, "count": count} for key, count in tier_counts.most_common()],
    }


def dashboard_gate_summary(review: dict[str, Any]) -> dict[str, int]:
    items = list(review.get("items") or [])
    return {
        "ready": sum(1 for item in items if (item.get("gate") or {}).get("is_ready")),
        "blocked_by_low_confidence": sum(1 for item in items if (item.get("gate") or {}).get("low_confidence_answers")),
        "blocked_by_missing_fields": sum(1 for item in items if (item.get("gate") or {}).get("missing_required_fields")),
        "blocked_by_missing_artifacts": sum(1 for item in items if (item.get("gate") or {}).get("missing_artifacts")),
        "blocked_by_ungrounded": sum(1 for item in items if (item.get("gate") or {}).get("ungrounded_answers")),
    }


