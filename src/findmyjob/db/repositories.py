from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from sqlalchemy import Select, delete, select
from sqlalchemy.orm import Session

from findmyjob.core.enums import (
    ApplicationMode,
    ArtifactKind,
    JobLifecycleStatus,
    PersonalSuppressionScope,
    PersonalTriageStatus,
    PolicyMode,
    ReviewStatus,
    RunStatus,
    TaskStatus,
)
from findmyjob.core.logging import redact_data
from findmyjob.core.types import ApplicationQuestion, GroundedAnswer, JobSearchQuery, NormalizedJobPosting, PersonalSuppressionRule, PersonalTriageDecision, ProfileFact, QualificationResult, SavedSearch, TrainingSampleSummary
from findmyjob.db.models import (
    AnswerMemoryRecord,
    ApplicationAnswerRecord,
    ApplicationQuestionRecord,
    ApplicationRecord,
    ArtifactRecord,
    AuditEventRecord,
    Company,
    JobPosting,
    JobRawRecord,
    PersonalJobTriageRecord,
    PersonalSuppressionRuleRecord,
    ProfileFactRecord,
    QualificationResultRecord,
    RunRecord,
    SavedSearchRecord,
    SubmitAttemptRecord,
    TaskRecord,
    TrainingSampleRecord,
    utcnow,
)


class JobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _prefer_incoming(current, incoming, *, unknown_values: set[Any] | None = None):
        unknown_values = unknown_values or set()
        if incoming is None:
            return current
        if isinstance(incoming, list):
            return incoming or current
        if isinstance(incoming, dict):
            return incoming or current
        if incoming in unknown_values and current not in {None, *unknown_values}:
            return current
        return incoming

    def upsert_company(
        self,
        normalized_name: str,
        display_name: str,
        domain: str | None = None,
        employee_count_min: int | None = None,
        employee_count_max: int | None = None,
        company_size_bucket: Any = None,
    ) -> Company:
        stmt = select(Company).where(Company.normalized_name == normalized_name)
        company = self.session.scalar(stmt)
        bucket_value = str(getattr(company_size_bucket, "value", company_size_bucket) or "unknown")
        if company is None:
            company = Company(
                normalized_name=normalized_name,
                display_name=display_name,
                domains=[domain] if domain else [],
                employee_count_min=employee_count_min,
                employee_count_max=employee_count_max,
                company_size_bucket=bucket_value,
            )
            self.session.add(company)
            self.session.flush()
        else:
            company.display_name = display_name
            if domain and domain not in company.domains:
                company.domains = [*company.domains, domain]
            if employee_count_min is not None:
                company.employee_count_min = employee_count_min
            if employee_count_max is not None:
                company.employee_count_max = employee_count_max
            if bucket_value != "unknown" or not company.company_size_bucket:
                company.company_size_bucket = bucket_value
        return company

    def upsert_job(self, job: NormalizedJobPosting, raw_payload: dict[str, Any] | None = None) -> JobPosting:
        company = self.upsert_company(
            job.company_key,
            job.company_name,
            employee_count_min=job.company_employee_count_min,
            employee_count_max=job.company_employee_count_max,
            company_size_bucket=job.company_size_bucket,
        )
        stmt = select(JobPosting).where(JobPosting.source_adapter == job.source, JobPosting.source_job_id == job.source_job_id)
        record = self.session.scalar(stmt)
        job_location_scope = str(getattr(job.location_scope, "value", job.location_scope) or "unknown")
        job_workplace_type = str(getattr(job.workplace_type, "value", job.workplace_type) or "unknown")
        job_experience_level = str(getattr(job.experience_level, "value", job.experience_level) or "unknown")
        job_compensation_interval = getattr(job.compensation_interval, "value", job.compensation_interval)
        if record is None:
            record = JobPosting(
                company_id=company.id,
                title=job.title,
                source_adapter=job.source,
                source_kind=job.source_kind,
                source_job_id=job.source_job_id,
                posting_url=str(job.posting_url),
                apply_url=str(job.apply_url) if job.apply_url else None,
                location_raw=job.location_raw,
                location_normalized=job.location_normalized,
                country_code=job.country_code,
                region_code=job.region_code,
                city=job.city,
                location_scope=job_location_scope,
                workplace_type=job_workplace_type,
                employment_type=job.employment_type,
                experience_level=job_experience_level,
                compensation=job.compensation,
                compensation_min=job.compensation_min,
                compensation_max=job.compensation_max,
                compensation_currency=job.compensation_currency,
                compensation_interval=job_compensation_interval,
                remote_country_codes=job.remote_country_codes or None,
                metadata_quality=job.metadata_quality,
                description=job.description,
                normalized_description=job.normalized_description,
                posted_at=job.posted_at,
                source_updated_at=job.source_updated_at,
                first_seen_at=job.discovered_at,
                last_seen_at=job.discovered_at,
                discovered_at=job.discovered_at,
                job_identity_key=job.job_identity_key,
                duplicate_cluster_key=job.duplicate_cluster_key,
                lifecycle_status=job.lifecycle_status,
                notes=job.notes,
            )
            self.session.add(record)
            self.session.flush()
        else:
            record.company_id = company.id
            record.last_seen_at = utcnow()
            record.title = job.title
            record.source_kind = job.source_kind
            record.posting_url = str(job.posting_url)
            record.apply_url = str(job.apply_url) if job.apply_url else record.apply_url
            record.location_raw = self._prefer_incoming(record.location_raw, job.location_raw)
            record.location_normalized = self._prefer_incoming(record.location_normalized, job.location_normalized)
            record.country_code = self._prefer_incoming(record.country_code, job.country_code)
            record.region_code = self._prefer_incoming(record.region_code, job.region_code)
            record.city = self._prefer_incoming(record.city, job.city)
            record.location_scope = self._prefer_incoming(record.location_scope, job_location_scope, unknown_values={"unknown"})
            record.workplace_type = self._prefer_incoming(record.workplace_type, job_workplace_type, unknown_values={"unknown"})
            record.employment_type = self._prefer_incoming(record.employment_type, job.employment_type)
            record.experience_level = self._prefer_incoming(record.experience_level, job_experience_level, unknown_values={"unknown"})
            record.compensation = self._prefer_incoming(record.compensation, job.compensation)
            record.compensation_min = self._prefer_incoming(record.compensation_min, job.compensation_min)
            record.compensation_max = self._prefer_incoming(record.compensation_max, job.compensation_max)
            record.compensation_currency = self._prefer_incoming(record.compensation_currency, job.compensation_currency)
            record.compensation_interval = self._prefer_incoming(record.compensation_interval, job_compensation_interval)
            record.remote_country_codes = self._prefer_incoming(record.remote_country_codes, job.remote_country_codes)
            record.metadata_quality = {**(record.metadata_quality or {}), **(job.metadata_quality or {})}
            record.description = job.description
            record.normalized_description = job.normalized_description
            record.posted_at = self._prefer_incoming(record.posted_at, job.posted_at)
            record.source_updated_at = self._prefer_incoming(record.source_updated_at, job.source_updated_at)
            record.job_identity_key = job.job_identity_key
            record.duplicate_cluster_key = job.duplicate_cluster_key
            record.notes = {**(record.notes or {}), **(job.notes or {})}
        if raw_payload is not None:
            self.store_raw_record(record.id, job.source, str(job.posting_url), raw_payload)
        return record

    def get_job(self, job_posting_id: str) -> JobPosting | None:
        return self.session.get(JobPosting, job_posting_id)

    def list_jobs(self, limit: int = 50, status: JobLifecycleStatus | None = None) -> Sequence[JobPosting]:
        stmt: Select[tuple[JobPosting]] = select(JobPosting).order_by(JobPosting.discovered_at.desc()).limit(limit)
        if status is not None:
            stmt = stmt.where(JobPosting.lifecycle_status == status)
        return self.session.scalars(stmt).all()

    def list_jobs_for_preparation(self) -> Sequence[JobPosting]:
        stmt = select(JobPosting).where(JobPosting.lifecycle_status.in_([JobLifecycleStatus.NORMALIZED, JobLifecycleStatus.CANDIDATE, JobLifecycleStatus.NEEDS_USER_INPUT]))
        return self.session.scalars(stmt.order_by(JobPosting.discovered_at.desc())).all()

    def store_raw_record(self, job_posting_id: str | None, source_adapter: str, source_url: str, payload: dict[str, Any]) -> JobRawRecord:
        payload_bytes = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        record = JobRawRecord(
            job_posting_id=job_posting_id,
            source_adapter=source_adapter,
            source_url=source_url,
            payload_hash=hashlib.sha256(payload_bytes).hexdigest(),
            payload=payload,
        )
        self.session.add(record)
        return record

    def save_qualification(self, job_posting_id: str, result: QualificationResult) -> QualificationResultRecord:
        stmt = select(QualificationResultRecord).where(QualificationResultRecord.job_posting_id == job_posting_id)
        record = self.session.scalar(stmt)
        if record is None:
            record = QualificationResultRecord(job_posting_id=job_posting_id, decision=result.decision)
            self.session.add(record)
        record.score = result.score
        record.decision = result.decision
        record.reasons = result.reasons
        record.sponsorship_current = result.sponsorship_current.value
        record.sponsorship_future = result.sponsorship_future.value
        record.cpt_support = result.cpt_support.value
        record.opt_support = result.opt_support.value
        record.fit = result.fit.value
        record.confidence = result.confidence
        record.evidence = [snippet.model_dump(mode="json") for snippet in result.evidence]
        job = self.session.get(JobPosting, job_posting_id)
        if job is not None:
            job.lifecycle_status = result.decision
        return record

    def duplicate_exists(self, duplicate_cluster_key: str, exclude_job_id: str | None = None) -> bool:
        stmt = select(JobPosting).where(JobPosting.duplicate_cluster_key == duplicate_cluster_key)
        candidates = self.session.scalars(stmt).all()
        for candidate in candidates:
            if exclude_job_id and candidate.id == exclude_job_id:
                continue
            if candidate.lifecycle_status in {
                JobLifecycleStatus.PREPARING,
                JobLifecycleStatus.READY_FOR_REVIEW,
                JobLifecycleStatus.APPROVED_FOR_SUBMIT,
                JobLifecycleStatus.SUBMITTING,
                JobLifecycleStatus.SUBMITTED,
                JobLifecycleStatus.SUBMISSION_UNCERTAIN,
            }:
                return True
        return False

    def duplicate_siblings(self, duplicate_cluster_key: str, exclude_job_id: str | None = None) -> list[JobPosting]:
        stmt = select(JobPosting).where(JobPosting.duplicate_cluster_key == duplicate_cluster_key).order_by(JobPosting.discovered_at.desc())
        candidates = self.session.scalars(stmt).all()
        return [candidate for candidate in candidates if not exclude_job_id or candidate.id != exclude_job_id]


class ProfileRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_fact(self, fact: ProfileFact) -> ProfileFactRecord:
        stmt = select(ProfileFactRecord).where(ProfileFactRecord.fact_id == fact.fact_id)
        record = self.session.scalar(stmt)
        if record is None:
            record = ProfileFactRecord(fact_id=fact.fact_id, kind=fact.kind)
            self.session.add(record)
        record.kind = fact.kind
        record.payload = fact.payload
        record.sensitivity = fact.sensitivity
        record.allowed_for_generation = fact.allowed_for_generation
        record.disallowed = fact.disallowed
        record.provenance = fact.provenance
        record.confirmed = fact.confirmed
        return record

    def list_facts(self) -> Sequence[ProfileFactRecord]:
        return self.session.scalars(select(ProfileFactRecord).order_by(ProfileFactRecord.kind, ProfileFactRecord.fact_id)).all()

    def delete_by_fact_id_prefix(self, prefix: str) -> int:
        pattern = f"{str(prefix).strip()}%"
        records = self.session.scalars(select(ProfileFactRecord).where(ProfileFactRecord.fact_id.like(pattern))).all()
        for record in records:
            self.session.delete(record)
        if records:
            self.session.flush()
        return len(records)


class SavedSearchRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_searches(self) -> Sequence[SavedSearchRecord]:
        stmt = select(SavedSearchRecord).order_by(SavedSearchRecord.is_default.desc(), SavedSearchRecord.name.asc())
        return self.session.scalars(stmt).all()

    def list_models(self) -> list[SavedSearch]:
        return [self.to_model(record) for record in self.list_searches()]

    def get(self, saved_search_id: str) -> SavedSearchRecord | None:
        return self.session.get(SavedSearchRecord, saved_search_id)

    def get_default(self) -> SavedSearchRecord | None:
        stmt = select(SavedSearchRecord).where(SavedSearchRecord.is_default == True).order_by(SavedSearchRecord.updated_at.desc())
        return self.session.scalar(stmt)

    def get_by_name(self, name: str) -> SavedSearchRecord | None:
        stmt = select(SavedSearchRecord).where(SavedSearchRecord.name == name)
        return self.session.scalar(stmt)

    def get_by_reference(self, reference: str) -> SavedSearchRecord | None:
        reference = str(reference).strip()
        if not reference:
            return None
        record = self.get(reference)
        if record is not None:
            return record
        return self.get_by_name(reference)

    def to_model(self, record: SavedSearchRecord) -> SavedSearch:
        return SavedSearch(
            id=record.id,
            name=record.name,
            description=record.description,
            query_payload=JobSearchQuery.model_validate(record.query_payload or {}),
            source_adapter_hint=record.source_adapter_hint,
            is_default=record.is_default,
            created_at=record.created_at,
            updated_at=record.updated_at,
            last_used_at=record.last_used_at,
        )

    def save(self, search: SavedSearch) -> SavedSearchRecord:
        record = self.get(search.id) if search.id else None
        if record is None:
            record = self.get_by_name(search.name)
        if record is None:
            record = SavedSearchRecord(name=search.name)
            self.session.add(record)
            self.session.flush()
        if search.is_default:
            self.clear_default(except_id=record.id)
        record.name = search.name
        record.description = search.description
        record.query_payload = search.query_payload.model_dump(mode="json")
        record.source_adapter_hint = search.source_adapter_hint or search.query_payload.source_adapter
        record.is_default = search.is_default
        self.session.flush()
        return record

    def rename(self, reference: str, name: str) -> SavedSearchRecord:
        record = self.require(reference)
        record.name = name
        self.session.flush()
        return record

    def delete(self, reference: str) -> SavedSearchRecord:
        record = self.require(reference)
        self.session.delete(record)
        self.session.flush()
        return record

    def mark_default(self, reference: str) -> SavedSearchRecord:
        record = self.require(reference)
        self.clear_default(except_id=record.id)
        record.is_default = True
        self.session.flush()
        return record

    def touch_last_used(self, reference: str) -> SavedSearchRecord:
        record = self.require(reference)
        record.last_used_at = utcnow()
        self.session.flush()
        return record

    def clear_default(self, except_id: str | None = None) -> None:
        for record in self.session.scalars(select(SavedSearchRecord).where(SavedSearchRecord.is_default == True)).all():
            if except_id and record.id == except_id:
                continue
            record.is_default = False

    def require(self, reference: str) -> SavedSearchRecord:
        record = self.get_by_reference(reference)
        if record is None:
            raise ValueError(f"Saved search not found: {reference}")
        return record


class PersonalTriageRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _normalize_title_key(value: str | None) -> str | None:
        cleaned = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
        return cleaned or None

    @staticmethod
    def triage_to_model(record: PersonalJobTriageRecord) -> PersonalTriageDecision:
        return PersonalTriageDecision(
            job_id=record.job_posting_id,
            status=record.status,
            reason_code=record.reason_code,
            note=record.note,
            created_at=record.created_at,
            updated_at=record.updated_at,
            last_updated_by=record.last_updated_by,
        )

    @staticmethod
    def rule_to_model(record: PersonalSuppressionRuleRecord) -> PersonalSuppressionRule:
        return PersonalSuppressionRule(
            id=record.id,
            scope=record.scope,
            company_normalized_name=record.company_normalized_name,
            company_display_name=record.company_display_name,
            title_key=record.title_key,
            title_label=record.title_label,
            job_id=record.job_posting_id,
            reason_code=record.reason_code,
            note=record.note,
            active=record.active,
            created_at=record.created_at,
            updated_at=record.updated_at,
            last_updated_by=record.last_updated_by,
        )

    def get_decision_record(self, job_posting_id: str) -> PersonalJobTriageRecord | None:
        stmt = select(PersonalJobTriageRecord).where(PersonalJobTriageRecord.job_posting_id == job_posting_id)
        return self.session.scalar(stmt)

    def get_decision(self, job_posting_id: str) -> PersonalTriageDecision | None:
        record = self.get_decision_record(job_posting_id)
        return self.triage_to_model(record) if record is not None else None

    def load_decision_records(self, job_posting_ids: Sequence[str]) -> dict[str, PersonalJobTriageRecord]:
        if not job_posting_ids:
            return {}
        stmt = select(PersonalJobTriageRecord).where(PersonalJobTriageRecord.job_posting_id.in_(list(job_posting_ids)))
        return {record.job_posting_id: record for record in self.session.scalars(stmt).all()}

    def load_decision_map(self, job_posting_ids: Sequence[str]) -> dict[str, PersonalTriageDecision]:
        return {
            job_id: self.triage_to_model(record)
            for job_id, record in self.load_decision_records(job_posting_ids).items()
        }

    def list_decision_records(
        self,
        *,
        statuses: Sequence[PersonalTriageStatus] | None = None,
        limit: int = 200,
    ) -> Sequence[PersonalJobTriageRecord]:
        stmt = select(PersonalJobTriageRecord).order_by(PersonalJobTriageRecord.updated_at.desc()).limit(limit)
        if statuses:
            stmt = stmt.where(PersonalJobTriageRecord.status.in_(list(statuses)))
        return self.session.scalars(stmt).all()

    def list_decisions(
        self,
        *,
        statuses: Sequence[PersonalTriageStatus] | None = None,
        limit: int = 200,
    ) -> list[PersonalTriageDecision]:
        return [self.triage_to_model(record) for record in self.list_decision_records(statuses=statuses, limit=limit)]

    def set_decision(
        self,
        job_posting_id: str,
        status: PersonalTriageStatus,
        *,
        reason_code: str | None = None,
        note: str | None = None,
        updated_by: str = "operator",
    ) -> PersonalTriageDecision:
        record = self.get_decision_record(job_posting_id)
        if record is None:
            record = PersonalJobTriageRecord(job_posting_id=job_posting_id, status=status)
            self.session.add(record)
            self.session.flush()
        record.status = status
        record.reason_code = reason_code
        record.note = note
        record.last_updated_by = updated_by
        self.session.flush()
        return self.triage_to_model(record)

    def list_rule_records(self, *, active_only: bool = True) -> Sequence[PersonalSuppressionRuleRecord]:
        stmt = select(PersonalSuppressionRuleRecord).order_by(PersonalSuppressionRuleRecord.created_at.desc())
        if active_only:
            stmt = stmt.where(PersonalSuppressionRuleRecord.active.is_(True))
        return self.session.scalars(stmt).all()

    def list_rules(self, *, active_only: bool = True) -> list[PersonalSuppressionRule]:
        return [self.rule_to_model(record) for record in self.list_rule_records(active_only=active_only)]

    def upsert_suppression_rule(
        self,
        job: JobPosting,
        scope: PersonalSuppressionScope,
        *,
        reason_code: str | None = None,
        note: str | None = None,
        updated_by: str = "operator",
    ) -> PersonalSuppressionRule | None:
        if scope == PersonalSuppressionScope.JOB:
            return None
        company_normalized_name = job.company.normalized_name if job.company is not None else None
        company_display_name = job.company.display_name if job.company is not None else None
        title_key = self._normalize_title_key(job.title) if scope == PersonalSuppressionScope.COMPANY_TITLE else None
        title_label = job.title if scope == PersonalSuppressionScope.COMPANY_TITLE else None
        stmt = select(PersonalSuppressionRuleRecord).where(
            PersonalSuppressionRuleRecord.active.is_(True),
            PersonalSuppressionRuleRecord.scope == scope,
            PersonalSuppressionRuleRecord.company_normalized_name == company_normalized_name,
        )
        if scope == PersonalSuppressionScope.COMPANY_TITLE:
            stmt = stmt.where(PersonalSuppressionRuleRecord.title_key == title_key)
        else:
            stmt = stmt.where(PersonalSuppressionRuleRecord.title_key.is_(None))
        record = self.session.scalar(stmt)
        if record is None:
            record = PersonalSuppressionRuleRecord(
                job_posting_id=job.id,
                scope=scope,
                company_normalized_name=company_normalized_name,
                company_display_name=company_display_name,
                title_key=title_key,
                title_label=title_label,
                created_by=updated_by,
                last_updated_by=updated_by,
            )
            self.session.add(record)
            self.session.flush()
        record.job_posting_id = job.id
        record.company_normalized_name = company_normalized_name
        record.company_display_name = company_display_name
        record.title_key = title_key
        record.title_label = title_label
        record.reason_code = reason_code
        record.note = note
        record.active = True
        record.last_updated_by = updated_by
        self.session.flush()
        return self.rule_to_model(record)

    def deactivate_matching_rules_for_job(
        self,
        job: JobPosting,
        *,
        scopes: Sequence[PersonalSuppressionScope],
        updated_by: str = "operator",
    ) -> list[PersonalSuppressionRule]:
        if not scopes:
            return []
        company_normalized_name = job.company.normalized_name if job.company is not None else None
        title_key = self._normalize_title_key(job.title)
        cleared: list[PersonalSuppressionRule] = []
        for record in self.list_rule_records(active_only=True):
            if record.scope not in scopes:
                continue
            if record.company_normalized_name != company_normalized_name:
                continue
            if record.scope == PersonalSuppressionScope.COMPANY_TITLE and record.title_key != title_key:
                continue
            record.active = False
            record.last_updated_by = updated_by
            cleared.append(self.rule_to_model(record))
        if cleared:
            self.session.flush()
        return cleared


class ApplicationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def ensure_application(self, job_posting_id: str, mode: ApplicationMode) -> ApplicationRecord:
        stmt = select(ApplicationRecord).where(ApplicationRecord.job_posting_id == job_posting_id)
        record = self.session.scalar(stmt)
        if record is None:
            record = ApplicationRecord(job_posting_id=job_posting_id, mode=mode)
            self.session.add(record)
            self.session.flush()
        else:
            record.mode = mode
        return record

    def get_application(self, application_id: str) -> ApplicationRecord | None:
        return self.session.get(ApplicationRecord, application_id)

    def get_application_for_job(self, job_posting_id: str) -> ApplicationRecord | None:
        stmt = select(ApplicationRecord).where(ApplicationRecord.job_posting_id == job_posting_id)
        return self.session.scalar(stmt)

    def list_review_queue(self) -> Sequence[ApplicationRecord]:
        stmt = select(ApplicationRecord).where(
            ApplicationRecord.status.in_([
                JobLifecycleStatus.READY_FOR_REVIEW,
                JobLifecycleStatus.NEEDS_USER_INPUT,
                JobLifecycleStatus.SUBMISSION_UNCERTAIN,
                JobLifecycleStatus.APPROVED_FOR_SUBMIT,
            ])
        )
        return self.session.scalars(stmt.order_by(ApplicationRecord.updated_at.desc())).all()

    def list_approved_for_submit(self) -> Sequence[ApplicationRecord]:
        stmt = select(ApplicationRecord).where(ApplicationRecord.status == JobLifecycleStatus.APPROVED_FOR_SUBMIT)
        return self.session.scalars(stmt.order_by(ApplicationRecord.updated_at.asc())).all()

    def set_review_status(self, application_id: str, review_status: ReviewStatus, *, reason: str | None = None) -> ApplicationRecord:
        record = self.session.get(ApplicationRecord, application_id)
        if record is None:
            raise ValueError(f"Application not found: {application_id}")
        record.review_status = review_status
        if review_status == ReviewStatus.APPROVED:
            record.status = JobLifecycleStatus.APPROVED_FOR_SUBMIT
        elif review_status == ReviewStatus.REJECTED:
            record.status = JobLifecycleStatus.FAILED_TERMINAL
        elif review_status == ReviewStatus.NEEDS_USER_INPUT:
            record.status = JobLifecycleStatus.NEEDS_USER_INPUT
        elif review_status == ReviewStatus.MANUAL_HANDOFF:
            record.status = JobLifecycleStatus.READY_FOR_REVIEW
            record.handoff_reason = reason
        return record

    def mark_prepared(self, application_id: str, flags: list[str] | None = None) -> ApplicationRecord:
        record = self.session.get(ApplicationRecord, application_id)
        if record is None:
            raise ValueError(f"Application not found: {application_id}")
        record.status = JobLifecycleStatus.READY_FOR_REVIEW
        record.review_flags = flags or []
        record.prepared_at = utcnow()
        return record

    def mark_submission_result(self, application_id: str, status: JobLifecycleStatus) -> ApplicationRecord:
        record = self.session.get(ApplicationRecord, application_id)
        if record is None:
            raise ValueError(f"Application not found: {application_id}")
        record.status = status
        if status == JobLifecycleStatus.SUBMITTED:
            record.submitted_at = utcnow()
        return record

    def clear_questions(self, application_id: str) -> None:
        question_ids = self.session.scalars(select(ApplicationQuestionRecord.id).where(ApplicationQuestionRecord.application_id == application_id)).all()
        if question_ids:
            self.session.execute(delete(ApplicationAnswerRecord).where(ApplicationAnswerRecord.question_id.in_(question_ids)))
        self.session.execute(delete(ApplicationQuestionRecord).where(ApplicationQuestionRecord.application_id == application_id))

    def store_question(self, application_id: str, question: ApplicationQuestion) -> ApplicationQuestionRecord:
        record = ApplicationQuestionRecord(
            application_id=application_id,
            source_field_name=question.source_field_name,
            prompt_text=question.prompt_text,
            normalized_key=question.normalized_key,
            question_type=question.question_type,
            widget_type=question.widget_type,
            required=question.required,
            options=question.options,
            field_config={
                "section": question.section,
                "step_id": question.step_id,
                "option_details": question.option_details,
                "file_constraints": question.file_constraints,
                "sensitive": question.sensitive,
                "input_role": question.input_role,
                "visible_to_operator": question.visible_to_operator,
                "submission_binding": question.submission_binding,
                "source_confidence": question.source_confidence,
            },
            source_snapshot_ref=question.source_snapshot_ref,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def list_questions(self, application_id: str) -> Sequence[ApplicationQuestionRecord]:
        stmt = select(ApplicationQuestionRecord).where(ApplicationQuestionRecord.application_id == application_id).order_by(ApplicationQuestionRecord.id)
        return self.session.scalars(stmt).all()

    def store_answer(self, question_id: str, answer: GroundedAnswer) -> ApplicationAnswerRecord:
        record = self.session.scalar(select(ApplicationAnswerRecord).where(ApplicationAnswerRecord.question_id == question_id))
        if record is None:
            record = ApplicationAnswerRecord(question_id=question_id)
            self.session.add(record)
        record.candidate_answer = answer.answer
        record.provenance = answer.provenance
        record.grounded_fact_ids = answer.used_fact_ids
        record.answer_source = answer.canonical_question or 'unknown'
        record.binding_payload = answer.artifact_binding.model_dump(mode='json') if answer.artifact_binding is not None else {}
        record.verification_status = answer.verification_status
        record.needs_user_input = answer.needs_user_input
        record.confidence = answer.confidence
        record.confidence_reason = answer.reason
        self.session.flush()
        return record

    def list_answers_for_application(self, application_id: str) -> list[tuple[ApplicationQuestionRecord, ApplicationAnswerRecord | None]]:
        questions = self.list_questions(application_id)
        pairs: list[tuple[ApplicationQuestionRecord, ApplicationAnswerRecord | None]] = []
        for question in questions:
            answer = self.session.scalar(select(ApplicationAnswerRecord).where(ApplicationAnswerRecord.question_id == question.id))
            pairs.append((question, answer))
        return pairs

    def store_answer_memory(
        self,
        canonical_question: str,
        answer: GroundedAnswer,
        approved: bool = False,
        context_constraints: dict[str, Any] | None = None,
    ) -> AnswerMemoryRecord:
        record = AnswerMemoryRecord(
            canonical_question=canonical_question,
            context_constraints=context_constraints or {},
            answer_text=answer.answer or "",
            grounded_fact_ids=answer.used_fact_ids,
            approved=approved,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def find_answer_memory(self, canonical_question: str, context_constraints: dict[str, Any] | None = None) -> list[AnswerMemoryRecord]:
        stmt = select(AnswerMemoryRecord).where(AnswerMemoryRecord.canonical_question == canonical_question, AnswerMemoryRecord.approved.is_(True))
        records = self.session.scalars(stmt).all()
        if not context_constraints:
            return records
        matched: list[AnswerMemoryRecord] = []
        for record in records:
            constraints = record.context_constraints or {}
            if all(constraints.get(key) == value for key, value in context_constraints.items()):
                matched.append(record)
        return matched

    def store_artifact(
        self,
        kind: ArtifactKind,
        path: str,
        content_hash: str,
        validation_results: dict[str, Any],
        job_posting_id: str | None = None,
        application_id: str | None = None,
        template_version: str | None = None,
        fact_set_hash: str | None = None,
    ) -> ArtifactRecord:
        record = ArtifactRecord(
            kind=kind,
            path=path,
            content_hash=content_hash,
            validation_results=validation_results,
            job_posting_id=job_posting_id,
            application_id=application_id,
            template_version=template_version,
            fact_set_hash=fact_set_hash,
        )
        self.session.add(record)
        return record

    def list_artifacts(self, application_id: str | None = None, job_posting_id: str | None = None) -> Sequence[ArtifactRecord]:
        stmt = select(ArtifactRecord)
        if application_id is not None:
            stmt = stmt.where(ArtifactRecord.application_id == application_id)
        if job_posting_id is not None:
            stmt = stmt.where(ArtifactRecord.job_posting_id == job_posting_id)
        return self.session.scalars(stmt.order_by(ArtifactRecord.created_at.asc())).all()

    def list_submit_attempts(self, application_id: str) -> Sequence[SubmitAttemptRecord]:
        stmt = select(SubmitAttemptRecord).where(SubmitAttemptRecord.application_id == application_id).order_by(SubmitAttemptRecord.created_at.desc())
        return self.session.scalars(stmt).all()

    def latest_submit_attempt(self, application_id: str) -> SubmitAttemptRecord | None:
        stmt = select(SubmitAttemptRecord).where(SubmitAttemptRecord.application_id == application_id).order_by(SubmitAttemptRecord.created_at.desc())
        return self.session.scalar(stmt)

    def record_submit_attempt(self, application_id: str, status: str, source_policy: PolicyMode, payload: dict[str, Any], snapshot_path: str | None = None) -> SubmitAttemptRecord:
        record = SubmitAttemptRecord(
            application_id=application_id,
            status=status,
            source_policy=source_policy,
            payload=payload,
            snapshot_path=snapshot_path,
        )
        self.session.add(record)
        return record


class TrainingSampleRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def to_model(record: TrainingSampleRecord) -> TrainingSampleSummary:
        return TrainingSampleSummary(
            sample_id=record.id,
            run_id=record.run_id,
            jobs_page_url=record.jobs_page_url,
            view_page_url=record.view_page_url,
            company_page_url=record.company_page_url,
            apply_page_url=record.apply_page_url,
            job_title=record.job_title,
            company_name=record.company_name,
            location=record.location,
            posted_text=record.posted_text,
            description_excerpt=record.description_excerpt,
            extracted_form_fields=list(record.extracted_form_fields or []),
            screenshot_paths=list(record.screenshot_paths or []),
            dom_snapshot_paths=list(record.dom_snapshot_paths or []),
            page_captures=list(record.page_captures or []),
            layout_notes=list(record.layout_notes or []),
            artifact_paths=dict(record.artifact_paths or {}),
            draft_change_summary=record.draft_change_summary,
            review_status=record.review_status,
            review_reason_code=record.review_reason_code,
            review_note=record.review_note,
            feedback_summary=record.feedback_summary,
            promoted_job_id=record.promoted_job_id,
            promoted_application_id=record.promoted_application_id,
            review_packet_path=record.review_packet_path,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    def get_record(self, sample_id: str) -> TrainingSampleRecord | None:
        return self.session.get(TrainingSampleRecord, sample_id)

    def get(self, sample_id: str) -> TrainingSampleSummary | None:
        record = self.get_record(sample_id)
        return self.to_model(record) if record is not None else None

    def save_sample(self, sample: TrainingSampleSummary) -> TrainingSampleRecord:
        record = self.get_record(sample.sample_id)
        if record is None:
            record = TrainingSampleRecord(
                id=sample.sample_id,
                run_id=sample.run_id,
                jobs_page_url=sample.jobs_page_url,
                view_page_url=sample.view_page_url,
            )
            self.session.add(record)
        record.run_id = sample.run_id
        record.jobs_page_url = sample.jobs_page_url
        record.view_page_url = sample.view_page_url
        record.company_page_url = sample.company_page_url
        record.apply_page_url = sample.apply_page_url
        record.job_title = sample.job_title
        record.company_name = sample.company_name
        record.location = sample.location
        record.posted_text = sample.posted_text
        record.description_excerpt = sample.description_excerpt
        record.extracted_form_fields = list(sample.extracted_form_fields)
        record.screenshot_paths = list(sample.screenshot_paths)
        record.dom_snapshot_paths = list(sample.dom_snapshot_paths)
        record.page_captures = [capture.model_dump(mode='json') if hasattr(capture, 'model_dump') else dict(capture) for capture in sample.page_captures]
        record.layout_notes = list(sample.layout_notes)
        record.artifact_paths = dict(sample.artifact_paths)
        record.draft_change_summary = sample.draft_change_summary
        record.review_status = sample.review_status
        record.review_reason_code = sample.review_reason_code
        record.review_note = sample.review_note
        record.feedback_summary = sample.feedback_summary
        record.promoted_job_id = sample.promoted_job_id
        record.promoted_application_id = sample.promoted_application_id
        record.review_packet_path = sample.review_packet_path
        self.session.flush()
        return record

    def list_sample_records(
        self,
        *,
        run_id: str | None = None,
        review_statuses: Sequence[str] | None = None,
        limit: int = 50,
    ) -> Sequence[TrainingSampleRecord]:
        stmt = select(TrainingSampleRecord).order_by(TrainingSampleRecord.updated_at.desc(), TrainingSampleRecord.created_at.desc()).limit(limit)
        if run_id is not None:
            stmt = stmt.where(TrainingSampleRecord.run_id == run_id)
        if review_statuses:
            stmt = stmt.where(TrainingSampleRecord.review_status.in_(list(review_statuses)))
        return self.session.scalars(stmt).all()

    def list_samples(
        self,
        *,
        run_id: str | None = None,
        review_statuses: Sequence[str] | None = None,
        limit: int = 50,
    ) -> list[TrainingSampleSummary]:
        return [self.to_model(record) for record in self.list_sample_records(run_id=run_id, review_statuses=review_statuses, limit=limit)]

    def attach_promotion(
        self,
        sample_id: str,
        *,
        job_id: str | None,
        application_id: str | None,
        review_packet_path: str | None = None,
    ) -> TrainingSampleRecord:
        record = self.get_record(sample_id)
        if record is None:
            raise ValueError(f"Training sample not found: {sample_id}")
        record.promoted_job_id = job_id
        record.promoted_application_id = application_id
        if review_packet_path:
            record.review_packet_path = review_packet_path
        self.session.flush()
        return record


class RunRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_run(self, run_type: str, mode: ApplicationMode, checkpoint_state: dict[str, Any] | None = None) -> RunRecord:
        record = RunRecord(run_type=run_type, mode=mode, status=RunStatus.RUNNING, started_at=utcnow(), checkpoint_state=checkpoint_state or {})
        self.session.add(record)
        self.session.flush()
        return record

    def get_run(self, run_id: str) -> RunRecord | None:
        return self.session.get(RunRecord, run_id)

    def list_runs(self, limit: int = 50) -> Sequence[RunRecord]:
        stmt = select(RunRecord).order_by(RunRecord.created_at.desc()).limit(limit)
        return self.session.scalars(stmt).all()

    def list_runs_by_type(self, run_type: str, limit: int = 50) -> Sequence[RunRecord]:
        stmt = select(RunRecord).where(RunRecord.run_type == run_type).order_by(RunRecord.created_at.desc()).limit(limit)
        return self.session.scalars(stmt).all()
    def complete_run(self, run_id: str, status: RunStatus = RunStatus.COMPLETED, checkpoint_state: dict[str, Any] | None = None) -> RunRecord:
        record = self.session.get(RunRecord, run_id)
        if record is None:
            raise ValueError(f"Run not found: {run_id}")
        record.status = status
        record.completed_at = utcnow()
        if checkpoint_state is not None:
            record.checkpoint_state = {**(record.checkpoint_state or {}), **checkpoint_state}
        return record

    def enqueue_task(self, run_id: str, task_type: str, payload: dict[str, Any], idempotency_key: str) -> TaskRecord:
        stmt = select(TaskRecord).where(TaskRecord.run_id == run_id, TaskRecord.idempotency_key == idempotency_key)
        existing = self.session.scalar(stmt)
        if existing is not None:
            return existing
        task = TaskRecord(run_id=run_id, task_type=task_type, payload=payload, idempotency_key=idempotency_key)
        self.session.add(task)
        self.session.flush()
        return task

    def list_tasks(self, run_id: str) -> Sequence[TaskRecord]:
        stmt = select(TaskRecord).where(TaskRecord.run_id == run_id).order_by(TaskRecord.created_at.asc())
        return self.session.scalars(stmt).all()

    def claim_task(self, worker_name: str, run_id: str | None = None, lease_seconds: int = 120) -> TaskRecord | None:
        now = utcnow()
        stmt = select(TaskRecord).where(TaskRecord.status.in_([TaskStatus.QUEUED, TaskStatus.FAILED_RETRYABLE])).order_by(TaskRecord.created_at)
        if run_id is not None:
            stmt = stmt.where(TaskRecord.run_id == run_id)
        tasks = self.session.scalars(stmt).all()
        task = next((item for item in tasks if item.status == TaskStatus.QUEUED or item.attempt_count < item.max_attempts), None)
        if task is None:
            return None
        task.status = TaskStatus.RUNNING
        task.attempt_count += 1
        task.lease_owner = worker_name
        task.lease_expires_at = now + timedelta(seconds=lease_seconds)
        task.heartbeat_at = now
        return task

    def heartbeat(self, task_id: str, lease_seconds: int = 120) -> TaskRecord:
        task = self.session.get(TaskRecord, task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        task.heartbeat_at = utcnow()
        task.lease_expires_at = utcnow() + timedelta(seconds=lease_seconds)
        return task

    def finish_task(self, task_id: str, status: TaskStatus, checkpoint_state: dict[str, Any] | None = None, error: str | None = None) -> TaskRecord:
        task = self.session.get(TaskRecord, task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        if status == TaskStatus.FAILED_RETRYABLE and task.attempt_count >= task.max_attempts:
            status = TaskStatus.FAILED_TERMINAL
        task.status = status
        task.lease_owner = None
        task.lease_expires_at = None
        task.heartbeat_at = utcnow()
        if checkpoint_state is not None:
            task.checkpoint_state = checkpoint_state
        task.last_error = error
        return task

    def reap_stale_leases(self) -> int:
        now = utcnow()
        stmt = select(TaskRecord).where(TaskRecord.status == TaskStatus.RUNNING, TaskRecord.lease_expires_at < now)
        tasks = self.session.scalars(stmt).all()
        for task in tasks:
            task.status = TaskStatus.FAILED_RETRYABLE if task.attempt_count < task.max_attempts else TaskStatus.FAILED_TERMINAL
            task.lease_owner = None
            task.lease_expires_at = None
            task.last_error = task.last_error or "Lease expired"
        return len(tasks)


class AuditRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_events(
        self,
        *,
        event_type: str | None = None,
        entity_type: str | None = None,
        limit: int = 100,
    ) -> Sequence[AuditEventRecord]:
        stmt = select(AuditEventRecord).order_by(AuditEventRecord.created_at.desc()).limit(limit)
        if event_type is not None:
            stmt = stmt.where(AuditEventRecord.event_type == event_type)
        if entity_type is not None:
            stmt = stmt.where(AuditEventRecord.entity_type == entity_type)
        return self.session.scalars(stmt).all()
    def emit(self, event_type: str, entity_type: str, entity_id: str | None = None, run_id: str | None = None, task_id: str | None = None, payload: dict[str, Any] | None = None) -> AuditEventRecord:
        record = AuditEventRecord(
            run_id=run_id,
            task_id=task_id,
            entity_type=entity_type,
            entity_id=entity_id,
            event_type=event_type,
            payload=redact_data(payload or {}),
        )
        self.session.add(record)
        return record


def hash_content(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()




















