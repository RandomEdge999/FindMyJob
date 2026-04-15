
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from findmyjob.core.enums import JobLifecycleStatus
from findmyjob.core.types import BoardDiscoveryEvidence, BoardRegistry, BoardSyncState
from findmyjob.db.models import BoardDiscoveryEvidenceRecord, BoardRegistryRecord, JobPosting, SourceCursorRecord, utcnow


class BoardRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_board(self, board: BoardRegistry) -> BoardRegistryRecord:
        stmt = select(BoardRegistryRecord).where(
            BoardRegistryRecord.source_adapter == board.source_adapter,
            BoardRegistryRecord.board_token == board.board_token,
        )
        record = self.session.scalar(stmt)
        if record is None:
            record = BoardRegistryRecord(
                source_adapter=board.source_adapter,
                board_token=board.board_token,
                first_seen_at=board.first_seen_at or utcnow(),
            )
            self.session.add(record)
            self.session.flush()
        record.company_hint = board.company_hint or record.company_hint
        record.source_url = board.source_url or record.source_url
        record.board_url = board.board_url or record.board_url
        record.source_domain = board.source_domain or record.source_domain
        record.discovery_method = board.discovery_method or record.discovery_method
        record.validation_status = board.validation_status or record.validation_status
        record.active = board.active
        record.last_sync_status = board.last_sync_status or record.last_sync_status
        record.last_error = board.last_error
        record.failure_count = board.failure_count
        record.live_job_count = board.live_job_count
        record.notes = {**(record.notes or {}), **board.notes}
        record.last_seen_at = board.last_seen_at or utcnow()
        if board.last_validated_at is not None:
            record.last_validated_at = board.last_validated_at
        if board.last_sync_at is not None:
            record.last_sync_at = board.last_sync_at
        return record

    def get_board(self, source_adapter: str, board_token: str) -> BoardRegistryRecord | None:
        stmt = select(BoardRegistryRecord).where(
            BoardRegistryRecord.source_adapter == source_adapter,
            BoardRegistryRecord.board_token == board_token,
        )
        return self.session.scalar(stmt)

    def list_boards(
        self,
        source_adapter: str | None = None,
        active_only: bool = False,
        limit: int = 200,
    ) -> Sequence[BoardRegistryRecord]:
        stmt = select(BoardRegistryRecord).order_by(BoardRegistryRecord.last_seen_at.desc()).limit(limit)
        if source_adapter:
            stmt = stmt.where(BoardRegistryRecord.source_adapter == source_adapter)
        if active_only:
            stmt = stmt.where(BoardRegistryRecord.active.is_(True))
        return self.session.scalars(stmt).all()

    def list_active_boards(self, source_adapter: str, limit: int | None = None) -> Sequence[BoardRegistryRecord]:
        stmt = (
            select(BoardRegistryRecord)
            .where(BoardRegistryRecord.source_adapter == source_adapter, BoardRegistryRecord.active.is_(True))
            .order_by(BoardRegistryRecord.last_sync_at.asc().nullsfirst(), BoardRegistryRecord.board_token.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return self.session.scalars(stmt).all()

    def record_evidence(self, evidence: BoardDiscoveryEvidence) -> BoardDiscoveryEvidenceRecord:
        board = self.get_board(evidence.source_adapter, evidence.board_token)
        if board is None:
            raise ValueError(f"Board not found: {evidence.source_adapter}:{evidence.board_token}")
        record = BoardDiscoveryEvidenceRecord(
            board_registry_id=board.id,
            source_adapter=evidence.source_adapter,
            source_url=evidence.source_url,
            discovery_method=evidence.discovery_method,
            payload=evidence.payload,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def mark_validation(
        self,
        source_adapter: str,
        board_token: str,
        *,
        validation_status: str,
        company_hint: str | None = None,
        board_url: str | None = None,
        source_url: str | None = None,
        source_domain: str | None = None,
        error: str | None = None,
        active: bool | None = None,
        notes: dict[str, Any] | None = None,
    ) -> BoardRegistryRecord:
        record = self.get_board(source_adapter, board_token)
        if record is None:
            record = self.upsert_board(
                BoardRegistry(
                    source_adapter=source_adapter,
                    board_token=board_token,
                    company_hint=company_hint,
                    board_url=board_url,
                    source_url=source_url,
                    source_domain=source_domain,
                    validation_status=validation_status,
                    active=validation_status == "valid" if active is None else active,
                    notes=notes or {},
                )
            )
        record.validation_status = validation_status
        record.company_hint = company_hint or record.company_hint
        record.board_url = board_url or record.board_url
        record.source_url = source_url or record.source_url
        record.source_domain = source_domain or record.source_domain
        record.last_validated_at = utcnow()
        record.last_seen_at = utcnow()
        record.last_error = error
        record.notes = {**(record.notes or {}), **(notes or {})}
        if validation_status == "valid":
            record.failure_count = 0
            if active is None:
                record.active = True
        else:
            record.failure_count += 1
            if active is not None:
                record.active = active
            elif record.failure_count >= 3:
                record.active = False
        return record
    def mark_sync_result(self, source_adapter: str, board_token: str, *, status: str, live_job_count: int, error: str | None = None) -> BoardRegistryRecord:
        record = self.get_board(source_adapter, board_token)
        if record is None:
            raise ValueError(f"Board not found: {source_adapter}:{board_token}")
        record.last_sync_at = utcnow()
        record.last_sync_status = status
        record.live_job_count = live_job_count
        record.last_error = error
        if status == "success":
            record.failure_count = 0
            record.active = True
        else:
            record.failure_count += 1
            if record.failure_count >= 3:
                record.active = False
                record.validation_status = "inactive"
        return record

    def count_live_jobs_for_board(self, source_adapter: str, board_token: str) -> int:
        stmt = select(JobPosting).where(
            JobPosting.source_adapter == source_adapter,
            JobPosting.board_token == board_token,
            JobPosting.lifecycle_status != JobLifecycleStatus.INACTIVE,
        )
        return len(self.session.scalars(stmt).all())

    def mark_missing_jobs_inactive(self, source_adapter: str, board_token: str, seen_source_job_ids: set[str], threshold: int) -> list[str]:
        stmt = select(JobPosting).where(JobPosting.source_adapter == source_adapter, JobPosting.board_token == board_token)
        jobs = self.session.scalars(stmt).all()
        inactivated: list[str] = []
        for job in jobs:
            notes = dict(job.notes or {})
            if job.source_job_id in seen_source_job_ids:
                if notes.get("missing_run_count"):
                    notes["missing_run_count"] = 0
                    notes["board_active"] = True
                    job.notes = notes
                continue
            missing_run_count = int(notes.get("missing_run_count") or 0) + 1
            notes["missing_run_count"] = missing_run_count
            notes["board_active"] = False
            if missing_run_count >= threshold and job.lifecycle_status in {
                JobLifecycleStatus.DISCOVERED,
                JobLifecycleStatus.NORMALIZED,
                JobLifecycleStatus.CANDIDATE,
                JobLifecycleStatus.SCREENED_OUT,
                JobLifecycleStatus.DUPLICATE_BLOCKED,
                JobLifecycleStatus.INACTIVE,
            }:
                job.lifecycle_status = JobLifecycleStatus.INACTIVE
                inactivated.append(job.id)
            job.notes = notes
        return inactivated


class SourceStateRepository:
    def __init__(self, session: Session) -> None:
        self.session = session
    def get_board_sync_state(self, source_adapter: str, board_token: str) -> BoardSyncState:
        cursor_key = self._cursor_key(board_token)
        stmt = select(SourceCursorRecord).where(
            SourceCursorRecord.source_adapter == source_adapter,
            SourceCursorRecord.cursor_key == cursor_key,
        )
        record = self.session.scalar(stmt)
        if record is None:
            return BoardSyncState(board_token=board_token, source_adapter=source_adapter)
        return BoardSyncState.model_validate({**record.cursor_value, "board_token": board_token, "source_adapter": source_adapter})

    def save_board_sync_state(self, state: BoardSyncState) -> SourceCursorRecord:
        cursor_key = self._cursor_key(state.board_token)
        stmt = select(SourceCursorRecord).where(
            SourceCursorRecord.source_adapter == state.source_adapter,
            SourceCursorRecord.cursor_key == cursor_key,
        )
        record = self.session.scalar(stmt)
        payload = state.model_dump(mode="json")
        if record is None:
            record = SourceCursorRecord(source_adapter=state.source_adapter, cursor_key=cursor_key, cursor_value=payload)
            self.session.add(record)
            self.session.flush()
        else:
            record.cursor_value = payload
        return record

    def _cursor_key(self, board_token: str) -> str:
        return f"board:{board_token}"
