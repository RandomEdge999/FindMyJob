from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from findmyjob.core.enums import (
    ApplicationMode,
    ArtifactKind,
    FactKind,
    JobLifecycleStatus,
    PolicyMode,
    PersonalSuppressionScope,
    PersonalTriageStatus,
    QuestionType,
    ReviewStatus,
    RunStatus,
    Sensitivity,
    TaskStatus,
    VerificationStatus,
)
from findmyjob.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid4().hex


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (Index("ix_company_size_bucket", "company_size_bucket"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    domains: Mapped[list[str]] = mapped_column(JSON, default=list)
    ats_hosts: Mapped[list[str]] = mapped_column(JSON, default=list)
    employee_count_min: Mapped[int | None] = mapped_column(Integer)
    employee_count_max: Mapped[int | None] = mapped_column(Integer)
    company_size_bucket: Mapped[str] = mapped_column(String(32), default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class BoardRegistryRecord(Base):
    __tablename__ = "board_registry"
    __table_args__ = (
        UniqueConstraint("source_adapter", "board_token", name="uq_board_registry_source_token"),
        Index("ix_board_registry_active", "source_adapter", "active"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    board_token: Mapped[str] = mapped_column(String(255), nullable=False)
    company_hint: Mapped[str | None] = mapped_column(String(255))
    source_url: Mapped[str | None] = mapped_column(Text)
    board_url: Mapped[str | None] = mapped_column(Text)
    source_domain: Mapped[str | None] = mapped_column(String(255))
    discovery_method: Mapped[str] = mapped_column(String(64), default="manual")
    validation_status: Mapped[str] = mapped_column(String(32), default="unknown")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync_status: Mapped[str | None] = mapped_column(String(32))
    last_error: Mapped[str | None] = mapped_column(Text)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    live_job_count: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class BoardDiscoveryEvidenceRecord(Base):
    __tablename__ = "board_discovery_evidence"
    __table_args__ = (Index("ix_board_discovery_board_id", "board_registry_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    board_registry_id: Mapped[str] = mapped_column(ForeignKey("board_registry.id", ondelete="CASCADE"), nullable=False)
    source_adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    discovery_method: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SavedSearchRecord(Base):
    __tablename__ = "saved_searches"
    __table_args__ = (
        UniqueConstraint("name", name="uq_saved_search_name"),
        Index("ix_saved_search_default", "is_default"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    query_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_adapter_hint: Mapped[str | None] = mapped_column(String(64))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobPosting(Base):
    __tablename__ = "job_postings"
    __table_args__ = (
        UniqueConstraint("source_adapter", "source_job_id", name="uq_job_source_id"),
        Index("ix_job_identity_key", "job_identity_key"),
        Index("ix_duplicate_cluster_key", "duplicate_cluster_key"),
        Index("ix_job_board_token", "source_adapter", "board_token"),
        Index("ix_job_country_region_city", "country_code", "region_code", "city"),
        Index("ix_job_location_scope", "location_scope"),
        Index("ix_job_experience_level", "experience_level"),
        Index("ix_job_posted_at", "posted_at"),
        Index("ix_job_compensation_floor", "compensation_currency", "compensation_min"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    source_adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    source_job_id: Mapped[str] = mapped_column(String(255), nullable=False)
    board_token: Mapped[str | None] = mapped_column(String(255))
    posting_url: Mapped[str] = mapped_column(Text, nullable=False)
    apply_url: Mapped[str | None] = mapped_column(Text)
    location_raw: Mapped[str | None] = mapped_column(String(255))
    location_normalized: Mapped[str | None] = mapped_column(String(255))
    country_code: Mapped[str | None] = mapped_column(String(8))
    region_code: Mapped[str | None] = mapped_column(String(16))
    city: Mapped[str | None] = mapped_column(String(255))
    location_scope: Mapped[str] = mapped_column(String(32), default="unknown")
    workplace_type: Mapped[str] = mapped_column(String(32), default="unknown")
    employment_type: Mapped[str | None] = mapped_column(String(64))
    experience_level: Mapped[str] = mapped_column(String(32), default="unknown")
    compensation: Mapped[dict[str, Any] | list[dict[str, Any]] | None] = mapped_column(JSON)
    compensation_min: Mapped[int | None] = mapped_column(Integer)
    compensation_max: Mapped[int | None] = mapped_column(Integer)
    compensation_currency: Mapped[str | None] = mapped_column(String(16))
    compensation_interval: Mapped[str | None] = mapped_column(String(32))
    remote_country_codes: Mapped[list[str] | None] = mapped_column(JSON)
    metadata_quality: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_description: Mapped[str] = mapped_column(Text, nullable=False)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    job_identity_key: Mapped[str] = mapped_column(String(64), nullable=False)
    duplicate_cluster_key: Mapped[str] = mapped_column(String(64), nullable=False)
    lifecycle_status: Mapped[JobLifecycleStatus] = mapped_column(Enum(JobLifecycleStatus), default=JobLifecycleStatus.NORMALIZED)
    notes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    company: Mapped[Company] = relationship()


class JobRawRecord(Base):
    __tablename__ = "job_raw_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_posting_id: Mapped[str | None] = mapped_column(ForeignKey("job_postings.id", ondelete="SET NULL"))
    source_adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    html_snapshot: Mapped[str | None] = mapped_column(Text)


class QualificationResultRecord(Base):
    __tablename__ = "qualification_results"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_posting_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id", ondelete="CASCADE"), unique=True)
    score: Mapped[int] = mapped_column(Integer, default=0)
    decision: Mapped[JobLifecycleStatus] = mapped_column(Enum(JobLifecycleStatus), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(64), default="v1")
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    sponsorship_current: Mapped[str] = mapped_column(String(32), default="unknown")
    sponsorship_future: Mapped[str] = mapped_column(String(32), default="unknown")
    cpt_support: Mapped[str] = mapped_column(String(32), default="unknown")
    opt_support: Mapped[str] = mapped_column(String(32), default="unknown")
    fit: Mapped[str] = mapped_column(String(32), default="review_required")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PersonalJobTriageRecord(Base):
    __tablename__ = "personal_job_triage"
    __table_args__ = (
        UniqueConstraint("job_posting_id", name="uq_personal_job_triage_job"),
        Index("ix_personal_job_triage_status", "status"),
        Index("ix_personal_job_triage_updated_at", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_posting_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[PersonalTriageStatus] = mapped_column(Enum(PersonalTriageStatus), default=PersonalTriageStatus.NEW)
    reason_code: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    last_updated_by: Mapped[str] = mapped_column(String(64), default="operator")


class PersonalSuppressionRuleRecord(Base):
    __tablename__ = "personal_suppression_rules"
    __table_args__ = (
        Index("ix_personal_suppression_rules_active", "active"),
        Index("ix_personal_suppression_rules_scope_company_title", "scope", "company_normalized_name", "title_key"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_posting_id: Mapped[str | None] = mapped_column(ForeignKey("job_postings.id", ondelete="SET NULL"))
    scope: Mapped[PersonalSuppressionScope] = mapped_column(Enum(PersonalSuppressionScope), nullable=False)
    company_normalized_name: Mapped[str | None] = mapped_column(String(255))
    company_display_name: Mapped[str | None] = mapped_column(String(255))
    title_key: Mapped[str | None] = mapped_column(String(255))
    title_label: Mapped[str | None] = mapped_column(String(255))
    reason_code: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    created_by: Mapped[str] = mapped_column(String(64), default="operator")
    last_updated_by: Mapped[str] = mapped_column(String(64), default="operator")


class ProfileFactRecord(Base):
    __tablename__ = "profile_facts"
    __table_args__ = (UniqueConstraint("fact_id", name="uq_profile_fact_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    fact_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[FactKind] = mapped_column(Enum(FactKind), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    sensitivity: Mapped[Sensitivity] = mapped_column(Enum(Sensitivity), default=Sensitivity.MEDIUM)
    allowed_for_generation: Mapped[bool] = mapped_column(Boolean, default=True)
    disallowed: Mapped[bool] = mapped_column(Boolean, default=False)
    provenance: Mapped[str] = mapped_column(String(64), default="user")
    confirmed: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TemplateRecord(Base):
    __tablename__ = "templates"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_template_version"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    renderer: Mapped[str] = mapped_column(String(64), default="typst")
    validation_settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ArtifactRecord(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_posting_id: Mapped[str | None] = mapped_column(ForeignKey("job_postings.id", ondelete="SET NULL"))
    application_id: Mapped[str | None] = mapped_column(ForeignKey("applications.id", ondelete="SET NULL"))
    kind: Mapped[ArtifactKind] = mapped_column(Enum(ArtifactKind), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    template_version: Mapped[str | None] = mapped_column(String(64))
    fact_set_hash: Mapped[str | None] = mapped_column(String(64))
    validation_results: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApplicationRecord(Base):
    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("job_posting_id", name="uq_application_job"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_posting_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False)
    mode: Mapped[ApplicationMode] = mapped_column(Enum(ApplicationMode), default=ApplicationMode.DRY_RUN)
    status: Mapped[JobLifecycleStatus] = mapped_column(Enum(JobLifecycleStatus), default=JobLifecycleStatus.CANDIDATE)
    review_status: Mapped[ReviewStatus] = mapped_column(Enum(ReviewStatus), default=ReviewStatus.PENDING)
    review_flags: Mapped[list[str]] = mapped_column(JSON, default=list)
    handoff_reason: Mapped[str | None] = mapped_column(Text)
    prepared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ApplicationQuestionRecord(Base):
    __tablename__ = "application_questions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), nullable=False)
    source_field_name: Mapped[str | None] = mapped_column(String(255))
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_key: Mapped[str | None] = mapped_column(String(255))
    question_type: Mapped[QuestionType] = mapped_column(Enum(QuestionType), default=QuestionType.UNKNOWN)
    widget_type: Mapped[str] = mapped_column(String(64), default="text")
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    options: Mapped[list[str]] = mapped_column(JSON, default=list)
    field_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_snapshot_ref: Mapped[str | None] = mapped_column(Text)


class ApplicationAnswerRecord(Base):
    __tablename__ = "application_answers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    question_id: Mapped[str] = mapped_column(ForeignKey("application_questions.id", ondelete="CASCADE"), nullable=False)
    candidate_answer: Mapped[str | None] = mapped_column(Text)
    provenance: Mapped[str] = mapped_column(String(64), default="generated")
    grounded_fact_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    answer_source: Mapped[str] = mapped_column(String(64), default="unknown")
    binding_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    verification_status: Mapped[VerificationStatus] = mapped_column(Enum(VerificationStatus), default=VerificationStatus.NEEDS_USER_INPUT)
    needs_user_input: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    confidence_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AnswerMemoryRecord(Base):
    __tablename__ = "answer_memory"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    canonical_question: Mapped[str] = mapped_column(Text, nullable=False)
    context_constraints: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    grounded_fact_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TrainingSampleRecord(Base):
    __tablename__ = "training_samples"
    __table_args__ = (
        Index("ix_training_samples_run_id", "run_id"),
        Index("ix_training_samples_review_status", "review_status", "updated_at"),
        Index("ix_training_samples_promoted_application", "promoted_application_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    jobs_page_url: Mapped[str] = mapped_column(Text, nullable=False)
    view_page_url: Mapped[str] = mapped_column(Text, nullable=False)
    company_page_url: Mapped[str | None] = mapped_column(Text)
    apply_page_url: Mapped[str | None] = mapped_column(Text)
    job_title: Mapped[str | None] = mapped_column(String(255))
    company_name: Mapped[str | None] = mapped_column(String(255))
    location: Mapped[str | None] = mapped_column(String(255))
    posted_text: Mapped[str | None] = mapped_column(String(255))
    description_excerpt: Mapped[str | None] = mapped_column(Text)
    extracted_form_fields: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    screenshot_paths: Mapped[list[str]] = mapped_column(JSON, default=list)
    dom_snapshot_paths: Mapped[list[str]] = mapped_column(JSON, default=list)
    page_captures: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    layout_notes: Mapped[list[str]] = mapped_column(JSON, default=list)
    artifact_paths: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    draft_change_summary: Mapped[str | None] = mapped_column(Text)
    review_status: Mapped[str] = mapped_column(String(32), default="pending")
    review_reason_code: Mapped[str | None] = mapped_column(String(64))
    review_note: Mapped[str | None] = mapped_column(Text)
    feedback_summary: Mapped[str | None] = mapped_column(Text)
    promoted_job_id: Mapped[str | None] = mapped_column(ForeignKey("job_postings.id", ondelete="SET NULL"))
    promoted_application_id: Mapped[str | None] = mapped_column(ForeignKey("applications.id", ondelete="SET NULL"))
    review_packet_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RunRecord(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus), default=RunStatus.PENDING)
    mode: Mapped[ApplicationMode] = mapped_column(Enum(ApplicationMode), default=ApplicationMode.DRY_RUN)
    checkpoint_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    resume_token: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaskRecord(Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("ix_task_status", "status"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.QUEUED)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkpoint_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SourceCursorRecord(Base):
    __tablename__ = "source_cursors"
    __table_args__ = (UniqueConstraint("source_adapter", "cursor_key", name="uq_source_cursor"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    cursor_key: Mapped[str] = mapped_column(String(255), nullable=False)
    cursor_value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SubmitAttemptRecord(Base):
    __tablename__ = "submit_attempts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    source_policy: Mapped[PolicyMode] = mapped_column(Enum(PolicyMode), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    snapshot_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEventRecord(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)




