from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, HttpUrl

from findmyjob.core.enums import (
    ApplicationMode,
    ArtifactKind,
    CaptureMode,
    CompanySizeBucket,
    CompensationInterval,
    ExperienceLevel,
    FactKind,
    JobLifecycleStatus,
    LocationScope,
    ModelRole,
    PolicyMode,
    PersonalSuppressionScope,
    PersonalTriageStatus,
    QuestionType,
    Sensitivity,
    SourceRisk,
    SponsorshipFit,
    SponsorshipSignal,
    VerificationStatus,
    WorkplaceType,
)


class EvidenceSnippet(BaseModel):
    source: str
    quote: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class NormalizedJobPosting(BaseModel):
    company_name: str
    company_key: str
    title: str
    source: str
    source_kind: str
    source_job_id: str
    posting_url: HttpUrl | str
    apply_url: HttpUrl | str | None = None
    location_raw: str | None = None
    location_normalized: str | None = None
    country_code: str | None = None
    region_code: str | None = None
    city: str | None = None
    location_scope: LocationScope = LocationScope.UNKNOWN
    workplace_type: WorkplaceType = WorkplaceType.UNKNOWN
    employment_type: str | None = None
    experience_level: ExperienceLevel = ExperienceLevel.UNKNOWN
    posted_at: datetime | None = None
    source_updated_at: datetime | None = None
    compensation: dict[str, Any] | list[dict[str, Any]] | None = None
    compensation_min: int | None = None
    compensation_max: int | None = None
    compensation_currency: str | None = None
    compensation_interval: CompensationInterval | None = None
    remote_country_codes: list[str] = Field(default_factory=list)
    company_employee_count_min: int | None = None
    company_employee_count_max: int | None = None
    company_size_bucket: CompanySizeBucket = CompanySizeBucket.UNKNOWN
    metadata_quality: dict[str, Any] = Field(default_factory=dict)
    description: str
    normalized_description: str
    discovered_at: datetime
    job_identity_key: str
    duplicate_cluster_key: str
    lifecycle_status: JobLifecycleStatus = JobLifecycleStatus.NORMALIZED
    notes: dict[str, Any] = Field(default_factory=dict)


class QualificationResult(BaseModel):
    score: int
    decision: JobLifecycleStatus
    reasons: list[str] = Field(default_factory=list)
    sponsorship_current: SponsorshipSignal = SponsorshipSignal.UNKNOWN
    sponsorship_future: SponsorshipSignal = SponsorshipSignal.UNKNOWN
    cpt_support: SponsorshipSignal = SponsorshipSignal.UNKNOWN
    opt_support: SponsorshipSignal = SponsorshipSignal.UNKNOWN
    fit: SponsorshipFit = SponsorshipFit.REVIEW_REQUIRED
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: list[EvidenceSnippet] = Field(default_factory=list)


class ProfileFact(BaseModel):
    fact_id: str
    kind: FactKind
    payload: dict[str, Any]
    sensitivity: Sensitivity = Sensitivity.MEDIUM
    allowed_for_generation: bool = True
    disallowed: bool = False
    provenance: str = "user"
    confirmed: bool = True


class ClaimEvidence(BaseModel):
    text: str
    fact_id: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class ApplicationQuestion(BaseModel):
    source_field_name: str | None = None
    prompt_text: str
    normalized_key: str | None = None
    question_type: QuestionType = QuestionType.UNKNOWN
    widget_type: str = "text"
    section: str | None = None
    step_id: str | None = None
    required: bool = False
    input_role: str = "data"
    visible_to_operator: bool = True
    options: list[str] = Field(default_factory=list)
    option_details: list[dict[str, Any]] = Field(default_factory=list)
    sensitive: bool = False
    file_constraints: dict[str, Any] = Field(default_factory=dict)
    submission_binding: dict[str, Any] = Field(default_factory=dict)
    source_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source_snapshot_ref: str | None = None


class ArtifactBinding(BaseModel):
    artifact_kind: ArtifactKind
    path: str
    mime_type: str | None = None
    source_artifact_kind: ArtifactKind | None = None


class GroundedAnswer(BaseModel):
    question: str
    question_type: QuestionType = QuestionType.UNKNOWN
    answer: str | None = None
    selected_option_values: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str | None = None
    used_fact_ids: list[str] = Field(default_factory=list)
    unsupported_segments: list[str] = Field(default_factory=list)
    claim_evidence: list[ClaimEvidence] = Field(default_factory=list)
    provenance: str = "generated"
    canonical_question: str | None = None
    artifact_binding: ArtifactBinding | None = None
    verification_status: VerificationStatus = VerificationStatus.NEEDS_USER_INPUT

    @property
    def needs_user_input(self) -> bool:
        return self.verification_status == VerificationStatus.NEEDS_USER_INPUT

    @property
    def candidate_answer(self) -> str | None:
        return self.answer


class ModelProfile(BaseModel):
    name: str
    role: ModelRole
    provider: str
    model: str
    local: bool = False
    transport: str | None = None
    temperature: float = 0.0
    max_tokens: int | None = None
    supports_structured_output: bool = False
    fallback_chain: list[str] = Field(default_factory=list)
    policy_tags: list[str] = Field(default_factory=list)
    base_url: str | None = None
    api_key_env: str | None = None
    command: list[str] = Field(default_factory=list)
    working_dir: str | None = None


class SourceCapabilities(BaseModel):
    adapter_name: str
    source_kind: str
    policy_mode: PolicyMode
    risk: SourceRisk
    supports_discovery: bool = True
    supports_apply: bool = False
    supports_auto_submit: bool = False
    supported_filters: list[str] = Field(default_factory=list)
    supported_question_types: list[QuestionType] = Field(default_factory=list)


class FormFieldBinding(BaseModel):
    source_field_name: str
    widget_type: str
    prompt_text: str
    required: bool = False
    value: str | None = None
    values: list[str] = Field(default_factory=list)
    option_value: str | None = None
    option_values: list[str] = Field(default_factory=list)
    artifact_binding: ArtifactBinding | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SubmissionPlan(BaseModel):
    source_kind: str
    application_url: str
    fields: list[FormFieldBinding] = Field(default_factory=list)
    missing_required_fields: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class SubmissionEvidence(BaseModel):
    pre_submit_snapshot_path: str | None = None
    final_snapshot_path: str | None = None
    trace_path: str | None = None
    dom_snapshot_path: str | None = None
    post_submit_dom_snapshot_path: str | None = None
    confirmation_text: str | None = None
    confirmation_strategy: str | None = None
    field_audit: list[dict[str, Any]] = Field(default_factory=list)
    failure_reason: str | None = None
    network_errors: list[str] = Field(default_factory=list)
    final_url: str | None = None
    visible_validation_errors: list[str] = Field(default_factory=list)
    matched_confirmation_markers: list[str] = Field(default_factory=list)
    missing_required_controls: list[str] = Field(default_factory=list)
    submit_button_present: bool | None = None
    submit_button_enabled: bool | None = None
    browser_left_open: bool = False


class ReviewPacket(BaseModel):
    job_id: str
    application_id: str
    qualification: QualificationResult
    source_policy: PolicyMode
    artifacts: list[str] = Field(default_factory=list)
    artifact_validation_failures: list[str] = Field(default_factory=list)
    questions: list[ApplicationQuestion] = Field(default_factory=list)
    answers: list[GroundedAnswer] = Field(default_factory=list)
    sensitive_questions: list[str] = Field(default_factory=list)
    duplicate_cluster_siblings: list[dict[str, Any]] = Field(default_factory=list)
    submit_ready: bool = False
    handoff_url: str | None = None


class SubmissionGateReport(BaseModel):
    application_mode: ApplicationMode
    duplicate_risk: bool = False
    missing_required_fields: list[str] = Field(default_factory=list)
    missing_artifacts: list[ArtifactKind] = Field(default_factory=list)
    ungrounded_answers: list[str] = Field(default_factory=list)
    low_confidence_answers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    source_policy: PolicyMode = PolicyMode.REVIEW_ONLY
    source_flow_valid: bool = False

    @property
    def is_ready(self) -> bool:
        return (
            not self.duplicate_risk
            and not self.missing_required_fields
            and not self.missing_artifacts
            and not self.ungrounded_answers
            and not self.low_confidence_answers
            and self.source_flow_valid
        )


class SubmissionResult(BaseModel):
    status: JobLifecycleStatus
    submitted: bool = False
    uncertain: bool = False
    external_id: str | None = None
    message: str | None = None
    snapshot_path: str | None = None
    trace_path: str | None = None
    plan: SubmissionPlan | None = None
    evidence: SubmissionEvidence | None = None


class BoardRegistry(BaseModel):
    source_adapter: str
    board_token: str
    company_hint: str | None = None
    source_url: str | None = None
    board_url: str | None = None
    source_domain: str | None = None
    discovery_method: str = "manual"
    validation_status: str = "unknown"
    active: bool = True
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    last_validated_at: datetime | None = None
    last_sync_at: datetime | None = None
    last_sync_status: str | None = None
    last_error: str | None = None
    failure_count: int = 0
    live_job_count: int = 0
    notes: dict[str, Any] = Field(default_factory=dict)


class BoardDiscoveryEvidence(BaseModel):
    board_token: str
    source_adapter: str
    source_url: str
    discovery_method: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class BoardSyncState(BaseModel):
    board_token: str
    source_adapter: str
    payload_hash: str | None = None
    job_count: int = 0
    last_validation_result: str = "unknown"
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    failure_count: int = 0
    backoff_until: datetime | None = None
    consecutive_missing_runs: int = 0


class BoardSyncResult(BaseModel):
    board_token: str
    source_adapter: str
    job_count: int
    new_jobs: int = 0
    changed_jobs: int = 0
    enriched_jobs: int = 0
    inactive_jobs: int = 0
    validation_status: str = "valid"
    notes: list[str] = Field(default_factory=list)


class JobSearchQuery(BaseModel):
    title_keywords: list[str] = Field(default_factory=list)
    keyword: str | None = None
    source_adapter: str | None = None
    board_token: str | None = None
    active_only: bool = False
    locations: list[str] = Field(default_factory=list)
    workplace_types: list[WorkplaceType] = Field(default_factory=list)
    employment_types: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    location_scopes: list[LocationScope] = Field(default_factory=list)
    experience_levels: list[ExperienceLevel] = Field(default_factory=list)
    posted_within_days: int | None = Field(default=None, ge=1, le=3650)
    compensation_present: bool | None = None
    compensation_min: int | None = Field(default=None, ge=0)
    compensation_currency: str | None = None
    company_size_buckets: list[CompanySizeBucket] = Field(default_factory=list)
    remote_only: bool = False
    allow_unknown_compensation: bool = False
    allow_unknown_experience_level: bool = False
    sponsorship_fit: str | None = None
    requires_future_sponsorship: bool = False
    limit: int = Field(default=50, ge=1, le=500)

    @classmethod
    def from_search_settings(cls, settings: Any) -> "JobSearchQuery":
        return cls(
            title_keywords=list(getattr(settings, "title_keywords", []) or []),
            locations=list(getattr(settings, "locations", []) or []),
            countries=list(getattr(settings, "countries", []) or []),
            regions=list(getattr(settings, "regions", []) or []),
            cities=list(getattr(settings, "cities", []) or []),
            workplace_types=list(getattr(settings, "workplace_types", []) or []),
            employment_types=list(getattr(settings, "employment_types", []) or []),
            location_scopes=list(getattr(settings, "location_scopes", []) or []),
            experience_levels=list(getattr(settings, "experience_levels", []) or []),
            posted_within_days=getattr(settings, "posted_within_days", None),
            compensation_min=getattr(settings, "compensation_min", None),
            compensation_currency=getattr(settings, "compensation_currency", None),
            company_size_buckets=list(getattr(settings, "company_size_buckets", []) or []),
            remote_only=bool(getattr(settings, "remote_only", False)),
            allow_unknown_compensation=bool(getattr(settings, "allow_unknown_compensation", False)),
            allow_unknown_experience_level=bool(getattr(settings, "allow_unknown_experience_level", False)),
            requires_future_sponsorship=bool(getattr(settings, "requires_future_sponsorship", False)),
        )

    def to_discovery_query(self):
        from findmyjob.sources.contracts import DiscoveryQuery

        return DiscoveryQuery(
            title_keywords=list(self.title_keywords),
            locations=list(self.locations),
            countries=list(self.countries),
            regions=list(self.regions),
            cities=list(self.cities),
            workplace_types=list(self.workplace_types),
            employment_types=list(self.employment_types),
            location_scopes=list(self.location_scopes),
            experience_levels=list(self.experience_levels),
            company_size_buckets=list(self.company_size_buckets),
            posted_within_days=self.posted_within_days,
            compensation_min=self.compensation_min,
            compensation_currency=self.compensation_currency,
            requires_future_sponsorship=self.requires_future_sponsorship,
            remote_only=self.remote_only,
            allow_unknown_compensation=self.allow_unknown_compensation,
            allow_unknown_experience_level=self.allow_unknown_experience_level,
        )

    def summary_parts(self) -> list[str]:
        parts: list[str] = []
        if self.keyword:
            parts.append(f"fts:{self.keyword}")
        if self.title_keywords:
            parts.append("title:" + ", ".join(self.title_keywords[:3]))
        if self.source_adapter:
            parts.append(f"source:{self.source_adapter}")
        if self.board_token:
            parts.append(f"board:{self.board_token}")
        if self.locations:
            parts.append("loc:" + ", ".join(self.locations[:2]))
        if self.countries:
            parts.append("countries:" + ", ".join(self.countries[:3]))
        if self.regions:
            parts.append("regions:" + ", ".join(self.regions[:3]))
        if self.cities:
            parts.append("cities:" + ", ".join(self.cities[:3]))
        if self.workplace_types:
            parts.append("workplace:" + ", ".join(value.value for value in self.workplace_types[:3]))
        if self.location_scopes:
            parts.append("scope:" + ", ".join(value.value for value in self.location_scopes[:3]))
        if self.experience_levels:
            parts.append("exp:" + ", ".join(value.value for value in self.experience_levels[:3]))
        if self.company_size_buckets:
            parts.append("size:" + ", ".join(value.value for value in self.company_size_buckets[:3]))
        if self.posted_within_days is not None:
            parts.append(f"posted<={self.posted_within_days}d")
        if self.remote_only:
            parts.append("remote-only")
        if self.compensation_present is True:
            parts.append("comp:present")
        elif self.compensation_present is False:
            parts.append("comp:missing")
        if self.compensation_min is not None:
            label = f"comp>={self.compensation_min}"
            if self.compensation_currency:
                label = f"{label} {self.compensation_currency.upper()}"
            parts.append(label)
        elif self.compensation_currency:
            parts.append(f"currency:{self.compensation_currency.upper()}")
        if self.allow_unknown_compensation:
            parts.append("allow-unknown-comp")
        if self.allow_unknown_experience_level:
            parts.append("allow-unknown-exp")
        if self.sponsorship_fit:
            parts.append(f"sponsorship:{self.sponsorship_fit}")
        if self.requires_future_sponsorship:
            parts.append("needs-sponsorship")
        if self.active_only:
            parts.append("active-only")
        parts.append(f"limit:{self.limit}")
        return parts

    def summary(self) -> str:
        parts = self.summary_parts()
        return " | ".join(parts) if parts else "No active filters"


class SavedSearch(BaseModel):
    id: str | None = None
    name: str
    description: str | None = None
    query_payload: JobSearchQuery = Field(default_factory=JobSearchQuery)
    source_adapter_hint: str | None = None
    is_default: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_used_at: datetime | None = None

    @property
    def query(self) -> JobSearchQuery:
        return self.query_payload

class PersonalTriageDecision(BaseModel):
    job_id: str
    status: PersonalTriageStatus = PersonalTriageStatus.NEW
    reason_code: str | None = None
    note: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_updated_by: str | None = None


class PersonalSuppressionRule(BaseModel):
    id: str | None = None
    scope: PersonalSuppressionScope
    company_normalized_name: str | None = None
    company_display_name: str | None = None
    title_key: str | None = None
    title_label: str | None = None
    job_id: str | None = None
    reason_code: str | None = None
    note: str | None = None
    active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_updated_by: str | None = None


class PersonalJobMatchExplanation(BaseModel):
    job_id: str
    score: int = 0
    priority_label: str = "normal"
    triage_status: PersonalTriageStatus = PersonalTriageStatus.NEW
    matched_query_names: list[str] = Field(default_factory=list)
    query_summaries: dict[str, str] = Field(default_factory=dict)
    match_reasons: list[str] = Field(default_factory=list)
    ranking_reasons: list[str] = Field(default_factory=list)
    penalties: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    breakdown: dict[str, int] = Field(default_factory=dict)
    suppressed: bool = False
    suppression_reasons: list[str] = Field(default_factory=list)
    headline: str | None = None
    ai_greenlight: bool | None = None
    ai_score: int | None = None
    ai_reasons: list[str] = Field(default_factory=list)
    ai_warnings: list[str] = Field(default_factory=list)
    ai_skip_reason: str | None = None


class ResumeDraft(BaseModel):
    headline: str | None = None
    summary_lines: list[str] = Field(default_factory=list)
    selected_work_fact_ids: list[str] = Field(default_factory=list)
    selected_project_fact_ids: list[str] = Field(default_factory=list)
    selected_skill_fact_ids: list[str] = Field(default_factory=list)
    custom_bullets: list[str] = Field(default_factory=list)


class CoverLetterDraft(BaseModel):
    salutation: str | None = None
    paragraphs: list[str] = Field(default_factory=list)
    closing: str | None = None
    signature_name: str | None = None


class ArtifactDraft(BaseModel):
    resume_draft: ResumeDraft = Field(default_factory=ResumeDraft)
    cover_letter_draft: CoverLetterDraft = Field(default_factory=CoverLetterDraft)
    writer_profile: str | None = None
    verifier_profile: str | None = None
    verified: bool = False
    verifier_issues: list[str] = Field(default_factory=list)
    validation_profile: str | None = None
    validation_issues: list[str] = Field(default_factory=list)
    repair_writer_profile: str | None = None
    repair_attempted: bool = False
    adaptation_summary: str | None = None
    feedback_context: list[str] = Field(default_factory=list)


class AutonomousDecisionReport(BaseModel):
    job_id: str
    hard_gate_passed: bool = True
    hard_gate_reasons: list[str] = Field(default_factory=list)
    green_light: bool = False
    score: int = 0
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    skip_reason: str | None = None
    classifier_profile: str | None = None


class AutonomousRunSummary(BaseModel):
    run_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    sync_run_id: str | None = None
    preset_names: list[str] = Field(default_factory=list)
    candidate_job_ids: list[str] = Field(default_factory=list)
    skipped_job_ids: list[str] = Field(default_factory=list)
    skipped_reasons_by_job_id: dict[str, str] = Field(default_factory=dict)
    queued_application_ids: list[str] = Field(default_factory=list)
    prepared_application_ids: list[str] = Field(default_factory=list)
    submitted_application_ids: list[str] = Field(default_factory=list)
    uncertain_application_ids: list[str] = Field(default_factory=list)
    failed_application_ids: list[str] = Field(default_factory=list)
    matched_presets_by_job_id: dict[str, list[str]] = Field(default_factory=dict)
    decision_by_job_id: dict[str, AutonomousDecisionReport] = Field(default_factory=dict)
    daily_submit_count: int = 0
    per_company_submit_counts: dict[str, int] = Field(default_factory=dict)
    queue_depth: int = 0
    notes: list[str] = Field(default_factory=list)


class QueuedQuestionSummary(BaseModel):
    application_id: str
    question_id: str
    job_id: str
    company: str
    title: str
    prompt_text: str
    normalized_key: str | None = None
    canonical_question: str
    question_type: str
    widget_type: str
    required: bool = False
    source_adapter: str | None = None
    option_signature: list[str] = Field(default_factory=list)
    option_details: list[dict[str, Any]] = Field(default_factory=list)
    input_role: str = "data"
    visible_to_operator: bool = True
    existing_answer: str | None = None
    has_approved_memory: bool = False


class SubmissionCapturePolicy(BaseModel):
    traces: CaptureMode = CaptureMode.FAILURES_ONLY
    dom_snapshots: CaptureMode = CaptureMode.FAILURES_ONLY
    screenshots: CaptureMode = CaptureMode.FAILURES_ONLY

    @staticmethod
    def should_persist(mode: CaptureMode, *, submitted: bool) -> bool:
        if mode == CaptureMode.OFF:
            return False
        if mode == CaptureMode.ALL:
            return True
        return not submitted


class ValidationFinding(BaseModel):
    key: str
    status: str
    summary: str
    detail: str | None = None
    hint: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ValidationReport(BaseModel):
    context: str
    workspace: str | None = None
    loaded_files: list[str] = Field(default_factory=list)
    findings: list[ValidationFinding] = Field(default_factory=list)

    def add(
        self,
        status: str,
        key: str,
        summary: str,
        *,
        detail: str | None = None,
        hint: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.findings.append(
            ValidationFinding(
                status=status,
                key=key,
                summary=summary,
                detail=detail,
                hint=hint,
                data=data or {},
            )
        )

    @property
    def blocked_count(self) -> int:
        return sum(1 for finding in self.findings if finding.status == "blocked")

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.status == "warning")

    @property
    def ok_count(self) -> int:
        return sum(1 for finding in self.findings if finding.status == "ok")

    @property
    def overall_status(self) -> str:
        if self.blocked_count:
            return "blocked"
        if self.warning_count:
            return "warnings"
        return "ready"


class AutopilotRunStatus(BaseModel):
    run_id: str | None = None
    stage: str = "idle"
    completed: bool = True
    blocked_applications: int = 0
    unresolved_prompts: int = 0
    latest_error: str | None = None
    browser_mode: str = "attached"
    latest_run: dict[str, Any] | None = None


class CleanupEntry(BaseModel):
    path: str
    action: str
    reason: str
    artifact_id: str | None = None
    application_id: str | None = None


class CleanupReport(BaseModel):
    dry_run: bool = True
    workspace: str
    retention_days: int
    preserve_active_applications: bool = True
    findings: list[CleanupEntry] = Field(default_factory=list)

    def add(
        self,
        *,
        path: str,
        action: str,
        reason: str,
        artifact_id: str | None = None,
        application_id: str | None = None,
    ) -> None:
        self.findings.append(
            CleanupEntry(
                path=path,
                action=action,
                reason=reason,
                artifact_id=artifact_id,
                application_id=application_id,
            )
        )

    @property
    def delete_count(self) -> int:
        return sum(1 for finding in self.findings if finding.action in {"delete", "deleted"})

    @property
    def skip_count(self) -> int:
        return sum(1 for finding in self.findings if finding.action.startswith("skip"))


class ModelLaunchRoleStatus(BaseModel):
    role: str
    profile_name: str | None = None
    transport: str | None = None
    provider: str | None = None
    model: str | None = None
    fallback_chain: list[str] = Field(default_factory=list)
    fallback_ready: list[str] = Field(default_factory=list)
    status: str = "pass"
    issues: list[str] = Field(default_factory=list)


class ModelLaunchProfileReport(BaseModel):
    required_roles: list[str] = Field(default_factory=list)
    optional_roles: list[str] = Field(default_factory=list)
    roles: list[ModelLaunchRoleStatus] = Field(default_factory=list)
    missing_required_roles: list[str] = Field(default_factory=list)
    transport_mix: str = "unbound"
    risks: list[str] = Field(default_factory=list)
    summary: str | None = None

    @property
    def fail_count(self) -> int:
        return sum(1 for role in self.roles if role.status == "fail")

    @property
    def warning_count(self) -> int:
        return sum(1 for role in self.roles if role.status == "warning")

    @property
    def overall_status(self) -> str:
        if self.fail_count:
            return "fail"
        if self.warning_count or self.missing_required_roles or self.risks:
            return "warning"
        return "pass"


class LaunchCheckFinding(BaseModel):
    key: str
    status: str
    summary: str
    detail: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class LaunchCheckReport(BaseModel):
    workspace: str
    checked_at: datetime | None = None
    findings: list[LaunchCheckFinding] = Field(default_factory=list)

    def add(
        self,
        status: str,
        key: str,
        summary: str,
        *,
        detail: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.findings.append(
            LaunchCheckFinding(
                key=key,
                status=status,
                summary=summary,
                detail=detail,
                data=data or {},
            )
        )

    @property
    def fail_count(self) -> int:
        return sum(1 for finding in self.findings if finding.status == "fail")

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.status == "warning")

    @property
    def pass_count(self) -> int:
        return sum(1 for finding in self.findings if finding.status == "pass")

    @property
    def overall_status(self) -> str:
        if self.fail_count:
            return "fail"
        if self.warning_count:
            return "pass_with_warnings"
        return "pass"


class SmokeTestResult(BaseModel):
    board_token: str
    source_job_id: str
    apply_url: str | None = None
    submit_confirmed: bool = False
    status: str = "fail"
    failure_reason: str | None = None
    job_posting_id: str | None = None
    application_id: str | None = None
    checked_at: datetime | None = None
    references: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.status == "pass"


class GreenhouseBenchmarkSummary(BaseModel):
    run_id: str
    status: str
    board_tokens: list[str] = Field(default_factory=list)
    boards_attempted: int = 0
    boards_succeeded: int = 0
    jobs_seen: int = 0
    jobs_enriched: int = 0
    inactive_jobs: int = 0
    request_count: int = 0
    rate_limited_count: int = 0
    failure_count: int = 0
    duration_seconds: float = 0.0
    jobs_per_minute: float = 0.0
    started_at: datetime | None = None
    completed_at: datetime | None = None

class ReleaseSnapshotReport(BaseModel):
    generated_at: datetime
    workspace: str
    workspace_name: str
    config_path: str
    launch_check: LaunchCheckReport
    config_validation: ValidationReport
    doctor: ValidationReport
    launch_profile: ModelLaunchProfileReport | None = None
    latest_smoke_result: SmokeTestResult | None = None
    smoke_results: list[SmokeTestResult] = Field(default_factory=list)
    latest_benchmark: GreenhouseBenchmarkSummary | None = None
    benchmark_summaries: list[GreenhouseBenchmarkSummary] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class SupportBundleArtifactReference(BaseModel):
    kind: str
    path: str
    exists: bool | None = None
    size_bytes: int | None = None


class SupportBundleApplicationSummary(BaseModel):
    application_id: str
    job_id: str | None = None
    company: str | None = None
    job_title: str | None = None
    submission_status: str | None = None
    review_status: str | None = None
    failure_reason: str | None = None
    confirmation_strategy: str | None = None
    final_url: str | None = None
    validation_errors: list[str] = Field(default_factory=list)
    matched_confirmation_markers: list[str] = Field(default_factory=list)
    missing_required_controls: list[str] = Field(default_factory=list)
    submit_button_present: bool | None = None
    submit_button_enabled: bool | None = None
    field_audit_summary: list[dict[str, Any]] = Field(default_factory=list)
    attempt_recorded_at: str | None = None
    artifact_paths: dict[str, str | None] = Field(default_factory=dict)
    sensitive_artifacts: list[SupportBundleArtifactReference] = Field(default_factory=list)


class PersonalArtifactPreviewSummary(BaseModel):
    name: str
    status: str
    renderer: str | None = None
    job_id: str | None = None
    company: str | None = None
    title: str | None = None
    synthetic_job: bool = False
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class SupportBundleReport(BaseModel):
    generated_at: datetime
    workspace: str
    workspace_name: str
    version: dict[str, Any]
    workspace_metadata: dict[str, Any] = Field(default_factory=dict)
    redaction: dict[str, Any] = Field(default_factory=dict)
    privacy: dict[str, Any] = Field(default_factory=dict)
    current_snapshot: ReleaseSnapshotReport
    onboarding: dict[str, Any] | None = None
    personal_preferences: dict[str, Any] = Field(default_factory=dict)
    inbox_summary: dict[str, Any] = Field(default_factory=dict)
    latest_daily_run: dict[str, Any] | None = None
    daily_dry_run: dict[str, Any] | None = None
    model_readiness: dict[str, Any] = Field(default_factory=dict)
    application_inspections: list[SupportBundleApplicationSummary] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PersonalRehearsalReport(BaseModel):
    generated_at: datetime
    workspace: str
    report: ValidationReport
    launch_snapshot: ReleaseSnapshotReport
    onboarding: dict[str, Any] = Field(default_factory=dict)
    personal_preferences: dict[str, Any] = Field(default_factory=dict)
    inbox_summary: dict[str, Any] = Field(default_factory=dict)
    latest_daily_run: dict[str, Any] | None = None
    daily_dry_run: dict[str, Any] | None = None
    resume_preview: PersonalArtifactPreviewSummary | None = None
    cover_letter_preview: PersonalArtifactPreviewSummary | None = None
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Greenhouse training mode types
# ---------------------------------------------------------------------------


class GreenhouseTrainingJobSummary(BaseModel):
    """Summary of a single job sampled during training mode."""
    job_url: str
    job_title: str | None = None
    company_name: str | None = None
    location: str | None = None
    posted_text: str | None = None
    company_page_url: str | None = None
    apply_url: str | None = None
    description_snippet: str | None = None
    form_fields: list[dict[str, Any]] = Field(default_factory=list)
    screenshot_paths: list[str] = Field(default_factory=list)
    dom_snapshot_path: str | None = None
    page_captures: list["TrainingPageCapture"] = Field(default_factory=list)
    layout_notes: list[str] = Field(default_factory=list)
    draft_change_summary: str | None = None
    linked_job_id: str | None = None
    linked_application_id: str | None = None
    review_packet_path: str | None = None


class TrainingPageCapture(BaseModel):
    """Captured page structure during training navigation."""
    stage: str | None = None
    url: str
    page_title: str | None = None
    screenshot_path: str | None = None
    dom_snapshot_path: str | None = None
    extracted_fields: list[dict[str, Any]] = Field(default_factory=list)
    job_description_text: str | None = None
    layout_notes: list[str] = Field(default_factory=list)


class TrainingReviewOutcome(BaseModel):
    """Per-job human approval or rejection during training."""
    job_url: str
    job_title: str | None = None
    company_name: str | None = None
    approved: bool = False
    rejection_note: str | None = None
    rejection_reason_code: str | None = None
    resume_artifact_path: str | None = None
    cover_letter_artifact_path: str | None = None
    reviewed_at: datetime | None = None
    feedback_summary: str | None = None
    linked_job_id: str | None = None
    linked_application_id: str | None = None
    review_packet_path: str | None = None


class TrainingRunSummary(BaseModel):
    """Summary of a full training run."""
    run_id: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    start_url: str = "https://my.greenhouse.io/jobs"
    posted_window: int = 10
    batch_size: int = 5
    cdp_url: str = "http://127.0.0.1:9222"
    sampled_jobs: list[GreenhouseTrainingJobSummary] = Field(default_factory=list)
    reviews: list[TrainingReviewOutcome] = Field(default_factory=list)
    approved_count: int = 0
    rejected_count: int = 0
    artifact_paths: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    promoted_application_ids: list[str] = Field(default_factory=list)
    review_packet_paths: list[str] = Field(default_factory=list)


class TrainingSampleSummary(BaseModel):
    """Durable record for a single training sample and its review outcome."""
    sample_id: str
    run_id: str
    jobs_page_url: str = "https://my.greenhouse.io/jobs"
    view_page_url: str
    company_page_url: str | None = None
    apply_page_url: str | None = None
    job_title: str | None = None
    company_name: str | None = None
    location: str | None = None
    posted_text: str | None = None
    description_excerpt: str | None = None
    extracted_form_fields: list[dict[str, Any]] = Field(default_factory=list)
    screenshot_paths: list[str] = Field(default_factory=list)
    dom_snapshot_paths: list[str] = Field(default_factory=list)
    page_captures: list[TrainingPageCapture] = Field(default_factory=list)
    layout_notes: list[str] = Field(default_factory=list)
    artifact_paths: dict[str, str | None] = Field(default_factory=dict)
    draft_change_summary: str | None = None
    review_status: str = "pending"
    review_reason_code: str | None = None
    review_note: str | None = None
    feedback_summary: str | None = None
    promoted_job_id: str | None = None
    promoted_application_id: str | None = None
    review_packet_path: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def approved(self) -> bool:
        return self.review_status == "approved"


class TrainingHistorySummary(BaseModel):
    """Compact listing response for training history."""
    run_id: str | None = None
    items: list[TrainingSampleSummary] = Field(default_factory=list)
    total_count: int = 0


class TrainingPromotionResultSummary(BaseModel):
    """Result for promoting one or more approved samples into review/apply state."""
    sample_id: str
    run_id: str | None = None
    review_status: str = "pending"
    promoted: bool = False
    job_id: str | None = None
    application_id: str | None = None
    review_packet_path: str | None = None
    notes: list[str] = Field(default_factory=list)



