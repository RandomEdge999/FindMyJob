from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from findmyjob.core.lmstudio import LMSTUDIO_AUTO_MODEL, LMSTUDIO_DEFAULT_HOST, LMSTUDIO_PROVIDER
from findmyjob.filefirst.portal_defaults import default_portal_sources_payload

_PORTAL_DISCOVERY_SOURCES = ("greenhouse", "lever", "ashby")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class LocalModelSettings(BaseModel):
    provider: str = LMSTUDIO_PROVIDER
    transport: str = "local_http"
    base_url: str | None = LMSTUDIO_DEFAULT_HOST
    api_key_env: str | None = None
    model: str = LMSTUDIO_AUTO_MODEL
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=8192, ge=256, le=32768)
    preferred_context_window: int = Field(default=131072, ge=8192, le=262144)
    local: bool = True
    command: list[str] = Field(default_factory=list)
    working_dir: str | None = None

    @model_validator(mode="after")
    def _normalize_lmstudio_defaults(self) -> "LocalModelSettings":
        if str(self.provider or "").strip().lower() == LMSTUDIO_PROVIDER:
            self.transport = "local_http"
            self.local = True
            self.base_url = str(self.base_url or "").strip() or LMSTUDIO_DEFAULT_HOST
            self.api_key_env = str(self.api_key_env or "").strip() or None
        return self


class AutomationSettings(BaseModel):
    enabled: bool = True
    submit_enabled: bool = False
    default_submit_mode: Literal["auto_submit", "preview_first"] = "preview_first"
    production_sources: list[str] = Field(default_factory=lambda: ["greenhouse"])
    ready_to_apply_threshold: int = Field(default=10, ge=1, le=5000)
    browser_mode: str = "headed"
    browser_attach_enabled: bool = False
    browser_cdp_url: str | None = "http://127.0.0.1:9222"
    max_open_tabs: int = Field(default=6, ge=1, le=50)
    daily_submit_cap: int = Field(default=100, ge=1, le=5000)
    per_company_daily_cap: int = Field(default=2, ge=1, le=1000)
    capture_traces: bool = False
    capture_dom: bool = False


class RuntimeSettings(BaseModel):
    model: LocalModelSettings = Field(default_factory=LocalModelSettings)
    automation: AutomationSettings = Field(default_factory=AutomationSettings)


class CandidateIdentity(BaseModel):
    name: str = ""
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    linkedin: str | None = None
    github: str | None = None
    website: str | None = None
    summary: str | None = None
    target_roles: list[str] = Field(default_factory=list)


class TargetSettings(BaseModel):
    title_keywords: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=lambda: ["US"])
    regions: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    remote_only: bool = True
    employment_types: list[str] = Field(default_factory=list)
    excluded_keywords: list[str] = Field(default_factory=list)
    blocked_companies: list[str] = Field(default_factory=list)
    posted_within_days: int | None = 30


class WorkspaceProfile(BaseModel):
    candidate: CandidateIdentity = Field(default_factory=CandidateIdentity)
    targets: TargetSettings = Field(default_factory=TargetSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)

    @property
    def display_name(self) -> str:
        value = self.candidate.name.strip()
        return value or "Candidate"


class FileFact(BaseModel):
    fact_id: str
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    sensitivity: str = "medium"
    allowed_for_generation: bool = True
    disallowed: bool = False
    provenance: str = "user"
    confirmed: bool = True


class AnswerMemoryEntry(BaseModel):
    canonical_question: str
    context_constraints: dict[str, Any] = Field(default_factory=dict)
    answer_text: str
    grounded_fact_ids: list[str] = Field(default_factory=list)
    approved: bool = True
    created_at: str = Field(default_factory=utcnow_iso)


class SourceBoardConfig(BaseModel):
    enabled: bool = True
    boards: list[str] = Field(default_factory=list)
    seed_urls: list[str] = Field(default_factory=list)
    seed_domains: list[str] = Field(default_factory=list)


class TrackedCompany(BaseModel):
    name: str
    careers_url: str | None = None
    source: str | None = None
    board: str | None = None
    api: str | None = None
    enabled: bool = True
    notes: str | None = None


class PortalsConfig(BaseModel):
    sources: dict[str, SourceBoardConfig] = Field(
        default_factory=lambda: {
            source_name: SourceBoardConfig.model_validate(payload)
            for source_name, payload in default_portal_sources_payload().items()
        }
    )
    tracked_companies: list[TrackedCompany] = Field(default_factory=list)


class SourceDiscoveryMetrics(BaseModel):
    boards_scanned: int = 0
    boards_discovered: int = 0
    jobs_discovered: int = 0
    eligible_jobs: int = 0
    rejected_jobs: int = 0
    errors: int = 0
    zero_result: bool = False
    zero_result_reason: str | None = None
    warning: str | None = None
    last_run_at: str | None = None


class SourceDiscoveryState(BaseModel):
    boards: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    metrics: SourceDiscoveryMetrics = Field(default_factory=SourceDiscoveryMetrics)


class BoardDiscoveryState(BaseModel):
    sources: dict[str, SourceDiscoveryState] = Field(
        default_factory=lambda: {
            source_name: SourceDiscoveryState()
            for source_name in _PORTAL_DISCOVERY_SOURCES
        }
    )


class ScreeningDecision(BaseModel):
    approved: bool
    reasons: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    internship_like: bool = False
    seniority_too_high: bool = False
    years_experience_signal: str | None = None
    notes: str | None = None
    overridden: bool = False
    screened_at: str = Field(default_factory=utcnow_iso)

    @property
    def status(self) -> str:
        if self.overridden:
            return "overridden"
        return "approved" if self.approved else "rejected"


class InboxJob(BaseModel):
    job_id: str
    company: str
    company_key: str | None = None
    title: str
    source: str
    source_kind: str
    source_job_id: str
    url: str
    apply_url: str | None = None
    location: str | None = None
    posted_at: str | None = None
    discovered_at: str = Field(default_factory=utcnow_iso)
    description: str = ""
    workflow_state: str = "pending"
    ats_family: str = "unknown"
    ats_preview_supported: bool = False
    hard_reject_reason: str | None = None
    auth_reject_reason: str | None = None
    login_wall_detected: bool = False
    rehearsal_eligible: bool = False
    rehearsal_rank: float = 0.0
    discovery_method: str | None = None
    board_family: str = "unknown"
    automation_tier: str = "unsupported_high_friction"
    job_identity_key: str
    duplicate_cluster_key: str
    screening: ScreeningDecision | None = None
    notes: dict[str, Any] = Field(default_factory=dict)


class ApplicationEntry(BaseModel):
    id: str
    job_id: str
    date: str
    company: str
    role: str
    score: float = 0.0
    grade: str = "F"
    status: str = "Evaluated"
    pdf: bool = False
    report: str
    url: str
    source: str = ""
    notes: str | None = None


class EvaluationResult(BaseModel):
    job_id: str
    company: str
    role: str
    source: str
    url: str
    evaluated_at: str = Field(default_factory=utcnow_iso)
    archetype: str = "Generalist AI Engineer"
    score: float = 0.0
    grade: str = "F"
    summary: str = ""
    keywords: list[str] = Field(default_factory=list)
    fit_reasons: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    report_markdown: str = ""
    resume_headline: str | None = None
    resume_summary_lines: list[str] = Field(default_factory=list)
    selected_work_fact_ids: list[str] = Field(default_factory=list)
    selected_project_fact_ids: list[str] = Field(default_factory=list)
    selected_skill_fact_ids: list[str] = Field(default_factory=list)
    custom_bullets: list[str] = Field(default_factory=list)
    cover_letter_paragraphs: list[str] = Field(default_factory=list)


class ResumePlan(BaseModel):
    headline: str | None = None
    summary_lines: list[str] = Field(default_factory=list)
    selected_work_fact_ids: list[str] = Field(default_factory=list)
    selected_project_fact_ids: list[str] = Field(default_factory=list)
    selected_skill_fact_ids: list[str] = Field(default_factory=list)
    custom_bullets: list[str] = Field(default_factory=list)
    cover_letter_paragraphs: list[str] = Field(default_factory=list)


class SubmissionQuestion(BaseModel):
    question_id: str
    source_field_name: str | None = None
    prompt_text: str
    normalized_key: str | None = None
    question_type: str = "unknown"
    widget_type: str = "text"
    section: str | None = None
    required: bool = False
    sensitive: bool = False
    options: list[str] = Field(default_factory=list)
    option_details: list[dict[str, Any]] = Field(default_factory=list)
    submission_binding: dict[str, Any] = Field(default_factory=dict)
    existing_answer: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    confidence_reason: str | None = None
    needs_user_input: bool = False
    verification_status: str = "needs_user_input"


class SubmissionRecord(BaseModel):
    application_id: str
    job_id: str
    company: str
    role: str
    source: str
    apply_url: str | None = None
    status: str = "pending_contract"
    submit_ready: bool = False
    questions: list[SubmissionQuestion] = Field(default_factory=list)
    missing_required_fields: list[str] = Field(default_factory=list)
    ungrounded_answers: list[str] = Field(default_factory=list)
    low_confidence_answers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
    manual_answers: dict[str, str] = Field(default_factory=dict)
    plan: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    preview_ready: bool = False
    event_status: str | None = None
    last_error: str | None = None
    run_id: str | None = None
    reviewed: bool = False
    created_at: str = Field(default_factory=utcnow_iso)
    updated_at: str = Field(default_factory=utcnow_iso)
    submitted_at: str | None = None
    previewed_at: str | None = None


class RunRecord(BaseModel):
    run_id: str
    run_type: str = "autonomous"
    status: str = "completed"
    event_status: str | None = None
    started_at: str = Field(default_factory=utcnow_iso)
    completed_at: str | None = None
    processed_job_ids: list[str] = Field(default_factory=list)
    evaluated_application_ids: list[str] = Field(default_factory=list)
    submitted_application_ids: list[str] = Field(default_factory=list)
    failed_application_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)


class LiveRunEvent(BaseModel):
    event_id: str
    run_id: str
    run_type: str = "autonomous"
    event_type: str
    phase: str | None = None
    status: str = "info"
    stage: str | None = None
    message: str
    created_at: str = Field(default_factory=utcnow_iso)
    job_id: str | None = None
    application_id: str | None = None
    submission_id: str | None = None
    company: str | None = None
    role: str | None = None
    source: str | None = None
    model_role: str | None = None
    model_profile: str | None = None
    model_call_id: str | None = None
    step: str | None = None
    artifact_paths: list[str] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    trace_ref: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class LiveRunState(BaseModel):
    run_id: str | None = None
    run_type: str = "idle"
    status: str = "idle"
    stage: str = "idle"
    started_at: str | None = None
    run_started_at: str | None = None
    updated_at: str = Field(default_factory=utcnow_iso)
    completed_at: str | None = None
    last_event_at: str | None = None
    elapsed_seconds: float = 0.0
    queue_depth: int = 0
    blocked_applications: int = 0
    pending_questions: int = 0
    submitted_count: int = 0
    failed_count: int = 0
    rejected_count: int = 0
    active_job_id: str | None = None
    active_application_id: str | None = None
    company: str | None = None
    role: str | None = None
    source: str | None = None
    current_company: str | None = None
    current_role: str | None = None
    current_title: str | None = None
    active_step: str | None = None
    latest_operator_message: str | None = None
    latest_error: str | None = None
    stream_health: str = "idle"
    event_count: int = 0
    model_activity: dict[str, Any] = Field(default_factory=dict)
    stage_counters: dict[str, int] = Field(default_factory=dict)
    stats: dict[str, Any] = Field(default_factory=dict)


