from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Sequence
import re

from findmyjob.core.enums import PersonalSuppressionScope, PersonalTriageStatus, SponsorshipFit
from findmyjob.core.filtering import evaluate_job_against_query, value_or_unknown
from findmyjob.core.types import PersonalJobMatchExplanation, PersonalSuppressionRule, PersonalTriageDecision
from findmyjob.db.models import JobPosting
from findmyjob.db.repositories import AuditRepository, PersonalTriageRepository

SUPPRESSING_TRIAGE_STATUSES = {
    PersonalTriageStatus.DISMISSED,
    PersonalTriageStatus.ARCHIVED,
}


@dataclass(slots=True)
class PersonalJobAssessment:
    job_id: str
    score: int
    explanation: PersonalJobMatchExplanation


def normalize_title_key(value: str | None) -> str | None:
    cleaned = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
    return cleaned or None


def triage_decision_or_default(job_id: str, decision: PersonalTriageDecision | None = None) -> PersonalTriageDecision:
    return decision or PersonalTriageDecision(job_id=job_id)


def is_suppressed_triage_status(status: PersonalTriageStatus) -> bool:
    return status in SUPPRESSING_TRIAGE_STATUSES


def suppression_reason_for_rule(rule: PersonalSuppressionRule) -> str:
    company = rule.company_display_name or rule.company_normalized_name or "company"
    if rule.scope == PersonalSuppressionScope.COMPANY:
        return f"Suppressed company: {company}"
    if rule.scope == PersonalSuppressionScope.COMPANY_TITLE:
        title = rule.title_label or rule.title_key or "similar title"
        return f"Suppressed similar title at {company}: {title}"
    return f"Suppressed job rule for {company}"


def rule_matches_job(job: JobPosting, rule: PersonalSuppressionRule) -> bool:
    if not rule.active:
        return False
    if rule.scope == PersonalSuppressionScope.JOB:
        return rule.job_id == job.id
    if rule.company_normalized_name and (job.company is None or job.company.normalized_name != rule.company_normalized_name):
        return False
    if rule.scope == PersonalSuppressionScope.COMPANY:
        return True
    if rule.scope == PersonalSuppressionScope.COMPANY_TITLE:
        return normalize_title_key(job.title) == rule.title_key
    return False


def suppression_reasons_for_job(
    job: JobPosting,
    decision: PersonalTriageDecision | None,
    rules: Sequence[PersonalSuppressionRule],
) -> list[str]:
    reasons: list[str] = []
    resolved = triage_decision_or_default(job.id, decision)
    if resolved.status == PersonalTriageStatus.DISMISSED:
        reasons.append("Job dismissed by operator")
    elif resolved.status == PersonalTriageStatus.ARCHIVED:
        reasons.append("Job archived by operator")
    for rule in rules:
        if rule_matches_job(job, rule):
            reasons.append(suppression_reason_for_rule(rule))
    return _unique(reasons)


def sort_key_for_assessment(job: JobPosting, assessment: PersonalJobAssessment) -> tuple[Any, ...]:
    anchor = _aware_datetime(job.posted_at or job.discovered_at)
    return (
        assessment.score,
        anchor.timestamp() if anchor is not None else 0.0,
        (job.company.display_name if job.company is not None else "").casefold(),
        job.title.casefold(),
        job.id,
    )


def assess_personal_job(
    job: JobPosting,
    qualification: Any,
    selections: Sequence[Any],
    personal: Any,
    *,
    decision: PersonalTriageDecision | None = None,
    rules: Sequence[PersonalSuppressionRule] = (),
) -> PersonalJobAssessment:
    resolved_decision = triage_decision_or_default(job.id, decision)
    matched_query_names: list[str] = []
    query_summaries: dict[str, str] = {}
    best_evaluation = None
    evaluation_job = _evaluation_job(job)

    for selection in selections:
        evaluation = evaluate_job_against_query(evaluation_job, selection.query)
        if not evaluation.matched:
            continue
        matched_query_names.append(selection.name)
        query_summaries[selection.name] = selection.query.summary()
        if best_evaluation is None or evaluation.score_delta > best_evaluation.score_delta:
            best_evaluation = evaluation

    match_reasons = _unique(list(best_evaluation.reasons if best_evaluation is not None else []))
    ranking_reasons: list[str] = []
    penalties: list[str] = []
    warnings: list[str] = []
    breakdown: dict[str, int] = {}
    score = 0

    qualification_score = int(getattr(qualification, "score", 0) or 0)
    breakdown["qualification"] = qualification_score
    score += qualification_score
    if qualification_score > 0:
        ranking_reasons.append(f"Qualification score +{qualification_score}")
    elif qualification_score < 0:
        penalties.append(f"Qualification score {qualification_score}")

    query_bonus = max(0, int(getattr(best_evaluation, "score_delta", 0) or 0))
    if query_bonus:
        breakdown["query_match"] = query_bonus
        score += query_bonus
        ranking_reasons.append(f"Strong preset match (+{query_bonus})")

    multi_preset_bonus = max(0, (len(matched_query_names) - 1) * 8)
    if multi_preset_bonus:
        breakdown["preset_overlap"] = multi_preset_bonus
        score += multi_preset_bonus
        ranking_reasons.append(f"Matched {len(matched_query_names)} presets")

    recency_delta, recency_reason = _recency_component(job)
    if recency_delta:
        breakdown["recency"] = recency_delta
        score += recency_delta
        if recency_delta > 0:
            ranking_reasons.append(recency_reason)
        else:
            penalties.append(recency_reason)
    elif recency_reason:
        warnings.append(recency_reason)

    location_delta, location_reason = _location_component(best_evaluation)
    if location_delta:
        breakdown["location_fit"] = location_delta
        score += location_delta
        ranking_reasons.append(location_reason)

    experience_delta, experience_reason, experience_warning = _experience_component(job, personal, selections)
    if experience_delta:
        breakdown["experience_fit"] = experience_delta
        score += experience_delta
        if experience_delta > 0:
            ranking_reasons.append(experience_reason)
        else:
            penalties.append(experience_reason)
    if experience_warning:
        warnings.append(experience_warning)

    compensation_delta, compensation_reason = _compensation_component(job, personal, selections)
    if compensation_delta:
        breakdown["compensation"] = compensation_delta
        score += compensation_delta
        if compensation_delta > 0:
            ranking_reasons.append(compensation_reason)
        else:
            penalties.append(compensation_reason)

    sponsorship_delta, sponsorship_reason, sponsorship_warning = _sponsorship_component(qualification, personal, selections)
    if sponsorship_delta:
        breakdown["sponsorship"] = sponsorship_delta
        score += sponsorship_delta
        if sponsorship_delta > 0:
            ranking_reasons.append(sponsorship_reason)
        else:
            penalties.append(sponsorship_reason)
    if sponsorship_warning:
        warnings.append(sponsorship_warning)

    clarity_delta, clarity_reason = _clarity_component(job, qualification)
    if clarity_delta:
        breakdown["clarity"] = clarity_delta
        score += clarity_delta
        penalties.append(clarity_reason)
    elif clarity_reason:
        warnings.append(clarity_reason)

    triage_bonus = {
        PersonalTriageStatus.SHORTLISTED: 35,
        PersonalTriageStatus.WATCHING: 12,
        PersonalTriageStatus.DISMISSED: -40,
        PersonalTriageStatus.ARCHIVED: -60,
        PersonalTriageStatus.NEW: 0,
    }[resolved_decision.status]
    if triage_bonus:
        breakdown["triage"] = triage_bonus
        score += triage_bonus
        if triage_bonus > 0:
            ranking_reasons.append(f"Marked {resolved_decision.status.value} by operator")
        else:
            penalties.append(f"Marked {resolved_decision.status.value} by operator")

    suppressed_reasons = suppression_reasons_for_job(job, resolved_decision, rules)
    suppressed = bool(suppressed_reasons)
    priority_label = _priority_label(score, resolved_decision.status, suppressed)
    headline = _headline(suppressed, suppressed_reasons, ranking_reasons, match_reasons)

    autonomous = dict(job.notes or {}).get('autonomous') or {}
    explanation = PersonalJobMatchExplanation(
        job_id=job.id,
        score=score,
        priority_label=priority_label,
        triage_status=resolved_decision.status,
        matched_query_names=matched_query_names,
        query_summaries=query_summaries,
        match_reasons=_unique(match_reasons),
        ranking_reasons=_unique(ranking_reasons),
        penalties=_unique(penalties),
        warnings=_unique(warnings),
        breakdown=breakdown,
        suppressed=suppressed,
        suppression_reasons=suppressed_reasons,
        headline=headline,
        ai_greenlight=autonomous.get('green_light'),
        ai_score=autonomous.get('score'),
        ai_reasons=list(autonomous.get('reasons') or []),
        ai_warnings=list(autonomous.get('warnings') or []),
        ai_skip_reason=autonomous.get('skip_reason'),
    )
    return PersonalJobAssessment(job_id=job.id, score=score, explanation=explanation)


def apply_job_triage(
    runtime,
    job_id: str,
    status: PersonalTriageStatus,
    *,
    reason_code: str | None = None,
    note: str | None = None,
    suppression_scope: PersonalSuppressionScope = PersonalSuppressionScope.JOB,
    updated_by: str = "operator",
) -> tuple[PersonalTriageDecision, list[PersonalSuppressionRule]]:
    with runtime.session_scope() as session:
        job = session.get(JobPosting, job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")
        repo = PersonalTriageRepository(session)
        decision = repo.set_decision(job_id, status, reason_code=reason_code, note=note, updated_by=updated_by)
        created_rules: list[PersonalSuppressionRule] = []
        if status in SUPPRESSING_TRIAGE_STATUSES and suppression_scope != PersonalSuppressionScope.JOB:
            rule = repo.upsert_suppression_rule(job, suppression_scope, reason_code=reason_code, note=note, updated_by=updated_by)
            if rule is not None:
                created_rules.append(rule)
        AuditRepository(session).emit(
            "personal.triage.updated",
            "job_posting",
            job_id,
            payload={
                "status": status.value,
                "reason_code": reason_code,
                "note": note,
                "suppression_scope": suppression_scope.value,
                "created_rule_ids": [rule.id for rule in created_rules if rule.id],
                "updated_by": updated_by,
            },
        )
        return decision, created_rules


def clear_job_suppression(
    runtime,
    job_id: str,
    *,
    clear_job_status: bool = True,
    clear_scopes: Sequence[PersonalSuppressionScope] = (),
    updated_by: str = "operator",
) -> tuple[PersonalTriageDecision, list[PersonalSuppressionRule]]:
    with runtime.session_scope() as session:
        job = session.get(JobPosting, job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")
        repo = PersonalTriageRepository(session)
        decision = repo.get_decision(job_id)
        if clear_job_status or decision is None:
            decision = repo.set_decision(job_id, PersonalTriageStatus.NEW, updated_by=updated_by)
        cleared_rules = repo.deactivate_matching_rules_for_job(job, scopes=list(clear_scopes), updated_by=updated_by)
        AuditRepository(session).emit(
            "personal.triage.cleared",
            "job_posting",
            job_id,
            payload={
                "clear_job_status": clear_job_status,
                "clear_scopes": [scope.value for scope in clear_scopes],
                "cleared_rule_ids": [rule.id for rule in cleared_rules if rule.id],
                "updated_by": updated_by,
            },
        )
        return decision, cleared_rules


def _evaluation_job(job: JobPosting) -> Any:
    return SimpleNamespace(
        title=job.title,
        normalized_description=job.normalized_description,
        location_raw=job.location_raw,
        location_normalized=job.location_normalized,
        city=job.city,
        region_code=job.region_code,
        country_code=job.country_code,
        remote_country_codes=list(job.remote_country_codes or []),
        workplace_type=job.workplace_type,
        employment_type=job.employment_type,
        location_scope=job.location_scope,
        experience_level=job.experience_level,
        company_size_bucket=job.company.company_size_bucket if job.company is not None else None,
        posted_at=job.posted_at,
        compensation_min=job.compensation_min,
        compensation_max=job.compensation_max,
        compensation_currency=job.compensation_currency,
    )


def _recency_component(job: JobPosting) -> tuple[int, str]:
    anchor = _aware_datetime(job.posted_at or job.discovered_at)
    if anchor is None:
        return 0, "Posting date is missing"
    age_days = max((datetime.now(timezone.utc) - anchor).total_seconds() / 86400.0, 0.0)
    if age_days <= 3:
        return 18, f"Recent posting ({int(age_days)}d old)"
    if age_days <= 7:
        return 12, f"Fresh posting ({int(age_days)}d old)"
    if age_days <= 14:
        return 6, f"Still recent ({int(age_days)}d old)"
    if age_days <= 30:
        return 0, ""
    if age_days <= 60:
        return -6, f"Aging posting ({int(age_days)}d old)"
    return -12, f"Stale posting ({int(age_days)}d old)"


def _location_component(best_evaluation: Any) -> tuple[int, str]:
    if best_evaluation is None:
        return 0, ""
    reasons = list(getattr(best_evaluation, "reasons", []) or [])
    delta = 0
    if any("Matched remote-only preference" in reason or "Matched requested workplace type" in reason for reason in reasons):
        delta += 6
    if any(
        token in reason
        for reason in reasons
        for token in (
            "Matched requested free-text location",
            "Matched requested country",
            "Matched requested region",
            "Matched requested city",
            "Matched requested location scope",
        )
    ):
        delta += 6
    if delta:
        return delta, "Location and workplace align with preferences"
    return 0, ""


def _experience_component(job: JobPosting, personal: Any, selections: Sequence[Any]) -> tuple[int, str, str | None]:
    desired_levels: list[str] = []
    for query in [selection.query for selection in selections]:
        for value in getattr(query, "experience_levels", []) or []:
            normalized = value_or_unknown(value)
            if normalized not in desired_levels:
                desired_levels.append(normalized)
    if not desired_levels:
        for value in getattr(personal, "experience_levels", []) or []:
            normalized = value_or_unknown(value)
            if normalized not in desired_levels:
                desired_levels.append(normalized)
    if not desired_levels:
        return 0, "", None
    job_level = value_or_unknown(job.experience_level)
    if job_level in desired_levels and job_level != "unknown":
        return 10, f"Experience level fits ({job_level})", None
    if job_level == "unknown":
        return -8, "Experience level is missing", "Experience level is unknown, so fit needs review"
    return -10, f"Experience level is outside the preferred range ({job_level})", None


def _compensation_component(job: JobPosting, personal: Any, selections: Sequence[Any]) -> tuple[int, str]:
    wants_compensation = bool(getattr(personal, "compensation_min", None) is not None or getattr(personal, "compensation_currency", None))
    if not wants_compensation:
        for query in [selection.query for selection in selections]:
            if getattr(query, "compensation_min", None) is not None or getattr(query, "compensation_currency", None):
                wants_compensation = True
                break
    has_compensation = job.compensation_min is not None or job.compensation_max is not None or bool(job.compensation_currency)
    if wants_compensation and has_compensation:
        return 8, "Compensation is disclosed"
    if wants_compensation and not has_compensation:
        return -12, "Compensation is missing despite compensation preferences"
    if has_compensation:
        return 2, "Compensation is disclosed"
    return 0, ""


def _sponsorship_component(qualification: Any, personal: Any, selections: Sequence[Any]) -> tuple[int, str, str | None]:
    fit_value = str(getattr(qualification, "fit", "") or "")
    needs_sponsorship = bool(getattr(personal, "requires_future_sponsorship", False) or getattr(personal, "sponsorship_fit", None))
    if not needs_sponsorship:
        needs_sponsorship = any(bool(getattr(selection.query, "requires_future_sponsorship", False)) for selection in selections)
    if fit_value == SponsorshipFit.LIKELY_COMPATIBLE.value:
        return 10, "Sponsorship fit looks compatible", None
    if fit_value == SponsorshipFit.LIKELY_INCOMPATIBLE.value:
        return -20, "Sponsorship fit looks incompatible", None
    if needs_sponsorship:
        return -8, "Sponsorship fit is unclear", "Sponsorship compatibility needs operator review"
    return 0, "", None


def _clarity_component(job: JobPosting, qualification: Any) -> tuple[int, str]:
    confidence = float(getattr(qualification, "confidence", 0.0) or 0.0)
    if qualification is not None and confidence and confidence < 0.5:
        return -4, "Qualification confidence is low"
    if not job.apply_url:
        return -3, "Apply URL is missing"
    if value_or_unknown(job.location_scope) == "unknown":
        return -3, "Location scope is unclear"
    return 0, ""


def _priority_label(score: int, status: PersonalTriageStatus, suppressed: bool) -> str:
    if suppressed:
        return "suppressed"
    if status == PersonalTriageStatus.SHORTLISTED or score >= 80:
        return "high"
    if status == PersonalTriageStatus.WATCHING or score >= 40:
        return "medium"
    return "normal"


def _headline(suppressed: bool, suppressed_reasons: list[str], ranking_reasons: list[str], match_reasons: list[str]) -> str:
    if suppressed and suppressed_reasons:
        return suppressed_reasons[0]
    if ranking_reasons:
        return ranking_reasons[0]
    if match_reasons:
        return match_reasons[0]
    return "Matched current personal filters"


def _aware_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _unique(values: Sequence[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.append(item)
    return seen

