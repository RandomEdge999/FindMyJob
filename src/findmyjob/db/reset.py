from __future__ import annotations

from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from findmyjob.db.models import ApplicationAnswerRecord, ApplicationQuestionRecord, ApplicationRecord, ArtifactRecord, AuditEventRecord, BoardDiscoveryEvidenceRecord, BoardRegistryRecord, Company, JobPosting, JobRawRecord, PersonalJobTriageRecord, QualificationResultRecord, RunRecord, SourceCursorRecord, SubmitAttemptRecord, TaskRecord, TrainingSampleRecord

_OPERATIONAL_RESET_ORDER = [
    BoardDiscoveryEvidenceRecord,
    TrainingSampleRecord,
    AuditEventRecord,
    SubmitAttemptRecord,
    TaskRecord,
    RunRecord,
    ApplicationAnswerRecord,
    ApplicationQuestionRecord,
    ArtifactRecord,
    ApplicationRecord,
    PersonalJobTriageRecord,
    QualificationResultRecord,
    JobRawRecord,
    JobPosting,
    BoardRegistryRecord,
    SourceCursorRecord,
    Company,
]


def reset_operational_data(runtime_or_session: Session | Any) -> dict[str, int]:
    if hasattr(runtime_or_session, 'session_scope') and not isinstance(runtime_or_session, Session):
        with runtime_or_session.session_scope() as session:
            return _reset_operational_data(session)
    return _reset_operational_data(runtime_or_session)


def _reset_operational_data(session: Session) -> dict[str, int]:
    counts: dict[str, int] = {}
    for model in _OPERATIONAL_RESET_ORDER:
        result = session.execute(delete(model))
        counts[model.__tablename__] = int(result.rowcount or 0)
    session.flush()
    return counts
