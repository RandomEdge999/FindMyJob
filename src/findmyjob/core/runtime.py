from __future__ import annotations

import os
import platform
import shutil
import sys
from importlib.metadata import PackageNotFoundError, version as package_version
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import anyio
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from findmyjob.core.config import (
    AppConfig,
    greenhouse_browser_launch_ready,
    greenhouse_launch_path_ready,
    inspect_app_config,
    source_has_discovery_targets,
)
from findmyjob.core.env import load_workspace_dotenv
from findmyjob.core.enums import CaptureMode, FactKind, JobLifecycleStatus
from findmyjob.core.lmstudio import LMSTUDIO_PROVIDER
from findmyjob.core.paths import ensure_workspace, workspace_config_file
from findmyjob.core.logging import redact_data
from findmyjob.core.tooling import find_latex_engine, find_typst_executable
from findmyjob.core.types import CleanupReport, GreenhouseBenchmarkSummary, LaunchCheckReport, ModelLaunchProfileReport, PersonalArtifactPreviewSummary, PersonalRehearsalReport, ReleaseSnapshotReport, SmokeTestResult, SupportBundleApplicationSummary, SupportBundleArtifactReference, SupportBundleReport, ValidationReport
from findmyjob.db.migrations import current_revision, upgrade_database
from findmyjob.db.models import ApplicationRecord, ArtifactRecord, SubmitAttemptRecord
from findmyjob.db.session import create_sqlite_engine, ensure_sqlite_fts, make_session_factory, session_scope
from findmyjob.db.repositories import AuditRepository, ProfileRepository
from findmyjob.documents.pipeline import DocumentPipeline, DocumentTemplateConfig
from findmyjob.grounding.service import GroundingService
from findmyjob.model_router.router import ModelRouter
from findmyjob.security.secrets import keyring_status

log = logging.getLogger("findmyjob.runtime")

_ACTIVE_APPLICATION_STATUSES = {
    JobLifecycleStatus.PREPARING,
    JobLifecycleStatus.NEEDS_USER_INPUT,
    JobLifecycleStatus.READY_FOR_REVIEW,
    JobLifecycleStatus.APPROVED_FOR_SUBMIT,
    JobLifecycleStatus.SUBMITTING,
    JobLifecycleStatus.SUBMISSION_UNCERTAIN,
}
_CAPTURE_PATH_KEYS = {
    "snapshot_path",
    "trace_path",
    "pre_submit_snapshot_path",
    "final_snapshot_path",
    "dom_snapshot_path",
    "post_submit_dom_snapshot_path",
}


@dataclass(slots=True)
class AppRuntime:
    workspace: Path
    config: AppConfig
    session_factory: sessionmaker[Session]
    model_router: ModelRouter
    grounding: GroundingService
    documents: DocumentPipeline

    @classmethod
    def bootstrap(cls, workspace: Path | None = None, config: AppConfig | None = None) -> "AppRuntime":
        root = (workspace or Path.cwd()).resolve()
        load_workspace_dotenv(root)
        ensure_workspace(root)
        resolved_config = config or AppConfig.load(root)
        database_path = resolved_config.database_path(root)
        for path in (
            database_path.parent,
            resolved_config.artifacts_dir(root),
            resolved_config.exports_dir(root),
            resolved_config.snapshots_dir(root),
        ):
            path.mkdir(parents=True, exist_ok=True)
        upgrade_database(database_path)
        engine = create_sqlite_engine(database_path)
        with engine.begin() as connection:
            ensure_sqlite_fts(connection)
        session_factory = make_session_factory(engine)
        model_router = ModelRouter(resolved_config)
        template_config = DocumentTemplateConfig(
            resume_renderer=resolved_config.personal.resume_renderer,
            resume_template_path=resolved_config.resume_template_path(root),
            cover_letter_template_path=resolved_config.cover_letter_template_path(root),
            cover_letter_reference_path=resolved_config.cover_letter_reference_path(root),
        )
        documents = DocumentPipeline(resolved_config.artifacts_dir(root), root / "templates" / "typst", template_config=template_config)
        grounding = GroundingService(model_router)
        return cls(
            workspace=root,
            config=resolved_config,
            session_factory=session_factory,
            model_router=model_router,
            grounding=grounding,
            documents=documents,
        )

    def session_scope(self):
        return session_scope(self.session_factory)

    def list_smoke_results(self, limit: int = 20) -> list[SmokeTestResult]:
        return list_recorded_smoke_results(runtime=self, limit=limit)

    def list_benchmark_summaries(self, limit: int = 10) -> list[GreenhouseBenchmarkSummary]:
        from findmyjob.orchestrator.greenhouse import GreenhouseScaleOrchestrator

        return GreenhouseScaleOrchestrator(self).list_benchmarks(limit=limit)

    def inspect_model_launch_profile(self) -> ModelLaunchProfileReport:
        return self.model_router.inspect_launch_profile()

    def collect_release_snapshot(self, *, smoke_limit: int = 20, benchmark_limit: int = 10) -> ReleaseSnapshotReport:
        return collect_release_snapshot(runtime=self, smoke_limit=smoke_limit, benchmark_limit=benchmark_limit)

    def collect_support_bundle(
        self,
        *,
        application_ids: list[str] | None = None,
        include_artifact_paths: bool = False,
        include_sensitive_artifacts: bool = False,
    ) -> SupportBundleReport:
        return collect_support_bundle(
            runtime=self,
            application_ids=application_ids,
            include_artifact_paths=include_artifact_paths,
            include_sensitive_artifacts=include_sensitive_artifacts,
        )

    def inspect_personal_rehearsal(
        self,
        *,
        include_daily_dry_run: bool = True,
        daily_dry_run_limit: int = 10,
    ) -> PersonalRehearsalReport:
        return inspect_personal_rehearsal(
            runtime=self,
            include_daily_dry_run=include_daily_dry_run,
            daily_dry_run_limit=daily_dry_run_limit,
        )


def inspect_readiness(
    workspace: Path | None = None,
    *,
    check_models: bool = True,
    check_browser: bool = True,
    check_typst: bool = True,
) -> ValidationReport:
    root = (workspace or Path.cwd()).resolve()
    config, base_report = inspect_app_config(root)
    report = ValidationReport(context="doctor", workspace=str(root), loaded_files=list(base_report.loaded_files), findings=list(base_report.findings))

    config_path = workspace_config_file(root)
    _add_path_finding(report, "workspace.root", "Workspace root", root, require_exists=True)
    if config_path.exists():
        report.add("ok", "workspace.config", "Workspace config found.", detail=str(config_path))
    else:
        report.add("blocked", "workspace.config", "Workspace config is missing.", hint="Run `fmj init` first.")

    if config is None:
        return report

    artifacts_dir = config.artifacts_dir(root)
    exports_dir = config.exports_dir(root)
    snapshots_dir = config.snapshots_dir(root)
    for key, label, path in (
        ("workspace.artifacts", "Artifacts directory", artifacts_dir),
        ("workspace.exports", "Exports directory", exports_dir),
        ("workspace.snapshots", "Snapshots directory", snapshots_dir),
    ):
        _add_path_finding(report, key, label, path, require_exists=False)

    greenhouse = config.sources.get("greenhouse")
    greenhouse_discovery_ready = source_has_discovery_targets(greenhouse)
    greenhouse_browser_ready = greenhouse_browser_launch_ready(greenhouse)
    greenhouse_builtin_ready = bool(greenhouse and greenhouse.enabled and greenhouse.use_builtin_board_universe)
    if greenhouse is None or not greenhouse.enabled:
        report.add(
            "warning",
            "sources.greenhouse",
            "Greenhouse is not enabled yet; My Greenhouse launch is not ready.",
            hint="Enable Greenhouse and keep the default My Greenhouse jobs URL or add discovery targets before launch.",
        )
    elif greenhouse_launch_path_ready(greenhouse):
        if greenhouse_discovery_ready and greenhouse_browser_ready:
            detail = f"boards={len(greenhouse.boards)} seed_urls={len(greenhouse.seed_urls)} seed_domains={len(greenhouse.seed_domains)} | browser_jobs_url={greenhouse.browser_jobs_url}"
            report.add("ok", "sources.greenhouse.targets", "Greenhouse discovery targets and My Greenhouse browser launch are configured.", detail=detail)
        elif greenhouse_discovery_ready:
            detail = f"boards={len(greenhouse.boards)} seed_urls={len(greenhouse.seed_urls)} seed_domains={len(greenhouse.seed_domains)}"
            report.add("ok", "sources.greenhouse.targets", "Greenhouse discovery targets are configured.", detail=detail)
        elif greenhouse_builtin_ready and greenhouse_browser_ready:
            detail = f"builtin_board_universe=true | browser_jobs_url={greenhouse.browser_jobs_url} | cdp_url={greenhouse.browser_cdp_url or 'http://127.0.0.1:9222'}"
            report.add("ok", "sources.greenhouse.targets", "Built-in Greenhouse board universe and My Greenhouse browser launch are configured.", detail=detail)
        elif greenhouse_builtin_ready:
            report.add("ok", "sources.greenhouse.targets", "Built-in Greenhouse board universe is configured for broad discovery.", detail="builtin_board_universe=true")
        else:
            report.add("ok", "sources.greenhouse.targets", "My Greenhouse browser launch path is configured.", detail=f"browser_jobs_url={greenhouse.browser_jobs_url} | cdp_url={greenhouse.browser_cdp_url or 'http://127.0.0.1:9222'}")
    else:
        report.add(
            "warning",
            "sources.greenhouse.targets",
            "Greenhouse is enabled but neither discovery targets nor the My Greenhouse jobs URL are configured.",
            hint="Configure the default My Greenhouse jobs URL or add at least one Greenhouse board, seed URL, or seed domain.",
        )

    if greenhouse is not None:
        if greenhouse.live_smoke_urls:
            report.add("ok", "sources.greenhouse.smoke", "Optional Greenhouse smoke allowlist configured.", detail=f"{len(greenhouse.live_smoke_urls)} allowlisted posting(s)")
        else:
            report.add("ok", "sources.greenhouse.smoke", "Controlled smoke allowlists are optional for the daily Greenhouse workflow.")

    database_path = config.database_path(root)
    if _is_writable_file_target(database_path):
        try:
            upgrade_database(database_path)
            revision = current_revision(database_path)
            report.add("ok", "database.migrations", "Database path is writable and migrations are current.", detail=f"{database_path} @ {revision}")
        except Exception as exc:
            report.add("blocked", "database.migrations", "Database migrations could not be applied.", detail=str(exc))
    else:
        report.add("blocked", "database.path", "Database path is not writable.", detail=str(database_path))

    if check_typst and config.personal.resume_renderer != "chatgpt_download":
        typst = find_typst_executable()
        if typst:
            report.add("ok", "runtime.typst", "Typst is available.", detail=typst)
        else:
            # Typst is needed for artifact rendering but not a hard system blocker;
            # the system can still discover, classify, and prepare jobs without it.
            report.add("warning", "runtime.typst", "Typst is not installed; PDF artifact rendering will be unavailable.", hint="Install Typst to render resume and cover-letter artifacts.")
    elif check_typst:
        report.add("ok", "runtime.typst", "Typst is not required for the active ChatGPT download renderer.", detail=find_typst_executable() or "not required")

    if config.personal.resume_renderer in {"latex", "latex_direct"}:
        latex = find_latex_engine()
        if latex:
            report.add("ok", "runtime.latex", "LaTeX resume renderer is available.", detail=latex)
        else:
            report.add("warning", "runtime.latex", "LaTeX resume renderer is not installed; configured LaTeX resume will not render.", hint="Install pdflatex or xelatex to render the configured local resume.")

    if check_browser:
        playwright = _inspect_playwright()
        browser_required = bool(greenhouse and (greenhouse.submit_enabled or greenhouse_browser_launch_ready(greenhouse)))
        package_status = "ok" if playwright["package_ok"] else ("blocked" if browser_required else "warning")
        browser_status = "ok" if playwright["browser_ok"] else ("blocked" if browser_required else "warning")
        report.add(package_status, "runtime.playwright.package", "Playwright package status.", detail=playwright["package_detail"])
        report.add(browser_status, "runtime.playwright.browser", "Playwright Chromium browser status.", detail=playwright["browser_detail"])

    router = ModelRouter(config)
    inspection = router.inspect_profiles()
    missing_roles = inspection["missing_required_roles"]
    if missing_roles:
        report.add("warning", "models.roles", "Required model roles are not fully bound.", detail=", ".join(missing_roles))
    elif inspection["profiles"]:
        report.add("ok", "models.roles", "Required model roles are bound.")
    else:
        report.add("warning", "models.roles", "No model profiles are configured.")

    for profile in inspection["profiles"]:
        if profile["status"] == "blocked":
            report.add("warning", f"models.{profile['name']}", f"Model profile `{profile['name']}` is not ready.", detail="; ".join(profile["issues"]))

    if check_models:
        process_profiles = [profile for profile in inspection["profiles"] if profile["transport"] == "process"]
        if process_profiles:
            blocked_process = [profile for profile in process_profiles if profile["status"] == "blocked"]
            if blocked_process:
                detail = "; ".join(f"{profile['name']}: {', '.join(profile['issues'])}" for profile in blocked_process)
                report.add("warning", "models.process", "One or more process-backed model profiles are not ready.", detail=detail)
            else:
                report.add("ok", "models.process", "Process-backed model profiles are configured.", detail=", ".join(profile["name"] for profile in process_profiles))
        else:
            report.add("ok", "models.process", "No process-backed model profiles are configured.")

        lmstudio_profiles = [
            profile
            for profile in inspection["profiles"]
            if str(profile.get("provider") or "").strip().lower() == LMSTUDIO_PROVIDER
        ]
        if lmstudio_profiles:
            blocked_lmstudio = [profile for profile in lmstudio_profiles if profile["status"] == "blocked"]
            if blocked_lmstudio:
                detail = "; ".join(f"{profile['name']}: {', '.join(profile['issues'])}" for profile in blocked_lmstudio)
                report.add(
                    "ok",
                    "models.lmstudio",
                    "LM Studio model profiles are configured.",
                    detail=detail,
                )
            else:
                detail = ", ".join(profile["name"] for profile in lmstudio_profiles)
                report.add("ok", "models.lmstudio", "LM Studio model profiles are configured.", detail=detail)
        else:
            report.add("warning", "models.lmstudio", "No LM Studio model profiles are configured.")

        llamacpp_profiles = [
            profile
            for profile in inspection["profiles"]
            if str(profile.get("provider") or "").strip().lower() in ("llama_cpp", "llamacpp", "llama.cpp")
        ]
        if llamacpp_profiles:
            blocked_llamacpp = [profile for profile in llamacpp_profiles if profile["status"] == "blocked"]
            if blocked_llamacpp:
                detail = "; ".join(f"{profile['name']}: {', '.join(profile['issues'])}" for profile in blocked_llamacpp)
                report.add("warning", "models.llamacpp", "One or more llama.cpp model profiles have configuration issues.", detail=detail)
            else:
                llamacpp_status, llamacpp_detail = _inspect_llamacpp_runtime(router, llamacpp_profiles)
                report.add(llamacpp_status, "models.llamacpp", "llama.cpp local server readiness.", detail=llamacpp_detail)
        else:
            report.add("ok", "models.llamacpp", "No llama.cpp local HTTP profiles are configured.")

        # Check Ollama local HTTP profiles
        ollama_profiles = [profile for profile in inspection["profiles"] if profile["transport"] == "local" and profile.get("provider") == "ollama"]
        if ollama_profiles:
            try:
                discovered = anyio.run(router.discover_local_models)
                detail = ", ".join(discovered[:8]) if discovered else "No local models reported by Ollama."
                status = "ok" if discovered else "warning"
                report.add(status, "models.ollama", "Ollama reachability check.", detail=detail)
            except Exception as exc:
                report.add("warning", "models.ollama", "Ollama is not reachable for configured Ollama profiles.", detail=str(exc))
        else:
            report.add("ok", "models.ollama", "No local Ollama profiles are configured.")

    keyring = keyring_status()
    report.add(
        "ok" if keyring["available"] else "warning",
        "runtime.keyring",
        "OS keyring status.",
        detail=keyring.get("backend") or keyring.get("detail") or "unavailable",
    )

    privacy = config.privacy
    privacy_status = "ok"
    privacy_notes: list[str] = [
        f"retention={privacy.artifact_retention_days}d",
        f"traces={privacy.traces.value}",
        f"dom={privacy.dom_snapshots.value}",
        f"screenshots={privacy.screenshots.value}",
        f"submit_evidence={privacy.submit_evidence.value}",
        f"log_redaction={privacy.log_redaction.value}",
    ]
    if privacy.submit_evidence.value == "full" or any(mode == CaptureMode.ALL for mode in (privacy.traces, privacy.dom_snapshots, privacy.screenshots)):
        privacy_status = "warning"
    report.add(privacy_status, "privacy.retention", "Capture and retention policy status.", detail=" | ".join(privacy_notes))
    return report


def cleanup_workspace(workspace: Path | None = None, *, apply: bool = False) -> CleanupReport:
    runtime = AppRuntime.bootstrap(workspace)
    root = runtime.workspace
    config = runtime.config
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.privacy.artifact_retention_days)
    report = CleanupReport(
        dry_run=not apply,
        workspace=str(root),
        retention_days=config.privacy.artifact_retention_days,
        preserve_active_applications=config.privacy.preserve_active_application_artifacts,
    )
    artifacts_root = config.artifacts_dir(root).resolve()
    snapshots_root = config.snapshots_dir(root).resolve()

    with runtime.session_scope() as session:
        active_application_ids = {
            row.id
            for row in session.scalars(select(ApplicationRecord).where(ApplicationRecord.status.in_(list(_ACTIVE_APPLICATION_STATUSES)))).all()
        }
        artifact_records = session.scalars(select(ArtifactRecord).order_by(ArtifactRecord.created_at.asc())).all()
        referenced_paths = {str(Path(record.path).resolve()) for record in artifact_records if record.path}

        for record in artifact_records:
            path = Path(record.path).resolve()
            if not _is_prunable_path(path, artifacts_root, snapshots_root):
                report.add(path=str(path), action="skip-outside", reason="outside configured artifact roots", artifact_id=record.id, application_id=record.application_id)
                continue
            created_at = _as_utc(record.created_at)
            if created_at is None or created_at >= cutoff:
                continue
            if config.privacy.preserve_active_application_artifacts and record.application_id in active_application_ids:
                report.add(path=str(path), action="skip-active", reason="active application preserved", artifact_id=record.id, application_id=record.application_id)
                continue
            if apply:
                if path.exists():
                    path.unlink()
                _clear_submit_attempt_references(session, str(path))
                session.delete(record)
                report.add(path=str(path), action="deleted", reason="expired artifact", artifact_id=record.id, application_id=record.application_id)
            else:
                report.add(path=str(path), action="delete", reason="expired artifact", artifact_id=record.id, application_id=record.application_id)

        for base_dir in (artifacts_root, snapshots_root):
            if not base_dir.exists():
                continue
            for candidate in sorted(base_dir.rglob("*")):
                if not candidate.is_file():
                    continue
                resolved = candidate.resolve()
                if str(resolved) in referenced_paths:
                    continue
                modified = _as_utc(datetime.fromtimestamp(candidate.stat().st_mtime, timezone.utc))
                if modified >= cutoff:
                    continue
                if config.privacy.preserve_active_application_artifacts and _belongs_to_active_snapshot(resolved, snapshots_root, active_application_ids):
                    report.add(path=str(resolved), action="skip-active", reason="active application snapshot preserved")
                    continue
                if apply:
                    resolved.unlink()
                    report.add(path=str(resolved), action="deleted", reason="expired orphan file")
                else:
                    report.add(path=str(resolved), action="delete", reason="expired orphan file")

    if apply:
        for base_dir in (artifacts_root, snapshots_root):
            _prune_empty_directories(base_dir)
    return report


def _add_path_finding(report: ValidationReport, key: str, label: str, path: Path, *, require_exists: bool) -> None:
    if require_exists and not path.exists():
        report.add("blocked", key, f"{label} is missing.", detail=str(path))
        return
    if path.exists() and not path.is_dir():
        report.add("blocked", key, f"{label} is not a directory.", detail=str(path))
        return
    if not _is_writable_directory(path):
        report.add("blocked", key, f"{label} is not writable.", detail=str(path))
        return
    report.add("ok", key, f"{label} is writable.", detail=str(path))


def _inspect_playwright() -> dict[str, Any]:
    try:
        import playwright  # noqa: F401
    except Exception as exc:
        return {
            "package_ok": False,
            "browser_ok": False,
            "package_detail": str(exc),
            "browser_detail": "Playwright import failed.",
        }

    executable = _discover_playwright_browser_executable()
    return {
        "package_ok": True,
        "browser_ok": executable is not None,
        "package_detail": "playwright import ok",
        "browser_detail": str(executable) if executable else "Chromium browser bundle not found. Run `python -m playwright install chromium`.",
    }


def _discover_playwright_browser_executable() -> Path | None:
    roots: list[Path] = []
    browsers_path = str(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if browsers_path and browsers_path != "0":
        roots.append(Path(browsers_path))

    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if local_app_data:
        roots.append(Path(local_app_data) / "ms-playwright")

    user_profile = str(os.environ.get("USERPROFILE") or "").strip()
    if user_profile:
        roots.append(Path(user_profile) / ".cache" / "ms-playwright")

    patterns = (
        "chromium-*\\chrome-win64\\chrome.exe",
        "chromium-*\\chrome-win\\chrome.exe",
        "chromium-*\\chrome-linux\\chrome",
        "chromium-*\\chrome-mac\\Chromium.app\\Contents\\MacOS\\Chromium",
    )
    candidates: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.exists() or root in seen:
            continue
        seen.add(root)
        for pattern in patterns:
            candidates.extend(root.glob(pattern))

    existing = [candidate.resolve() for candidate in candidates if candidate.exists()]
    if not existing:
        return None
    return sorted(existing)[-1]

_PERSONAL_REHEARSAL_DOCTOR_WARNING_KEYS = {
    "sources.greenhouse",
    "sources.greenhouse.targets",
    "sources.greenhouse.smoke",
}
_PERSONAL_REHEARSAL_LAUNCH_WARNING_KEYS = {
    "greenhouse.source",
    "greenhouse.targets",
    "greenhouse.smoke_allowlist",
    "greenhouse.smoke_history",
}


def _validation_status_with_downgraded_keys(report: ValidationReport, *, downgrade_keys: set[str]) -> str:
    blocked = [finding for finding in report.findings if finding.status == "blocked" and finding.key not in downgrade_keys]
    if blocked:
        return "blocked"
    if report.warning_count or any(finding.status == "blocked" for finding in report.findings):
        return "warning"
    return "ok"


def _launch_status_with_downgraded_keys(report: LaunchCheckReport, *, downgrade_keys: set[str]) -> str:
    blocking = [finding for finding in report.findings if finding.status == "fail" and finding.key not in downgrade_keys]
    if blocking:
        return "blocked"
    if report.warning_count or any(finding.status == "fail" for finding in report.findings):
        return "warning"
    return "ok"


def _inspect_llamacpp_runtime(router: ModelRouter, profiles: list[dict[str, Any]]) -> tuple[str, str]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for profile in profiles:
        configured_base_url = str(profile.get("base_url") or "").strip() or router._default_local_base_url(profile)
        grouped.setdefault(configured_base_url, []).append(profile)

    details: list[str] = []
    unhealthy = False
    for base_url, group in grouped.items():
        model_names = sorted({str(profile.get("model") or "") for profile in group if str(profile.get("model") or "").strip()})
        try:
            response = httpx.get(f"{base_url.rstrip('/')}/models", timeout=httpx.Timeout(1.0, connect=0.35))
            response.raise_for_status()
            payload = response.json()
            available = {str(item.get("id") or "").strip() for item in payload.get("data", []) if str(item.get("id") or "").strip()}
            missing = [model for model in model_names if model not in available]
            if missing:
                unhealthy = True
                details.append(f"{base_url}: server reachable but missing model(s): {', '.join(missing[:3])}")
            else:
                details.append(f"{base_url}: reachable with {len(available)} model(s)")
        except Exception as exc:
            unhealthy = True
            details.append(f"{base_url}: {exc}. Ensure the llama.cpp server is running and exposing the expected model.")

    if unhealthy:
        return "warning", "; ".join(details)
    return "ok", "; ".join(details) if details else f"Default base URL: {router._default_local_base_url({'provider': 'llama_cpp'})}"


def _is_writable_directory(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".fmj-write-check"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        log.debug("Failed to verify writable directory: %s", path, exc_info=True)
        return False


def _is_writable_file_target(path: Path) -> bool:
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        probe = parent / ".fmj-db-write-check"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        log.debug("Failed to verify writable file target: %s", path, exc_info=True)
        return False


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_prunable_path(path: Path, artifacts_root: Path, snapshots_root: Path) -> bool:
    return _is_within(path, artifacts_root) or _is_within(path, snapshots_root)


def _belongs_to_active_snapshot(path: Path, snapshots_root: Path, active_ids: set[str]) -> bool:
    if not _is_within(path, snapshots_root):
        return False
    return path.parent.name in active_ids


def _clear_submit_attempt_references(session: Session, path: str) -> None:
    for attempt in session.scalars(select(SubmitAttemptRecord)).all():
        changed = False
        if attempt.snapshot_path == path:
            attempt.snapshot_path = None
            changed = True
        payload = dict(attempt.payload or {})
        evidence = dict(payload.get("evidence") or {})
        for key in _CAPTURE_PATH_KEYS:
            if payload.get(key) == path:
                payload[key] = None
                changed = True
            if evidence.get(key) == path:
                evidence[key] = None
                changed = True
        if changed:
            payload["evidence"] = evidence
            attempt.payload = payload


def _prune_empty_directories(base_dir: Path) -> None:
    if not base_dir.exists():
        return
    for candidate in sorted((path for path in base_dir.rglob("*") if path.is_dir()), key=lambda item: len(item.parts), reverse=True):
        try:
            candidate.rmdir()
        except OSError:
            continue


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False



_SMOKE_RESULT_EVENT_TYPE = "greenhouse.smoke_test.recorded"
_SMOKE_RESULT_ENTITY_TYPE = "greenhouse_smoke"


def list_recorded_smoke_results(
    workspace: Path | None = None,
    *,
    runtime: AppRuntime | None = None,
    limit: int = 20,
) -> list[SmokeTestResult]:
    active_runtime = runtime or AppRuntime.bootstrap(workspace)
    with active_runtime.session_scope() as session:
        events = AuditRepository(session).list_events(
            event_type=_SMOKE_RESULT_EVENT_TYPE,
            entity_type=_SMOKE_RESULT_ENTITY_TYPE,
            limit=limit,
        )
    results: list[SmokeTestResult] = []
    for event in events:
        payload = dict(event.payload or {})
        if payload.get("checked_at") is None:
            payload["checked_at"] = event.created_at
        try:
            results.append(SmokeTestResult.model_validate(payload))
        except Exception:
            log.debug(
                "Failed to validate smoke test payload for audit event %s; retrying with fallback timestamp.",
                getattr(event, "id", None),
                exc_info=True,
            )
            payload["checked_at"] = event.created_at
            try:
                results.append(SmokeTestResult.model_validate(payload))
            except Exception:
                log.debug(
                    "Failed to validate smoke test payload for audit event %s after fallback timestamp.",
                    getattr(event, "id", None),
                    exc_info=True,
                )
                continue
    return results


def _bootstrap_runtime_for_snapshot(
    workspace: Path | None = None,
    *,
    runtime: AppRuntime | None = None,
    config: AppConfig | None = None,
) -> tuple[AppRuntime | None, list[str]]:
    if runtime is not None:
        return runtime, []
    root = (workspace or Path.cwd()).resolve()
    if config is None:
        try:
            config = AppConfig.load(root)
        except Exception as exc:
            return None, [f"Runtime bootstrap unavailable: {exc}"]
    try:
        return AppRuntime.bootstrap(root, config=config), []
    except Exception as exc:
        return None, [f"Runtime bootstrap unavailable: {exc}"]


def collect_release_snapshot(
    workspace: Path | None = None,
    *,
    runtime: AppRuntime | None = None,
    smoke_limit: int = 20,
    benchmark_limit: int = 10,
) -> ReleaseSnapshotReport:
    active_runtime = runtime
    root = runtime.workspace if runtime is not None else (workspace or Path.cwd()).resolve()
    config, config_report = inspect_app_config(root)
    doctor_report = inspect_readiness(root, check_models=True, check_browser=True, check_typst=True)
    notes: list[str] = []

    if active_runtime is None:
        active_runtime, runtime_notes = _bootstrap_runtime_for_snapshot(root, config=config)
        notes.extend(runtime_notes)

    launch_profile: ModelLaunchProfileReport | None = None
    smoke_results: list[SmokeTestResult] = []
    benchmark_summaries: list[GreenhouseBenchmarkSummary] = []
    if active_runtime is not None:
        try:
            launch_profile = active_runtime.inspect_model_launch_profile()
        except Exception as exc:
            notes.append(f"Model launch profile unavailable: {exc}")
        try:
            smoke_results = active_runtime.list_smoke_results(limit=smoke_limit)
        except Exception as exc:
            notes.append(f"Smoke history unavailable: {exc}")
        try:
            benchmark_summaries = active_runtime.list_benchmark_summaries(limit=benchmark_limit)
        except Exception as exc:
            notes.append(f"Benchmark history unavailable: {exc}")

    launch_check = inspect_launch_acceptance(
        root,
        runtime=active_runtime,
        config=config,
        config_report=config_report,
        doctor_report=doctor_report,
    )
    return ReleaseSnapshotReport(
        generated_at=datetime.now(timezone.utc),
        workspace=str(root),
        workspace_name=root.name,
        config_path=str(workspace_config_file(root)),
        launch_check=launch_check,
        config_validation=config_report,
        doctor=doctor_report,
        launch_profile=launch_profile,
        latest_smoke_result=smoke_results[0] if smoke_results else None,
        smoke_results=smoke_results,
        latest_benchmark=benchmark_summaries[0] if benchmark_summaries else None,
        benchmark_summaries=benchmark_summaries,
        notes=notes,
    )


def _current_version_payload() -> dict[str, str]:
    try:
        installed_version = package_version("findmyjob")
    except PackageNotFoundError:
        from findmyjob import __version__

        installed_version = __version__
    return {
        "package": "findmyjob",
        "version": installed_version,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "executable": sys.executable,
    }


def _path_metadata(path: Path, workspace: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    inside_workspace = _is_within(resolved, workspace) or resolved == workspace
    payload: dict[str, Any] = {
        "exists": resolved.exists(),
        "inside_workspace": inside_workspace,
    }
    if inside_workspace:
        payload["path"] = str(resolved)
        payload["relative_to_workspace"] = "." if resolved == workspace else str(resolved.relative_to(workspace))
    else:
        payload["path"] = "[outside-workspace]"
    return payload


def _path_exists(value: str | None) -> bool:
    if not value:
        return False
    try:
        return Path(value).expanduser().exists()
    except OSError:
        return False


def _safe_path_label(workspace: Path, value: str | None) -> str | None:
    if not value:
        return None
    try:
        resolved = Path(value).expanduser().resolve()
    except OSError:
        return "[unavailable]"
    if _is_within(resolved, workspace) or resolved == workspace:
        return "." if resolved == workspace else str(resolved.relative_to(workspace))
    return "[outside-workspace]"


def _collect_workspace_metadata(workspace: Path, config: AppConfig | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "workspace_root": str(workspace),
        "config_path": _path_metadata(workspace_config_file(workspace), workspace),
    }
    if config is None:
        return payload
    payload["database_path"] = _path_metadata(config.database_path(workspace), workspace)
    payload["artifacts_dir"] = _path_metadata(config.artifacts_dir(workspace), workspace)
    payload["exports_dir"] = _path_metadata(config.exports_dir(workspace), workspace)
    payload["snapshots_dir"] = _path_metadata(config.snapshots_dir(workspace), workspace)
    return payload


def _redacted_model_readiness(runtime: AppRuntime) -> dict[str, Any]:
    inspection = runtime.model_router.inspect_profiles()
    profiles = []
    for profile in inspection.get("profiles", []):
        transport = str(profile.get("transport") or "unknown")
        profiles.append(
            {
                "name": profile.get("name"),
                "role": profile.get("role"),
                "provider": profile.get("provider"),
                "model": profile.get("model"),
                "transport": transport,
                "base_url_configured": bool(profile.get("base_url")),
                "base_url_source": "config" if profile.get("base_url_source") == "config" else ("env" if profile.get("base_url_source") else None),
                "secret_required": transport == "remote",
                "secret_satisfied": profile.get("secret_satisfied"),
                "secret_source": profile.get("secret_source"),
                "fallback_chain": list(profile.get("fallback_chain") or []),
                "missing_fallbacks": list(profile.get("missing_fallbacks") or []),
                "policy_tags": list(profile.get("policy_tags") or []),
                "status": profile.get("status"),
                "issues": list(profile.get("issues") or []),
            }
        )
    return redact_data(
        {
            "required_roles": list(inspection.get("required_roles") or []),
            "missing_required_roles": list(inspection.get("missing_required_roles") or []),
            "duplicate_roles": dict(inspection.get("duplicate_roles") or {}),
            "profiles": profiles,
        },
        mode=runtime.config.privacy.log_redaction,
    )


def _onboarding_summary(workspace: Path) -> dict[str, Any]:
    from findmyjob.personal.onboarding import inspect_personal_onboarding

    inspection = inspect_personal_onboarding(workspace)
    fact_counts = dict(inspection.get("fact_counts") or {})
    review_only_facts = list(inspection.get("review_only_facts") or [])
    return {
        "onboarding_enabled": bool(inspection.get("onboarding_enabled")),
        "resume_renderer": inspection.get("resume_renderer"),
        "saved_search_presets": list(inspection.get("saved_search_presets") or []),
        "enabled_saved_search_presets": list(inspection.get("enabled_saved_search_presets") or []),
        "fact_counts": fact_counts,
        "contact_fact_count": int(fact_counts.get(FactKind.CONTACT.value, 0)),
        "authorization_fact_count": int(fact_counts.get(FactKind.AUTHORIZATION.value, 0)),
        "review_only_fact_count": len(review_only_facts),
        "flagged_items": list(inspection.get("flagged_items") or []),
        "skipped_items": list(inspection.get("skipped_items") or []),
        "facts_file": _safe_path_label(workspace, inspection.get("facts_file")),
        "manifest_path": _safe_path_label(workspace, inspection.get("manifest_path")),
        "source_dir_configured": inspection.get("source_dir") is not None,
        "source_dir_present": _path_exists(inspection.get("source_dir")),
        "resume_template_configured": inspection.get("resume_template") is not None,
        "resume_template_present": _path_exists(inspection.get("resume_template")),
        "cover_letter_template_configured": inspection.get("cover_letter_template") is not None,
        "cover_letter_template_present": _path_exists(inspection.get("cover_letter_template")),
        "cover_letter_reference_configured": inspection.get("cover_letter_reference") is not None,
        "cover_letter_reference_present": _path_exists(inspection.get("cover_letter_reference")),
    }


def _with_profile_fact_availability(runtime: AppRuntime, onboarding: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    with runtime.session_scope() as session:
        for record in ProfileRepository(session).list_facts():
            kind = record.kind.value if hasattr(record.kind, 'value') else str(record.kind)
            counts[kind] = counts.get(kind, 0) + 1
    enriched = dict(onboarding)
    enriched['available_fact_counts'] = counts
    enriched['available_contact_fact_count'] = int(counts.get(FactKind.CONTACT.value, 0))
    enriched['available_authorization_fact_count'] = int(counts.get(FactKind.AUTHORIZATION.value, 0))
    return enriched


def _personal_preferences_summary(config: AppConfig) -> dict[str, Any]:
    from findmyjob.personal.preferences import effective_enabled_saved_search_presets

    personal = config.personal
    return {
        "enabled_saved_search_presets": list(effective_enabled_saved_search_presets(personal)),
        "configured_enabled_saved_search_presets": list(personal.enabled_saved_search_presets),
        "countries": list(personal.countries),
        "regions": list(personal.regions),
        "cities": list(personal.cities),
        "remote_only": personal.remote_only,
        "workplace_types": [value.value for value in personal.workplace_types],
        "experience_levels": [value.value for value in personal.experience_levels],
        "posted_within_days": personal.posted_within_days,
        "company_size_buckets": [value.value for value in personal.company_size_buckets],
        "compensation_min": personal.compensation_min,
        "compensation_currency": personal.compensation_currency,
        "sponsorship_fit": personal.sponsorship_fit.value if personal.sponsorship_fit is not None else None,
        "requires_future_sponsorship": personal.requires_future_sponsorship,
        "default_result_limit": personal.default_result_limit,
        "auto_prepare_after_discovery": personal.auto_prepare_after_discovery,
        "allow_unknown_compensation": personal.allow_unknown_compensation,
        "allow_unknown_experience_level": personal.allow_unknown_experience_level,
    }


def _inbox_summary_payload(summary) -> dict[str, Any]:
    return {
        "latest_daily_run_id": summary.latest_daily_run_id,
        "latest_daily_run_at": summary.latest_daily_run_at,
        "enabled_presets": list(summary.enabled_presets),
        "counts": {
            "shortlisted": len(summary.shortlisted_jobs),
            "watching": len(summary.watching_jobs),
            "new_matching": len(summary.new_matching_jobs),
            "ready_for_review": len(summary.ready_for_review),
            "needs_user_input": len(summary.needs_user_input),
            "approved_pending_submit": len(summary.approved_pending_submit),
            "suppressed": len(summary.suppressed_jobs),
        },
    }


def _daily_run_summary_payload(summary) -> dict[str, Any] | None:
    if summary is None:
        return None
    return {
        "run_id": summary.run_id,
        "started_at": summary.started_at,
        "completed_at": summary.completed_at,
        "preset_names": list(summary.preset_names),
        "sync_run_id": summary.sync_run_id,
        "prepare_run_count": len(summary.prepare_run_ids),
        "matching_job_count": len(summary.matching_job_ids),
        "ranked_job_count": len(summary.ranked_job_ids),
        "new_job_count": len(summary.new_job_ids),
        "updated_job_count": len(summary.updated_job_ids),
        "ready_for_preparation_count": len(summary.ready_for_preparation_job_ids),
        "added_to_review_count": len(summary.added_to_review_job_ids),
        "needs_user_input_count": len(summary.needs_user_input_application_ids),
        "screened_out_count": len(summary.screened_out_job_ids),
        "suppressed_count": len(summary.suppressed_job_ids),
        "auto_prepare": summary.auto_prepare,
    }


def _artifact_reference(kind: str, path_value: str) -> SupportBundleArtifactReference:
    path = Path(path_value).expanduser()
    exists = path.exists()
    size_bytes = None
    if exists:
        try:
            size_bytes = path.stat().st_size
        except OSError:
            size_bytes = None
    return SupportBundleArtifactReference(kind=kind, path=str(path), exists=exists, size_bytes=size_bytes)


def _application_inspection_summary(
    runtime: AppRuntime,
    application_id: str,
    *,
    include_artifact_paths: bool,
    include_sensitive_artifacts: bool,
) -> SupportBundleApplicationSummary:
    from findmyjob.orchestrator.service import Orchestrator

    payload = anyio.run(Orchestrator(runtime).inspect_submission_result, application_id)
    if payload is None:
        raise ValueError(f"Application inspection is unavailable: {application_id}")
    artifact_paths = {
        "receipt_path": payload.get("receipt_path"),
        "trace_path": payload.get("trace_path"),
        "pre_submit_snapshot_path": payload.get("pre_submit_snapshot_path"),
        "final_snapshot_path": payload.get("final_snapshot_path"),
        "dom_snapshot_path": payload.get("dom_snapshot_path"),
        "post_submit_dom_snapshot_path": payload.get("post_submit_dom_snapshot_path"),
    }
    sensitive_artifacts = []
    if include_sensitive_artifacts:
        for kind, key in (
            ("submission_receipt", "receipt_path"),
            ("submission_trace", "trace_path"),
            ("pre_submit_snapshot", "pre_submit_snapshot_path"),
            ("final_snapshot", "final_snapshot_path"),
            ("dom_snapshot", "dom_snapshot_path"),
            ("post_submit_dom_snapshot", "post_submit_dom_snapshot_path"),
        ):
            if artifact_paths.get(key):
                sensitive_artifacts.append(_artifact_reference(kind, str(artifact_paths[key])).model_dump(mode="json"))
    summary_payload = {
        "application_id": payload.get("application_id") or application_id,
        "job_id": payload.get("job_id"),
        "company": payload.get("company"),
        "job_title": payload.get("job_title"),
        "submission_status": payload.get("submission_status"),
        "review_status": payload.get("review_status"),
        "failure_reason": payload.get("failure_reason"),
        "confirmation_strategy": payload.get("confirmation_strategy"),
        "final_url": payload.get("final_url"),
        "validation_errors": list(payload.get("validation_errors") or []),
        "matched_confirmation_markers": list(payload.get("matched_confirmation_markers") or []),
        "missing_required_controls": list(payload.get("missing_required_controls") or []),
        "submit_button_present": payload.get("submit_button_present"),
        "submit_button_enabled": payload.get("submit_button_enabled"),
        "field_audit_summary": list(payload.get("field_audit_summary") or []),
        "attempt_recorded_at": payload.get("attempt_recorded_at"),
        "artifact_paths": artifact_paths if include_artifact_paths else {},
        "sensitive_artifacts": sensitive_artifacts,
    }
    return SupportBundleApplicationSummary.model_validate(
        redact_data(summary_payload, mode=runtime.config.privacy.log_redaction)
    )


def _rehearsal_status_from_validation(report: ValidationReport) -> str:
    if report.blocked_count:
        return "blocked"
    if report.warning_count:
        return "warning"
    return "ok"


def _rehearsal_status_from_launch_profile(report: ModelLaunchProfileReport | None) -> str:
    if report is None:
        return "warning"
    if report.overall_status == "fail":
        return "blocked"
    if report.overall_status == "warning":
        return "warning"
    return "ok"


def _artifact_preview_summary(name: str, payload: dict[str, Any]) -> PersonalArtifactPreviewSummary:
    return PersonalArtifactPreviewSummary(
        name=name,
        status="ok",
        renderer=payload.get("renderer"),
        job_id=payload.get("job_id"),
        company=payload.get("company"),
        title=payload.get("title"),
        synthetic_job=bool(payload.get("synthetic_job")),
        artifacts=[
            {
                "kind": artifact.get("kind"),
                "valid": bool((artifact.get("validation") or {}).get("valid", True)),
                "failure_reason": (artifact.get("validation") or {}).get("failure_reason"),
            }
            for artifact in payload.get("artifacts") or []
        ],
    )


def collect_support_bundle(
    workspace: Path | None = None,
    *,
    runtime: AppRuntime | None = None,
    application_ids: list[str] | None = None,
    include_artifact_paths: bool = False,
    include_sensitive_artifacts: bool = False,
) -> SupportBundleReport:
    active_runtime = runtime
    root = runtime.workspace if runtime is not None else (workspace or Path.cwd()).resolve()
    snapshot = collect_release_snapshot(root, runtime=active_runtime)
    notes = list(snapshot.notes)

    if active_runtime is None:
        active_runtime, runtime_notes = _bootstrap_runtime_for_snapshot(root)
        notes.extend(runtime_notes)

    config = active_runtime.config if active_runtime is not None else None
    onboarding = None
    personal_preferences: dict[str, Any] = {}
    inbox_summary: dict[str, Any] = {}
    latest_daily_run = None
    daily_dry_run = None
    model_readiness: dict[str, Any] = {}
    application_inspections: list[SupportBundleApplicationSummary] = []

    if active_runtime is not None:
        try:
            onboarding = _with_profile_fact_availability(active_runtime, _onboarding_summary(root))
        except Exception as exc:
            notes.append(f"Personal onboarding summary unavailable: {exc}")
        try:
            personal_preferences = _personal_preferences_summary(active_runtime.config)
        except Exception as exc:
            notes.append(f"Personal preference summary unavailable: {exc}")
        try:
            model_readiness = _redacted_model_readiness(active_runtime)
        except Exception as exc:
            notes.append(f"Model readiness summary unavailable: {exc}")
        try:
            from findmyjob.personal.workflow import build_personal_inbox

            inbox_summary = _inbox_summary_payload(build_personal_inbox(active_runtime, limit=5, include_suppressed=True))
        except Exception as exc:
            notes.append(f"Personal inbox summary unavailable: {exc}")
        try:
            from findmyjob.personal.workflow import latest_personal_daily_summary

            latest_daily_run = _daily_run_summary_payload(latest_personal_daily_summary(active_runtime))
        except Exception as exc:
            notes.append(f"Personal daily-run summary unavailable: {exc}")
        try:
            from findmyjob.personal.workflow import preview_personal_daily

            daily_dry_run = preview_personal_daily(active_runtime, limit=10).model_dump(mode="json")
        except Exception as exc:
            notes.append(f"Personal daily-run preview unavailable: {exc}")

        for application_id in application_ids or []:
            try:
                application_inspections.append(
                    _application_inspection_summary(
                        active_runtime,
                        application_id,
                        include_artifact_paths=include_artifact_paths,
                        include_sensitive_artifacts=include_sensitive_artifacts,
                    )
                )
            except Exception as exc:
                notes.append(f"Application inspection unavailable for {application_id}: {exc}")

    privacy_payload = config.privacy.model_dump(mode="json") if config is not None else {}
    return SupportBundleReport(
        generated_at=datetime.now(timezone.utc),
        workspace=str(root),
        workspace_name=root.name,
        version=_current_version_payload(),
        workspace_metadata=_collect_workspace_metadata(root, config),
        redaction={
            "log_redaction": privacy_payload.get("log_redaction") if privacy_payload else None,
            "artifact_paths_included": include_artifact_paths,
            "sensitive_artifacts_included": include_sensitive_artifacts,
            "raw_personal_files_included": False,
            "resume_or_cover_letter_bodies_included": False,
        },
        privacy=privacy_payload,
        current_snapshot=snapshot,
        onboarding=onboarding,
        personal_preferences=personal_preferences,
        inbox_summary=inbox_summary,
        latest_daily_run=latest_daily_run,
        daily_dry_run=daily_dry_run,
        model_readiness=model_readiness,
        application_inspections=application_inspections,
        notes=list(dict.fromkeys(notes)),
    )


def inspect_personal_rehearsal(
    workspace: Path | None = None,
    *,
    runtime: AppRuntime | None = None,
    include_daily_dry_run: bool = True,
    daily_dry_run_limit: int = 10,
) -> PersonalRehearsalReport:
    active_runtime = runtime
    root = runtime.workspace if runtime is not None else (workspace or Path.cwd()).resolve()
    snapshot = collect_release_snapshot(root, runtime=active_runtime)
    notes = list(snapshot.notes)
    report = ValidationReport(context="personal_rehearse", workspace=str(root))

    report.add(
        _validation_status_with_downgraded_keys(snapshot.doctor, downgrade_keys=_PERSONAL_REHEARSAL_DOCTOR_WARNING_KEYS),
        "doctor.readiness",
        _validation_summary("Doctor readiness", snapshot.doctor),
        detail=_validation_detail(snapshot.doctor),
    )
    launch_detail = ", ".join(finding.key for finding in snapshot.launch_check.findings if finding.status in {"fail", "warning"}) or None
    report.add(
        _launch_status_with_downgraded_keys(snapshot.launch_check, downgrade_keys=_PERSONAL_REHEARSAL_LAUNCH_WARNING_KEYS),
        "launch.launch_check",
        f"Launch-check is {snapshot.launch_check.overall_status}.",
        detail=launch_detail,
    )
    report.add(
        _rehearsal_status_from_launch_profile(snapshot.launch_profile),
        "models.launch_profile",
        snapshot.launch_profile.summary if snapshot.launch_profile is not None and snapshot.launch_profile.summary else "Launch-profile summary unavailable.",
        detail=_model_launch_profile_detail(snapshot.launch_profile) if snapshot.launch_profile is not None else None,
    )

    if active_runtime is None:
        active_runtime, runtime_notes = _bootstrap_runtime_for_snapshot(root)
        notes.extend(runtime_notes)

    onboarding: dict[str, Any] = {}
    personal_preferences: dict[str, Any] = {}
    inbox_summary: dict[str, Any] = {}
    latest_daily_run = None
    daily_dry_run = None
    resume_preview = None
    cover_letter_preview = None

    if active_runtime is None:
        report.add("blocked", "runtime.bootstrap", "Workspace runtime could not bootstrap for rehearsal.")
        return PersonalRehearsalReport(
            generated_at=datetime.now(timezone.utc),
            workspace=str(root),
            report=report,
            launch_snapshot=snapshot,
            notes=list(dict.fromkeys(notes)),
        )

    try:
        from findmyjob.personal.training import inspect_greenhouse_training_readiness

        training_readiness = inspect_greenhouse_training_readiness(active_runtime)
    except Exception as exc:
        report.add("warning", "training.readiness", "Greenhouse training readiness could not be inspected.", detail=str(exc))
    else:
        report.add(
            "warning" if training_readiness.blocked_count else _rehearsal_status_from_validation(training_readiness),
            "training.readiness",
            _validation_summary("Greenhouse training readiness", training_readiness),
            detail=_validation_detail(training_readiness),
            data=training_readiness.model_dump(mode="json"),
        )

    try:
        onboarding = _with_profile_fact_availability(active_runtime, _onboarding_summary(root))
    except Exception as exc:
        report.add("blocked", "personal.onboarding", "Personal onboarding inspection failed.", detail=str(exc))
    else:
        if onboarding.get("onboarding_enabled"):
            report.add("ok", "personal.onboarding", "Personal onboarding is enabled.")
        else:
            report.add("blocked", "personal.onboarding", "Personal onboarding is not enabled.", detail="Run `fmj onboard personal <source_dir>` to seed local facts and templates.")
        available_contact_count = int(onboarding.get("available_contact_fact_count") or 0)
        available_authorization_count = int(onboarding.get("available_authorization_fact_count") or 0)
        if available_contact_count:
            report.add("ok", "personal.contact_facts", f"Grounded contact facts available: {available_contact_count}.")
        else:
            report.add("blocked", "personal.contact_facts", "Grounded contact facts are missing for personal workflows.")
        if available_authorization_count:
            report.add("ok", "personal.authorization_facts", f"Grounded authorization facts available: {available_authorization_count}.")
        else:
            report.add("blocked", "personal.authorization_facts", "Grounded authorization facts are missing for personal workflows.")
        if onboarding.get("flagged_items"):
            report.add("warning", "personal.onboarding_flags", f"Onboarding flagged {len(onboarding['flagged_items'])} item(s) for review.")

    personal_preferences = _personal_preferences_summary(active_runtime.config)

    try:
        from findmyjob.personal.workflow import resolve_personal_queries

        queries = resolve_personal_queries(active_runtime)
    except Exception as exc:
        report.add("blocked", "personal.presets", "Enabled personal presets are not usable.", detail=str(exc))
    else:
        report.add("ok", "personal.presets", f"Personal presets ready: {len(queries)}.")

    try:
        from findmyjob.personal.workflow import build_personal_inbox, latest_personal_daily_summary, preview_personal_cover_letter, preview_personal_daily, preview_personal_resume

        inbox_summary = _inbox_summary_payload(build_personal_inbox(active_runtime, limit=5, include_suppressed=True))
        latest_daily = latest_personal_daily_summary(active_runtime)
        latest_daily_run = _daily_run_summary_payload(latest_daily)
        if latest_daily is None:
            report.add("warning", "personal.daily_run_history", "No recorded personal daily-run summary is available yet.")
        else:
            report.add(
                "ok",
                "personal.daily_run_history",
                "A personal daily-run summary is available.",
                detail=f"matches={len(latest_daily.matching_job_ids)} | review={len(latest_daily.added_to_review_job_ids)} | needs_user_input={len(latest_daily.needs_user_input_application_ids)}",
            )
        report.add(
            "ok",
            "personal.inbox",
            "Personal inbox summary loaded.",
            detail=(
                f"new={inbox_summary.get('counts', {}).get('new_matching', 0)} | "
                f"review={inbox_summary.get('counts', {}).get('ready_for_review', 0)} | "
                f"needs_user_input={inbox_summary.get('counts', {}).get('needs_user_input', 0)} | "
                f"approved={inbox_summary.get('counts', {}).get('approved_pending_submit', 0)}"
            ),
        )
        if include_daily_dry_run:
            daily_dry = preview_personal_daily(active_runtime, limit=daily_dry_run_limit)
            daily_dry_run = daily_dry.model_dump(mode="json")
            report.add(
                "ok",
                "personal.daily_dry_run",
                f"Local daily-run preview found {daily_dry.visible_match_count} visible match(es).",
                detail=f"matched={daily_dry.matched_job_count} | ready_for_preparation={daily_dry.ready_for_preparation_count}",
            )

        async def _resume_preview():
            return await preview_personal_resume(active_runtime, allow_synthetic=True)

        resume_payload = anyio.run(_resume_preview)
        resume_preview = _artifact_preview_summary("resume", resume_payload)
        report.add(
            "ok",
            "personal.resume_preview",
            "Resume preview rendered successfully.",
            detail=f"renderer={resume_preview.renderer} | synthetic_job={resume_preview.synthetic_job}",
        )
        if resume_preview.synthetic_job:
            notes.append("Used a synthetic local preview job because no local job was available for rehearsal.")

        async def _cover_letter_preview():
            return await preview_personal_cover_letter(active_runtime, allow_synthetic=True)

        cover_payload = anyio.run(_cover_letter_preview)
        cover_letter_preview = _artifact_preview_summary("cover_letter", cover_payload)
        report.add(
            "ok",
            "personal.cover_letter_preview",
            "Cover-letter preview rendered successfully.",
            detail=f"renderer={cover_letter_preview.renderer} | synthetic_job={cover_letter_preview.synthetic_job}",
        )
    except ValueError as exc:
        message = str(exc)
        target = "personal.preview"
        if "resume" in message.lower():
            target = "personal.resume_preview"
        elif "cover" in message.lower():
            target = "personal.cover_letter_preview"
        report.add("blocked", target, "Personal rehearsal failed.", detail=message)
    except Exception as exc:
        report.add("blocked", "personal.rehearsal", "Personal rehearsal failed unexpectedly.", detail=str(exc))

    return PersonalRehearsalReport(
        generated_at=datetime.now(timezone.utc),
        workspace=str(root),
        report=report,
        launch_snapshot=snapshot,
        onboarding=onboarding,
        personal_preferences=personal_preferences,
        inbox_summary=inbox_summary,
        latest_daily_run=latest_daily_run,
        daily_dry_run=daily_dry_run,
        resume_preview=resume_preview,
        cover_letter_preview=cover_letter_preview,
        notes=list(dict.fromkeys(notes)),
    )


def inspect_launch_acceptance(
    workspace: Path | None = None,
    *,
    runtime: AppRuntime | None = None,
    config: AppConfig | None = None,
    config_report: ValidationReport | None = None,
    doctor_report: ValidationReport | None = None,
    smoke_window_days: int = 30,
) -> LaunchCheckReport:
    root = runtime.workspace if runtime is not None else (workspace or Path.cwd()).resolve()
    now = datetime.now(timezone.utc)
    resolved_config = config
    if resolved_config is None or config_report is None:
        resolved_config, config_report = inspect_app_config(root)
    if doctor_report is None:
        doctor_report = inspect_readiness(root, check_models=True, check_browser=True, check_typst=True)
    report = LaunchCheckReport(workspace=str(root), checked_at=now)

    report.add(
        _launch_status_from_validation(config_report),
        "config.validation",
        _validation_summary("Config validation", config_report),
        detail=_validation_detail(config_report),
        data=config_report.model_dump(mode="json"),
    )
    report.add(
        _launch_status_from_validation(doctor_report),
        "doctor.readiness",
        _validation_summary("Doctor readiness", doctor_report),
        detail=_validation_detail(doctor_report),
        data=doctor_report.model_dump(mode="json"),
    )

    if resolved_config is None:
        report.add(
            "fail",
            "launch.context",
            "Launch-check could not load workspace configuration.",
            detail="Resolve config validation blockers before rerunning launch-check.",
        )
        return report

    model_report = runtime.inspect_model_launch_profile() if runtime is not None else ModelRouter(resolved_config).inspect_launch_profile()
    report.add(
        model_report.overall_status if model_report.overall_status != "warning" else "warning",
        "models.launch_profile",
        model_report.summary or "Model launch profile evaluated.",
        detail=_model_launch_profile_detail(model_report),
        data=model_report.model_dump(mode="json"),
    )

    active_runtime = runtime
    if active_runtime is None:
        try:
            active_runtime = AppRuntime.bootstrap(root, config=resolved_config)
        except Exception as exc:
            report.add(
                "fail",
                "launch.runtime",
                "Workspace runtime could not bootstrap for launch inspection.",
                detail=str(exc),
            )
            return report

    with active_runtime.session_scope() as session:
        facts = list(ProfileRepository(session).list_facts())

    contact_ready = any(
        fact.kind == FactKind.CONTACT
        and not fact.disallowed
        and str((fact.payload or {}).get("name") or "").strip()
        and str((fact.payload or {}).get("email") or "").strip()
        for fact in facts
    )
    authorization_ready = any(
        fact.kind == FactKind.AUTHORIZATION
        and not fact.disallowed
        and isinstance(fact.payload, dict)
        and "is_authorized" in fact.payload
        for fact in facts
    )
    evidence_fact_count = sum(1 for fact in facts if fact.kind in {FactKind.WORK, FactKind.PROJECT, FactKind.SKILL} and not fact.disallowed)

    report.add(
        "pass" if contact_ready else "fail",
        "profile.contact_facts",
        "Grounded contact facts are available for apply flows." if contact_ready else "Required contact facts for apply flows are missing.",
        detail="Need at least one contact fact with name and email.",
    )
    report.add(
        "pass" if authorization_ready else "fail",
        "profile.authorization_facts",
        "Grounded authorization facts are available for apply flows." if authorization_ready else "Authorization facts needed for apply flows are missing.",
        detail="Import at least one authorization fact with an `is_authorized` value.",
    )
    report.add(
        "pass" if evidence_fact_count else "warning",
        "profile.experience_facts",
        f"Grounded work/project/skill facts available: {evidence_fact_count}.",
        detail="Launch can proceed, but weak grounded facts reduce artifact quality." if evidence_fact_count == 0 else None,
    )

    template_dir = active_runtime.documents.template_dir
    template_state = active_runtime.documents.inspect_template_state()
    missing_templates: list[str] = []
    if template_state.get("resume_renderer") == "chatgpt_download":
        pass
    elif template_state.get("resume_renderer") in {"latex", "latex_direct"}:
        resume_source = active_runtime.documents.template_config.resume_template_path
        if resume_source is None or not resume_source.exists():
            missing_templates.append(str(resume_source or "missing_resume_template"))
    else:
        resume_template = template_dir / "resume.typ"
        if not resume_template.exists():
            missing_templates.append(str(resume_template))
    if template_state.get("resume_renderer") not in {"latex_direct", "chatgpt_download"}:
        cover_letter_template = template_dir / "cover_letter.typ"
        if not cover_letter_template.exists():
            missing_templates.append(str(cover_letter_template))
    local_cover_letter_template = active_runtime.documents.template_config.cover_letter_template_path
    if template_state.get("resume_renderer") == "latex_direct" and local_cover_letter_template is None:
        missing_templates.append("missing_cover_letter_template")
    elif resolved_config.personal.enabled and local_cover_letter_template is not None and not local_cover_letter_template.exists():
        missing_templates.append(str(local_cover_letter_template))
    report.add(
        "pass" if not missing_templates else "fail",
        "artifacts.templates",
        "Active document templates are present." if not missing_templates else "Active document templates are missing.",
        detail=(f"resume_renderer={template_state.get('resume_renderer')} | " + str(template_dir)) if not missing_templates else ", ".join(missing_templates),
    )

    greenhouse = resolved_config.sources.get("greenhouse")
    greenhouse_discovery_ready = source_has_discovery_targets(greenhouse)
    greenhouse_browser_ready = greenhouse_browser_launch_ready(greenhouse)
    greenhouse_builtin_ready = bool(greenhouse and greenhouse.enabled and greenhouse.use_builtin_board_universe)
    greenhouse_targets_ready = greenhouse_launch_path_ready(greenhouse)
    report.add(
        "pass" if greenhouse and greenhouse.enabled else "fail",
        "greenhouse.source",
        "Greenhouse is enabled for the launch path." if greenhouse and greenhouse.enabled else "Greenhouse is not enabled for the launch path.",
        detail=None if greenhouse and greenhouse.enabled else "Enable Greenhouse in Settings or .fmj/config.toml before live discovery and launch.",
    )
    if greenhouse_targets_ready:
        if greenhouse_discovery_ready and greenhouse_browser_ready:
            target_summary = "Greenhouse discovery targets and the My Greenhouse jobs URL are configured for launch."
            target_detail = f"boards={len(greenhouse.boards)} seed_urls={len(greenhouse.seed_urls)} seed_domains={len(greenhouse.seed_domains)} | browser_jobs_url={greenhouse.browser_jobs_url}"
        elif greenhouse_discovery_ready:
            target_summary = "Greenhouse boards or seeds are configured for launch discovery."
            target_detail = f"boards={len(greenhouse.boards)} seed_urls={len(greenhouse.seed_urls)} seed_domains={len(greenhouse.seed_domains)}"
        elif greenhouse_builtin_ready and greenhouse_browser_ready:
            target_summary = "Built-in Greenhouse board universe and My Greenhouse browser launch are configured for launch."
            target_detail = f"builtin_board_universe=true | browser_jobs_url={greenhouse.browser_jobs_url} | cdp_url={greenhouse.browser_cdp_url or 'http://127.0.0.1:9222'}"
        elif greenhouse_builtin_ready:
            target_summary = "Built-in Greenhouse board universe is configured for broad launch discovery."
            target_detail = "builtin_board_universe=true"
        else:
            target_summary = "My Greenhouse browser launch is configured for the default launch path."
            target_detail = f"browser_jobs_url={greenhouse.browser_jobs_url} | cdp_url={greenhouse.browser_cdp_url or 'http://127.0.0.1:9222'}"
    else:
        target_summary = "Greenhouse launch discovery targets or the My Greenhouse jobs URL are not configured."
        target_detail = (f"boards={len(greenhouse.boards)} seed_urls={len(greenhouse.seed_urls)} seed_domains={len(greenhouse.seed_domains)}" if greenhouse is not None else None)
    report.add(
        "pass" if greenhouse_targets_ready else "fail",
        "greenhouse.targets",
        target_summary,
        detail=target_detail,
    )

    submit_enabled = bool(greenhouse and greenhouse.submit_enabled)
    allowlist_count = len([url for url in greenhouse.live_smoke_urls if str(url).strip()]) if greenhouse is not None else 0
    report.add(
        "pass",
        "greenhouse.smoke_allowlist",
        "Optional Greenhouse smoke allowlist is configured." if allowlist_count else "Controlled smoke allowlists are optional for the daily Greenhouse workflow.",
        detail=f"allowlisted_urls={allowlist_count}",
    )
    if submit_enabled:
        smoke_results = active_runtime.list_smoke_results(limit=50)
        cutoff = now - timedelta(days=smoke_window_days)
        recent_results = [result for result in smoke_results if _as_utc(result.checked_at) and _as_utc(result.checked_at) >= cutoff]
        recent_successes = [result for result in recent_results if result.succeeded]
        recent_confirmed = [result for result in recent_successes if result.submit_confirmed]
        latest_recent = recent_results[0] if recent_results else None
        if recent_confirmed:
            summary = f"A fully confirmed controlled smoke result was recorded in the last {smoke_window_days} days."
            detail = _smoke_history_detail(recent_confirmed[0])
        elif recent_successes:
            summary = f"Controlled smoke checks passed in the last {smoke_window_days} days; this evidence remains optional for daily operation."
            detail = _smoke_history_detail(recent_successes[0])
        else:
            summary = "No recent controlled smoke evidence is recorded; this is optional for the daily Greenhouse workflow."
            detail = _smoke_history_detail(latest_recent)
        report.add(
            "pass",
            "greenhouse.smoke_history",
            summary,
            detail=detail,
            data={"window_days": smoke_window_days, "recent_results": [result.model_dump(mode="json") for result in recent_results[:5]]},
        )
    else:
        report.add(
            "pass",
            "greenhouse.smoke_history",
            "Greenhouse submit is disabled; controlled smoke evidence is not required for this workspace.",
        )

    return report

def _launch_status_from_validation(report: ValidationReport) -> str:
    if report.blocked_count:
        return "fail"
    if report.warning_count:
        return "warning"
    return "pass"


def _validation_summary(label: str, report: ValidationReport) -> str:
    if report.blocked_count:
        return f"{label} has {report.blocked_count} blocking issue(s)."
    if report.warning_count:
        return f"{label} has {report.warning_count} warning(s)."
    return f"{label} passed."


def _validation_detail(report: ValidationReport) -> str | None:
    issues = [finding.key for finding in report.findings if finding.status in {"blocked", "warning"}]
    if not issues:
        return None
    return ", ".join(issues[:5])


def _model_launch_profile_detail(report) -> str:
    detail: list[str] = [f"transport={report.transport_mix}"]
    if report.missing_required_roles:
        detail.append("missing=" + ", ".join(report.missing_required_roles))
    if report.risks:
        detail.append("risks=" + "; ".join(report.risks[:3]))
    return " | ".join(detail)


def _smoke_history_detail(result: SmokeTestResult | None) -> str | None:
    if result is None:
        return None
    mode = "confirmed_submit" if result.submit_confirmed else "check_only"
    checked_at = _as_utc(result.checked_at)
    timestamp = checked_at.isoformat() if checked_at is not None else "unknown"
    summary = f"{timestamp} | board={result.board_token} | job={result.source_job_id} | mode={mode} | status={result.status}"
    if result.failure_reason:
        summary = f"{summary} | reason={result.failure_reason}"
    return summary






















