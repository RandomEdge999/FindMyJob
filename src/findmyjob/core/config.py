from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, ValidationError, field_validator
from tomlkit import document, dumps, item, table

from findmyjob.core.enums import (
    ApplicationMode,
    CaptureMode,
    CompanySizeBucket,
    ExperienceLevel,
    LocationScope,
    LogRedactionMode,
    PolicyMode,
    SponsorshipFit,
    SubmitEvidenceMode,
    WorkplaceType,
)
from findmyjob.core.env import load_workspace_dotenv
from findmyjob.core.lmstudio import (
    LMSTUDIO_AUTO_MODEL,
    LMSTUDIO_DEFAULT_HOST,
    LMSTUDIO_DEFAULT_WRITER_MODEL,
    LMSTUDIO_PROVIDER,
)
from findmyjob.core.paths import global_config_file, workspace_config_file, workspace_database_file
from findmyjob.core.types import JobSearchQuery, ModelProfile, ValidationReport


class SourceSettings(BaseModel):
    enabled: bool = False
    boards: list[str] = Field(default_factory=list)
    seed_urls: list[str] = Field(default_factory=list)
    seed_domains: list[str] = Field(default_factory=list)
    use_builtin_board_universe: bool = True
    browser_attach_enabled: bool = False
    browser_jobs_url: str | None = None
    browser_cdp_url: str | None = None
    concurrency: int = Field(default=2, ge=1, le=64)
    submit_enabled: bool = False
    browser_timeout_seconds: int = Field(default=30, ge=5, le=600)
    request_timeout_seconds: int = Field(default=30, ge=5, le=600)
    live_smoke_urls: list[str] = Field(default_factory=list)
    crawl_depth: int = Field(default=2, ge=1, le=10)
    per_host_request_rate: float = Field(default=1.0, gt=0.0, le=60.0)
    max_boards_per_run: int = Field(default=250, ge=1)
    max_jobs_per_run: int = Field(default=10000, ge=1)
    max_job_enrichment_tasks_per_run: int = Field(default=1000, ge=1)
    stale_job_threshold: int = Field(default=2, ge=1)


class PathsSettings(BaseModel):
    database: str = Field(default='.fmj/findmyjob.db', min_length=1)
    artifacts_dir: str = Field(default='.fmj/artifacts', min_length=1)
    exports_dir: str = Field(default='.fmj/exports', min_length=1)
    snapshots_dir: str = Field(default='.fmj/snapshots', min_length=1)


class PolicySettings(BaseModel):
    default_application_mode: ApplicationMode = ApplicationMode.DRY_RUN
    require_human_review_for_submit: bool = True
    default_source_policy: PolicyMode = PolicyMode.HUMAN_IN_LOOP_SUBMIT


class SearchSettings(BaseModel):
    title_keywords: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    workplace_types: list[WorkplaceType] = Field(default_factory=list)
    employment_types: list[str] = Field(default_factory=list)
    location_scopes: list[LocationScope] = Field(default_factory=list)
    experience_levels: list[ExperienceLevel] = Field(default_factory=list)
    company_size_buckets: list[CompanySizeBucket] = Field(default_factory=list)
    posted_within_days: int | None = Field(default=None, ge=1, le=3650)
    compensation_min: int | None = Field(default=None, ge=0)
    compensation_currency: str | None = None
    requires_future_sponsorship: bool = False
    remote_only: bool = False
    allow_unknown_compensation: bool = False
    allow_unknown_experience_level: bool = False


class PrivacySettings(BaseModel):
    artifact_retention_days: int = Field(default=30, ge=1, le=3650)
    preserve_active_application_artifacts: bool = True
    traces: CaptureMode = CaptureMode.FAILURES_ONLY
    dom_snapshots: CaptureMode = CaptureMode.FAILURES_ONLY
    screenshots: CaptureMode = CaptureMode.FAILURES_ONLY
    submit_evidence: SubmitEvidenceMode = SubmitEvidenceMode.SUMMARY
    log_redaction: LogRedactionMode = LogRedactionMode.SAFE


class PersonalSettings(BaseModel):
    enabled: bool = False
    source_dir: str | None = None
    profile_facts_file: str = Field(default='.fmj/local_profile/profile_facts.yaml', min_length=1)
    onboarding_manifest: str = Field(default='.fmj/onboarding/personal_onboarding.json', min_length=1)
    resume_renderer: str = Field(default='chatgpt_download', pattern='^(typst|latex|latex_direct|html_to_pdf|chatgpt_download)$')
    resume_template: str | None = None
    cover_letter_template: str | None = None
    cover_letter_reference: str | None = None
    saved_search_presets: list[str] = Field(default_factory=list)
    enabled_saved_search_presets: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    remote_only: bool | None = None
    workplace_types: list[WorkplaceType] = Field(default_factory=list)
    experience_levels: list[ExperienceLevel] = Field(default_factory=list)
    posted_within_days: int | None = Field(default=None, ge=1, le=3650)
    company_size_buckets: list[CompanySizeBucket] = Field(default_factory=list)
    compensation_min: int | None = Field(default=None, ge=0)
    compensation_currency: str | None = None
    sponsorship_fit: SponsorshipFit | None = None
    requires_future_sponsorship: bool | None = None
    default_result_limit: int | None = Field(default=None, ge=1, le=500)
    auto_prepare_after_discovery: bool | None = None
    allow_unknown_compensation: bool | None = None
    allow_unknown_experience_level: bool | None = None


class ChatGPTDraftingSettings(BaseModel):
    enabled: bool = True
    gpt_url: str | None = "https://chatgpt.com/g/your-custom-resume-cover-letter-writer"
    completion_start_marker: str = "[[PDF_OUTPUT_READY]]"
    completion_end_marker: str = "[[PDF_OUTPUT_COMPLETE]]"
    profile_dir: str = ".fmj/browser/chatgpt-profile"
    chrome_user_data_dir: str | None = None
    downloads_dir: str = ".fmj/runtime/chatgpt-downloads"
    browser_mode: str = Field(default="attached", pattern="^(attached)$")
    browser_cdp_url: str = "http://127.0.0.1:9333"
    launch_if_missing: bool = True
    use_temporary_chat: bool = False
    timeout_seconds: int = Field(default=900, ge=15, le=1800)
    prompt_submit_delay_ms: int = Field(default=300, ge=0, le=10000)
    download_timeout_seconds: int = Field(default=300, ge=10, le=1800)
    max_parallel_jobs: int = Field(default=10, ge=1, le=20)


class CaptchaSettings(BaseModel):
    strategy: str = Field(default='skip', pattern='^(solve|skip|manual)$')
    provider: str = Field(default='2captcha', pattern='^(2captcha|capmonster|anti-captcha)$')
    api_key_env: str = 'CAPTCHA_API_KEY'
    solve_timeout_seconds: int = Field(default=300, ge=30, le=600)
    max_solve_attempts: int = Field(default=2, ge=1, le=5)


class RetrySettings(BaseModel):
    max_retries: int = Field(default=3, ge=0, le=10)
    backoff_base: float = Field(default=2.0, ge=1.0, le=10.0)
    backoff_max: float = Field(default=60.0, ge=5.0, le=300.0)
    retryable_http_codes: list[int] = Field(default_factory=lambda: [429, 500, 502, 503, 504])


class AutonomousSettings(BaseModel):
    enabled: bool = True
    source: str = Field(default='greenhouse', pattern='^(greenhouse|lever|ashby)$')
    countries: list[str] = Field(default_factory=lambda: ['US'])
    use_personal_presets: bool = True
    review_mode: str = Field(default='full_auto', pattern='^(full_auto|review_exceptions|manual)$')
    browser_mode: str = Field(default='headed', pattern='^(attached|headless|headed)$')
    persist_mode: str = Field(default='thin_cache', pattern='^(thin_cache|operator_memory)$')
    max_open_tabs: int = Field(default=6, ge=1, le=50)
    strict_apply: bool = False
    ai_greenlight_required: bool = False
    greenlight_min_score: int = Field(default=80, ge=0, le=100)
    always_generate_cover_letter: bool = True
    upload_cover_letter_when_available: bool = True
    daily_submit_cap: int = Field(default=100, ge=1, le=5000)
    per_company_daily_cap: int = Field(default=2, ge=1, le=1000)
    min_submit_delay_seconds: int = Field(default=20, ge=0, le=3600)
    max_submit_delay_seconds: int = Field(default=90, ge=0, le=3600)
    cooldown_after_429_minutes: int = Field(default=15, ge=1, le=1440)
    cooldown_after_captcha_minutes: int = Field(default=60, ge=1, le=1440)
    max_consecutive_submit_failures: int = Field(default=5, ge=1, le=1000)
    auto_export_ledger: bool = True
    ledger_output: str = Field(default='.fmj/exports/ledger', min_length=1)

    @field_validator('source', mode='before')
    @classmethod
    def _normalize_source(cls, value: Any) -> str:
        normalized = str(value or '').strip().lower()
        if normalized in {'', 'all', 'default'}:
            return 'greenhouse'
        return normalized


class AppConfig(BaseModel):
    workspace: str = '.'
    paths: PathsSettings = Field(default_factory=PathsSettings)
    policy: PolicySettings = Field(default_factory=PolicySettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    privacy: PrivacySettings = Field(default_factory=PrivacySettings)
    personal: PersonalSettings = Field(default_factory=PersonalSettings)
    chatgpt_drafting: ChatGPTDraftingSettings = Field(default_factory=ChatGPTDraftingSettings)
    autonomous: AutonomousSettings = Field(default_factory=AutonomousSettings)
    captcha: CaptchaSettings = Field(default_factory=CaptchaSettings)
    retry: RetrySettings = Field(default_factory=RetrySettings)
    models: dict[str, ModelProfile] = Field(default_factory=dict)
    sources: dict[str, SourceSettings] = Field(default_factory=dict)

    @classmethod
    def load_raw(cls, workspace: Path | None = None) -> tuple[dict[str, Any], list[Path]]:
        root = (workspace or Path.cwd()).resolve()
        load_workspace_dotenv(root)

        merged: dict[str, Any] = {}
        loaded_files: list[Path] = []
        for candidate in (global_config_file(), workspace_config_file(root)):
            if candidate.exists():
                with candidate.open('rb') as handle:
                    data = tomllib.load(handle)
                merged = _deep_merge(merged, data)
                loaded_files.append(candidate)

        env_workspace = os.environ.get('FMJ_WORKSPACE')
        if env_workspace:
            merged['workspace'] = env_workspace

        env_database = os.environ.get('FMJ_DATABASE')
        if env_database:
            merged.setdefault('paths', {})
            merged['paths']['database'] = env_database

        return merged, loaded_files

    @classmethod
    def load(cls, workspace: Path | None = None) -> 'AppConfig':
        merged, _loaded_files = cls.load_raw(workspace)
        config = cls.model_validate(merged or {})
        if not config.paths.database:
            config.paths.database = str(workspace_database_file(workspace))
        return config

    def _resolve_path(self, value: str, workspace: Path | None = None) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        return (workspace or Path.cwd()).resolve() / path

    def database_path(self, workspace: Path | None = None) -> Path:
        return self._resolve_path(self.paths.database, workspace)

    def artifacts_dir(self, workspace: Path | None = None) -> Path:
        return self._resolve_path(self.paths.artifacts_dir, workspace)

    def exports_dir(self, workspace: Path | None = None) -> Path:
        return self._resolve_path(self.paths.exports_dir, workspace)

    def snapshots_dir(self, workspace: Path | None = None) -> Path:
        return self._resolve_path(self.paths.snapshots_dir, workspace)

    def personal_source_dir(self, workspace: Path | None = None) -> Path | None:
        value = str(self.personal.source_dir or '').strip()
        return self._resolve_path(value, workspace) if value else None

    def profile_facts_path(self, workspace: Path | None = None) -> Path:
        return self._resolve_path(self.personal.profile_facts_file, workspace)

    def onboarding_manifest_path(self, workspace: Path | None = None) -> Path:
        return self._resolve_path(self.personal.onboarding_manifest, workspace)

    def resume_template_path(self, workspace: Path | None = None) -> Path | None:
        value = str(self.personal.resume_template or '').strip()
        return self._resolve_path(value, workspace) if value else None

    def cover_letter_template_path(self, workspace: Path | None = None) -> Path | None:
        value = str(self.personal.cover_letter_template or '').strip()
        return self._resolve_path(value, workspace) if value else None

    def cover_letter_reference_path(self, workspace: Path | None = None) -> Path | None:
        value = str(self.personal.cover_letter_reference or '').strip()
        return self._resolve_path(value, workspace) if value else None

    def chatgpt_profile_dir(self, workspace: Path | None = None) -> Path:
        return self._resolve_path(self.chatgpt_drafting.profile_dir, workspace)

    def chatgpt_downloads_dir(self, workspace: Path | None = None) -> Path:
        return self._resolve_path(self.chatgpt_drafting.downloads_dir, workspace)

    def autonomous_ledger_output_path(self, workspace: Path | None = None) -> Path:
        return self._resolve_path(self.autonomous.ledger_output, workspace)

    def model_for_role(self, role):
        for profile in self.models.values():
            if profile.role == role:
                return profile
        return None


GREENHOUSE_DEFAULTS = {
    'enabled': True,
    'boards': item([]),
    'seed_urls': item([]),
    'seed_domains': item([]),
    'use_builtin_board_universe': True,
    'browser_attach_enabled': False,
    'browser_jobs_url': 'https://my.greenhouse.io/jobs',
    'browser_cdp_url': 'http://127.0.0.1:9222',
    'concurrency': 8,
    'submit_enabled': False,
    'browser_timeout_seconds': 30,
    'request_timeout_seconds': 30,
    'live_smoke_urls': item([]),
    'crawl_depth': 2,
    'per_host_request_rate': 1.0,
    'max_boards_per_run': 1000,
    'max_jobs_per_run': 25000,
    'max_job_enrichment_tasks_per_run': 5000,
    'stale_job_threshold': 2,
}

# Default LM Studio profiles for the local runtime path.
LMSTUDIO_DEFAULT_BASE_URL = LMSTUDIO_DEFAULT_HOST
LMSTUDIO_DEFAULT_VLM_MODEL = LMSTUDIO_DEFAULT_WRITER_MODEL
GREENHOUSE_BROWSER_JOBS_URL = 'https://my.greenhouse.io/jobs'
GREENHOUSE_BROWSER_CDP_URL = 'http://127.0.0.1:9222'


def source_has_discovery_targets(settings: SourceSettings | None) -> bool:
    return bool(settings and (settings.boards or settings.seed_urls or settings.seed_domains))


def source_launch_path_ready(settings: SourceSettings | None) -> bool:
    return bool(settings and settings.enabled and (settings.use_builtin_board_universe or source_has_discovery_targets(settings)))


def greenhouse_browser_launch_ready(settings: SourceSettings | None) -> bool:
    if settings is None or not settings.enabled or not settings.browser_attach_enabled:
        return False
    return _is_http_url(str(settings.browser_jobs_url or '').strip())


def greenhouse_launch_path_ready(settings: SourceSettings | None) -> bool:
    return source_launch_path_ready(settings) or greenhouse_browser_launch_ready(settings)


def recommended_lmstudio_model_metadata() -> dict[str, Any]:
    return {
        'provider': LMSTUDIO_PROVIDER,
        'base_url': LMSTUDIO_DEFAULT_BASE_URL,
        'writer_model': LMSTUDIO_AUTO_MODEL,
        'screening_model': LMSTUDIO_AUTO_MODEL,
        'vision_model': LMSTUDIO_AUTO_MODEL,
    }


def recommended_lmstudio_profiles() -> dict[str, dict[str, Any]]:
    metadata = recommended_lmstudio_model_metadata()
    profiles = {
        'lmstudio-writer': {
            'name': 'lmstudio-writer',
            'role': 'writer',
            'provider': metadata['provider'],
            'model': metadata['writer_model'],
            'local': True,
            'transport': 'local_http',
            'base_url': metadata['base_url'],
            'temperature': 0.7,
            'supports_structured_output': True,
            'fallback_chain': [],
            'policy_tags': ['reasoning', 'drafting'],
        },
        'lmstudio-question-answerer': {
            'name': 'lmstudio-question-answerer',
            'role': 'question_answerer',
            'provider': metadata['provider'],
            'model': metadata['screening_model'],
            'local': True,
            'transport': 'local_http',
            'base_url': metadata['base_url'],
            'temperature': 0.2,
            'supports_structured_output': False,
            'fallback_chain': [],
            'policy_tags': ['reasoning', 'grounded_answers'],
        },
        'lmstudio-extractor': {
            'name': 'lmstudio-extractor',
            'role': 'extractor',
            'provider': metadata['provider'],
            'model': metadata['screening_model'],
            'local': True,
            'transport': 'local_http',
            'base_url': metadata['base_url'],
            'temperature': 0.0,
            'supports_structured_output': True,
            'fallback_chain': [],
            'policy_tags': ['reasoning', 'operator_extraction'],
        },
        'lmstudio-classifier': {
            'name': 'lmstudio-classifier',
            'role': 'classifier',
            'provider': metadata['provider'],
            'model': metadata['screening_model'],
            'local': True,
            'transport': 'local_http',
            'base_url': metadata['base_url'],
            'temperature': 0.0,
            'supports_structured_output': True,
            'fallback_chain': [],
            'policy_tags': ['reasoning', 'screening'],
        },
        'lmstudio-resume-writer': {
            'name': 'lmstudio-resume-writer',
            'role': 'resume_writer',
            'provider': metadata['provider'],
            'model': metadata['writer_model'],
            'local': True,
            'transport': 'local_http',
            'base_url': metadata['base_url'],
            'temperature': 0.7,
            'supports_structured_output': True,
            'fallback_chain': [],
            'policy_tags': ['reasoning', 'resume'],
        },
        'lmstudio-cover-letter-writer': {
            'name': 'lmstudio-cover-letter-writer',
            'role': 'cover_letter_writer',
            'provider': metadata['provider'],
            'model': metadata['writer_model'],
            'local': True,
            'transport': 'local_http',
            'base_url': metadata['base_url'],
            'temperature': 0.7,
            'supports_structured_output': True,
            'fallback_chain': [],
            'policy_tags': ['reasoning', 'cover_letter'],
        },
        'lmstudio-page-vlm': {
            'name': 'lmstudio-page-vlm',
            'role': 'page_vlm',
            'provider': metadata['provider'],
            'model': metadata['vision_model'],
            'local': True,
            'transport': 'local_http',
            'base_url': metadata['base_url'],
            'temperature': 0.1,
            'supports_structured_output': False,
            'fallback_chain': [],
            'policy_tags': ['vision', 'document'],
        },
        'lmstudio-page-vlm-verifier': {
            'name': 'lmstudio-page-vlm-verifier',
            'role': 'page_vlm_verifier',
            'provider': metadata['provider'],
            'model': metadata['vision_model'],
            'local': True,
            'transport': 'local_http',
            'base_url': metadata['base_url'],
            'temperature': 0.0,
            'supports_structured_output': True,
            'fallback_chain': [],
            'policy_tags': ['vision', 'document', 'verifier'],
        },
    }
    return profiles


def recommended_lmstudio_models_table() -> Any:
    """Build a tomlkit table with the recommended LM Studio model profiles."""
    models_tbl = table()
    for name, payload in recommended_lmstudio_profiles().items():
        profile_tbl = table()
        for key, value in payload.items():
            if isinstance(value, list):
                profile_tbl[key] = item(value)
            else:
                profile_tbl[key] = value
        models_tbl[name] = profile_tbl
    return models_tbl


def inspect_app_config_payload(
    workspace: Path | None,
    merged: dict[str, Any],
    *,
    loaded_files: list[Path] | list[str] | None = None,
    context: str = 'config',
) -> tuple[AppConfig | None, ValidationReport]:
    root = (workspace or Path.cwd()).resolve()
    report = ValidationReport(
        context=context,
        workspace=str(root),
        loaded_files=[str(path) for path in (loaded_files or [])],
    )

    try:
        config = AppConfig.model_validate(merged or {})
    except ValidationError as exc:
        for error in exc.errors():
            location = '.'.join(str(part) for part in error.get('loc', ())) or 'config'
            report.add('blocked', f'config.{location}', f'Invalid `{location}`.', detail=str(error.get('msg') or 'Invalid value.'))
        return None, report

    _validate_paths(root, config, report)
    _validate_sources(config, report)
    _validate_search(config, report)
    _validate_models(config, report)
    _validate_privacy(config, report)
    _validate_personal(root, config, report)
    _validate_chatgpt_drafting(root, config, report)
    _validate_autonomous(root, config, report)
    return config, report


def inspect_app_config(workspace: Path | None = None) -> tuple[AppConfig | None, ValidationReport]:
    root = (workspace or Path.cwd()).resolve()
    report = ValidationReport(context='config', workspace=str(root))
    workspace_cfg = workspace_config_file(root)
    if not workspace_cfg.exists():
        report.add('blocked', 'config.workspace_file', 'Workspace config is missing.', hint='Run `fmj init` first.')
        return None, report

    try:
        merged, loaded_files = AppConfig.load_raw(root)
    except tomllib.TOMLDecodeError as exc:
        report.add('blocked', 'config.parse', 'Config TOML could not be parsed.', detail=str(exc))
        return None, report

    return inspect_app_config_payload(root, merged, loaded_files=loaded_files, context='config')


def write_default_workspace_config(destination: Path, *, include_local_models: bool = True) -> None:
    doc = document()
    doc['workspace'] = '.'

    paths_tbl = table()
    paths_tbl['database'] = '.fmj/findmyjob.db'
    paths_tbl['artifacts_dir'] = '.fmj/artifacts'
    paths_tbl['exports_dir'] = '.fmj/exports'
    paths_tbl['snapshots_dir'] = '.fmj/snapshots'
    doc['paths'] = paths_tbl

    policy_tbl = table()
    policy_tbl['default_application_mode'] = ApplicationMode.DRY_RUN.value
    policy_tbl['require_human_review_for_submit'] = True
    policy_tbl['default_source_policy'] = PolicyMode.HUMAN_IN_LOOP_SUBMIT.value
    doc['policy'] = policy_tbl

    search_tbl = table()
    search_tbl['title_keywords'] = item([])
    search_tbl['locations'] = item([])
    search_tbl['countries'] = item([])
    search_tbl['regions'] = item([])
    search_tbl['cities'] = item([])
    search_tbl['workplace_types'] = item([])
    search_tbl['employment_types'] = item([])
    search_tbl['location_scopes'] = item([])
    search_tbl['experience_levels'] = item([])
    search_tbl['company_size_buckets'] = item([])
    search_tbl['requires_future_sponsorship'] = False
    search_tbl['remote_only'] = False
    search_tbl['allow_unknown_compensation'] = False
    search_tbl['allow_unknown_experience_level'] = False
    doc['search'] = search_tbl

    privacy_tbl = table()
    privacy_tbl['artifact_retention_days'] = 30
    privacy_tbl['preserve_active_application_artifacts'] = True
    privacy_tbl['traces'] = CaptureMode.FAILURES_ONLY.value
    privacy_tbl['dom_snapshots'] = CaptureMode.FAILURES_ONLY.value
    privacy_tbl['screenshots'] = CaptureMode.FAILURES_ONLY.value
    privacy_tbl['submit_evidence'] = SubmitEvidenceMode.SUMMARY.value
    privacy_tbl['log_redaction'] = LogRedactionMode.SAFE.value
    doc['privacy'] = privacy_tbl

    personal_tbl = table()
    personal_tbl['enabled'] = True
    personal_tbl['source_dir'] = 'my_personal_information'
    personal_tbl['profile_facts_file'] = '.fmj/local_profile/profile_facts.yaml'
    personal_tbl['onboarding_manifest'] = '.fmj/onboarding/personal_onboarding.json'
    personal_tbl['resume_renderer'] = 'chatgpt_download'
    personal_tbl['resume_template'] = ''
    personal_tbl['cover_letter_template'] = ''
    personal_tbl['cover_letter_reference'] = ''
    personal_tbl['saved_search_presets'] = item([])
    personal_tbl['enabled_saved_search_presets'] = item([])
    personal_tbl['countries'] = item([])
    personal_tbl['regions'] = item([])
    personal_tbl['cities'] = item([])
    personal_tbl['workplace_types'] = item([])
    personal_tbl['experience_levels'] = item([])
    personal_tbl['company_size_buckets'] = item([])
    doc['personal'] = personal_tbl

    chatgpt_tbl = table()
    chatgpt_tbl['enabled'] = True
    chatgpt_tbl['gpt_url'] = 'https://chatgpt.com/g/your-custom-resume-cover-letter-writer'
    chatgpt_tbl['completion_start_marker'] = '[[PDF_OUTPUT_READY]]'
    chatgpt_tbl['completion_end_marker'] = '[[PDF_OUTPUT_COMPLETE]]'
    chatgpt_tbl['profile_dir'] = '.fmj/browser/chatgpt-profile'
    chatgpt_tbl['downloads_dir'] = '.fmj/runtime/chatgpt-downloads'
    chatgpt_tbl['browser_mode'] = 'attached'
    chatgpt_tbl['browser_cdp_url'] = 'http://127.0.0.1:9333'
    chatgpt_tbl['launch_if_missing'] = True
    chatgpt_tbl['use_temporary_chat'] = False
    chatgpt_tbl['timeout_seconds'] = 900
    chatgpt_tbl['prompt_submit_delay_ms'] = 300
    chatgpt_tbl['download_timeout_seconds'] = 300
    chatgpt_tbl['max_parallel_jobs'] = 10
    doc['chatgpt_drafting'] = chatgpt_tbl

    autonomous_tbl = table()
    autonomous_tbl['enabled'] = True
    autonomous_tbl['source'] = 'greenhouse'
    autonomous_tbl['countries'] = item(['US'])
    autonomous_tbl['use_personal_presets'] = True
    autonomous_tbl['review_mode'] = 'full_auto'
    autonomous_tbl['browser_mode'] = 'headed'
    autonomous_tbl['persist_mode'] = 'thin_cache'
    autonomous_tbl['max_open_tabs'] = 6
    autonomous_tbl['strict_apply'] = False
    autonomous_tbl['ai_greenlight_required'] = False
    autonomous_tbl['greenlight_min_score'] = 80
    autonomous_tbl['always_generate_cover_letter'] = True
    autonomous_tbl['upload_cover_letter_when_available'] = True
    autonomous_tbl['daily_submit_cap'] = 100
    autonomous_tbl['per_company_daily_cap'] = 2
    autonomous_tbl['min_submit_delay_seconds'] = 20
    autonomous_tbl['max_submit_delay_seconds'] = 90
    autonomous_tbl['cooldown_after_429_minutes'] = 15
    autonomous_tbl['cooldown_after_captcha_minutes'] = 60
    autonomous_tbl['max_consecutive_submit_failures'] = 5
    autonomous_tbl['auto_export_ledger'] = True
    autonomous_tbl['ledger_output'] = '.fmj/exports/ledger'
    doc['autonomous'] = autonomous_tbl

    if include_local_models:
        doc['models'] = recommended_lmstudio_models_table()
    else:
        doc['models'] = table()

    sources_tbl = table()
    for source_name in ('greenhouse', 'lever', 'ashby'):
        source_tbl = table()
        defaults = GREENHOUSE_DEFAULTS if source_name == 'greenhouse' else {
            'enabled': False,
            'boards': item([]),
            'seed_urls': item([]),
            'seed_domains': item([]),
            'use_builtin_board_universe': True,
            'concurrency': 2,
            'submit_enabled': False,
            'browser_timeout_seconds': 30,
            'request_timeout_seconds': 30,
            'live_smoke_urls': item([]),
            'crawl_depth': 1,
            'per_host_request_rate': 1.0,
            'max_boards_per_run': 250,
            'max_jobs_per_run': 10000,
            'max_job_enrichment_tasks_per_run': 1000,
            'stale_job_threshold': 2,
        }
        for key, value in defaults.items():
            source_tbl[key] = value
        sources_tbl[source_name] = source_tbl
    doc['sources'] = sources_tbl

    destination.write_text(dumps(doc), encoding='utf-8')


def _validate_paths(root: Path, config: AppConfig, report: ValidationReport) -> None:
    for key, path in {
        'database': config.database_path(root),
        'artifacts_dir': config.artifacts_dir(root),
        'exports_dir': config.exports_dir(root),
        'snapshots_dir': config.snapshots_dir(root),
    }.items():
        if key in {'artifacts_dir', 'snapshots_dir'} and not _is_within(path, root):
            report.add(
                'blocked',
                f'paths.{key}',
                f'`paths.{key}` must stay inside the workspace.',
                detail=str(path),
            )
        elif key == 'exports_dir' and not _is_within(path, root):
            report.add('warning', f'paths.{key}', f'`paths.{key}` resolves outside the workspace.', detail=str(path))


def _validate_sources(config: AppConfig, report: ValidationReport) -> None:
    if not any(source.enabled for source in config.sources.values()):
        report.add('warning', 'sources.none_enabled', 'No sources are enabled yet.')

    for source_name, settings in config.sources.items():
        launch_targets_ready = greenhouse_launch_path_ready(settings) if source_name == 'greenhouse' else source_launch_path_ready(settings)
        target_summary = (
            'boards, seed URLs, seed domains, the built-in board universe, or the My Greenhouse jobs URL'
            if source_name == 'greenhouse'
            else 'boards, seed URLs, seed domains, or the built-in board universe'
        )

        if settings.submit_enabled and not settings.enabled:
            report.add(
                'blocked',
                f'sources.{source_name}.submit_enabled',
                f'sources.{source_name}.submit_enabled requires the source to be enabled.',
            )
        if settings.enabled and not launch_targets_ready:
            report.add(
                'warning',
                f'sources.{source_name}.targets',
                f'Enabled source {source_name} has no {target_summary} configured.',
            )
        if settings.submit_enabled and not launch_targets_ready:
            report.add(
                'blocked',
                f'sources.{source_name}.submit_targets',
                f'sources.{source_name}.submit_enabled requires at least one {target_summary}.',
            )
        if source_name == 'greenhouse':
            if settings.browser_attach_enabled and not str(settings.browser_jobs_url or '').strip():
                report.add(
                    'blocked',
                    'sources.greenhouse.browser_jobs_url',
                    'sources.greenhouse.browser_jobs_url is required when browser attach mode is enabled.',
                )
            elif str(settings.browser_jobs_url or '').strip() and not _is_http_url(str(settings.browser_jobs_url or '').strip()):
                report.add(
                    'blocked',
                    'sources.greenhouse.browser_jobs_url',
                    'Greenhouse browser jobs URL must be an http(s) URL.',
                    detail=str(settings.browser_jobs_url),
                )
            if str(settings.browser_cdp_url or '').strip() and not _is_http_url(str(settings.browser_cdp_url or '').strip()):
                report.add(
                    'blocked',
                    'sources.greenhouse.browser_cdp_url',
                    'Greenhouse browser CDP URL must be an http(s) URL.',
                    detail=str(settings.browser_cdp_url),
                )
        if settings.live_smoke_urls and source_name != 'greenhouse':
            report.add(
                'warning',
                f'sources.{source_name}.live_smoke_urls',
                'Live smoke allowlists are only used by the Greenhouse workflow.',
            )
        if settings.live_smoke_urls and not settings.submit_enabled:
            report.add(
                'warning',
                f'sources.{source_name}.live_smoke_urls',
                f'sources.{source_name}.live_smoke_urls is configured but submit is disabled.',
            )
        for url in [*settings.seed_urls, *settings.live_smoke_urls]:
            if not _is_http_url(url):
                report.add('blocked', f'sources.{source_name}.url', 'Expected an http(s) URL.', detail=url)
        for domain in settings.seed_domains:
            cleaned = domain.strip().lower()
            if not cleaned or '://' in cleaned or '/' in cleaned:
                report.add('blocked', f'sources.{source_name}.seed_domains', 'Seed domains must be bare hostnames.', detail=domain)


def _validate_search(config: AppConfig, report: ValidationReport) -> None:
    try:
        JobSearchQuery.from_search_settings(config.search)
    except ValidationError as exc:
        for error in exc.errors():
            location = '.'.join(str(part) for part in error.get('loc', ())) or 'search'
            report.add('blocked', f'search.{location}', f'Invalid search setting `{location}`.', detail=str(error.get('msg') or 'Invalid value.'))


def _validate_models(config: AppConfig, report: ValidationReport) -> None:
    from findmyjob.model_router.router import ModelRouter

    if not config.models:
        report.add('warning', 'models.none_configured', 'No model profiles are configured; question answering will fall back to heuristic behavior.')
        return

    router = ModelRouter(config)
    duplicate_roles: dict[str, list[str]] = {}
    role_map: dict[str, list[str]] = {}
    for profile in config.models.values():
        role_map.setdefault(profile.role.value, []).append(profile.name)
        transport = router.transport_mode(profile)
        if transport == 'process':
            if not profile.command:
                report.add('blocked', f'models.{profile.name}.command', f'Process model profile `{profile.name}` is missing a command.')
        elif transport == 'remote_http':
            if not router.resolve_base_url(profile):
                report.add('blocked', f'models.{profile.name}.base_url', f'Remote model profile `{profile.name}` is missing a base URL.')
            if not profile.api_key_env:
                report.add('blocked', f'models.{profile.name}.api_key_env', f'Remote model profile `{profile.name}` is missing `api_key_env`.')
        missing_fallbacks = [name for name in profile.fallback_chain if name not in config.models]
        if missing_fallbacks:
            report.add(
                'blocked',
                f'models.{profile.name}.fallback_chain',
                f'Model profile `{profile.name}` references unknown fallbacks.',
                detail=', '.join(missing_fallbacks),
            )
        if profile.name in profile.fallback_chain:
            report.add('blocked', f'models.{profile.name}.fallback_chain', f'Model profile `{profile.name}` cannot list itself as a fallback.')

    for role, names in role_map.items():
        if len(names) > 1:
            duplicate_roles[role] = names
    for role, names in duplicate_roles.items():
        report.add('blocked', f'models.role.{role}', f'Multiple profiles are bound to the `{role}` role.', detail=', '.join(names))

    if _has_model_cycle(config.models):
        report.add('blocked', 'models.fallback_cycle', 'Model fallback chains contain a cycle.')

    missing_roles = [role.value for role in router.required_roles() if config.model_for_role(role) is None]
    if missing_roles:
        report.add('warning', 'models.required_roles', 'Required model roles are not fully bound.', detail=', '.join(missing_roles))


def _validate_privacy(config: AppConfig, report: ValidationReport) -> None:
    privacy = config.privacy
    if privacy.submit_evidence == SubmitEvidenceMode.FULL:
        report.add('warning', 'privacy.submit_evidence', 'Submit evidence is configured for full persistence.')
    if privacy.traces == CaptureMode.ALL:
        report.add('warning', 'privacy.traces', 'Trace capture is configured for all submissions.')
    if privacy.dom_snapshots == CaptureMode.ALL:
        report.add('warning', 'privacy.dom_snapshots', 'DOM snapshot capture is configured for all submissions.')
    if privacy.screenshots == CaptureMode.ALL:
        report.add('warning', 'privacy.screenshots', 'Screenshot capture is configured for all submissions.')


def _validate_personal(root: Path, config: AppConfig, report: ValidationReport) -> None:
    for key, path in {
        'profile_facts_file': config.profile_facts_path(root),
        'onboarding_manifest': config.onboarding_manifest_path(root),
    }.items():
        if not _is_within(path, root):
            report.add(
                'blocked',
                f'personal.{key}',
                f'`personal.{key}` must stay inside the workspace.',
                detail=str(path),
            )

    personal = config.personal
    source_dir = config.personal_source_dir(root)
    if source_dir is not None and not _is_within(source_dir, root):
        report.add(
            'warning',
            'personal.source_dir',
            '`personal.source_dir` resolves outside the workspace.',
            detail=str(source_dir),
        )

    if not personal.enabled:
        return

    if source_dir is None:
        report.add('warning', 'personal.source_dir', '`personal.source_dir` is recommended when personal onboarding is enabled.', hint='Run `fmj onboard personal <source_dir>` to seed local facts and templates.')
    elif not source_dir.exists():
        report.add('warning', 'personal.source_dir', '`personal.source_dir` does not exist yet.', detail=str(source_dir), hint='Create the directory or run `fmj onboard personal <source_dir>` to seed local facts and templates.')

    resume_template = config.resume_template_path(root)
    if personal.resume_renderer in {'latex', 'latex_direct'}:
        if resume_template is None:
            report.add('blocked', 'personal.resume_template', 'A local LaTeX resume source is required when `personal.resume_renderer` is `latex` or `latex_direct`.')
        elif not resume_template.exists():
            report.add('blocked', 'personal.resume_template', 'Configured local resume source does not exist.', detail=str(resume_template))

    cover_letter_template = config.cover_letter_template_path(root)
    if personal.resume_renderer == 'latex_direct' and cover_letter_template is None:
        report.add('blocked', 'personal.cover_letter_template', 'A local LaTeX cover-letter template is required when `personal.resume_renderer` is `latex_direct`.')
    elif cover_letter_template is not None and not cover_letter_template.exists():
        report.add('blocked', 'personal.cover_letter_template', 'Configured local cover-letter template does not exist.', detail=str(cover_letter_template))

    cover_letter_reference = config.cover_letter_reference_path(root)
    if cover_letter_reference is not None and not cover_letter_reference.exists():
        report.add('warning', 'personal.cover_letter_reference', 'Configured cover-letter reference file does not exist.', detail=str(cover_letter_reference))


def _validate_chatgpt_drafting(root: Path, config: AppConfig, report: ValidationReport) -> None:
    drafting = config.chatgpt_drafting
    profile_dir = config.chatgpt_profile_dir(root)
    downloads_dir = config.chatgpt_downloads_dir(root)

    for key, path in {
        'profile_dir': profile_dir,
        'downloads_dir': downloads_dir,
    }.items():
        if not _is_within(path, root):
            report.add(
                'blocked',
                f'chatgpt_drafting.{key}',
                f'`chatgpt_drafting.{key}` must stay inside the workspace.',
                detail=str(path),
            )

    if config.personal.resume_renderer != 'chatgpt_download':
        return

    if not drafting.enabled:
        report.add(
            'blocked',
            'chatgpt_drafting.enabled',
            '`chatgpt_drafting.enabled` must be true when `personal.resume_renderer` is `chatgpt_download`.',
        )

    gpt_url = str(drafting.gpt_url or '').strip()
    if not gpt_url:
        report.add(
            'blocked',
            'chatgpt_drafting.gpt_url',
            'A custom GPT URL is required when `personal.resume_renderer` is `chatgpt_download`.',
        )
    elif not _is_http_url(gpt_url):
        report.add(
            'blocked',
            'chatgpt_drafting.gpt_url',
            'ChatGPT drafting URL must be an http(s) URL.',
            detail=gpt_url,
        )

    for key, value in (
        ('completion_start_marker', drafting.completion_start_marker),
        ('completion_end_marker', drafting.completion_end_marker),
    ):
        if not str(value or '').strip():
            report.add(
                'blocked',
                f'chatgpt_drafting.{key}',
                f'`chatgpt_drafting.{key}` is required for the ChatGPT drafting contract.',
            )

    cdp_url = str(drafting.browser_cdp_url or '').strip()
    if not cdp_url:
        report.add(
            'blocked',
            'chatgpt_drafting.browser_cdp_url',
            'A dedicated ChatGPT browser CDP URL is required for managed-profile drafting.',
        )
    elif not _is_http_url(cdp_url):
        report.add(
            'blocked',
            'chatgpt_drafting.browser_cdp_url',
            'ChatGPT browser CDP URL must be an http(s) URL.',
            detail=cdp_url,
        )


def _validate_autonomous(root: Path, config: AppConfig, report: ValidationReport) -> None:
    autonomous = config.autonomous
    ledger_output = config.autonomous_ledger_output_path(root)
    if not _is_within(ledger_output, root):
        report.add(
            'blocked',
            'autonomous.ledger_output',
            '`autonomous.ledger_output` must stay inside the workspace.',
            detail=str(ledger_output),
        )
    if autonomous.max_submit_delay_seconds < autonomous.min_submit_delay_seconds:
        report.add(
            'blocked',
            'autonomous.submit_delay',
            '`autonomous.max_submit_delay_seconds` must be greater than or equal to `autonomous.min_submit_delay_seconds`.',
        )
    if autonomous.enabled:
        source_name = str(autonomous.source or '').strip().lower() or 'greenhouse'
        selected_source = config.sources.get(source_name)
        if selected_source is None or not selected_source.enabled:
            report.add('blocked', 'autonomous.source_enabled', f'Autonomous mode requires `{source_name}` to be enabled.')
        elif not selected_source.submit_enabled:
            report.add('warning', 'autonomous.submit', f'Autonomous dry-run mode active for `{source_name}`: submit is disabled. Jobs will be discovered, evaluated, and routed to review but not submitted.')
        if autonomous.use_personal_presets and not config.personal.enabled:
            report.add('warning', 'autonomous.personal', 'Autonomous mode is configured to use personal presets, but personal onboarding is not enabled.')
        bound_roles = {profile.role.value for profile in config.models.values()}
        missing = [role for role in ('writer', 'question_answerer', 'classifier') if role not in bound_roles]
        if missing:
            report.add('warning', 'autonomous.model_roles', 'Autonomous mode is missing one or more preferred AI roles.', detail=', '.join(missing))


def _is_http_url(value: str) -> bool:
    parsed = urlparse(str(value).strip())
    return parsed.scheme in {'http', 'https'} and bool(parsed.netloc)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _has_model_cycle(models: dict[str, ModelProfile]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> bool:
        if name in visited:
            return False
        if name in visiting:
            return True
        visiting.add(name)
        profile = models.get(name)
        for fallback in profile.fallback_chain if profile is not None else []:
            if fallback in models and visit(fallback):
                return True
        visiting.remove(name)
        visited.add(name)
        return False

    return any(visit(name) for name in models)


def _deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in incoming.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged





