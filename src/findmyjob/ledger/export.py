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
    return _export_ledger_with_session(session_or_runtime, destination_without_suffix)


def _export_ledger_with_session(session: Session, destination_without_suffix: Path) -> tuple[Path, Path]:
    import xlsxwriter

    destination_without_suffix = Path(destination_without_suffix)
    destination_without_suffix.parent.mkdir(parents=True, exist_ok=True)

    application_rows = _application_rows(session)
    question_rows = _question_rows(session)
    account_rows = _account_rows()

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


def _account_rows() -> list[dict[str, Any]]:
    return []


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
