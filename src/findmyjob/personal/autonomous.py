from __future__ import annotations

import json
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import logging
import anyio
from sqlalchemy import select

log = logging.getLogger("findmyjob.autonomous")

from findmyjob.apply.service import ApplicationService
from findmyjob.core.enums import (
    ApplicationMode,
    ArtifactKind,
    JobLifecycleStatus,
    ModelRole,
    QuestionType,
    ReviewStatus,
    RunStatus,
    VerificationStatus,
)
from findmyjob.core.types import (
    ApplicationQuestion,
    ArtifactDraft,
    AutonomousDecisionReport,
    AutonomousRunSummary,
    GroundedAnswer,
    JobSearchQuery,
    ProfileFact,
    QueuedQuestionSummary,
)
from findmyjob.db.board_repository import SourceStateRepository
from findmyjob.db.models import (
    ApplicationAnswerRecord,
    ApplicationQuestionRecord,
    ApplicationRecord,
    Company,
    JobPosting,
    SubmitAttemptRecord,
    utcnow,
)
from findmyjob.db.repositories import (
    ApplicationRepository,
    AuditRepository,
    JobRepository,
    ProfileRepository,
    RunRepository,
)
from findmyjob.ledger.export import export_ledger
from findmyjob.orchestrator.greenhouse import GreenhouseScaleOrchestrator
from findmyjob.orchestrator.service import Orchestrator
from findmyjob.sources.classification import (
    AutomationTier,
    BoardClassification,
    classify_job,
    is_auto_submittable,
)
from findmyjob.personal.workflow import (
    PersonalQuerySelection,
    _build_personal_assessments,
    _query_names_for_jobs,
    _touched_job_ids_for_run,
    build_personal_sync_query,
    resolve_personal_queries,
)
from findmyjob.personal.triage import sort_key_for_assessment
from findmyjob.sources.normalizer import normalize_text

AUTONOMOUS_RUN_TYPE = 'autonomous'
CITIZEN_ONLY_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r'\b(?:u\.s\.|us|united states)\s+(?:citizen|citizens|citizenship)\b',
        r'\b(?:u\.s\.|us|united states)\s+national\b',
        r'\bcitizens?\s+only\b',
        r'\bu\.s\.\s+person\b',
    ]
]
CLEARANCE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r'\bsecurity clearance\b',
        r'\bactive clearance\b',
        r'\btop secret\b',
        r'\bsecret clearance\b',
        r'\bts\/sci\b',
        r'\bpublic trust\b',
    ]
]
SPONSORSHIP_DENIAL_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r'\bno sponsorship\b',
        r'\bunable to sponsor\b',
        r'\bcannot sponsor\b',
        r'\bwill not sponsor\b',
        r'\bdo not provide .*sponsorship\b',
        r'\bwithout sponsorship\b',
        r'\bvisa sponsorship .* not available\b',
    ]
]
LOGIN_WALL_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r'/users/sign_in',
        r'/login',
        r'/account',
        r'sign in to apply',
        r'create an account',
    ]
]
CAPTCHA_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r'captcha',
        r'hcaptcha',
        r'recaptcha',
        r'anti-bot',
    ]
]
PLACEHOLDER_PATTERNS = ('todo', 'placeholder', 'lorem ipsum', '{', '}')


def resolve_autonomous_queries(runtime) -> list[PersonalQuerySelection]:
    configured = runtime.config.autonomous
    queries: list[PersonalQuerySelection] = []
    if configured.use_personal_presets:
        try:
            queries = resolve_personal_queries(runtime)
        except ValueError:
            queries = []
    if not queries:
        base_query = JobSearchQuery.from_search_settings(runtime.config.search)
        queries = [
            PersonalQuerySelection(
                name='autonomous_default',
                saved_search_id=None,
                summary=base_query.summary(),
                query=base_query,
            )
        ]

    normalized: list[PersonalQuerySelection] = []
    for selection in queries:
        query = selection.query.model_copy(deep=True)
        query.source_adapter = 'greenhouse'
        if configured.countries:
            query.countries = list(configured.countries)
        if not query.limit:
            query.limit = runtime.config.personal.default_result_limit or 50
        normalized.append(
            PersonalQuerySelection(
                name=selection.name,
                saved_search_id=selection.saved_search_id,
                summary=query.summary(),
                query=query,
            )
        )
    return normalized


def latest_autonomous_run_summary(runtime) -> AutonomousRunSummary | None:
    with runtime.session_scope() as session:
        run = next(iter(RunRepository(session).list_runs_by_type(AUTONOMOUS_RUN_TYPE, limit=1)), None)
        if run is None:
            return None
        payload = dict(run.checkpoint_state or {})
        payload.setdefault('run_id', run.id)
        payload.setdefault('started_at', run.started_at)
        payload.setdefault('completed_at', run.completed_at)
    try:
        return AutonomousRunSummary.model_validate(payload)
    except Exception as exc:
        log.debug("[autonomous] Failed to parse run summary: %s: %s", type(exc).__name__, exc)
        return None


def list_question_queue(runtime) -> list[QueuedQuestionSummary]:
    queue: list[QueuedQuestionSummary] = []
    with runtime.session_scope() as session:
        app_repo = ApplicationRepository(session)
        for application in session.scalars(
            select(ApplicationRecord)
            .where(ApplicationRecord.status == JobLifecycleStatus.NEEDS_USER_INPUT)
            .order_by(ApplicationRecord.updated_at.asc())
        ).all():
            job = session.get(JobPosting, application.job_posting_id)
            if job is None:
                continue
            for question_record, answer_record in app_repo.list_answers_for_application(application.id):
                app_question = _question_model(question_record)
                if ApplicationService._question_hidden_from_operator(question_record):
                    continue
                if answer_record is not None and not answer_record.needs_user_input and answer_record.candidate_answer:
                    continue
                queue.append(_queued_question_summary(runtime, app_repo, application, job, question_record, answer_record))
    return queue


async def answer_next_question(runtime, answer_text: str, auto_approve_memory: bool = False) -> QueuedQuestionSummary:
    queue = list_question_queue(runtime)
    if not queue:
        raise ValueError('No unresolved application questions are currently queued.')
    item = answer_queued_question(runtime, queue[0].application_id, queue[0].question_id, answer_text)
    if auto_approve_memory:
        return approve_question_memory(runtime, item.application_id, item.question_id)
    return item


def answer_queued_question(runtime, application_id: str, question_id: str, answer_text: str) -> QueuedQuestionSummary:
    cleaned = str(answer_text or '').strip()
    if not cleaned:
        raise ValueError('Answer text is required.')
    with runtime.session_scope() as session:
        app_repo = ApplicationRepository(session)
        application = app_repo.get_application(application_id)
        if application is None:
            raise ValueError(f'Application not found: {application_id}')
        question = session.get(ApplicationQuestionRecord, question_id)
        if question is None or question.application_id != application.id:
            raise ValueError(f'Question not found for application: {question_id}')
        job = session.get(JobPosting, application.job_posting_id)
        if job is None:
            raise ValueError(f'Job not found for application: {application_id}')
        answer = GroundedAnswer(
            question=question.prompt_text,
            canonical_question=question.normalized_key or runtime.grounding.canonicalize_question(question.prompt_text),
            question_type=question.question_type,
            answer=cleaned,
            provenance='user',
            verification_status=VerificationStatus.VERIFIED,
        )
        app_repo.store_answer(question.id, answer)
        AuditRepository(session).emit(
            'questions.answer.recorded',
            'application',
            application.id,
            payload={'question_id': question.id, 'canonical_question': answer.canonical_question, 'source': 'user'},
        )
        answer_record = session.scalar(select(ApplicationAnswerRecord).where(ApplicationAnswerRecord.question_id == question.id))
        return _queued_question_summary(runtime, app_repo, application, job, question, answer_record)


def approve_question_memory(runtime, application_id: str, question_id: str) -> QueuedQuestionSummary:
    with runtime.session_scope() as session:
        app_repo = ApplicationRepository(session)
        application = app_repo.get_application(application_id)
        if application is None:
            raise ValueError(f'Application not found: {application_id}')
        question = session.get(ApplicationQuestionRecord, question_id)
        if question is None or question.application_id != application.id:
            raise ValueError(f'Question not found for application: {question_id}')
        answer_record = session.scalar(select(ApplicationAnswerRecord).where(ApplicationAnswerRecord.question_id == question.id))
        if answer_record is None or not str(answer_record.candidate_answer or '').strip():
            raise ValueError('Question does not have an answer to approve yet.')
        job = session.get(JobPosting, application.job_posting_id)
        if job is None:
            raise ValueError(f'Job not found for application: {application_id}')
        app_question = _question_model(question)
        memory_context = _answer_memory_context(app_question, job)
        answer = GroundedAnswer(
            question=question.prompt_text,
            canonical_question=answer_record.answer_source or question.normalized_key or runtime.grounding.canonicalize_question(question.prompt_text),
            question_type=question.question_type,
            answer=answer_record.candidate_answer,
            used_fact_ids=list(answer_record.grounded_fact_ids or []),
            provenance=answer_record.provenance,
            verification_status=answer_record.verification_status,
        )
        app_repo.store_answer_memory(answer.canonical_question or runtime.grounding.canonicalize_question(question.prompt_text), answer, approved=True, context_constraints=memory_context)
        AuditRepository(session).emit(
            'questions.answer_memory.approved',
            'application',
            application.id,
            payload={'question_id': question.id, 'canonical_question': answer.canonical_question, 'context': memory_context},
        )
        return _queued_question_summary(runtime, app_repo, application, job, question, answer_record, has_approved_memory=True)

async def retry_pending_autonomous(runtime) -> dict[str, Any]:
    retried: list[str] = []
    still_pending: list[str] = []
    submitted: list[str] = []
    failed: list[str] = []
    orchestrator = Orchestrator(runtime)
    with runtime.session_scope() as session:
        pending_ids = [application.id for application in session.scalars(select(ApplicationRecord).where(ApplicationRecord.status == JobLifecycleStatus.NEEDS_USER_INPUT)).all()]
    for application_id in pending_ids:
        ready = await _application_ready_for_submit(runtime, application_id)
        if not ready:
            still_pending.append(application_id)
            continue
        retried.append(application_id)
        try:
            await orchestrator.review_action(application_id, ReviewStatus.APPROVED, 'Autonomous retry after queued answers')
            await orchestrator.run_apply_for_application(application_id, ApplicationMode.AUTO_SUBMIT)
        except Exception as exc:
            log.error("[autonomous] Retry failed for application %s: %s: %s", application_id, type(exc).__name__, exc)
            failed.append(application_id)
            continue
        inspection = await orchestrator.inspect_submission_result(application_id)
        status = (inspection or {}).get('submission_status')
        if status == JobLifecycleStatus.SUBMITTED.value:
            submitted.append(application_id)
        else:
            failed.append(application_id)
    _auto_export_ledger(runtime)
    return {
        'retried_application_ids': retried,
        'still_pending_application_ids': still_pending,
        'submitted_application_ids': submitted,
        'failed_application_ids': failed,
    }


async def run_autonomous_tick(runtime) -> AutonomousRunSummary:
    configured = runtime.config.autonomous
    log.info("=" * 60)
    log.info("AUTONOMOUS TICK STARTING")
    log.info("=" * 60)
    queries = resolve_autonomous_queries(runtime)
    log.info("Resolved %d search queries: %s", len(queries), [q.name for q in queries])
    sync_query, board_tokens = build_personal_sync_query([selection.query for selection in queries])
    sync_query.source_adapter = 'greenhouse'
    if configured.countries:
        sync_query.countries = list(configured.countries)
    started_at = utcnow()
    with runtime.session_scope() as session:
        run_repo = RunRepository(session)
        audit_repo = AuditRepository(session)
        run = run_repo.create_run(
            AUTONOMOUS_RUN_TYPE,
            ApplicationMode.AUTO_SUBMIT,
            checkpoint_state={
                'started_at': started_at.isoformat(),
                'preset_names': [selection.name for selection in queries],
                'query_summaries': {selection.name: selection.query.summary() for selection in queries},
            },
        )
        audit_repo.emit(
            'autonomous.run.started',
            'run',
            run.id,
            run_id=run.id,
            payload={'preset_names': [selection.name for selection in queries]},
        )
        run_id = run.id
    log.info("Created run: %s", run_id)

    summary = AutonomousRunSummary(
        run_id=run_id,
        started_at=started_at,
        preset_names=[selection.name for selection in queries],
    )
    try:
        log.info("Starting board sync (discovery phase)...")
        summary.sync_run_id = await GreenhouseScaleOrchestrator(runtime).sync_boards(query_override=sync_query, board_tokens=board_tokens or None)
        log.info("Board sync complete (sync_run_id=%s)", summary.sync_run_id)
        with runtime.session_scope() as session:
            touched_job_ids = set(_touched_job_ids_for_run(session, summary.sync_run_id))
            query_names_by_job_id = _query_names_for_jobs(session, queries)
            live_job_ids = [job_id for job_id in query_names_by_job_id if job_id in touched_job_ids]
            assessments = _build_personal_assessments(session, runtime.config.personal, queries, live_job_ids)
            jobs = {assessment.job_id: session.get(JobPosting, assessment.job_id) for assessment in assessments.values()}
            candidate_assessments = [
                assessment
                for assessment in assessments.values()
                if jobs.get(assessment.job_id) is not None
                and not assessment.explanation.suppressed
                and jobs[assessment.job_id].lifecycle_status not in {
                    JobLifecycleStatus.SCREENED_OUT,
                    JobLifecycleStatus.DUPLICATE_BLOCKED,
                    JobLifecycleStatus.INACTIVE,
                }
            ]
            candidate_assessments.sort(key=lambda assessment: sort_key_for_assessment(jobs[assessment.job_id], assessment), reverse=True)
            facts = _load_facts(session)
            summary.candidate_job_ids = [assessment.job_id for assessment in candidate_assessments]
            log.info("Discovery: %d touched jobs, %d total assessments, %d candidate jobs after filtering",
                     len(touched_job_ids), len(assessments), len(candidate_assessments))
            summary.matched_presets_by_job_id = {job_id: list(query_names_by_job_id.get(job_id, [])) for job_id in summary.candidate_job_ids}
            if touched_job_ids and not summary.candidate_job_ids:
                summary.notes.append('Live sync completed, but no freshly discovered jobs matched the active autonomous filters.')

        summary.daily_submit_count, summary.per_company_submit_counts = _today_submit_counts(runtime)
        log.info("Daily submit count so far: %d / %d cap", summary.daily_submit_count, configured.daily_submit_cap)
        orchestrator = Orchestrator(runtime)
        consecutive_failures = 0

        log.info("-" * 60)
        log.info("PROCESSING %d CANDIDATE JOBS", len(candidate_assessments))
        log.info("-" * 60)
        for idx, assessment in enumerate(candidate_assessments, 1):
            job_id = assessment.job_id
            with runtime.session_scope() as session:
                job = session.get(JobPosting, job_id)
                if job is None:
                    log.warning("[%d/%d] Job %s not found in DB, skipping", idx, len(candidate_assessments), job_id)
                    continue
                job_label = f"{(job.company.display_name if job.company else '?')} - {job.title}"
                log.info("[%d/%d] Evaluating: %s (id=%s)", idx, len(candidate_assessments), job_label, job_id[:12])
                application = session.scalar(select(ApplicationRecord).where(ApplicationRecord.job_posting_id == job.id))
                if application is not None and application.status in {
                    JobLifecycleStatus.SUBMITTED,
                    JobLifecycleStatus.SUBMITTING,
                    JobLifecycleStatus.APPROVED_FOR_SUBMIT,
                }:
                    log.info("  -> Already %s, skipping", application.status.value)
                    continue
                if application is not None and application.status == JobLifecycleStatus.NEEDS_USER_INPUT:
                    summary.queued_application_ids.append(application.id)
                    log.info("  -> NEEDS_USER_INPUT (queued), skipping")
                    continue
                # --- Source automation classification ---
                classification = classify_job(
                    source_kind=job.source_kind,
                    apply_url=job.apply_url,
                    posting_url=job.posting_url,
                    source_adapter=job.source_adapter,
                    notes=job.notes,
                )
                _update_autonomous_notes(job, matched_presets=summary.matched_presets_by_job_id.get(job.id, []), classification=classification)
                if not is_auto_submittable(classification):
                    skip_reason = classification.automation_skip_reason or f'automation_tier:{classification.automation_tier.value}'
                    summary.skipped_job_ids.append(job.id)
                    summary.skipped_reasons_by_job_id[job.id] = skip_reason
                    log.info("  -> SKIP (not auto-submittable): %s", skip_reason)
                    continue

                source_cfg = runtime.config.sources.get(classification.board_family.value) or runtime.config.sources.get(str(job.source_kind or "").strip())
                submit_enabled = source_cfg is not None and source_cfg.submit_enabled
                log.info("  -> Classification: tier=%s, family=%s, submit_enabled=%s",
                         classification.automation_tier.value, classification.board_family.value, submit_enabled)
                if submit_enabled and _board_is_backed_off(session, job):
                    summary.skipped_job_ids.append(job.id)
                    summary.skipped_reasons_by_job_id[job.id] = 'board_backoff_active'
                    log.info("  -> SKIP: board is in backoff")
                    _update_autonomous_notes(job, matched_presets=summary.matched_presets_by_job_id.get(job.id, []), skip_reason='board_backoff_active')
                    continue
                log.info("  -> Evaluating AI decision gate...")
                decision = await _evaluate_autonomous_job(runtime, job, facts)
                summary.decision_by_job_id[job.id] = decision
                log.info("  -> AI decision: score=%s, green_light=%s, hard_gate=%s",
                         decision.score, decision.green_light, decision.hard_gate_passed)
                _update_autonomous_notes(
                    job,
                    matched_presets=summary.matched_presets_by_job_id.get(job.id, []),
                    decision=decision,
                )
                if not decision.hard_gate_passed or not decision.green_light or decision.score < configured.greenlight_min_score:
                    skip_reason = decision.skip_reason or '; '.join(decision.hard_gate_reasons or decision.warnings or ['auto_skip'])
                    summary.skipped_job_ids.append(job.id)
                    summary.skipped_reasons_by_job_id[job.id] = skip_reason
                    log.info("  -> SKIP (decision gate): %s", skip_reason)
                    continue
                log.info("  -> Building artifact draft (resume/cover letter)...")
                try:
                    draft = await _build_artifact_draft(runtime, job, facts)
                    log.info("  -> Artifact draft built successfully")
                except Exception as exc:
                    summary.skipped_job_ids.append(job.id)
                    summary.skipped_reasons_by_job_id[job.id] = f'artifact_draft_failed: {exc}'
                    log.error("  -> SKIP (artifact draft failed): %s", exc)
                    _update_autonomous_notes(job, matched_presets=summary.matched_presets_by_job_id.get(job.id, []), decision=decision, skip_reason=f'artifact_draft_failed: {exc}')
                    continue
                _update_autonomous_notes(job, matched_presets=summary.matched_presets_by_job_id.get(job.id, []), decision=decision, artifact_draft=draft)

            log.info("  -> Running prepare phase...")
            await orchestrator.run_prepare_for_job(job_id, ApplicationMode.AUTO_SUBMIT)
            with runtime.session_scope() as session:
                application = session.scalar(select(ApplicationRecord).where(ApplicationRecord.job_posting_id == job_id))
                if application is None:
                    summary.skipped_job_ids.append(job_id)
                    summary.skipped_reasons_by_job_id[job_id] = 'prepare_did_not_create_application'
                    log.warning("  -> SKIP: prepare did not create application record")
                    continue
                if application.status == JobLifecycleStatus.NEEDS_USER_INPUT:
                    summary.queued_application_ids.append(application.id)
                    log.info("  -> QUEUED: application needs user input (app_id=%s)", application.id[:12])
                    continue
                summary.prepared_application_ids.append(application.id)
                log.info("  -> Prepared application: %s (status=%s)", application.id[:12], application.status.value)
                job = session.get(JobPosting, job_id)
                company_key = job.company.normalized_name if job and job.company is not None else job_id
                if summary.daily_submit_count >= configured.daily_submit_cap:
                    log.info("  -> SKIP: daily submit cap reached (%d/%d)", summary.daily_submit_count, configured.daily_submit_cap)
                    summary.skipped_job_ids.append(job_id)
                    summary.skipped_reasons_by_job_id[job_id] = 'daily_submit_cap_reached'
                    _update_autonomous_notes(job, matched_presets=summary.matched_presets_by_job_id.get(job_id, []), skip_reason='daily_submit_cap_reached', submit_attempted=False)
                    continue
                if summary.per_company_submit_counts.get(company_key, 0) >= configured.per_company_daily_cap:
                    log.info("  -> SKIP: per-company daily cap reached for %s", company_key)
                    summary.skipped_job_ids.append(job_id)
                    summary.skipped_reasons_by_job_id[job_id] = 'per_company_daily_cap_reached'
                    _update_autonomous_notes(job, matched_presets=summary.matched_presets_by_job_id.get(job_id, []), skip_reason='per_company_daily_cap_reached', submit_attempted=False)
                    continue

            log.info("  -> APPROVING application for submit...")
            await orchestrator.review_action(application.id, ReviewStatus.APPROVED, 'Autonomous Greenhouse apply')
            if summary.submitted_application_ids or summary.uncertain_application_ids or summary.failed_application_ids:
                delay = random.uniform(configured.min_submit_delay_seconds, configured.max_submit_delay_seconds)
                log.info("  -> Waiting %.1fs before submit (rate limiting)...", delay)
                await anyio.sleep(delay)
            log.info("  -> SUBMITTING application %s via browser...", application.id[:12])
            await orchestrator.run_apply_for_application(application.id, ApplicationMode.AUTO_SUBMIT)
            inspection = await orchestrator.inspect_submission_result(application.id)
            with runtime.session_scope() as session:
                job = session.get(JobPosting, job_id)
                failure_reason = (inspection or {}).get('failure_reason')
                submit_status = (inspection or {}).get('submission_status')
                log.info("  -> Submit result: status=%s, failure_reason=%s", submit_status, failure_reason)
                summary.daily_submit_count += 1
                summary.per_company_submit_counts[company_key] = summary.per_company_submit_counts.get(company_key, 0) + 1
                _update_autonomous_notes(
                    job,
                    matched_presets=summary.matched_presets_by_job_id.get(job_id, []),
                    submit_attempted=True,
                    submit_result=submit_status,
                    submit_failure_reason=failure_reason,
                )
                _apply_submit_backoff(session, job, failure_reason)
            _auto_export_ledger(runtime)
            if submit_status == JobLifecycleStatus.SUBMITTED.value:
                summary.submitted_application_ids.append(application.id)
                consecutive_failures = 0
                log.info("  -> SUCCESS: Application submitted!")
            elif submit_status == JobLifecycleStatus.SUBMISSION_UNCERTAIN.value:
                summary.uncertain_application_ids.append(application.id)
                consecutive_failures += 1
                log.warning("  -> UNCERTAIN: Submission uncertain (consecutive_failures=%d)", consecutive_failures)
            else:
                summary.failed_application_ids.append(application.id)
                consecutive_failures += 1
                log.error("  -> FAILED: Submission failed (consecutive_failures=%d)", consecutive_failures)
            if consecutive_failures >= configured.max_consecutive_submit_failures:
                summary.notes.append('Paused autonomous tick after consecutive submit failures reached the configured cap.')
                break

        summary.queue_depth = len(list_question_queue(runtime))
        summary.completed_at = utcnow()
        with runtime.session_scope() as session:
            RunRepository(session).complete_run(run_id, RunStatus.COMPLETED, checkpoint_state=summary.model_dump(mode='json'))
            AuditRepository(session).emit('autonomous.run.completed', 'run', run_id, run_id=run_id, payload=summary.model_dump(mode='json'))
        _auto_export_ledger(runtime)
        log.info("=" * 60)
        log.info("AUTONOMOUS TICK COMPLETE")
        log.info("  Candidates: %d | Prepared: %d | Submitted: %d | Uncertain: %d | Failed: %d | Skipped: %d | Queued: %d",
                 len(summary.candidate_job_ids), len(summary.prepared_application_ids),
                 len(summary.submitted_application_ids), len(summary.uncertain_application_ids),
                 len(summary.failed_application_ids), len(summary.skipped_job_ids),
                 len(summary.queued_application_ids))
        if summary.skipped_job_ids:
            log.info("  Skip reasons:")
            for jid, reason in list(summary.skipped_reasons_by_job_id.items())[:10]:
                log.info("    %s: %s", jid[:12], reason)
        log.info("  Queue depth (pending user input): %d", summary.queue_depth)
        log.info("=" * 60)
        return summary
    except Exception as exc:
        summary.completed_at = utcnow()
        summary.notes.append(str(exc))
        log.error("=" * 60)
        log.error("AUTONOMOUS TICK FAILED: %s", exc, exc_info=True)
        log.error("=" * 60)
        with runtime.session_scope() as session:
            RunRepository(session).complete_run(run_id, RunStatus.FAILED, checkpoint_state=summary.model_dump(mode='json'))
            AuditRepository(session).emit('autonomous.run.failed', 'run', run_id, run_id=run_id, payload={'error': str(exc)})
        _auto_export_ledger(runtime)
        raise

async def run_autonomous_loop(runtime, interval_seconds: int = 300) -> None:
    configured = runtime.config.autonomous
    consecutive_failures = 0
    tick_count = 0
    log.info("Starting autonomous loop (interval=%ds, max_consecutive_failures=%d)", interval_seconds, configured.max_consecutive_submit_failures)
    while True:
        tick_count += 1
        log.info(">>> LOOP TICK #%d (consecutive_failures=%d)", tick_count, consecutive_failures)
        summary = await run_autonomous_tick(runtime)
        tick_failures = len(summary.failed_application_ids) + len(summary.uncertain_application_ids)
        consecutive_failures = consecutive_failures + tick_failures if tick_failures else 0
        if consecutive_failures >= configured.max_consecutive_submit_failures:
            log.warning("Stopping autonomous loop: consecutive failures (%d) >= cap (%d)", consecutive_failures, configured.max_consecutive_submit_failures)
            return
        log.info("Sleeping %ds before next tick...", interval_seconds)
        await anyio.sleep(max(interval_seconds, 1))


def autonomous_status(runtime) -> dict[str, Any]:
    latest = latest_autonomous_run_summary(runtime)
    queue = list_question_queue(runtime)
    with runtime.session_scope() as session:
        pending = [application.id for application in session.scalars(select(ApplicationRecord).where(ApplicationRecord.status == JobLifecycleStatus.NEEDS_USER_INPUT)).all()]
        submit_count, per_company = _today_submit_counts(runtime, session=session)
        backed_off: dict[str, str] = {}
        for job in session.scalars(select(JobPosting).where(JobPosting.source_adapter == 'greenhouse')).all():
            if not job.board_token or job.board_token in backed_off:
                continue
            state = SourceStateRepository(session).get_board_sync_state('greenhouse', job.board_token)
            if state.backoff_until and state.backoff_until > utcnow():
                backed_off[job.board_token] = state.backoff_until.isoformat()
        last_failure = None
        latest_attempt = session.scalars(select(SubmitAttemptRecord).order_by(SubmitAttemptRecord.created_at.desc())).first()
        if latest_attempt is not None:
            evidence = (latest_attempt.payload or {}).get('evidence') or {}
            last_failure = evidence.get('failure_reason') or (latest_attempt.payload or {}).get('message') or latest_attempt.status
    return {
        'latest_run': latest.model_dump(mode='json') if latest is not None else None,
        'queue_depth': len(queue),
        'unresolved_prompt_count': len(queue),
        'blocked_application_count': len(pending),
        'pending_application_ids': pending,
        'daily_submit_count': submit_count,
        'per_company_submit_counts': per_company,
        'backed_off_boards': backed_off,
        'last_failure': last_failure,
    }


async def _evaluate_autonomous_job(runtime, job: JobPosting, facts) -> AutonomousDecisionReport:
    decision = AutonomousDecisionReport(job_id=job.id)
    hard_gate_reasons = _hard_gate_reasons(job, facts)
    if hard_gate_reasons:
        decision.hard_gate_passed = False
        decision.hard_gate_reasons = hard_gate_reasons
        decision.green_light = False
        decision.skip_reason = hard_gate_reasons[0]
        return decision

    try:
        payload, profile_name = await runtime.model_router.generate_json_with_profile(
            ModelRole.CLASSIFIER,
            json.dumps(_decision_prompt_payload(job, facts), indent=2, sort_keys=True),
            system_prompt=(
                'You are a strict job-application gatekeeper. Return only JSON with keys '
                'green_light:boolean, score:int, reasons:list[string], warnings:list[string], skip_reason:string|null. '
                'Reject jobs with explicit authorization conflicts, security clearance requirements, or obvious mismatch.'
            ),
        )
        decision.classifier_profile = profile_name
        decision.green_light = bool(payload.get('green_light'))
        decision.score = max(0, min(100, int(payload.get('score', 0) or 0)))
        decision.reasons = [str(item).strip() for item in payload.get('reasons', []) if str(item).strip()]
        decision.warnings = [str(item).strip() for item in payload.get('warnings', []) if str(item).strip()]
        decision.skip_reason = str(payload.get('skip_reason')).strip() if payload.get('skip_reason') else None
    except Exception as exc:
        if runtime.config.autonomous.ai_greenlight_required:
            decision.green_light = False
            decision.score = 0
            decision.warnings = [f'classifier_failed: {exc}']
            decision.skip_reason = 'classifier_failed'
        else:
            decision.green_light = True
            decision.score = runtime.config.autonomous.greenlight_min_score
            decision.warnings = [f'classifier_failed_but_not_required: {exc}']
    if runtime.config.autonomous.ai_greenlight_required and (not decision.green_light or decision.score < runtime.config.autonomous.greenlight_min_score):
        decision.skip_reason = decision.skip_reason or 'ai_greenlight_rejected'
    return decision


async def _build_artifact_draft(runtime, job: JobPosting, facts) -> ArtifactDraft:
    job_model = Orchestrator(runtime)._job_model(job)
    context = runtime.documents.build_resume_context(job_model, facts)

    async def writer_request(repair_issues: list[str] | None = None) -> ArtifactDraft:
        writer_input = {
            'job': {
                'company_name': job_model.company_name,
                'title': job_model.title,
                'location_raw': job_model.location_raw,
                'description_excerpt': normalize_text(job_model.description)[:3500],
            },
            'profile': {
                'work': context['profile'].get('work', []),
                'projects': context['profile'].get('projects', []),
                'skills': context['profile'].get('skills', []),
                'education': context['profile'].get('education', []),
            },
            'schema': {
                'resume_draft': {
                    'headline': 'string|null',
                    'summary_lines': ['string'],
                    'selected_work_fact_ids': ['string'],
                    'selected_project_fact_ids': ['string'],
                    'selected_skill_fact_ids': ['string'],
                    'custom_bullets': ['string'],
                },
                'cover_letter_draft': {
                    'salutation': 'string|null',
                    'paragraphs': ['string - MUST be exactly 4 paragraphs, 300-400 words total'],
                    'closing': 'string|null',
                    'signature_name': 'string|null',
                },
            },
        }
        if repair_issues:
            writer_input['repair_issues'] = list(repair_issues)
            writer_input['repair_instruction'] = 'Revise the draft to fix every listed verifier issue while staying grounded only in the supplied profile facts.'
        writer_payload, writer_profile = await runtime.model_router.generate_json_with_profile(
            ModelRole.WRITER,
            json.dumps(writer_input, indent=2, sort_keys=True),
            system_prompt=(
                'You are a professional resume writer and career coach generating tailored job application documents.\n\n'
                'STRICT ANTI-HALLUCINATION RULES - violating ANY of these makes the output invalid:\n'
                '1. Every skill, project, employer, metric, and technology you mention MUST appear verbatim in the provided profile facts. '
                'If a fact is not in the input, you MUST NOT reference it.\n'
                '2. Do NOT invent company names, lab names, team names, brand names, or technologies. '
                'Phrases like "design/brand labs", "Company A", "XYZ Corp" are FORBIDDEN.\n'
                '3. Do NOT use placeholder text: "TODO", "lorem ipsum", "[insert here]", template markers like {company_name}.\n'
                '4. Do NOT fabricate metrics, percentages, or quantitative claims not present in the profile.\n\n'
                'COVER LETTER REQUIREMENTS:\n'
                '- The cover_letter_draft.paragraphs array MUST contain exactly 4 full paragraphs.\n'
                '- Total word count: 300-400 words across all 4 paragraphs.\n'
                '- Paragraph 1 (Hook): Connect a specific personal experience or accomplishment from the profile '
                'to the company mission or role requirements. Show genuine interest.\n'
                '- Paragraph 2 (Technical Fit): Map specific skills, projects, and work experience from the profile '
                'to the job description requirements. Be concrete - name real projects and real skills only.\n'
                '- Paragraph 3 (Cultural/Values Alignment): Explain why this specific company and role appeals to the candidate. '
                'Reference details from the job description.\n'
                '- Paragraph 4 (Closing): Reiterate enthusiasm, mention availability, and thank the reader.\n\n'
                'RESUME REQUIREMENTS:\n'
                '- summary_lines: 2-3 bullet points highlighting the most relevant qualifications for THIS role.\n'
                '- custom_bullets: 0-3 additional achievement bullets tailored to the job, sourced only from provided facts.\n'
                '- Use professional but human tone - not robotic, not overly casual.\n\n'
                'Return ONLY valid JSON matching the schema. No markdown, no code fences, no commentary.'
            ),
        )
        draft = ArtifactDraft.model_validate(writer_payload)
        draft.writer_profile = writer_profile
        _ensure_draft_defaults(draft, context, job)
        return draft

    async def verifier_request(draft: ArtifactDraft) -> ArtifactDraft:
        verifier_payload, verifier_profile = await runtime.model_router.generate_json_with_profile(
            ModelRole.VERIFIER,
            json.dumps(
                {
                    'job': {'company_name': job_model.company_name, 'title': job_model.title},
                    'profile_fact_ids': [fact.fact_id for fact in facts if not fact.disallowed],
                    'profile_employers': list({
                        str(fact.payload.get('company', '') or fact.payload.get('employer', '')).strip()
                        for fact in facts if getattr(fact.kind, 'value', fact.kind) == 'work' and not fact.disallowed
                    }),
                    'profile_project_names': list({
                        str(fact.payload.get('name', '') or fact.payload.get('title', '')).strip()
                        for fact in facts if getattr(fact.kind, 'value', fact.kind) == 'project' and not fact.disallowed
                    }),
                    'resume_draft': draft.resume_draft.model_dump(mode='json'),
                    'cover_letter_draft': draft.cover_letter_draft.model_dump(mode='json'),
                    'checks': [
                        'English only - reject any non-English text',
                        'No placeholders - reject TODO, lorem ipsum, template markers, curly braces',
                        'No invented entities - every employer, project, lab, or team name mentioned must appear in profile_employers or profile_project_names',
                        'No fabricated metrics - reject percentages or numbers not in the provided profile',
                        'Cover letter is exactly 4 paragraphs and 300-400 words total',
                        'ATS-safe plain language',
                        'One-page resume friendly',
                    ],
                },
                indent=2,
                sort_keys=True,
            ),
            system_prompt=(
                'You are a strict quality-assurance verifier for job application documents.\n\n'
                'Return ONLY JSON with keys: approved:boolean, issues:list[string].\n\n'
                'REJECT the draft (approved=false) if ANY of these are true:\n'
                '- Any employer, lab, team, or company name appears that is NOT in profile_employers\n'
                '- Any project name appears that is NOT in profile_project_names\n'
                '- Any fabricated metric, percentage, or quantitative claim not grounded in profile facts\n'
                '- Any placeholder text: TODO, lorem ipsum, {variable}, [insert], "X/Y labs", "Company A"\n'
                '- Cover letter has fewer than 4 paragraphs or fewer than 250 words total\n'
                '- Any non-English text\n'
                '- Resume summary exceeds 4 lines\n\n'
                'If ALL checks pass, return {"approved": true, "issues": []}.'
            ),
        )
        issues = [str(item).strip() for item in verifier_payload.get('issues', []) if str(item).strip()]
        issues.extend(_local_draft_issues(draft))
        draft.verifier_profile = verifier_profile
        draft.verifier_issues = list(dict.fromkeys(issues))
        draft.verified = bool(verifier_payload.get('approved')) and not draft.verifier_issues
        return draft

    log.info("      [artifact] Starting writer request...")
    draft = await writer_request()
    log.info("      [artifact] Writer done. Starting verifier...")
    draft = await verifier_request(draft)
    log.info("      [artifact] Verifier done: verified=%s, issues=%s", draft.verified, (draft.verifier_issues or [])[:3])
    if draft.verified:
        return draft

    log.info("      [artifact] Draft not verified, running repair cycle...")
    repaired = await writer_request(list(draft.verifier_issues or []))
    log.info("      [artifact] Repair writer done. Re-verifying...")
    repaired = await verifier_request(repaired)
    log.info("      [artifact] Re-verification: verified=%s, issues=%s", repaired.verified, (repaired.verifier_issues or [])[:3])
    if not repaired.verified:
        raise ValueError('; '.join(repaired.verifier_issues or ['artifact draft was not approved']))
    return repaired
def _ensure_draft_defaults(draft: ArtifactDraft, context: dict[str, Any], job: JobPosting) -> None:
    selected_work_ids = [item.get('fact_id') for item in context['profile'].get('work', []) if item.get('fact_id')]
    selected_project_ids = [item.get('fact_id') for item in context['profile'].get('projects', []) if item.get('fact_id')]
    selected_skill_ids = [item.get('fact_id') for item in context['profile'].get('skills', []) if item.get('fact_id')]
    if not draft.resume_draft.selected_work_fact_ids:
        draft.resume_draft.selected_work_fact_ids = selected_work_ids[:3]
    if not draft.resume_draft.selected_project_fact_ids:
        draft.resume_draft.selected_project_fact_ids = selected_project_ids[:2]
    if not draft.resume_draft.selected_skill_fact_ids:
        draft.resume_draft.selected_skill_fact_ids = selected_skill_ids[:8]
    if not draft.cover_letter_draft.salutation:
        draft.cover_letter_draft.salutation = f'Dear Hiring Team at {job.company.display_name},'
    if not draft.cover_letter_draft.paragraphs:
        draft.cover_letter_draft.paragraphs = [
            f'I am applying for the {job.title} role at {job.company.display_name}.',
            'My background aligns well with the posted responsibilities, and I would value the opportunity to contribute immediately.',
        ]
    if not draft.cover_letter_draft.closing:
        draft.cover_letter_draft.closing = 'Sincerely,'

async def _application_ready_for_submit(runtime, application_id: str) -> bool:
    orchestrator = Orchestrator(runtime)
    with runtime.session_scope() as session:
        app_repo = ApplicationRepository(session)
        job_repo = JobRepository(session)
        application = app_repo.get_application(application_id)
        if application is None:
            raise ValueError(f'Application not found: {application_id}')
        job = job_repo.get_job(application.job_posting_id)
        if job is None:
            raise ValueError(f'Job not found for application: {application_id}')
        adapter = orchestrator.source_adapters().get(job.source_adapter)
        if adapter is None:
            return False
        question_answers = app_repo.list_answers_for_application(application.id)
        artifacts = app_repo.list_artifacts(application_id=application.id)
        artifact_kinds = {artifact.kind for artifact in artifacts}
        artifact_kinds.add(ArtifactKind.REVIEW_PACKET)
        artifact_paths = {artifact.kind.value: artifact.path for artifact in artifacts}
        app_service = ApplicationService(job_repo, app_repo)
        artifact_validation_failures = app_service.artifact_validation_failures(artifacts)
        artifact_validation_warnings = app_service.artifact_validation_warnings(artifacts)
        plan = adapter.bind_answers(orchestrator._job_model(job), question_answers, artifact_paths)
        gate = app_service.submission_gate(
            job,
            application,
            artifact_kinds,
            app_service.ungrounded_question_prompts(question_answers),
            orchestrator._policy_for_source(job.source_kind),
            missing_required_fields=app_service.missing_required_questions(question_answers) + plan.missing_required_fields,
            artifact_validation_failures=artifact_validation_failures,
            low_confidence_answers=app_service.low_confidence_answers(question_answers),
            warnings=artifact_validation_warnings,
        )
        return gate.is_ready


def _hard_gate_reasons(job: JobPosting, facts) -> list[str]:
    classification = classify_job(
        source_kind=job.source_kind,
        apply_url=job.apply_url,
        posting_url=job.posting_url,
        source_adapter=job.source_adapter,
    )
    if not is_auto_submittable(classification):
        return [classification.automation_skip_reason or f'unsupported_board_family:{classification.board_family.value}']
    if not str(job.apply_url or '').strip():
        return ['missing_apply_url']
    haystack = ' '.join([
        str(job.title or ''),
        str(job.description or ''),
        str(job.normalized_description or ''),
        str(job.apply_url or ''),
        json.dumps(job.notes or {}, sort_keys=True),
    ])
    lowered = haystack.lower()
    reasons: list[str] = []
    if any(pattern.search(haystack) for pattern in CITIZEN_ONLY_PATTERNS):
        reasons.append('requires_us_citizen_or_national')
    if any(pattern.search(haystack) for pattern in CLEARANCE_PATTERNS):
        reasons.append('requires_security_clearance')
    if any(pattern.search(lowered) for pattern in LOGIN_WALL_PATTERNS):
        reasons.append('login_or_account_wall_detected')
    if any(pattern.search(lowered) for pattern in CAPTCHA_PATTERNS):
        reasons.append('captcha_or_antibot_detected')
    authorization = _authorization_summary(facts)
    if authorization.get('requires_future_sponsorship') and any(pattern.search(haystack) for pattern in SPONSORSHIP_DENIAL_PATTERNS):
        reasons.append('future_sponsorship_incompatible')
    if authorization.get('is_authorized') is False and ('authorized to work' in lowered or 'work authorization' in lowered):
        reasons.append('not_authorized_for_required_work_authorization')
    return reasons


def _authorization_summary(facts) -> dict[str, Any]:
    summary = {'is_authorized': None, 'requires_future_sponsorship': None}
    for fact in facts:
        if getattr(fact.kind, 'value', fact.kind) != 'authorization' or fact.disallowed:
            continue
        if 'is_authorized' in fact.payload and summary['is_authorized'] is None:
            summary['is_authorized'] = bool(fact.payload['is_authorized'])
        if 'requires_future_sponsorship' in fact.payload and summary['requires_future_sponsorship'] is None:
            summary['requires_future_sponsorship'] = bool(fact.payload['requires_future_sponsorship'])
    return summary


def _decision_prompt_payload(job: JobPosting, facts) -> dict[str, Any]:
    return {
        'job': {
            'id': job.id,
            'company': job.company.display_name if job.company is not None else '',
            'title': job.title,
            'location': job.location_raw,
            'description_excerpt': normalize_text(job.description)[:3500],
            'apply_url': job.apply_url,
        },
        'authorization': _authorization_summary(facts),
        'required_output': {
            'green_light': 'boolean',
            'score': '0-100 integer',
            'reasons': ['string'],
            'warnings': ['string'],
            'skip_reason': 'string|null',
        },
    }


def _local_draft_issues(draft: ArtifactDraft) -> list[str]:
    chunks = [
        draft.resume_draft.headline or '',
        *draft.resume_draft.summary_lines,
        *draft.resume_draft.custom_bullets,
        draft.cover_letter_draft.salutation or '',
        *draft.cover_letter_draft.paragraphs,
        draft.cover_letter_draft.closing or '',
    ]
    rendered = ' '.join(chunks).strip()
    lowered = rendered.lower()
    issues: list[str] = []
    if not rendered:
        issues.append('draft_is_empty')
    if any(marker in lowered for marker in PLACEHOLDER_PATTERNS):
        issues.append('draft_contains_placeholder_text')
    if not draft.cover_letter_draft.paragraphs:
        issues.append('cover_letter_missing_paragraphs')
    if len(draft.resume_draft.summary_lines) > 4:
        issues.append('resume_summary_lines_exceed_limit')
    if len(draft.resume_draft.custom_bullets) > 4:
        issues.append('resume_custom_bullets_exceed_limit')
    return issues


def _load_facts(session) -> list[ProfileFact]:
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


def _question_model(question: ApplicationQuestionRecord) -> ApplicationQuestion:
    field_config = question.field_config or {}
    return ApplicationQuestion(
        source_field_name=question.source_field_name,
        prompt_text=question.prompt_text,
        normalized_key=question.normalized_key,
        question_type=question.question_type,
        widget_type=question.widget_type,
        section=field_config.get('section'),
        step_id=field_config.get('step_id'),
        required=question.required,
        input_role=str(field_config.get('input_role') or 'data'),
        visible_to_operator=bool(field_config.get('visible_to_operator', True)),
        options=list(question.options or []),
        option_details=list(field_config.get('option_details') or []),
        sensitive=bool(field_config.get('sensitive')),
        file_constraints=dict(field_config.get('file_constraints') or {}),
        submission_binding=dict(field_config.get('submission_binding') or {}),
        source_confidence=float(field_config.get('source_confidence') or 1.0),
        source_snapshot_ref=question.source_snapshot_ref,
    )


def _answer_memory_context(question: ApplicationQuestion, job: JobPosting) -> dict[str, Any]:
    options = [str(option).strip().lower() for option in question.options if str(option).strip()]
    return {
        'question_type': question.question_type.value,
        'source_adapter': job.source_adapter,
        'option_signature': '|'.join(sorted(options)),
    }


def _queued_question_summary(runtime, app_repo: ApplicationRepository, application: ApplicationRecord, job: JobPosting, question: ApplicationQuestionRecord, answer_record: ApplicationAnswerRecord | None, *, has_approved_memory: bool | None = None) -> QueuedQuestionSummary:
    app_question = _question_model(question)
    canonical_question = question.normalized_key or runtime.grounding.canonicalize_question(question.prompt_text)
    context = _answer_memory_context(app_question, job)
    approved_memory = app_repo.find_answer_memory(canonical_question, context)
    options = sorted(str(option).strip().lower() for option in question.options if str(option).strip())
    return QueuedQuestionSummary(
        application_id=application.id,
        question_id=question.id,
        job_id=job.id,
        company=job.company.display_name if job.company is not None else '',
        title=job.title,
        prompt_text=question.prompt_text,
        normalized_key=question.normalized_key,
        canonical_question=canonical_question,
        question_type=question.question_type.value,
        widget_type=question.widget_type,
        required=question.required,
        source_adapter=job.source_adapter,
        option_signature=options,
        option_details=list(app_question.option_details or []),
        input_role=app_question.input_role,
        visible_to_operator=app_question.visible_to_operator,
        existing_answer=answer_record.candidate_answer if answer_record is not None else None,
        has_approved_memory=bool(approved_memory) if has_approved_memory is None else has_approved_memory,
    )


def _today_submit_counts(runtime, *, session=None) -> tuple[int, dict[str, int]]:
    owns_session = session is None
    if owns_session:
        scope = runtime.session_scope()
        session = scope.__enter__()
    try:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        rows = session.execute(
            select(SubmitAttemptRecord, JobPosting, Company)
            .join(ApplicationRecord, SubmitAttemptRecord.application_id == ApplicationRecord.id)
            .join(JobPosting, ApplicationRecord.job_posting_id == JobPosting.id)
            .join(Company, JobPosting.company_id == Company.id)
            .where(SubmitAttemptRecord.created_at >= start)
        ).all()
        per_company: dict[str, int] = {}
        for _attempt, _job, company in rows:
            per_company[company.normalized_name] = per_company.get(company.normalized_name, 0) + 1
        return len(rows), per_company
    finally:
        if owns_session:
            scope.__exit__(None, None, None)


def _board_is_backed_off(session, job: JobPosting) -> bool:
    if not job.board_token:
        return False
    state = SourceStateRepository(session).get_board_sync_state('greenhouse', job.board_token)
    return bool(state.backoff_until and state.backoff_until > utcnow())


def reset_board_backoff(runtime) -> dict[str, str]:
    """Clear all board backoff state. Returns dict of board_token -> previous backoff_until."""
    cleared: dict[str, str] = {}
    with runtime.session_scope() as session:
        state_repo = SourceStateRepository(session)
        for job in session.scalars(select(JobPosting).where(JobPosting.source_adapter == 'greenhouse')).all():
            if not job.board_token or job.board_token in cleared:
                continue
            state = state_repo.get_board_sync_state('greenhouse', job.board_token)
            if state.backoff_until:
                cleared[job.board_token] = state.backoff_until.isoformat()
                state.backoff_until = None
                state_repo.save_board_sync_state(state)
    return cleared


def _apply_submit_backoff(session, job: JobPosting | None, failure_reason: str | None) -> None:
    if job is None or not job.board_token or not failure_reason:
        return
    lowered = str(failure_reason).lower()
    state_repo = SourceStateRepository(session)
    state = state_repo.get_board_sync_state('greenhouse', job.board_token)
    if '429' in lowered or 'rate limit' in lowered:
        state.backoff_until = utcnow() + timedelta(minutes=15)
        state.failure_count += 1
        state.last_failure_at = utcnow()
        state_repo.save_board_sync_state(state)
    elif 'captcha' in lowered or 'login' in lowered or 'account wall' in lowered:
        state.backoff_until = utcnow() + timedelta(minutes=60)
        state.failure_count += 1
        state.last_failure_at = utcnow()
        state_repo.save_board_sync_state(state)


def _update_autonomous_notes(job: JobPosting | None, *, matched_presets: list[str] | None = None, decision: AutonomousDecisionReport | None = None, artifact_draft: ArtifactDraft | None = None, skip_reason: str | None = None, submit_attempted: bool | None = None, submit_result: str | None = None, submit_failure_reason: str | None = None, classification: BoardClassification | None = None) -> None:
    if job is None:
        return
    notes = dict(job.notes or {})
    payload = dict(notes.get('autonomous') or {})
    payload['last_evaluated_at'] = utcnow().isoformat()
    if matched_presets is not None:
        payload['matched_presets'] = list(matched_presets)
    if classification is not None:
        payload['board_family'] = classification.board_family.value
        payload['automation_tier'] = classification.automation_tier.value
        payload['supports_auto_submit'] = classification.supports_auto_submit
        payload['automation_skip_reason'] = classification.automation_skip_reason
        payload['classification_method'] = classification.detection_method
    if decision is not None:
        payload.update(decision.model_dump(mode='json'))
    if artifact_draft is not None:
        payload['artifact_draft'] = artifact_draft.model_dump(mode='json')
    if skip_reason is not None:
        payload['skip_reason'] = skip_reason
    if submit_attempted is not None:
        payload['submit_attempted'] = submit_attempted
    if submit_result is not None:
        payload['submit_result'] = submit_result
    if submit_failure_reason is not None:
        payload['submit_failure_reason'] = submit_failure_reason
    notes['autonomous'] = payload
    job.notes = notes


def _auto_export_ledger(runtime) -> None:
    if not runtime.config.autonomous.auto_export_ledger:
        return
    destination = runtime.config.autonomous_ledger_output_path(runtime.workspace)
    with runtime.session_scope() as session:
        export_ledger(session, destination)






