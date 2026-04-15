from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from pydantic import BaseModel, Field
from sqlalchemy import select

from findmyjob.core.enums import ApplicationMode, JobLifecycleStatus, PersonalSuppressionScope, PersonalTriageStatus, RunStatus
from findmyjob.core.types import JobSearchQuery, NormalizedJobPosting, PersonalJobMatchExplanation, PersonalSuppressionRule, PersonalTriageDecision, ProfileFact
from findmyjob.db.models import ApplicationRecord, AuditEventRecord, JobPosting, QualificationResultRecord, utcnow
from findmyjob.db.repositories import AuditRepository, PersonalTriageRepository, ProfileRepository, RunRepository
from findmyjob.db.search import search_jobs
from findmyjob.documents.pipeline import RenderedArtifact
from findmyjob.orchestrator.greenhouse import GreenhouseScaleOrchestrator
from findmyjob.orchestrator.service import Orchestrator
from findmyjob.personal.preferences import compose_personal_query, effective_enabled_saved_search_presets, resolve_personal_saved_searches
from findmyjob.personal.triage import PersonalJobAssessment, apply_job_triage, assess_personal_job, clear_job_suppression, sort_key_for_assessment

PERSONAL_DAILY_RUN_TYPE = 'personal_daily'


class PersonalQuerySelection(BaseModel):
    name: str
    saved_search_id: str | None = None
    summary: str
    query: JobSearchQuery


class PersonalDailyRunSummary(BaseModel):
    run_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    preset_names: list[str] = Field(default_factory=list)
    sync_run_id: str | None = None
    prepare_run_ids: list[str] = Field(default_factory=list)
    query_summaries: dict[str, str] = Field(default_factory=dict)
    matching_job_ids: list[str] = Field(default_factory=list)
    ranked_job_ids: list[str] = Field(default_factory=list)
    new_job_ids: list[str] = Field(default_factory=list)
    updated_job_ids: list[str] = Field(default_factory=list)
    ready_for_preparation_job_ids: list[str] = Field(default_factory=list)
    added_to_review_job_ids: list[str] = Field(default_factory=list)
    needs_user_input_application_ids: list[str] = Field(default_factory=list)
    screened_out_job_ids: list[str] = Field(default_factory=list)
    suppressed_job_ids: list[str] = Field(default_factory=list)
    suppressed_reasons_by_job_id: dict[str, list[str]] = Field(default_factory=dict)
    query_names_by_job_id: dict[str, list[str]] = Field(default_factory=dict)
    explanations_by_job_id: dict[str, PersonalJobMatchExplanation] = Field(default_factory=dict)
    auto_prepare: bool = False


class PersonalDailyDryRunSummary(BaseModel):
    generated_at: datetime | None = None
    preset_names: list[str] = Field(default_factory=list)
    query_summaries: dict[str, str] = Field(default_factory=dict)
    matched_job_count: int = 0
    visible_match_count: int = 0
    suppressed_match_count: int = 0
    ready_for_preparation_count: int = 0
    ready_for_review_count: int = 0
    needs_user_input_count: int = 0
    approved_pending_submit_count: int = 0
    top_job_ids: list[str] = Field(default_factory=list)


class PersonalInboxItem(BaseModel):
    bucket: str
    job_id: str
    application_id: str | None = None
    company: str
    title: str
    job_status: str
    review_status: str | None = None
    triage_status: str = PersonalTriageStatus.NEW.value
    priority_label: str | None = None
    ranking_score: int | None = None
    explanation_headline: str | None = None
    query_names: list[str] = Field(default_factory=list)
    discovered_at: datetime | None = None
    prepared_at: datetime | None = None


class PersonalInboxSummary(BaseModel):
    latest_daily_run_id: str | None = None
    latest_daily_run_at: datetime | None = None
    enabled_presets: list[str] = Field(default_factory=list)
    query_names_by_job_id: dict[str, list[str]] = Field(default_factory=dict)
    explanations_by_job_id: dict[str, PersonalJobMatchExplanation] = Field(default_factory=dict)
    suppressed_reasons_by_job_id: dict[str, list[str]] = Field(default_factory=dict)
    shortlisted_jobs: list[PersonalInboxItem] = Field(default_factory=list)
    watching_jobs: list[PersonalInboxItem] = Field(default_factory=list)
    new_matching_jobs: list[PersonalInboxItem] = Field(default_factory=list)
    ready_for_review: list[PersonalInboxItem] = Field(default_factory=list)
    needs_user_input: list[PersonalInboxItem] = Field(default_factory=list)
    approved_pending_submit: list[PersonalInboxItem] = Field(default_factory=list)
    suppressed_jobs: list[PersonalInboxItem] = Field(default_factory=list)


class PersonalDecisionList(BaseModel):
    decisions: list[PersonalTriageDecision] = Field(default_factory=list)
    suppression_rules: list[PersonalSuppressionRule] = Field(default_factory=list)


class PersonalTriageMutation(BaseModel):
    decision: PersonalTriageDecision
    created_rules: list[PersonalSuppressionRule] = Field(default_factory=list)
    cleared_rules: list[PersonalSuppressionRule] = Field(default_factory=list)


class PersonalJobExplanationPayload(BaseModel):
    job_id: str
    company: str
    title: str
    job_status: str
    application_id: str | None = None
    application_status: str | None = None
    review_status: str | None = None
    query_names: list[str] = Field(default_factory=list)
    explanation: PersonalJobMatchExplanation
    decision: PersonalTriageDecision
    posting_url: str
    apply_url: str | None = None


def resolve_personal_queries(runtime) -> list[PersonalQuerySelection]:
    searches = resolve_personal_saved_searches(runtime)
    return [
        PersonalQuerySelection(
            name=search.name,
            saved_search_id=search.id,
            summary=compose_personal_query(search.query_payload, runtime.config.personal).summary(),
            query=compose_personal_query(search.query_payload, runtime.config.personal),
        )
        for search in searches
    ]


async def run_personal_daily(runtime) -> PersonalDailyRunSummary:
    queries = resolve_personal_queries(runtime)
    sync_query, board_tokens = build_personal_sync_query([item.query for item in queries])
    started_at = utcnow()
    with runtime.session_scope() as session:
        existing_jobs = {
            job.id: {
                'list_payload_hash': str((job.notes or {}).get('list_payload_hash') or ''),
                'detail_payload_hash': str((job.notes or {}).get('detail_payload_hash') or ''),
            }
            for job in session.scalars(select(JobPosting).where(JobPosting.source_adapter == 'greenhouse')).all()
        }
        existing_app_status = {
            application.job_posting_id: application.status.value
            for application in session.scalars(select(ApplicationRecord)).all()
        }
        run_repo = RunRepository(session)
        audit_repo = AuditRepository(session)
        run = run_repo.create_run(
            PERSONAL_DAILY_RUN_TYPE,
            ApplicationMode.DRY_RUN,
            checkpoint_state={
                'started_at': started_at.isoformat(),
                'preset_names': [item.name for item in queries],
                'query_summaries': {item.name: item.query.summary() for item in queries},
                'effective_enabled_presets': effective_enabled_saved_search_presets(runtime.config.personal),
            },
        )
        audit_repo.emit(
            'run.started',
            'run',
            run.id,
            run_id=run.id,
            payload={'type': PERSONAL_DAILY_RUN_TYPE, 'preset_names': [item.name for item in queries]},
        )
        run_id = run.id

    try:
        sync_run_id = await GreenhouseScaleOrchestrator(runtime).sync_boards(
            query_override=sync_query,
            board_tokens=board_tokens or None,
        )
        with runtime.session_scope() as session:
            query_names_by_job_id = _query_names_for_jobs(session, queries)
            matched_job_ids = list(query_names_by_job_id)
            jobs = _load_jobs(session, matched_job_ids)
            touched_job_ids = _touched_job_ids_for_run(session, sync_run_id)
            touched_jobs = {job_id: job for job_id, job in jobs.items() if job_id in touched_job_ids}
            assessments = _build_personal_assessments(session, runtime.config.personal, queries, matched_job_ids)
            screened_out_job_ids = [
                job_id
                for job_id, job in touched_jobs.items()
                if job.lifecycle_status in {JobLifecycleStatus.SCREENED_OUT, JobLifecycleStatus.DUPLICATE_BLOCKED, JobLifecycleStatus.INACTIVE}
            ]
            visible_assessments = [
                assessment
                for job_id, assessment in assessments.items()
                if job_id not in screened_out_job_ids and not assessment.explanation.suppressed
            ]
            suppressed_assessments = [
                assessment
                for job_id, assessment in assessments.items()
                if job_id not in screened_out_job_ids and assessment.explanation.suppressed
            ]
            visible_assessments.sort(key=lambda assessment: sort_key_for_assessment(jobs[assessment.job_id], assessment), reverse=True)
            suppressed_assessments.sort(key=lambda assessment: sort_key_for_assessment(jobs[assessment.job_id], assessment), reverse=True)
            ranked_job_ids = [assessment.job_id for assessment in visible_assessments]
            suppressed_job_ids = [assessment.job_id for assessment in suppressed_assessments]
            suppressed_reasons_by_job_id = {
                assessment.job_id: list(assessment.explanation.suppression_reasons)
                for assessment in suppressed_assessments
            }
            new_job_ids = [job_id for job_id in ranked_job_ids if job_id not in existing_jobs]
            updated_job_ids = [
                job_id
                for job_id in ranked_job_ids
                if job_id in existing_jobs and _job_payload_changed(jobs.get(job_id), existing_jobs[job_id])
            ]
            ready_for_preparation_job_ids = [
                job_id
                for job_id in ranked_job_ids
                if _job_ready_for_preparation(session, job_id)
            ]

        prepare_run_ids: list[str] = []
        auto_prepare = bool(runtime.config.personal.auto_prepare_after_discovery)
        if auto_prepare:
            for job_id in ready_for_preparation_job_ids:
                prepare_run_ids.append(await Orchestrator(runtime).run_prepare_for_job(job_id, ApplicationMode.DRY_RUN))

        with runtime.session_scope() as session:
            added_to_review_job_ids: list[str] = []
            needs_user_input_application_ids: list[str] = []
            for application in session.scalars(select(ApplicationRecord).where(ApplicationRecord.job_posting_id.in_(ranked_job_ids))).all():
                previous_status = existing_app_status.get(application.job_posting_id)
                if application.status == JobLifecycleStatus.READY_FOR_REVIEW and previous_status != JobLifecycleStatus.READY_FOR_REVIEW.value:
                    added_to_review_job_ids.append(application.job_posting_id)
                if application.status == JobLifecycleStatus.NEEDS_USER_INPUT:
                    needs_user_input_application_ids.append(application.id)

        summary = PersonalDailyRunSummary(
            run_id=run_id,
            started_at=started_at,
            completed_at=utcnow(),
            preset_names=[item.name for item in queries],
            sync_run_id=sync_run_id,
            prepare_run_ids=prepare_run_ids,
            query_summaries={item.name: item.query.summary() for item in queries},
            matching_job_ids=ranked_job_ids,
            ranked_job_ids=ranked_job_ids,
            new_job_ids=new_job_ids,
            updated_job_ids=updated_job_ids,
            ready_for_preparation_job_ids=ready_for_preparation_job_ids,
            added_to_review_job_ids=sorted(dict.fromkeys(added_to_review_job_ids)),
            needs_user_input_application_ids=sorted(dict.fromkeys(needs_user_input_application_ids)),
            screened_out_job_ids=sorted(dict.fromkeys(screened_out_job_ids)),
            suppressed_job_ids=suppressed_job_ids,
            suppressed_reasons_by_job_id=suppressed_reasons_by_job_id,
            query_names_by_job_id=query_names_by_job_id,
            explanations_by_job_id={job_id: assessment.explanation for job_id, assessment in assessments.items()},
            auto_prepare=auto_prepare,
        )
        with runtime.session_scope() as session:
            RunRepository(session).complete_run(run_id, RunStatus.COMPLETED, checkpoint_state=summary.model_dump(mode='json'))
            AuditRepository(session).emit(
                'personal.daily.completed',
                'run',
                run_id,
                run_id=run_id,
                payload=summary.model_dump(mode='json'),
            )
        return summary
    except Exception as exc:
        with runtime.session_scope() as session:
            RunRepository(session).complete_run(
                run_id,
                RunStatus.FAILED,
                checkpoint_state={'started_at': started_at.isoformat(), 'error': str(exc)},
            )
            AuditRepository(session).emit(
                'personal.daily.failed',
                'run',
                run_id,
                run_id=run_id,
                payload={'error': str(exc)},
            )
        raise


def build_personal_inbox(runtime, *, limit: int = 8, include_suppressed: bool = False) -> PersonalInboxSummary:
    latest = latest_personal_daily_summary(runtime)
    queries = _safe_resolve_personal_queries(runtime)
    with runtime.session_scope() as session:
        query_names_by_job_id = _query_names_for_jobs(session, queries)
        triage_repo = PersonalTriageRepository(session)
        shortlisted_ids = _triaged_job_ids(triage_repo, PersonalTriageStatus.SHORTLISTED)
        watching_ids = _triaged_job_ids(triage_repo, PersonalTriageStatus.WATCHING)
        app_status_job_ids = _application_job_ids(session)
        candidate_job_ids = _unique_ids(
            list(query_names_by_job_id)
            + shortlisted_ids
            + watching_ids
            + [job_id for job_ids in app_status_job_ids.values() for job_id in job_ids]
            + (latest.suppressed_job_ids if latest is not None else [])
        )
        assessments = _build_personal_assessments(session, runtime.config.personal, queries, candidate_job_ids)
        explanations_by_job_id = {job_id: assessment.explanation for job_id, assessment in assessments.items()}
        visible_application_job_ids = {job_id for job_ids in app_status_job_ids.values() for job_id in job_ids}
        shortlisted_jobs = _build_inbox_job_items(
            session,
            [job_id for job_id in shortlisted_ids if job_id not in visible_application_job_ids],
            explanations_by_job_id,
            query_names_by_job_id,
            bucket='shortlisted',
            limit=limit,
            include_suppressed=include_suppressed,
        )
        watching_jobs = _build_inbox_job_items(
            session,
            [job_id for job_id in watching_ids if job_id not in visible_application_job_ids],
            explanations_by_job_id,
            query_names_by_job_id,
            bucket='watching',
            limit=limit,
            include_suppressed=include_suppressed,
        )
        new_matching_jobs = _build_inbox_job_items(
            session,
            _new_inbox_job_ids(latest, assessments),
            explanations_by_job_id,
            query_names_by_job_id,
            bucket='new_matching',
            limit=limit,
            include_suppressed=include_suppressed,
            allowed_statuses={PersonalTriageStatus.NEW},
        )
        ready_for_review = _build_inbox_application_items(
            session,
            status=JobLifecycleStatus.READY_FOR_REVIEW,
            explanations_by_job_id=explanations_by_job_id,
            query_names_by_job_id=query_names_by_job_id,
            bucket='ready_for_review',
            limit=limit,
            include_suppressed=include_suppressed,
        )
        needs_user_input = _build_inbox_application_items(
            session,
            status=JobLifecycleStatus.NEEDS_USER_INPUT,
            explanations_by_job_id=explanations_by_job_id,
            query_names_by_job_id=query_names_by_job_id,
            bucket='needs_user_input',
            limit=limit,
            include_suppressed=include_suppressed,
        )
        approved_pending_submit = _build_inbox_application_items(
            session,
            status=JobLifecycleStatus.APPROVED_FOR_SUBMIT,
            explanations_by_job_id=explanations_by_job_id,
            query_names_by_job_id=query_names_by_job_id,
            bucket='approved_pending_submit',
            limit=limit,
            include_suppressed=include_suppressed,
        )
        suppressed_jobs = _build_inbox_job_items(
            session,
            latest.suppressed_job_ids if latest is not None else [],
            explanations_by_job_id,
            query_names_by_job_id,
            bucket='suppressed',
            limit=limit,
            include_suppressed=True,
        ) if include_suppressed else []
    return PersonalInboxSummary(
        latest_daily_run_id=latest.run_id if latest is not None else None,
        latest_daily_run_at=latest.completed_at if latest is not None else None,
        enabled_presets=[item.name for item in queries],
        query_names_by_job_id=query_names_by_job_id,
        explanations_by_job_id=explanations_by_job_id,
        suppressed_reasons_by_job_id=latest.suppressed_reasons_by_job_id if latest is not None else {},
        shortlisted_jobs=shortlisted_jobs,
        watching_jobs=watching_jobs,
        new_matching_jobs=new_matching_jobs,
        ready_for_review=ready_for_review,
        needs_user_input=needs_user_input,
        approved_pending_submit=approved_pending_submit,
        suppressed_jobs=suppressed_jobs,
    )


def latest_personal_daily_summary(runtime) -> PersonalDailyRunSummary | None:
    with runtime.session_scope() as session:
        runs = RunRepository(session).list_runs_by_type(PERSONAL_DAILY_RUN_TYPE, limit=1)
        if not runs:
            return None
        payload = dict(runs[0].checkpoint_state or {})
    try:
        return PersonalDailyRunSummary.model_validate(payload)
    except Exception:
        return None


def preview_personal_daily(runtime, *, limit: int = 10) -> PersonalDailyDryRunSummary:
    queries = resolve_personal_queries(runtime)
    query_summaries = {item.name: item.query.summary() for item in queries}
    with runtime.session_scope() as session:
        query_names_by_job_id = _query_names_for_jobs(session, queries)
        matched_job_ids = list(query_names_by_job_id)
        jobs = _load_jobs(session, matched_job_ids)
        assessments = _build_personal_assessments(session, runtime.config.personal, queries, matched_job_ids)
        visible_assessments: list[PersonalJobAssessment] = []
        suppressed_assessments: list[PersonalJobAssessment] = []
        for assessment in assessments.values():
            job = jobs.get(assessment.job_id)
            if job is None or job.lifecycle_status in {JobLifecycleStatus.SCREENED_OUT, JobLifecycleStatus.DUPLICATE_BLOCKED, JobLifecycleStatus.INACTIVE}:
                continue
            if assessment.explanation.suppressed:
                suppressed_assessments.append(assessment)
            else:
                visible_assessments.append(assessment)
        visible_assessments.sort(key=lambda assessment: sort_key_for_assessment(jobs[assessment.job_id], assessment), reverse=True)
        ready_for_preparation_count = sum(1 for assessment in visible_assessments if _job_ready_for_preparation(session, assessment.job_id))
        application_job_ids = _application_job_ids(session)
    return PersonalDailyDryRunSummary(
        generated_at=datetime.now(timezone.utc),
        preset_names=[item.name for item in queries],
        query_summaries=query_summaries,
        matched_job_count=len(matched_job_ids),
        visible_match_count=len(visible_assessments),
        suppressed_match_count=len(suppressed_assessments),
        ready_for_preparation_count=ready_for_preparation_count,
        ready_for_review_count=len(application_job_ids.get(JobLifecycleStatus.READY_FOR_REVIEW, [])),
        needs_user_input_count=len(application_job_ids.get(JobLifecycleStatus.NEEDS_USER_INPUT, [])),
        approved_pending_submit_count=len(application_job_ids.get(JobLifecycleStatus.APPROVED_FOR_SUBMIT, [])),
        top_job_ids=[assessment.job_id for assessment in visible_assessments[:limit]],
    )


async def shortlist_job(runtime, job_id: str, reason_code: str | None = None, note: str | None = None) -> PersonalTriageMutation:
    decision, created_rules = apply_job_triage(runtime, job_id, PersonalTriageStatus.SHORTLISTED, reason_code=reason_code, note=note, updated_by='cli.personal.shortlist')
    return PersonalTriageMutation(decision=decision, created_rules=created_rules)


async def watch_job(runtime, job_id: str, reason_code: str | None = None, note: str | None = None) -> PersonalTriageMutation:
    decision, created_rules = apply_job_triage(runtime, job_id, PersonalTriageStatus.WATCHING, reason_code=reason_code, note=note, updated_by='cli.personal.watch')
    return PersonalTriageMutation(decision=decision, created_rules=created_rules)


async def dismiss_job(runtime, job_id: str, reason_code: str | None = None, note: str | None = None, suppression_scope: PersonalSuppressionScope = PersonalSuppressionScope.JOB) -> PersonalTriageMutation:
    decision, created_rules = apply_job_triage(runtime, job_id, PersonalTriageStatus.DISMISSED, reason_code=reason_code, note=note, suppression_scope=suppression_scope, updated_by='cli.personal.dismiss')
    return PersonalTriageMutation(decision=decision, created_rules=created_rules)


async def archive_job(runtime, job_id: str, reason_code: str | None = None, note: str | None = None, suppression_scope: PersonalSuppressionScope = PersonalSuppressionScope.JOB) -> PersonalTriageMutation:
    decision, created_rules = apply_job_triage(runtime, job_id, PersonalTriageStatus.ARCHIVED, reason_code=reason_code, note=note, suppression_scope=suppression_scope, updated_by='cli.personal.archive')
    return PersonalTriageMutation(decision=decision, created_rules=created_rules)


async def unsuppress_job(runtime, job_id: str, clear_job_status: bool = True, clear_scopes: Sequence[PersonalSuppressionScope] = ()) -> PersonalTriageMutation:
    decision, cleared_rules = clear_job_suppression(runtime, job_id, clear_job_status=clear_job_status, clear_scopes=clear_scopes, updated_by='cli.personal.unsuppress')
    return PersonalTriageMutation(decision=decision, cleared_rules=cleared_rules)


def list_personal_decisions(runtime, *, limit: int = 100) -> PersonalDecisionList:
    with runtime.session_scope() as session:
        repo = PersonalTriageRepository(session)
        return PersonalDecisionList(decisions=repo.list_decisions(limit=limit), suppression_rules=repo.list_rules(active_only=False))


def explain_personal_job(runtime, job_id: str) -> PersonalJobExplanationPayload:
    queries = _safe_resolve_personal_queries(runtime)
    with runtime.session_scope() as session:
        job = session.get(JobPosting, job_id)
        if job is None:
            raise ValueError(f'Job not found: {job_id}')
        assessments = _build_personal_assessments(session, runtime.config.personal, queries, [job_id])
        assessment = assessments[job_id]
        repo = PersonalTriageRepository(session)
        decision = repo.get_decision(job_id) or PersonalTriageDecision(job_id=job_id)
        application = session.scalar(select(ApplicationRecord).where(ApplicationRecord.job_posting_id == job_id))
        return PersonalJobExplanationPayload(
            job_id=job.id,
            company=job.company.display_name,
            title=job.title,
            job_status=job.lifecycle_status.value,
            application_id=application.id if application is not None else None,
            application_status=application.status.value if application is not None else None,
            review_status=application.review_status.value if application is not None else None,
            query_names=list(assessment.explanation.matched_query_names),
            explanation=assessment.explanation,
            decision=decision,
            posting_url=job.posting_url,
            apply_url=job.apply_url,
        )


async def preview_personal_resume(runtime, job_id: str | None = None, *, allow_synthetic: bool = False) -> dict[str, Any]:
    resolved_job_id, job_model, facts, synthetic_job = _preview_context(runtime, job_id=job_id, allow_synthetic=allow_synthetic)
    base_name = runtime.documents.deterministic_base_name(job_model) + '-preview'
    context = runtime.documents.build_resume_context(job_model, facts)
    artifacts: list[RenderedArtifact] = [runtime.documents.write_context(base_name, context)]
    if runtime.documents.resume_renderer == 'latex' and runtime.documents.template_config.resume_template_path is not None:
        resume_pdf = runtime.documents.render_latex_resume(base_name, context)
        resume_text = runtime.documents.write_resume_text_from_pdf(base_name, resume_pdf, context)
    else:
        resume_text = runtime.documents.write_resume_text(base_name, context)
        resume_pdf = runtime.documents.render_typst('resume.typ', base_name, context)
    artifacts.extend([resume_text, resume_pdf])
    failures = runtime.documents.validation_failures(artifacts, expected_suffixes={'resume.txt', 'resume.pdf'})
    if failures:
        raise ValueError('; '.join(failures))
    return {
        'job_id': resolved_job_id,
        'company': job_model.company_name,
        'title': job_model.title,
        'renderer': runtime.documents.resume_renderer,
        'synthetic_job': synthetic_job,
        'artifacts': [{'kind': artifact.kind, 'path': str(artifact.path), 'validation': artifact.validation_results} for artifact in artifacts],
    }


async def preview_personal_cover_letter(runtime, job_id: str | None = None, *, allow_synthetic: bool = False) -> dict[str, Any]:
    resolved_job_id, job_model, facts, synthetic_job = _preview_context(runtime, job_id=job_id, allow_synthetic=allow_synthetic)
    base_name = runtime.documents.deterministic_base_name(job_model) + '-preview'
    context = runtime.documents.build_resume_context(job_model, facts)
    context['cover_letter'] = runtime.documents.build_cover_letter_payload(context)
    artifacts: list[RenderedArtifact] = [runtime.documents.write_context(base_name, context)]
    cover_letter_text = runtime.documents.write_cover_letter_text(base_name, context)
    cover_letter_pdf = runtime.documents.render_typst('cover_letter.typ', base_name, context)
    artifacts.extend([cover_letter_text, cover_letter_pdf])
    failures = runtime.documents.validation_failures(artifacts, expected_suffixes={'cover_letter.txt', 'cover_letter.pdf'})
    if failures:
        raise ValueError('; '.join(failures))
    return {
        'job_id': resolved_job_id,
        'company': job_model.company_name,
        'title': job_model.title,
        'renderer': runtime.documents.resume_renderer,
        'synthetic_job': synthetic_job,
        'artifacts': [{'kind': artifact.kind, 'path': str(artifact.path), 'validation': artifact.validation_results} for artifact in artifacts],
    }


def build_personal_sync_query(queries: list[JobSearchQuery]) -> tuple[JobSearchQuery, list[str]]:
    if not queries:
        raise ValueError('At least one personal query is required.')
    result = JobSearchQuery(source_adapter='greenhouse', limit=max(query.limit for query in queries))
    result.title_keywords = _union_strings(query.title_keywords for query in queries)
    result.locations = _union_strings(query.locations for query in queries)
    result.countries = _union_strings(query.countries for query in queries)
    result.regions = _union_strings(query.regions for query in queries)
    result.cities = _union_strings(query.cities for query in queries)
    result.workplace_types = _union_enum_lists(query.workplace_types for query in queries)
    result.employment_types = _union_strings(query.employment_types for query in queries)
    result.location_scopes = _union_enum_lists(query.location_scopes for query in queries)
    result.experience_levels = _union_enum_lists(query.experience_levels for query in queries)
    result.company_size_buckets = _union_enum_lists(query.company_size_buckets for query in queries)
    result.remote_only = all(query.remote_only for query in queries)
    result.active_only = any(query.active_only for query in queries)
    result.allow_unknown_compensation = any(query.allow_unknown_compensation for query in queries)
    result.allow_unknown_experience_level = any(query.allow_unknown_experience_level for query in queries)
    posted_within = [query.posted_within_days for query in queries if query.posted_within_days is not None]
    if posted_within:
        result.posted_within_days = max(posted_within)
    compensation_min = [query.compensation_min for query in queries if query.compensation_min is not None]
    if compensation_min:
        result.compensation_min = min(compensation_min)
    currencies = sorted({str(query.compensation_currency).upper() for query in queries if query.compensation_currency})
    if len(currencies) == 1:
        result.compensation_currency = currencies[0]
    comp_present = {query.compensation_present for query in queries}
    if len(comp_present) == 1:
        result.compensation_present = next(iter(comp_present))
    sponsorship_fit = {query.sponsorship_fit for query in queries if query.sponsorship_fit}
    if len(sponsorship_fit) == 1:
        result.sponsorship_fit = next(iter(sponsorship_fit))
    result.requires_future_sponsorship = any(query.requires_future_sponsorship for query in queries)
    board_tokens = _union_strings([[query.board_token] if query.board_token else [] for query in queries])
    return result, board_tokens


def _safe_resolve_personal_queries(runtime) -> list[PersonalQuerySelection]:
    try:
        return resolve_personal_queries(runtime)
    except ValueError:
        return []


def _load_jobs(session, job_ids: Sequence[str]) -> dict[str, JobPosting]:
    if not job_ids:
        return {}
    return {job.id: job for job in session.scalars(select(JobPosting).where(JobPosting.id.in_(list(job_ids)))).all()}


def _build_personal_assessments(session, personal, queries: Sequence[PersonalQuerySelection], job_ids: Sequence[str]) -> dict[str, PersonalJobAssessment]:
    jobs = _load_jobs(session, job_ids)
    triage_repo = PersonalTriageRepository(session)
    decisions = triage_repo.load_decision_map(list(jobs))
    rules = triage_repo.list_rules(active_only=True)
    qualification_records = {
        record.job_posting_id: record
        for record in session.scalars(select(QualificationResultRecord).where(QualificationResultRecord.job_posting_id.in_(list(jobs)))).all()
    } if jobs else {}
    return {
        job_id: assess_personal_job(
            job,
            qualification_records.get(job_id),
            queries,
            personal,
            decision=decisions.get(job_id),
            rules=rules,
        )
        for job_id, job in jobs.items()
    }


def _query_names_for_jobs(session, queries: Sequence[PersonalQuerySelection]) -> dict[str, list[str]]:
    query_names_by_job_id: dict[str, list[str]] = {}
    for selection in queries:
        for job in search_jobs(session, selection.query):
            query_names_by_job_id.setdefault(job.id, [])
            if selection.name not in query_names_by_job_id[job.id]:
                query_names_by_job_id[job.id].append(selection.name)
    return query_names_by_job_id


def _triaged_job_ids(repo: PersonalTriageRepository, status: PersonalTriageStatus) -> list[str]:
    return [record.job_posting_id for record in repo.list_decision_records(statuses=[status])]


def _application_job_ids(session) -> dict[JobLifecycleStatus, list[str]]:
    result: dict[JobLifecycleStatus, list[str]] = {
        JobLifecycleStatus.READY_FOR_REVIEW: [],
        JobLifecycleStatus.NEEDS_USER_INPUT: [],
        JobLifecycleStatus.APPROVED_FOR_SUBMIT: [],
    }
    for application in session.scalars(
        select(ApplicationRecord).where(
            ApplicationRecord.status.in_(
                [
                    JobLifecycleStatus.READY_FOR_REVIEW,
                    JobLifecycleStatus.NEEDS_USER_INPUT,
                    JobLifecycleStatus.APPROVED_FOR_SUBMIT,
                ]
            )
        )
    ).all():
        result.setdefault(application.status, []).append(application.job_posting_id)
    return result


def _new_inbox_job_ids(latest: PersonalDailyRunSummary | None, assessments: dict[str, PersonalJobAssessment]) -> list[str]:
    if latest is not None and latest.new_job_ids:
        return list(latest.new_job_ids)
    ranked = [assessment for assessment in assessments.values() if not assessment.explanation.suppressed]
    return [assessment.job_id for assessment in sorted(ranked, key=lambda assessment: assessment.score, reverse=True)]


def _build_inbox_job_items(
    session,
    job_ids: Sequence[str],
    explanations_by_job_id: dict[str, PersonalJobMatchExplanation],
    query_names_by_job_id: dict[str, list[str]],
    *,
    bucket: str,
    limit: int,
    include_suppressed: bool,
    allowed_statuses: set[PersonalTriageStatus] | None = None,
) -> list[PersonalInboxItem]:
    if not job_ids:
        return []
    jobs = _load_jobs(session, job_ids)
    items: list[PersonalInboxItem] = []
    for job_id in job_ids:
        job = jobs.get(job_id)
        explanation = explanations_by_job_id.get(job_id)
        if job is None or explanation is None:
            continue
        triage_status = PersonalTriageStatus(explanation.triage_status)
        if allowed_statuses is not None and triage_status not in allowed_statuses:
            continue
        if explanation.suppressed and not include_suppressed:
            continue
        items.append(
            PersonalInboxItem(
                bucket=bucket,
                job_id=job.id,
                company=job.company.display_name,
                title=job.title,
                job_status=job.lifecycle_status.value,
                triage_status=triage_status.value,
                priority_label=explanation.priority_label,
                ranking_score=explanation.score,
                explanation_headline=explanation.headline,
                query_names=list(query_names_by_job_id.get(job.id) or explanation.matched_query_names),
                discovered_at=job.discovered_at,
            )
        )
    items.sort(key=lambda item: ((item.ranking_score or 0), item.discovered_at or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    return items[:limit]


def _build_inbox_application_items(
    session,
    *,
    status: JobLifecycleStatus,
    explanations_by_job_id: dict[str, PersonalJobMatchExplanation],
    query_names_by_job_id: dict[str, list[str]],
    bucket: str,
    limit: int,
    include_suppressed: bool,
) -> list[PersonalInboxItem]:
    items: list[PersonalInboxItem] = []
    stmt = select(ApplicationRecord).where(ApplicationRecord.status == status).order_by(ApplicationRecord.updated_at.desc())
    for application in session.scalars(stmt).all():
        job = session.get(JobPosting, application.job_posting_id)
        explanation = explanations_by_job_id.get(application.job_posting_id)
        if job is None or explanation is None:
            continue
        if explanation.suppressed and not include_suppressed:
            continue
        items.append(
            PersonalInboxItem(
                bucket=bucket,
                job_id=job.id,
                application_id=application.id,
                company=job.company.display_name,
                title=job.title,
                job_status=job.lifecycle_status.value,
                review_status=application.review_status.value,
                triage_status=explanation.triage_status.value,
                priority_label=explanation.priority_label,
                ranking_score=explanation.score,
                explanation_headline=explanation.headline,
                query_names=list(query_names_by_job_id.get(job.id) or explanation.matched_query_names),
                discovered_at=job.discovered_at,
                prepared_at=application.prepared_at,
            )
        )
    return items[:limit]


def _job_payload_changed(job: JobPosting | None, before: dict[str, str]) -> bool:
    if job is None:
        return False
    notes = job.notes or {}
    return (
        str(notes.get('list_payload_hash') or '') != before.get('list_payload_hash', '')
        or str(notes.get('detail_payload_hash') or '') != before.get('detail_payload_hash', '')
    )


def _job_ready_for_preparation(session, job_id: str) -> bool:
    job = session.get(JobPosting, job_id)
    if job is None:
        return False
    if job.lifecycle_status not in {JobLifecycleStatus.CANDIDATE, JobLifecycleStatus.NORMALIZED}:
        return False
    application = session.scalar(select(ApplicationRecord).where(ApplicationRecord.job_posting_id == job_id))
    return application is None


def _load_profile_facts(runtime) -> list[ProfileFact]:
    with runtime.session_scope() as session:
        return [
            ProfileFact(
                fact_id=record.fact_id,
                kind=record.kind,
                payload=record.payload,
                sensitivity=record.sensitivity,
                allowed_for_generation=record.allowed_for_generation,
                disallowed=record.disallowed,
                provenance=record.provenance,
                confirmed=record.confirmed,
            )
            for record in ProfileRepository(session).list_facts()
        ]


def _preview_context(runtime, job_id: str | None = None, *, allow_synthetic: bool = False) -> tuple[str, NormalizedJobPosting, list[ProfileFact], bool]:
    with runtime.session_scope() as session:
        job = None
        if job_id:
            job = session.get(JobPosting, job_id)
            if job is None:
                raise ValueError(f'Job not found: {job_id}')
        if job is None:
            latest = latest_personal_daily_summary(runtime)
            preferred_ids = []
            if latest is not None:
                preferred_ids.extend(latest.new_job_ids)
                preferred_ids.extend(latest.ready_for_preparation_job_ids)
                preferred_ids.extend(latest.added_to_review_job_ids)
            for candidate_id in preferred_ids:
                job = session.get(JobPosting, candidate_id)
                if job is not None:
                    break
        if job is None:
            job = session.scalar(select(JobPosting).order_by(JobPosting.discovered_at.desc()).limit(1))
        if job is None:
            if allow_synthetic:
                return 'rehearsal-preview', _synthetic_preview_job(), _load_profile_facts(runtime), True
            raise ValueError('No jobs are available for preview. Run `fmj personal daily-run` or `fmj greenhouse sync` first.')
        return job.id, _job_model(job), _load_profile_facts(runtime), False


def _synthetic_preview_job() -> NormalizedJobPosting:
    now = datetime.now(timezone.utc)
    return NormalizedJobPosting(
        company_name='Local Preflight Company',
        company_key='local-preflight-company',
        title='Workspace Preflight Role',
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id='rehearsal-preview',
        posting_url='https://example.invalid/fmj/rehearsal-preview',
        apply_url='https://example.invalid/fmj/rehearsal-preview',
        location_raw='Local-only preflight',
        description='Synthetic local-only role used to validate personal artifact rendering without live ATS access.',
        normalized_description='synthetic local only role used to validate personal artifact rendering without live ats access',
        discovered_at=now,
        posted_at=now,
        source_updated_at=now,
        job_identity_key='rehearsal-preview',
        duplicate_cluster_key='rehearsal-preview',
    )


def _job_model(job: JobPosting) -> NormalizedJobPosting:
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


def _touched_job_ids_for_run(session, run_id: str) -> list[str]:
    stmt = select(AuditEventRecord.entity_id).where(
        AuditEventRecord.run_id == run_id,
        AuditEventRecord.event_type == 'job.discovered',
        AuditEventRecord.entity_type == 'job_posting',
    )
    return [row for row in session.scalars(stmt).all() if row]


def _union_strings(groups) -> list[str]:
    seen: list[str] = []
    for group in groups:
        for value in group:
            item = str(value or '').strip()
            if item and item not in seen:
                seen.append(item)
    return seen


def _union_enum_lists(groups) -> list[Any]:
    seen: list[Any] = []
    for group in groups:
        for value in group:
            if value not in seen:
                seen.append(value)
    return seen


def _unique_ids(values: Sequence[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        item = str(value or '').strip()
        if item and item not in seen:
            seen.append(item)
    return seen


