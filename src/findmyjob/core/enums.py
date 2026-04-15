from __future__ import annotations

from enum import StrEnum


class SourceKind(StrEnum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    COMPANY = "company"
    AGGREGATOR = "aggregator"


class WorkplaceType(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERN = "intern"
    TEMPORARY = "temporary"
    UNKNOWN = "unknown"


class ExperienceLevel(StrEnum):
    INTERNSHIP = "internship"
    ENTRY_LEVEL = "entry_level"
    ASSOCIATE = "associate"
    MID_LEVEL = "mid_level"
    SENIOR = "senior"
    STAFF = "staff"
    PRINCIPAL = "principal"
    MANAGER = "manager"
    DIRECTOR = "director"
    UNKNOWN = "unknown"


class CompanySizeBucket(StrEnum):
    STARTUP = "startup"
    SMALL = "small"
    MIDSIZE = "midsize"
    LARGE = "large"
    ENTERPRISE = "enterprise"
    UNKNOWN = "unknown"


class LocationScope(StrEnum):
    COUNTRY_WIDE = "country_wide"
    STATE_WIDE = "state_wide"
    CITY_SPECIFIC = "city_specific"
    REMOTE_GLOBAL = "remote_global"
    REMOTE_US = "remote_us"
    REMOTE_COUNTRY = "remote_country"
    UNKNOWN = "unknown"


class CompensationInterval(StrEnum):
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"


class JobLifecycleStatus(StrEnum):
    DISCOVERED = "discovered"
    NORMALIZED = "normalized"
    INACTIVE = "inactive"
    DUPLICATE_BLOCKED = "duplicate_blocked"
    SCREENED_OUT = "screened_out"
    CANDIDATE = "candidate"
    PREPARING = "preparing"
    NEEDS_USER_INPUT = "needs_user_input"
    READY_FOR_REVIEW = "ready_for_review"
    APPROVED_FOR_SUBMIT = "approved_for_submit"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    SUBMISSION_UNCERTAIN = "submission_uncertain"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"


class ApplicationMode(StrEnum):
    DRY_RUN = "dry_run"
    REVIEW_QUEUE = "review_queue"
    AUTO_SUBMIT = "auto_submit"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_USER_INPUT = "needs_user_input"
    MANUAL_HANDOFF = "manual_handoff"


class QuestionType(StrEnum):
    DETERMINISTIC = "deterministic"
    SELECT = "select"
    BOOLEAN = "boolean"
    NUMERIC = "numeric"
    DATE = "date"
    NARRATIVE = "narrative"
    SENSITIVE = "sensitive"
    FILE = "file"
    UNKNOWN = "unknown"


class VerificationStatus(StrEnum):
    VERIFIED = "verified"
    NEEDS_USER_INPUT = "needs_user_input"
    REJECTED = "rejected"
    REVIEW_REQUIRED = "review_required"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    CANCELLED = "cancelled"


class PolicyMode(StrEnum):
    PUBLIC_READ_ONLY = "public_read_only"
    REVIEW_ONLY = "review_only"
    HUMAN_IN_LOOP_SUBMIT = "human_in_loop_submit"
    MANUAL_ONLY = "manual_only"


class SourceRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RunType(StrEnum):
    DISCOVER = "discover"
    DISCOVER_BOARDS = "discover_boards"
    SYNC = "sync"
    BENCHMARK = "benchmark"
    PREPARE = "prepare"
    APPLY = "apply"


class TaskType(StrEnum):
    DISCOVER_BOARDS = "discover_boards"
    DISCOVER_BOARD = "discover_board"
    SYNC_BOARD = "sync_board"
    ENRICH_JOB = "enrich_job"
    PREPARE_APPLICATION = "prepare_application"
    SUBMIT_APPLICATION = "submit_application"


class Sensitivity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SponsorshipSignal(StrEnum):
    EXPLICIT_YES = "explicit_yes"
    EXPLICIT_NO = "explicit_no"
    UNKNOWN = "unknown"
    CONFLICTING = "conflicting"


class SponsorshipFit(StrEnum):
    LIKELY_COMPATIBLE = "likely_compatible"
    LIKELY_INCOMPATIBLE = "likely_incompatible"
    REVIEW_REQUIRED = "review_required"


class PersonalTriageStatus(StrEnum):
    NEW = "new"
    SHORTLISTED = "shortlisted"
    DISMISSED = "dismissed"
    ARCHIVED = "archived"
    WATCHING = "watching"


class PersonalSuppressionScope(StrEnum):
    JOB = "job"
    COMPANY_TITLE = "company_title"
    COMPANY = "company"

class FactKind(StrEnum):
    CONTACT = "contact"
    EDUCATION = "education"
    WORK = "work"
    PROJECT = "project"
    SKILL = "skill"
    AUTHORIZATION = "authorization"
    LOCATION = "location"
    PREFERENCE = "preference"
    PERSONAL = "personal"


class ArtifactKind(StrEnum):
    RESUME_PDF = "resume_pdf"
    RESUME_TEXT = "resume_text"
    COVER_LETTER_PDF = "cover_letter_pdf"
    COVER_LETTER_TEXT = "cover_letter_text"
    REVIEW_PACKET = "review_packet"
    SNAPSHOT = "snapshot"
    SUBMISSION_RECEIPT = "submission_receipt"
    SUBMISSION_TRACE = "submission_trace"


class CaptureMode(StrEnum):
    OFF = "off"
    FAILURES_ONLY = "failures_only"
    ALL = "all"


class SubmitEvidenceMode(StrEnum):
    SUMMARY = "summary"
    FULL = "full"


class LogRedactionMode(StrEnum):
    SAFE = "safe"
    STRICT = "strict"


class ModelRole(StrEnum):
    TEXT_ROUTER = "text_router"
    WRITER = "writer"
    PAGE_VLM = "page_vlm"
    PAGE_VLM_VERIFIER = "page_vlm_verifier"
    PLANNING = "planning"
    EXTRACTOR = "extractor"
    CLASSIFIER = "classifier"
    VERIFIER = "verifier"
    RESUME_WRITER = "resume_writer"
    COVER_LETTER_WRITER = "cover_letter_writer"
    QUESTION_ANSWERER = "question_answerer"




