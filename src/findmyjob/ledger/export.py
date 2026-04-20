from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from findmyjob.apply.service import ApplicationService
from findmyjob.db.models import ApplicationAnswerRecord, ApplicationQuestionRecord, ApplicationRecord, ArtifactRecord, Company, JobPosting, PersonalJobTriageRecord, QualificationResultRecord, SubmitAttemptRecord


def export_ledger(session_or_runtime: Session | Any, destination_without_suffix: Path) -> tuple[Path, Path]:
    if hasattr(session_or_runtime, 'session_scope') and not isinstance(session_or_runtime, Session):
        with session_or_runtime.session_scope() as session:
            return _export_ledger_with_session(session, destination_without_suffix)
    if _looks_like_filefirst_workspace(session_or_runtime):
        return _export_ledger_with_filefirst_workspace(session_or_runtime, destination_without_suffix)
    return _export_ledger_with_session(session_or_runtime, destination_without_suffix)


def _export_ledger_with_session(session: Session, destination_without_suffix: Path) -> tuple[Path, Path]:
    application_rows = _application_rows(session)
    question_rows = _question_rows(session)
    account_rows = _account_rows()
    return _write_exports(destination_without_suffix, application_rows, question_rows, account_rows)


def _export_ledger_with_filefirst_workspace(workspace: Any, destination_without_suffix: Path) -> tuple[Path, Path]:
    application_rows = _filefirst_application_rows(workspace)
    question_rows = _filefirst_question_rows(workspace)
    account_rows = _account_rows()
    return _write_exports(destination_without_suffix, application_rows, question_rows, account_rows)


def _write_exports(
    destination_without_suffix: Path,
    application_rows: list[dict[str, Any]],
    question_rows: list[dict[str, Any]],
    account_rows: list[dict[str, Any]],
) -> tuple[Path, Path]:
    import xlsxwriter

    destination_without_suffix = Path(destination_without_suffix)
    destination_without_suffix.parent.mkdir(parents=True, exist_ok=True)

    csv_path = destination_without_suffix.with_suffix('.csv')
    xlsx_path = destination_without_suffix.with_suffix('.xlsx')
    applications_csv = destination_without_suffix.parent / 'applications.csv'
    questions_csv = destination_without_suffix.parent / 'questions.csv'
    accounts_csv = destination_without_suffix.parent / 'accounts.csv'

    application_headers = list(application_rows[0].keys()) if application_rows else _application_headers()
    question_headers = list(question_rows[0].keys()) if question_rows else _question_headers()
    account_headers = list(account_rows[0].keys()) if account_rows else _account_headers()

    _write_csv(csv_path, application_headers, application_rows)
    _write_csv(applications_csv, application_headers, application_rows)
    _write_csv(questions_csv, question_headers, question_rows)
    _write_csv(accounts_csv, account_headers, account_rows)

    workbook = xlsxwriter.Workbook(str(xlsx_path))
    _write_sheet(workbook, 'applications', application_headers, application_rows)
    _write_sheet(workbook, 'questions', question_headers, question_rows)
    _write_sheet(workbook, 'accounts', account_headers, account_rows)
    workbook.close()
    return csv_path, xlsx_path


def _looks_like_filefirst_workspace(source: Any) -> bool:
    return all(
        hasattr(source, attribute)
        for attribute in ('load_applications', 'load_submissions', 'load_job', 'load_evaluation')
    )


def _application_rows(session: Session) -> list[dict[str, Any]]:
    rows = []
    jobs = session.execute(
        select(JobPosting, Company, ApplicationRecord, QualificationResultRecord)
        .join(Company, JobPosting.company_id == Company.id)
        .join(ApplicationRecord, ApplicationRecord.job_posting_id == JobPosting.id, isouter=True)
        .join(QualificationResultRecord, QualificationResultRecord.job_posting_id == JobPosting.id, isouter=True)
        .order_by(JobPosting.discovered_at.desc())
    ).all()

    for job, company, application, qualification in jobs:
        artifacts = session.scalars(select(ArtifactRecord).where(ArtifactRecord.job_posting_id == job.id)).all()
        triage = session.scalar(select(PersonalJobTriageRecord).where(PersonalJobTriageRecord.job_posting_id == job.id))
        latest_attempt = None
        if application is not None:
            latest_attempt = session.scalar(
                select(SubmitAttemptRecord)
                .where(SubmitAttemptRecord.application_id == application.id)
                .order_by(SubmitAttemptRecord.created_at.desc())
            )
        autonomous = dict((job.notes or {}).get('autonomous') or {})
        rows.append(
            {
                'application_id': application.id if application is not None else '',
                'job_id': job.id,
                'company': company.display_name,
                'title': job.title,
                'board': job.source_adapter,
                'posting_url': job.posting_url,
                'application_url': job.apply_url or '',
                'location': job.location_raw or '',
                'status': application.status.value if application is not None else job.lifecycle_status.value,
                'review_status': application.review_status.value if application is not None else '',
                'skip_reason': autonomous.get('skip_reason') or '',
                'submit_outcome': autonomous.get('submit_result') or (latest_attempt.status if latest_attempt is not None else ''),
                'submit_failure_reason': autonomous.get('submit_failure_reason') or (((latest_attempt.payload or {}).get('evidence') or {}).get('failure_reason') if latest_attempt is not None else ''),
                'matched_presets': ';'.join(autonomous.get('matched_presets') or []),
                'artifacts_paths': ';'.join(artifact.path for artifact in artifacts),
                'date_discovered': job.discovered_at.isoformat(),
                'date_prepared': application.prepared_at.isoformat() if application and application.prepared_at else '',
                'date_submitted': application.submitted_at.isoformat() if application and application.submitted_at else '',
                'sponsorship_classification': qualification.fit if qualification else '',
                'confidence': qualification.confidence if qualification else '',
                'triage_status': triage.status.value if triage is not None else 'new',
                'ai_greenlight': autonomous.get('green_light'),
                'ai_score': autonomous.get('score'),
                'job_description_excerpt': str(job.description or '').replace('\n', ' ')[:400],
                'notes': str(job.notes or {}),
            }
        )
    return rows


def _filefirst_application_rows(workspace: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    submissions = {item.application_id: item for item in workspace.load_submissions()}
    applications = sorted(
        workspace.load_applications(),
        key=lambda item: (str(getattr(item, 'date', '') or ''), str(getattr(item, 'id', '') or '')),
        reverse=True,
    )
    for application in applications:
        job = workspace.load_job(application.job_id)
        evaluation = workspace.load_evaluation(application.job_id)
        submission = submissions.get(application.id)
        job_notes = getattr(job, 'notes', {}) if job is not None else {}
        autonomous_notes = dict((job_notes or {}).get('autonomous') or {}) if isinstance(job_notes, dict) else {}
        rows.append(
            {
                'application_id': application.id,
                'job_id': application.job_id,
                'company': application.company,
                'title': application.role,
                'board': _coalesce(
                    getattr(job, 'source', None),
                    getattr(submission, 'source', None),
                    getattr(application, 'source', None),
                    '',
                ),
                'posting_url': _coalesce(getattr(job, 'url', None), getattr(application, 'url', None), ''),
                'application_url': _coalesce(
                    getattr(submission, 'apply_url', None),
                    getattr(job, 'apply_url', None),
                    getattr(application, 'url', None),
                    '',
                ),
                'location': _coalesce(getattr(job, 'location', None), ''),
                'status': application.status,
                'review_status': _filefirst_review_status(submission),
                'skip_reason': autonomous_notes.get('skip_reason') or '',
                'submit_outcome': _filefirst_submit_outcome(submission, autonomous_notes),
                'submit_failure_reason': _filefirst_submit_failure_reason(submission, autonomous_notes),
                'matched_presets': ';'.join(str(item) for item in autonomous_notes.get('matched_presets') or []),
                'artifacts_paths': ';'.join(_filefirst_artifact_paths(workspace, application, submission)),
                'date_discovered': _coalesce(getattr(job, 'discovered_at', None), getattr(application, 'date', None), ''),
                'date_prepared': _coalesce(
                    getattr(submission, 'previewed_at', None),
                    getattr(submission, 'created_at', None),
                    getattr(application, 'date', None),
                    '',
                ),
                'date_submitted': _coalesce(getattr(submission, 'submitted_at', None), ''),
                'sponsorship_classification': '',
                'confidence': '',
                'triage_status': _coalesce(getattr(job, 'workflow_state', None), 'new'),
                'ai_greenlight': autonomous_notes.get('green_light'),
                'ai_score': _coalesce(autonomous_notes.get('score'), getattr(evaluation, 'score', None), getattr(application, 'score', None), ''),
                'job_description_excerpt': str(getattr(job, 'description', '') or '').replace('\n', ' ')[:400],
                'notes': _filefirst_notes(application=application, job=job, submission=submission),
            }
        )
    return rows


def _question_rows(session: Session) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    questions = session.execute(
        select(ApplicationQuestionRecord, ApplicationAnswerRecord, ApplicationRecord, JobPosting, Company)
        .join(ApplicationRecord, ApplicationQuestionRecord.application_id == ApplicationRecord.id)
        .join(JobPosting, ApplicationRecord.job_posting_id == JobPosting.id)
        .join(Company, JobPosting.company_id == Company.id)
        .join(ApplicationAnswerRecord, ApplicationAnswerRecord.question_id == ApplicationQuestionRecord.id, isouter=True)
        .order_by(ApplicationRecord.updated_at.desc(), ApplicationQuestionRecord.id.asc())
    ).all()
    for question, answer, application, job, company in questions:
        if ApplicationService._question_hidden_from_operator(question):
            continue
        answer_text = str((answer.candidate_answer if answer is not None else '') or '').strip()
        if answer is not None and not answer.needs_user_input and answer_text:
            continue
        rows.append(
            {
                'application_id': application.id,
                'job_id': job.id,
                'company': company.display_name,
                'title': job.title,
                'board': job.source_adapter,
                'question_id': question.id,
                'prompt_text': ApplicationService._blocker_label(question),
                'question_type': question.question_type.value if hasattr(question.question_type, 'value') else str(question.question_type),
                'widget_type': question.widget_type,
                'required': bool(question.required),
                'existing_answer': answer_text,
                'needs_user_input': bool(answer is None or answer.needs_user_input or not answer_text),
                'verification_status': answer.verification_status.value if answer is not None and hasattr(answer.verification_status, 'value') else (str(answer.verification_status) if answer is not None else ''),
                'option_details': json.dumps((question.field_config or {}).get('option_details') or []),
            }
        )
    return rows


def _filefirst_question_rows(workspace: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    submissions = sorted(
        workspace.load_submissions(),
        key=lambda item: str(getattr(item, 'updated_at', '') or ''),
        reverse=True,
    )
    for submission in submissions:
        job = workspace.load_job(submission.job_id)
        for question in submission.questions:
            answer_text = _filefirst_question_answer(submission, question)
            needs_user_input = bool(question.needs_user_input or not answer_text)
            if not needs_user_input and answer_text:
                continue
            rows.append(
                {
                    'application_id': submission.application_id,
                    'job_id': submission.job_id,
                    'company': submission.company,
                    'title': submission.role,
                    'board': _coalesce(getattr(job, 'source', None), submission.source, ''),
                    'question_id': question.question_id,
                    'prompt_text': question.prompt_text,
                    'question_type': question.question_type,
                    'widget_type': question.widget_type,
                    'required': bool(question.required),
                    'existing_answer': answer_text,
                    'needs_user_input': needs_user_input,
                    'verification_status': question.verification_status,
                    'option_details': json.dumps(question.option_details or []),
                }
            )
    return rows


def _account_rows() -> list[dict[str, Any]]:
    return []


def _filefirst_review_status(submission: Any | None) -> str:
    if submission is None:
        return ''
    if getattr(submission, 'status', '') == 'submitted':
        return 'submitted'
    if getattr(submission, 'submit_ready', False):
        return 'ready_to_submit'
    if any(getattr(question, 'needs_user_input', False) for question in getattr(submission, 'questions', [])):
        return 'needs_user_input'
    if getattr(submission, 'preview_ready', False):
        return 'preview_ready'
    return str(getattr(submission, 'status', '') or '')


def _filefirst_submit_outcome(submission: Any | None, autonomous_notes: dict[str, Any]) -> str:
    if submission is None:
        return str(autonomous_notes.get('submit_result') or '')
    result_payload = dict(getattr(submission, 'result', {}) or {})
    return str(
        result_payload.get('submission_status')
        or result_payload.get('status')
        or autonomous_notes.get('submit_result')
        or getattr(submission, 'status', '')
        or ''
    )


def _filefirst_submit_failure_reason(submission: Any | None, autonomous_notes: dict[str, Any]) -> str:
    if submission is None:
        return str(autonomous_notes.get('submit_failure_reason') or '')
    result_payload = dict(getattr(submission, 'result', {}) or {})
    return str(
        result_payload.get('failure_reason')
        or getattr(submission, 'last_error', None)
        or autonomous_notes.get('submit_failure_reason')
        or ''
    )


def _filefirst_artifact_paths(workspace: Any, application: Any, submission: Any | None) -> list[str]:
    collected: list[str] = []
    report_ref = str(getattr(application, 'report', '') or '').strip()
    if report_ref:
        report_path = Path(report_ref)
        if not report_path.is_absolute():
            report_path = Path(getattr(workspace, 'root', Path.cwd())) / report_path
        if report_path.exists():
            collected.append(_workspace_relative_path(workspace, report_path))
    try:
        resume_pdf_path = workspace.resume_pdf_path_for(application.id, application.company, getattr(application, 'date', None))
    except Exception:
        resume_pdf_path = None
    if resume_pdf_path is not None and Path(resume_pdf_path).exists():
        collected.append(_workspace_relative_path(workspace, Path(resume_pdf_path)))
    if submission is not None:
        for raw_value in dict(getattr(submission, 'artifacts', {}) or {}).values():
            candidate = str(raw_value or '').strip()
            if not candidate or candidate.startswith('http://') or candidate.startswith('https://'):
                continue
            candidate_path = Path(candidate)
            if not candidate_path.is_absolute():
                candidate_path = Path(getattr(workspace, 'root', Path.cwd())) / candidate_path
            if candidate_path.exists():
                collected.append(_workspace_relative_path(workspace, candidate_path))
    return list(dict.fromkeys(collected))


def _filefirst_question_answer(submission: Any, question: Any) -> str:
    existing = str(getattr(question, 'existing_answer', '') or '').strip()
    if existing:
        return existing
    manual_answers = dict(getattr(submission, 'manual_answers', {}) or {})
    normalized_key = str(getattr(question, 'normalized_key', '') or '').strip()
    if normalized_key and manual_answers.get(normalized_key):
        return str(manual_answers[normalized_key]).strip()
    question_id = str(getattr(question, 'question_id', '') or '').strip()
    if question_id and manual_answers.get(question_id):
        return str(manual_answers[question_id]).strip()
    return ''


def _filefirst_notes(*, application: Any, job: Any | None, submission: Any | None) -> str:
    payload: dict[str, Any] = {}
    application_notes = str(getattr(application, 'notes', '') or '').strip()
    if application_notes:
        payload['application_notes'] = application_notes
    job_notes = getattr(job, 'notes', None) if job is not None else None
    if job_notes:
        payload['job_notes'] = job_notes
    submission_notes = list(getattr(submission, 'notes', []) or []) if submission is not None else []
    if submission_notes:
        payload['submission_notes'] = submission_notes
    return json.dumps(payload, sort_keys=True) if payload else ''


def _workspace_relative_path(workspace: Any, path: Path) -> str:
    candidate = Path(path)
    try:
        return str(workspace.relative_path(candidate)).replace('\\', '/')
    except Exception:
        root = Path(getattr(workspace, 'root', Path.cwd()))
        try:
            return candidate.resolve().relative_to(root.resolve()).as_posix()
        except Exception:
            return str(candidate).replace('\\', '/')


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
            continue
        return value
    return ''


def _write_csv(path: Path, headers: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        if rows:
            writer.writerows(rows)


def _write_sheet(workbook, name: str, headers: list[str], rows: list[dict[str, Any]]) -> None:
    worksheet = workbook.add_worksheet(name)
    for column, header in enumerate(headers):
        worksheet.write(0, column, header)
    for row_index, row in enumerate(rows, start=1):
        for column, header in enumerate(headers):
            worksheet.write(row_index, column, row.get(header, ''))


def _application_headers() -> list[str]:
    return [
        'application_id',
        'job_id',
        'company',
        'title',
        'board',
        'posting_url',
        'application_url',
        'location',
        'status',
        'review_status',
        'skip_reason',
        'submit_outcome',
        'submit_failure_reason',
        'matched_presets',
        'artifacts_paths',
        'date_discovered',
        'date_prepared',
        'date_submitted',
        'sponsorship_classification',
        'confidence',
        'triage_status',
        'ai_greenlight',
        'ai_score',
        'job_description_excerpt',
        'notes',
    ]


def _question_headers() -> list[str]:
    return [
        'application_id',
        'job_id',
        'company',
        'title',
        'board',
        'question_id',
        'prompt_text',
        'question_type',
        'widget_type',
        'required',
        'existing_answer',
        'needs_user_input',
        'verification_status',
        'option_details',
    ]


def _account_headers() -> list[str]:
    return [
        'provider',
        'account_key',
        'login_email',
        'status',
        'verification_required',
        'note',
    ]
