from __future__ import annotations

from datetime import datetime, timezone
import uuid
from pathlib import Path
from typing import Any

from findmyjob.filefirst.models import LiveRunEvent, LiveRunState
from findmyjob.filefirst.workspace import FileWorkspace

_SUPPORTED_SOURCE_ORDER = ("greenhouse", "lever", "ashby")
_ACTIVE_RUN_STATUSES = {"queued", "starting", "running"}
_TERMINAL_RUN_STATUSES = {"completed", "completed_with_failures", "failed", "interrupted", "blocked"}
_SUBMISSION_START_EVENTS = {
    "submission.prepare.started",
    "submission.preview.started",
    "submission.submit.started",
}
_SUBMISSION_COMPLETION_EVENTS = {
    "submission.submit.completed",
    "submission.preview.completed",
}
_SUBMISSION_BLOCKED_EVENTS = {
    "submission.prepare.unsupported_source",
    "submission.submit.blocked",
    "submission.preview.blocked",
}
_SUBMISSION_FAILED_EVENTS = {
    "submission.prepare.failed",
    "submission.submit.failed",
    "submission.preview.failed",
}


def _workspace(workspace: FileWorkspace | Path) -> FileWorkspace:
    ws = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
    ws.ensure()
    return ws


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None


def _elapsed_seconds(started_at: str | None, ended_at: str | None = None) -> float:
    start = _parse_iso(started_at)
    if start is None:
        return 0.0
    end = _parse_iso(ended_at) or datetime.now(timezone.utc)
    return max(0.0, (end - start).total_seconds())


def _stream_health_for_state(state: LiveRunState, *, last_event_at: str | None = None) -> str:
    if state.status in {'completed', 'completed_with_failures', 'failed', 'interrupted'}:
        return 'connected'
    if state.status in {'running', 'starting', 'queued'}:
        latest = _parse_iso(last_event_at or state.last_event_at or state.updated_at)
        if latest is None:
            return 'reconnecting'
        age_seconds = (datetime.now(timezone.utc) - latest).total_seconds()
        if age_seconds > 15:
            return 'stale'
        return 'connected'
    return 'idle'


def _merge_mapping(base: dict[str, Any] | None, incoming: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(base or {})
    for key, value in (incoming or {}).items():
        merged[str(key)] = value
    return merged


def _merge_int_mapping(base: dict[str, Any] | None, incoming: dict[str, Any] | None) -> dict[str, int]:
    merged: dict[str, int] = {}
    for source in (base or {}, incoming or {}):
        for key, value in source.items():
            try:
                merged[str(key)] = int(value or 0)
            except (TypeError, ValueError):
                continue
    return merged


def _bump_stage_counter(state: LiveRunState, stage: str | None) -> dict[str, int]:
    counters = dict(state.stage_counters or {})
    stage_name = str(stage or '').strip()
    if stage_name:
        counters[stage_name] = int(counters.get(stage_name, 0) or 0) + 1
    return counters


def _count_mapping(values: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value or '').strip()
        if not key:
            continue
        counts[key] = int(counts.get(key, 0) or 0) + 1
    return counts


def _screened_out_reason(job: Any) -> str:
    screening = getattr(job, 'screening', None)
    reasons = list(getattr(screening, 'reasons', []) or [])
    if reasons:
        return str(reasons[0])
    if getattr(job, 'hard_reject_reason', None):
        return str(job.hard_reject_reason)
    if getattr(job, 'auth_reject_reason', None):
        return str(job.auth_reject_reason)
    return 'screened_out'


def _workspace_stats(workspace: FileWorkspace) -> dict[str, Any]:
    jobs = list(workspace.load_inbox())
    applications = [item for item in workspace.load_applications() if item.status not in _HIDDEN_APP_STATUSES]
    application_status_by_id = {item.id: item.status for item in applications}
    active_submissions = [
        item
        for item in workspace.load_submissions()
        if application_status_by_id.get(item.application_id) not in _HIDDEN_APP_STATUSES
    ]
    terminal_submission_statuses = {'submitted', 'failed', 'rejected', 'submission_failed'}
    queued_submissions = [item for item in active_submissions if str(item.status or '').strip().lower() not in terminal_submission_statuses]
    question_blocked = [
        item
        for item in queued_submissions
        if item.missing_required_fields
        or item.ungrounded_answers
        or item.low_confidence_answers
        or any(question.needs_user_input for question in item.questions)
    ]
    blocked = sum(1 for item in queued_submissions if item in question_blocked or item.warnings or item.last_error)
    pending_questions = sum(1 for item in queued_submissions for question in item.questions if question.needs_user_input)
    submitted_count = sum(1 for item in active_submissions if str(item.status or '').strip().lower() == 'submitted')
    failed_count = sum(1 for item in active_submissions if str(item.status or '').strip().lower() in {'failed', 'submission_failed', 'preview_failed', 'contract_error'})
    queued_statuses = {'pending', 'evaluated', 'pdf_ready', 'preview_ready', 'applied'}
    visible_jobs = [job for job in jobs if str(job.workflow_state or '').strip().lower() not in {'dismissed', 'archived', 'rejected'}]
    discovered_jobs = [job for job in visible_jobs if str(job.workflow_state or '').strip().lower() != 'screened_out']
    screened_out_jobs = [job for job in jobs if str(job.workflow_state or '').strip().lower() == 'screened_out']
    source_mix = _count_mapping([str(job.source or '').strip().lower() for job in discovered_jobs])
    configured_sources = [
        str(item or '').strip().lower()
        for item in workspace.load_profile().runtime.automation.production_sources
        if str(item or '').strip()
    ]
    zero_result_sources = [source for source in configured_sources if int(source_mix.get(source, 0) or 0) == 0]
    ready_for_submit = sum(
        1
        for item in active_submissions
        if item.submit_ready and str(item.status or '').strip().lower() in {'ready_for_submit', 'preview_ready'}
    )
    drafted_statuses = {'PDF Ready', 'Ready to Submit', 'Needs Input', 'Preview Ready', 'Applied', 'Submit Failed', 'Submission Uncertain'}
    drafted_count = sum(1 for item in applications if bool(item.pdf) or str(item.status or '').strip() in drafted_statuses)
    screened_out_reasons = _count_mapping([
        _screened_out_reason(job)
        for job in jobs
        if str(job.workflow_state or '').strip().lower() == 'screened_out'
    ])
    promoted_to_pipeline = sum(
        1
        for job in visible_jobs
        if (
            getattr(job, 'screening', None) is not None and bool(getattr(job.screening, 'approved', False))
        ) or str(job.workflow_state or '').strip().lower() in {'evaluated', 'pdf_ready', 'applied'}
    )
    portals = workspace.load_portals()
    configured_sources = [
        source_name
        for source_name in _SUPPORTED_SOURCE_ORDER
        if source_name in [
            str(item or '').strip().lower()
            for item in workspace.load_profile().runtime.automation.production_sources
            if str(item or '').strip()
        ] and bool(getattr(portals.sources.get(source_name), 'enabled', False))
    ]
    board_discovery = workspace.load_board_discovery_state()
    source_metrics: dict[str, Any] = {}
    discovery_error_counts: dict[str, int] = {}
    source_warnings: list[str] = []
    zero_result_sources: list[str] = []
    persisted_boards: dict[str, int] = {}
    persisted_domains: dict[str, int] = {}
    for source_name in _SUPPORTED_SOURCE_ORDER:
        record = board_discovery.sources.get(source_name)
        metrics = getattr(record, 'metrics', None)
        payload = metrics.model_dump(mode='json') if metrics is not None else {}
        source_metrics[source_name] = payload
        discovery_error_counts[source_name] = int((payload or {}).get('errors') or 0)
        persisted_boards[source_name] = len(list(getattr(record, 'boards', []) or []))
        persisted_domains[source_name] = len(list(getattr(record, 'domains', []) or []))
        warning = str((payload or {}).get('warning') or '').strip()
        if warning:
            source_warnings.append(warning)
        if source_name in configured_sources and bool((payload or {}).get('zero_result')):
            zero_result_sources.append(source_name)
    if not zero_result_sources:
        zero_result_sources = [source for source in configured_sources if int(source_mix.get(source, 0) or 0) == 0]
    return {
        'discovered': len(discovered_jobs),
        'screened_out': len(screened_out_jobs),
        'evaluated': len(applications),
        'drafted': drafted_count,
        'ready_to_apply': ready_for_submit,
        'blocked_by_questions': len(question_blocked),
        'eligible_after_filters': sum(1 for job in visible_jobs if str(job.workflow_state or '').strip().lower() in queued_statuses),
        'promoted_to_pipeline': promoted_to_pipeline,
        'applications_created': len(applications),
        'ready_for_submit': ready_for_submit,
        'blocked': blocked,
        'submitted': submitted_count,
        'failed': failed_count,
        'pending_questions': pending_questions,
        'source_mix': source_mix,
        'zero_result_sources': zero_result_sources,
        'source_metrics': source_metrics,
        'source_warnings': source_warnings,
        'discovery_error_counts': discovery_error_counts,
        'persisted_board_counts': persisted_boards,
        'persisted_domain_counts': persisted_domains,
        'configured_sources': configured_sources,
        'screened_out_reasons': screened_out_reasons,
    }


def _normalize_error_payload(error: dict[str, Any] | str | Exception | None) -> dict[str, Any] | None:
    if error is None:
        return None
    if isinstance(error, dict):
        return {str(key): value for key, value in error.items()}
    return {'message': str(error)}


def _normalize_artifact_paths(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        candidates = value.values()
    elif isinstance(value, (list, tuple, set)):
        candidates = value
    else:
        candidates = [value]
    normalized: list[str] = []
    for item in candidates:
        cleaned = str(item or '').strip()
        if cleaned and cleaned not in normalized:
            normalized.append(cleaned)
    return normalized


def _next_run_status(
    state: LiveRunState,
    *,
    run_id: str,
    run_type: str,
    event_type: str,
    event_status: str,
) -> str:
    normalized_status = str(event_status or "").strip().lower()
    current_status = str(state.status or "").strip().lower() or "idle"
    current_run_matches = str(state.run_id or "").strip() == str(run_id or "").strip()
    is_finished_event = event_type == f"{run_type}.finished"

    if normalized_status in _ACTIVE_RUN_STATUSES:
        return normalized_status
    if not normalized_status:
        return current_status
    if not current_run_matches or current_status == "idle":
        return normalized_status
    if is_finished_event and normalized_status in _TERMINAL_RUN_STATUSES:
        return normalized_status
    if current_status in _ACTIVE_RUN_STATUSES:
        return current_status
    return normalized_status


def _derived_state_updates(
    state: LiveRunState,
    *,
    run_id: str,
    run_type: str,
    event_type: str,
    event_status: str,
    event_at: str,
) -> dict[str, Any]:
    if str(run_type or "").strip().lower() != "submission":
        return {}

    normalized_status = str(event_status or "").strip().lower()
    current_status = str(state.status or "").strip().lower() or "idle"
    current_run_matches = str(state.run_id or "").strip() == str(run_id or "").strip()

    if event_type in _SUBMISSION_START_EVENTS and (
        not current_run_matches or current_status not in _ACTIVE_RUN_STATUSES or bool(state.completed_at)
    ):
        return {
            "started_at": event_at,
            "run_started_at": event_at,
            "completed_at": None,
            "latest_error": None,
        }

    if event_type in _SUBMISSION_COMPLETION_EVENTS:
        terminal_status = "completed" if normalized_status == "completed" else "completed_with_failures"
        if normalized_status in {"completed", "warning"}:
            return {"status": terminal_status, "completed_at": event_at}

    if event_type in _SUBMISSION_BLOCKED_EVENTS or normalized_status == "blocked":
        return {"status": "blocked", "completed_at": event_at}

    if event_type in _SUBMISSION_FAILED_EVENTS or normalized_status == "failed":
        return {"status": "failed", "completed_at": event_at}

    return {}


def _normalize_state(state: LiveRunState, *, last_event: LiveRunEvent | None = None) -> LiveRunState:
    normalized_status = str(state.status or "").strip().lower() or "idle"
    normalized_completed_at = state.completed_at
    if last_event is not None and str(state.run_id or "").strip() == str(last_event.run_id or "").strip():
        if last_event.event_type in _SUBMISSION_COMPLETION_EVENTS and last_event.status in {"completed", "warning"} and normalized_status in _ACTIVE_RUN_STATUSES:
            normalized_status = "completed" if last_event.status == "completed" else "completed_with_failures"
            normalized_completed_at = normalized_completed_at or last_event.created_at
        elif last_event.event_type in _SUBMISSION_BLOCKED_EVENTS and normalized_status in _ACTIVE_RUN_STATUSES:
            normalized_status = "blocked"
            normalized_completed_at = normalized_completed_at or last_event.created_at
        elif last_event.event_type in _SUBMISSION_FAILED_EVENTS and normalized_status in _ACTIVE_RUN_STATUSES:
            normalized_status = "failed"
            normalized_completed_at = normalized_completed_at or last_event.created_at

    started_at = state.run_started_at or state.started_at
    last_event_at = last_event.created_at if last_event is not None else state.last_event_at
    stream_health = _stream_health_for_state(
        state.model_copy(update={"status": normalized_status, "completed_at": normalized_completed_at}),
        last_event_at=last_event_at,
    )
    current_company = last_event.company if last_event and last_event.company else state.current_company or state.company
    current_role = last_event.role if last_event and last_event.role else state.current_role or state.role
    current_title = state.current_title
    if current_company and current_role:
        current_title = f'{current_company} / {current_role}'
    elif last_event and last_event.payload.get('current_title'):
        current_title = str(last_event.payload.get('current_title') or '') or current_title
    model_activity = dict(state.model_activity or {})
    if last_event is not None:
        if last_event.model_profile:
            model_activity['profile'] = last_event.model_profile
        if last_event.model_role:
            model_activity['role'] = last_event.model_role
        if last_event.step:
            model_activity['step'] = last_event.step
        if last_event.stage:
            model_activity['stage'] = last_event.stage
        model_activity['event_type'] = last_event.event_type
    stage_counters = _merge_int_mapping(state.stage_counters, (state.stats or {}).get('stage_counters'))
    stats = dict(state.stats or {})
    if stage_counters:
        stats['stage_counters'] = stage_counters
    if model_activity:
        stats['model_activity'] = dict(model_activity)
    elapsed_seconds = _elapsed_seconds(started_at, normalized_completed_at or last_event_at)
    return state.model_copy(
        update={
            'status': normalized_status,
            'run_started_at': started_at,
            'completed_at': normalized_completed_at,
            'last_event_at': last_event_at,
            'elapsed_seconds': elapsed_seconds,
            'current_company': current_company,
            'current_role': current_role,
            'current_title': current_title,
            'active_step': last_event.step if last_event and last_event.step else (last_event.stage if last_event and last_event.stage else state.active_step),
            'latest_operator_message': last_event.message if last_event is not None else state.latest_operator_message,
            'stream_health': stream_health,
            'model_activity': model_activity,
            'stage_counters': stage_counters,
            'stats': stats,
        }
    )


def live_metrics(workspace: FileWorkspace | Path) -> dict[str, int]:
    ws = _workspace(workspace)
    submissions = ws.load_submissions()
    applications = ws.load_applications()
    hidden_application_statuses = {'Rejected', 'Dismissed', 'Archived'}
    application_status_by_id = {item.id: item.status for item in applications}
    active_submissions = [
        item
        for item in submissions
        if application_status_by_id.get(item.application_id) not in hidden_application_statuses
    ]
    terminal_submission_statuses = {'submitted', 'failed', 'rejected', 'submission_failed'}
    queued_submissions = [item for item in active_submissions if str(item.status or '').strip().lower() not in terminal_submission_statuses]
    rejected_count = sum(1 for item in applications if item.status in hidden_application_statuses)
    snapshot = _workspace_stats(ws)
    return {
        'queue_depth': len(queued_submissions),
        'blocked_applications': int(snapshot.get('blocked_by_questions', snapshot.get('blocked', 0)) or 0),
        'pending_questions': int(snapshot.get('pending_questions', 0) or 0),
        'submitted_count': int(snapshot.get('submitted', 0) or 0),
        'failed_count': int(snapshot.get('failed', 0) or 0),
        'rejected_count': rejected_count,
    }


def save_live_state(workspace: FileWorkspace | Path, **updates: Any) -> LiveRunState:
    ws = _workspace(workspace)
    state = ws.load_live_state()
    metrics = live_metrics(ws)
    merged_stats = _merge_mapping(state.stats, _workspace_stats(ws))
    merged_stats = _merge_mapping(merged_stats, updates.pop('stats', None))
    merged_model_activity = _merge_mapping(state.model_activity, updates.pop('model_activity', None))
    merged_stage_counters = _merge_int_mapping(state.stage_counters, updates.pop('stage_counters', None))
    merged = state.model_copy(update={**metrics, **updates, 'stats': merged_stats, 'model_activity': merged_model_activity, 'stage_counters': merged_stage_counters})
    merged = _normalize_state(merged)
    ws.save_live_state(merged)
    return merged


def emit_live_event(
    workspace: FileWorkspace | Path,
    *,
    run_id: str,
    run_type: str,
    event_type: str,
    message: str,
    status: str = 'info',
    stage: str | None = None,
    phase: str | None = None,
    job_id: str | None = None,
    application_id: str | None = None,
    submission_id: str | None = None,
    company: str | None = None,
    role: str | None = None,
    source: str | None = None,
    model_role: str | None = None,
    model_profile: str | None = None,
    model_call_id: str | None = None,
    step: str | None = None,
    artifact_paths: list[str] | dict[str, Any] | None = None,
    error: dict[str, Any] | str | Exception | None = None,
    metrics: dict[str, Any] | None = None,
    trace_ref: str | None = None,
    payload: dict[str, Any] | None = None,
    state_updates: dict[str, Any] | None = None,
) -> LiveRunEvent:
    ws = _workspace(workspace)
    state = ws.load_live_state()
    live_state_metrics = live_metrics(ws)
    event_at = _utcnow_iso()
    normalized_error = _normalize_error_payload(error)
    normalized_artifact_paths = _normalize_artifact_paths(artifact_paths)
    derived_state_updates = _derived_state_updates(
        state,
        run_id=run_id,
        run_type=run_type,
        event_type=event_type,
        event_status=status,
        event_at=event_at,
    )
    event = LiveRunEvent(
        event_id=uuid.uuid4().hex[:12],
        run_id=run_id,
        run_type=run_type,
        event_type=event_type,
        phase=phase or stage or step or event_type,
        status=status,
        stage=stage,
        message=message,
        created_at=event_at,
        job_id=job_id,
        application_id=application_id,
        submission_id=submission_id,
        company=company,
        role=role,
        source=source,
        model_role=model_role,
        model_profile=model_profile,
        model_call_id=model_call_id,
        step=step or stage or event_type,
        artifact_paths=normalized_artifact_paths,
        error=normalized_error,
        metrics=dict(metrics or {}),
        trace_ref=trace_ref,
        payload=payload or {},
    )
    pending_updates = _merge_mapping(derived_state_updates, dict(state_updates or {}))
    if normalized_error is not None and 'latest_error' not in pending_updates:
        pending_updates['latest_error'] = str(normalized_error.get('message') or normalized_error)
    merged_stats = _merge_mapping(state.stats, _workspace_stats(ws))
    merged_stats = _merge_mapping(merged_stats, pending_updates.pop('stats', None))
    merged_model_activity = _merge_mapping(state.model_activity, pending_updates.pop('model_activity', None))
    merged_stage_counters = _merge_int_mapping(_bump_stage_counter(state, stage), pending_updates.pop('stage_counters', None))
    if model_profile:
        merged_model_activity['profile'] = model_profile
    if model_role:
        merged_model_activity['role'] = model_role
    if stage:
        merged_model_activity['stage'] = stage
    if step:
        merged_model_activity['step'] = step
    if merged_stage_counters:
        merged_stats['stage_counters'] = merged_stage_counters
    if merged_model_activity:
        merged_stats['model_activity'] = dict(merged_model_activity)
    state_payload: dict[str, Any] = {
        **live_state_metrics,
        'run_id': run_id,
        'run_type': run_type,
        'status': _next_run_status(
            state,
            run_id=run_id,
            run_type=run_type,
            event_type=event_type,
            event_status=status,
        ),
        'stage': stage or state.stage,
        'active_job_id': job_id,
        'active_application_id': application_id,
        'company': company,
        'role': role,
        'source': source,
        'last_event_at': event_at,
        'latest_operator_message': message,
        'active_step': step or stage or event_type,
        'current_company': company or state.current_company,
        'current_role': role or state.current_role,
        'current_title': f'{company} / {role}' if company and role else state.current_title,
        'stream_health': 'connected',
        'run_started_at': state.run_started_at or state.started_at,
        'elapsed_seconds': _elapsed_seconds(state.run_started_at or state.started_at, event_at),
        'event_count': int(state.event_count or 0) + 1,
        'stats': merged_stats,
        'model_activity': merged_model_activity,
        'stage_counters': merged_stage_counters,
    }
    state_payload.update(pending_updates)
    merged_state = state.model_copy(update=state_payload)
    merged_state = _normalize_state(merged_state, last_event=event)
    ws.save_live_state(merged_state)
    ws.append_live_event(event)
    return event


def begin_live_run(workspace: FileWorkspace | Path, *, run_id: str, run_type: str, stage: str, message: str) -> dict[str, Any]:
    started_at = _utcnow_iso()
    state = save_live_state(
        workspace,
        run_id=run_id,
        run_type=run_type,
        status='running',
        stage=stage,
        started_at=started_at,
        run_started_at=started_at,
        completed_at=None,
        latest_error=None,
        latest_operator_message=message,
        active_step=stage,
        stream_health='connected',
        stage_counters={stage: 0},
    )
    event = emit_live_event(workspace, run_id=run_id, run_type=run_type, event_type=f'{run_type}.started', stage=stage, message=message, status='running', step=stage)
    return {'state': state, 'event': event}


def finish_live_run(
    workspace: FileWorkspace | Path,
    *,
    run_id: str,
    run_type: str,
    status: str,
    stage: str,
    message: str,
    latest_error: str | None = None,
) -> dict[str, Any]:
    completed_at = _utcnow_iso()
    state = save_live_state(
        workspace,
        run_id=run_id,
        run_type=run_type,
        status=status,
        stage=stage,
        completed_at=completed_at,
        latest_error=latest_error,
        latest_operator_message=message,
        active_step=stage,
        stream_health='connected' if status not in {'failed', 'interrupted'} else 'stale',
    )
    event = emit_live_event(
        workspace,
        run_id=run_id,
        run_type=run_type,
        event_type=f'{run_type}.finished',
        stage=stage,
        message=message,
        status=status,
        payload={'latest_error': latest_error} if latest_error else {},
        state_updates={'completed_at': state.completed_at, 'latest_error': latest_error},
        step=stage,
    )
    return {'state': state, 'event': event}


def interrupt_live_run(workspace: FileWorkspace | Path, *, reason: str) -> dict[str, Any] | None:
    ws = _workspace(workspace)
    state = ws.load_live_state()
    if state.status != 'running' or not state.run_id:
        return None
    return finish_live_run(
        ws,
        run_id=state.run_id,
        run_type=state.run_type or 'unknown',
        status='interrupted',
        stage=state.stage or 'idle',
        message='Run interrupted by server restart or worker loss.',
        latest_error=reason,
    )


def live_status_payload(workspace: FileWorkspace | Path, *, limit: int = 100) -> dict[str, Any]:
    ws = _workspace(workspace)
    state = ws.load_live_state()
    events = [item.model_dump(mode='json') for item in ws.load_live_events(limit=limit)]
    last_event = LiveRunEvent.model_validate(events[-1]) if events else None
    current_state = _normalize_state(state, last_event=last_event)
    return {'state': current_state.model_dump(mode='json'), 'events': events}


_HIDDEN_WORKFLOW_STATES = {'screened_out', 'dismissed', 'archived', 'rejected'}
_HIDDEN_APP_STATUSES = {'Rejected', 'Dismissed', 'Archived'}


def jobs_table_payload(workspace: FileWorkspace | Path, *, limit: int | None = None, include_rejected: bool = False) -> dict[str, Any]:
    ws = _workspace(workspace)
    jobs = {job.job_id: job for job in ws.load_inbox()}
    applications = ws.load_applications()
    submissions = {record.application_id: record for record in ws.load_submissions()}
    evaluations = {record.job_id: record for record in [ws.load_evaluation(job_id) for job_id in jobs] if record is not None}
    rows: list[dict[str, Any]] = []
    seen_job_ids: set[str] = set()

    for application in applications:
        if not include_rejected and application.status in _HIDDEN_APP_STATUSES:
            continue
        job = jobs.get(application.job_id)
        if not include_rejected and job and job.workflow_state in _HIDDEN_WORKFLOW_STATES:
            continue
        submission = submissions.get(application.id)
        evaluation = evaluations.get(application.job_id) or ws.load_evaluation(application.job_id)
        seen_job_ids.add(application.job_id)
        blockers = [] if submission is None else [*submission.missing_required_fields, *submission.ungrounded_answers, *submission.low_confidence_answers, *submission.warnings]
        rows.append(
            {
                'job_id': application.job_id,
                'application_id': application.id,
                'company': application.company,
                'role': application.role,
                'source': application.source or (job.source if job is not None else ''),
                'url': application.url or (job.url if job is not None else ''),
                'apply_url': job.apply_url if job is not None else None,
                'location': job.location if job is not None else None,
                'discovered_at': job.discovered_at if job is not None else None,
                'evaluated_at': evaluation.evaluated_at if evaluation is not None else None,
                'submitted_at': submission.submitted_at if submission is not None else None,
                'previewed_at': submission.previewed_at if submission is not None else None,
                'application_status': application.status,
                'submission_status': submission.status if submission is not None else None,
                'event_status': submission.event_status if submission is not None else None,
                'preview_ready': submission.preview_ready if submission is not None else False,
                'submit_ready': submission.submit_ready if submission is not None else False,
                'blocked': bool(blockers or (submission and submission.last_error)),
                'blockers': blockers,
                'last_error': submission.last_error if submission is not None else None,
                'report': application.report,
                'pdf': application.pdf,
                'score': application.score,
                'grade': application.grade,
                'workflow_state': job.workflow_state if job is not None else None,
            }
        )

    for job_id, job in jobs.items():
        if job_id in seen_job_ids:
            continue
        evaluation = evaluations.get(job_id) or ws.load_evaluation(job_id)
        rows.append(
            {
                'job_id': job.job_id,
                'application_id': None,
                'company': job.company,
                'role': job.title,
                'source': job.source,
                'url': job.url,
                'apply_url': job.apply_url,
                'location': job.location,
                'discovered_at': job.discovered_at,
                'evaluated_at': evaluation.evaluated_at if evaluation is not None else None,
                'submitted_at': None,
                'previewed_at': None,
                'application_status': job.workflow_state,
                'submission_status': None,
                'event_status': None,
                'preview_ready': False,
                'submit_ready': False,
                'blocked': False,
                'blockers': [],
                'last_error': None,
                'report': None,
                'pdf': False,
                'score': evaluation.score if evaluation is not None else 0.0,
                'grade': evaluation.grade if evaluation is not None else None,
                'workflow_state': job.workflow_state,
            }
        )

    rows.sort(key=lambda item: str(item.get('submitted_at') or item.get('previewed_at') or item.get('evaluated_at') or item.get('discovered_at') or ''), reverse=True)
    if limit is not None:
        rows = rows[:limit]
    counts = {
        'rows': len(rows),
        'applied': sum(1 for row in rows if row.get('submission_status') == 'submitted' or row.get('application_status') == 'Applied'),
        'blocked': sum(1 for row in rows if row.get('blocked')),
        'preview_ready': sum(1 for row in rows if row.get('preview_ready')),
        'submit_ready': sum(1 for row in rows if row.get('submit_ready')),
    }
    return {'count': len(rows), 'counts': counts, 'items': rows}
