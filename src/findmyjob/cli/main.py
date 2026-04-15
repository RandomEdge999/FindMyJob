from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any

import anyio
import httpx
import typer
import yaml
from rich.console import Console
from rich.table import Table
from sqlalchemy import select
from tomlkit import parse, table

from findmyjob import __version__
from findmyjob.core.assets import ensure_default_workspace_templates
from findmyjob.core.config import (
    AppConfig,
    inspect_app_config,
    write_default_workspace_config,
)
from findmyjob.core.enums import ApplicationMode, CompanySizeBucket, ExperienceLevel, FactKind, JobLifecycleStatus, LocationScope, ModelRole, PersonalSuppressionScope, ReviewStatus, RunStatus, SponsorshipFit, WorkplaceType
from findmyjob.core.logging import configure_logging, redact_data
from findmyjob.core.paths import ensure_workspace, workspace_config_file
from findmyjob.core.runtime import AppRuntime, cleanup_workspace, collect_release_snapshot, collect_support_bundle, inspect_launch_acceptance, inspect_personal_rehearsal, inspect_readiness, list_recorded_smoke_results
from findmyjob.core.types import GreenhouseBenchmarkSummary, JobSearchQuery, PersonalRehearsalReport, ProfileFact, ReleaseSnapshotReport, SavedSearch, SmokeTestResult, SupportBundleReport
from findmyjob.db.migrations import current_revision, upgrade_database
from findmyjob.db.models import ApplicationRecord, AuditEventRecord, BoardDiscoveryEvidenceRecord, JobPosting, TaskRecord
from findmyjob.db.board_repository import BoardRepository, SourceStateRepository
from findmyjob.db.repositories import ApplicationRepository, JobRepository, ProfileRepository, RunRepository, SavedSearchRepository
from findmyjob.db.search import search_jobs
from findmyjob.model_router.router import ModelRouter
from findmyjob.orchestrator.greenhouse import GreenhouseScaleOrchestrator
from findmyjob.orchestrator.service import Orchestrator
from findmyjob.qualification.rules import qualification_for_job
from findmyjob.sources.greenhouse_scale import GreenhouseScaleClient
from findmyjob.sources.normalizer import build_normalized_job
from findmyjob.personal.autonomous import answer_next_question, answer_queued_question, approve_question_memory, list_question_queue
from findmyjob.personal.facts import delete_personal_fact, get_personal_fact, list_personal_facts, update_personal_fact_flags
from findmyjob.personal.onboarding import inspect_personal_onboarding, run_personal_onboarding
from findmyjob.personal.preferences import PERSONAL_PREFERENCE_KEYS, describe_personal_preferences, reset_personal_preferences, update_personal_preferences
from findmyjob.personal.workflow import archive_job, build_personal_inbox, dismiss_job, explain_personal_job, list_personal_decisions, preview_personal_cover_letter, preview_personal_resume, run_personal_daily, shortlist_job, unsuppress_job, watch_job
from findmyjob.core.workflow_snapshot import collect_workflow_snapshot, WorkflowSnapshot
from findmyjob.cli.filefirst import register_filefirst_commands
from findmyjob.filefirst import FileWorkspace
from findmyjob.filefirst.readiness import collect_filefirst_release_snapshot, inspect_filefirst_readiness
from findmyjob.web.frontend_sync import sync_frontend_bundle

collect_release_snapshot = collect_filefirst_release_snapshot
inspect_readiness = inspect_filefirst_readiness

app = typer.Typer(help="Find My Job terminal operator")
config_app = typer.Typer()
models_app = typer.Typer()
profile_app = typer.Typer()
discover_app = typer.Typer()
prepare_app = typer.Typer()
apply_app = typer.Typer()
jobs_app = typer.Typer()
searches_app = typer.Typer()
review_app = typer.Typer()
onboard_app = typer.Typer()
personal_app = typer.Typer()
personal_prefs_app = typer.Typer()
personal_facts_app = typer.Typer()
personal_preview_app = typer.Typer()
support_app = typer.Typer()
ledger_app = typer.Typer()
runs_app = typer.Typer()
sources_app = typer.Typer()
greenhouse_app = typer.Typer()
auto_app = typer.Typer()
questions_app = typer.Typer()
boards_app = typer.Typer()
db_app = typer.Typer()
workflow_app = typer.Typer()

app.add_typer(config_app, name="config")
app.add_typer(models_app, name="models")
app.add_typer(profile_app, name="profile")
app.add_typer(discover_app, name="discover")
app.add_typer(prepare_app, name="prepare")
app.add_typer(apply_app, name="apply")
app.add_typer(jobs_app, name="jobs")
app.add_typer(searches_app, name="searches")
app.add_typer(review_app, name="review")
app.add_typer(onboard_app, name="onboard")
app.add_typer(personal_app, name="personal")
personal_app.add_typer(personal_prefs_app, name="prefs")
personal_app.add_typer(personal_facts_app, name="facts")
personal_app.add_typer(personal_preview_app, name="preview")
app.add_typer(support_app, name="support")
app.add_typer(ledger_app, name="ledger")
app.add_typer(runs_app, name="runs")
app.add_typer(sources_app, name="sources")
app.add_typer(greenhouse_app, name="greenhouse")
app.add_typer(auto_app, name="auto")
app.add_typer(questions_app, name="questions")
app.add_typer(boards_app, name="boards")
app.add_typer(db_app, name="db")
app.add_typer(workflow_app, name="workflow")

console = Console(width=200)
register_filefirst_commands(app, onboard_app, models_app)


def _current_version() -> str:
    try:
        return package_version("findmyjob")
    except PackageNotFoundError:
        return __version__


def _version_payload() -> dict[str, str]:
    return {
        "package": "findmyjob",
        "version": _current_version(),
        "python": sys.version.split()[0],
        "executable": sys.executable,
    }


def _render_report(title: str, report) -> None:
    table = Table(title=f"{title} [{report.overall_status.upper()}]")
    table.add_column("Status")
    table.add_column("Check")
    table.add_column("Summary")
    table.add_column("Detail")
    if report.findings:
        for finding in report.findings:
            detail = finding.detail or finding.hint or ""
            table.add_row(finding.status.upper(), finding.key, finding.summary, detail)
    else:
        table.add_row("OK", "report", "No issues detected.", "")
    console.print(table)
    console.print(f"{title}: {report.overall_status.upper()} ({report.blocked_count} blocked, {report.warning_count} warnings)")


def _render_cleanup_report(report) -> None:
    title = "Cleanup Dry Run" if report.dry_run else "Cleanup"
    table = Table(title=f"{title} [{report.retention_days}d]")
    table.add_column("Action")
    table.add_column("Path")
    table.add_column("Reason")
    if report.findings:
        for finding in report.findings:
            table.add_row(finding.action, finding.path, finding.reason)
    else:
        table.add_row("none", "", "No files matched the current retention policy.")
    console.print(table)
    console.print(f"Candidates: {report.delete_count} | Skipped: {report.skip_count}")
    if report.dry_run:
        console.print("Dry run only. Re-run with `fmj cleanup --apply` to delete expired files.")


def _report_payload(report) -> dict[str, Any]:
    payload = report.model_dump(mode="json")
    payload["blocked_count"] = getattr(report, "blocked_count", 0)
    payload["warning_count"] = getattr(report, "warning_count", 0)
    payload["ok_count"] = getattr(report, "ok_count", 0)
    payload["overall_status"] = getattr(report, "overall_status", None)
    return payload


def _cleanup_payload(report) -> dict[str, Any]:
    payload = report.model_dump(mode="json")
    payload["delete_count"] = getattr(report, "delete_count", 0)
    payload["skip_count"] = getattr(report, "skip_count", 0)
    return payload


def _launch_payload(report) -> dict[str, Any]:
    payload = report.model_dump(mode="json")
    payload["fail_count"] = getattr(report, "fail_count", 0)
    payload["warning_count"] = getattr(report, "warning_count", 0)
    payload["pass_count"] = getattr(report, "pass_count", 0)
    payload["overall_status"] = getattr(report, "overall_status", None)
    return payload


def _render_launch_report(report) -> None:
    table = Table(title=f"Launch Check [{report.overall_status.upper()}]")
    table.add_column("Status")
    table.add_column("Check")
    table.add_column("Summary")
    table.add_column("Detail")
    for finding in report.findings:
        table.add_row(finding.status.upper(), finding.key, finding.summary, finding.detail or "")
    console.print(table)


def _model_launch_profile_payload(report: ModelLaunchProfileReport | None) -> dict[str, Any] | None:
    if report is None:
        return None
    payload = report.model_dump(mode="json")
    payload["fail_count"] = getattr(report, "fail_count", 0)
    payload["warning_count"] = getattr(report, "warning_count", 0)
    payload["overall_status"] = getattr(report, "overall_status", None)
    return payload


def _render_model_launch_profile(report) -> None:
    table = Table(title=f"Model Launch Profile [{report.overall_status.upper()}]")
    table.add_column("Role")
    table.add_column("Status")
    table.add_column("Model")
    table.add_column("Notes")
    for role_status in report.roles:
        notes_parts = []
        if role_status.transport:
            notes_parts.append(f"transport={role_status.transport}")
        if role_status.issues:
            notes_parts.append("; ".join(role_status.issues[:2]))
        table.add_row(
            role_status.role,
            role_status.status.upper(),
            role_status.model or "-",
            " | ".join(notes_parts) if notes_parts else "",
        )
    console.print(table)
    console.print(f"Model Launch Profile: {report.overall_status.upper()} ({report.summary})")


def _render_smoke_results(results: list[SmokeTestResult]) -> None:
    table = Table(title="Smoke Test Results")
    table.add_column("Status")
    table.add_column("Board")
    table.add_column("Source Job")
    table.add_column("Duration")
    table.add_column("Notes")
    for result in results:
        table.add_row(
            result.status.upper(),
            result.board_token or "-",
            result.source_job_id or "-",
            f"{result.duration_ms}ms" if result.duration_ms else "-",
            result.notes or "",
        )
    console.print(table)


def _release_snapshot_payload(snapshot: ReleaseSnapshotReport) -> dict[str, Any]:
    payload = snapshot.model_dump(mode="json")
    payload["launch_check"] = _launch_payload(snapshot.launch_check)
    payload["config_validation"] = _report_payload(snapshot.config_validation)
    payload["doctor"] = _report_payload(snapshot.doctor)
    if snapshot.launch_profile is not None:
        payload["launch_profile"] = _model_launch_profile_payload(snapshot.launch_profile)
    return payload


def _write_release_snapshot(path: Path, snapshot: ReleaseSnapshotReport) -> Path:
    return _write_json_payload(path, _release_snapshot_payload(snapshot))


def _support_bundle_payload(bundle: SupportBundleReport) -> dict[str, Any]:
    return bundle.model_dump(mode="json")


def _personal_rehearsal_payload(report: PersonalRehearsalReport) -> dict[str, Any]:
    payload = report.model_dump(mode="json")
    payload["report"] = _report_payload(report.report)
    payload["launch_snapshot"] = _release_snapshot_payload(report.launch_snapshot)
    return payload


def _write_json_payload(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _default_export_path(workspace: Path, prefix: str) -> Path:
    root = workspace.resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    export_dir = root / ".fmj" / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return export_dir / f"{prefix}-{ts}.json"


def _render_support_bundle(bundle: SupportBundleReport, exported_path: Path | None = None) -> None:
    console.print()
    console.rule("[bold]Support Bundle[/bold]")
    console.print(f"  Generated: {bundle.generated_at}")
    console.print(f"  Workspace: {bundle.workspace}")
    console.print(f"  Version: {bundle.version}")
    console.print(f"  Python: {bundle.python_version}")
    console.print()
    console.rule("[bold]Application Summary[/bold]")
    app_summary = bundle.application_summary
    console.print(f"  Total Applications: {app_summary.total}")
    console.print(f"  Pending: {app_summary.pending}")
    console.print(f"  Submitted: {app_summary.submitted}")
    console.print(f"  Uncertain: {app_summary.uncertain}")
    console.print()
    if bundle.artifacts:
        console.rule("[bold]Artifacts[/bold]")
        for artifact in bundle.artifacts:
            console.print(f"  {artifact.kind}: {artifact.path}")
    console.print()
    if exported_path:
        console.print(f"Bundle exported to: {exported_path}")


def _render_personal_rehearsal(report: PersonalRehearsalReport) -> None:
    console.print()
    console.rule("[bold]Personal Rehearsal[/bold]")
    console.print(f"  Status: {report.overall_status.upper()}")
    console.print(f"  Onboarding: {report.onboarding_status}")
    console.print(f"  Preferences: {report.preferences_status}")
    console.print(f"  Validation: {report.validation_status}")
    if report.model_launch_profile:
        console.print(f"  Model Launch Profile: {report.model_launch_profile.overall_status}")
    if report.smoke_test_result:
        console.print(f"  Smoke Test: {report.smoke_test_result.status}")
    console.print()
    if report.artifact_previews:
        console.rule("[bold]Artifact Previews[/bold]")
        for preview in report.artifact_previews:
            console.print(f"  {preview.name}: {preview.status}")
    console.print()


def _render_benchmark_summaries(summaries: list[GreenhouseBenchmarkSummary]) -> None:
    table = Table(title="Benchmark Summaries")
    table.add_column("Board")
    table.add_column("Status")
    table.add_column("Duration")
    table.add_column("Notes")
    for summary in summaries:
        table.add_row(
            summary.board_token or "-",
            summary.status.upper(),
            f"{summary.duration_ms}ms" if summary.duration_ms else "-",
            summary.notes or "",
        )
    console.print(table)


def _render_model_inspection(inspection: dict[str, Any]) -> None:
    console.print()
    console.rule("[bold]Model Inspection[/bold]")
    for key, value in inspection.items():
        console.print(f"  {key}: {value}")
    console.print()


def runtime(workspace: Path | None = None) -> AppRuntime:
    return AppRuntime.bootstrap(workspace)


_ENTRYPOINT_PAGE_PATHS: dict[str, str] = {
    "dashboard": "/",
    "setup": "/setup",
    "daily": "/daily",
    "training": "/training",
    "review": "/review",
    "runs": "/runs",
    "settings": "/settings",
}
_ENTRYPOINT_PAGE_ALIASES = {
    "home": "dashboard",
}
_ENTRYPOINT_CDP_URL = "http://127.0.0.1:9222"
_ENTRYPOINT_CHROME_START_URL = "https://my.greenhouse.io/jobs"


def _entrypoint_severity(status: str) -> int:
    normalized = str(status or "").strip().lower()
    if normalized in {"fail", "blocked"}:
        return 2
    if normalized == "warning":
        return 1
    return 0


def _entrypoint_status(issues: list[dict[str, Any]]) -> str:
    if any(_entrypoint_severity(issue.get("status", "")) == 2 for issue in issues):
        return "blocked"
    if issues:
        return "warnings"
    return "ready"


def _normalize_console_page(page: str | None, *, default: str) -> str:
    normalized = str(page or default).strip().lower().replace("-", "_")
    normalized = _ENTRYPOINT_PAGE_ALIASES.get(normalized, normalized)
    if normalized not in _ENTRYPOINT_PAGE_PATHS:
        allowed = ", ".join(_ENTRYPOINT_PAGE_PATHS)
        raise typer.BadParameter(f"Expected one of: {allowed}")
    return normalized


def _display_host(host: str) -> str:
    return "127.0.0.1" if host in {"0.0.0.0", "::"} else host


def _web_console_url(host: str, port: int, open_path: str) -> str:
    return f"http://{_display_host(host)}:{port}{open_path}"


def _summarize_onboarding(workspace: Path) -> dict[str, Any]:
    ws = FileWorkspace(workspace.resolve())
    ws.ensure()
    fact_counts: dict[str, int] = {}
    for fact in ws.load_facts():
        fact_counts[fact.kind] = fact_counts.get(fact.kind, 0) + 1
    return {
        "available": True,
        "onboarding_enabled": bool(fact_counts),
        "resume_renderer": "typst",
        "saved_search_presets": [],
        "enabled_saved_search_presets": [],
        "contact_fact_count": int(fact_counts.get("contact", 0)),
        "authorization_fact_count": int(fact_counts.get("authorization", 0)),
        "flagged_item_count": 0,
        "review_only_fact_count": sum(1 for fact in ws.load_facts() if fact.disallowed),
        "source_dir_configured": (workspace.resolve() / "my_personal_information").exists(),
    }


def _initialize_workspace_for_start(workspace: Path) -> dict[str, Any]:
    root = workspace.resolve()
    ws = FileWorkspace(root)
    template_dir = root / "templates" / "typst"
    tracked_paths = [
        ws.profile_path,
        ws.portals_path,
        ws.cv_path,
        ws.facts_path,
        ws.answer_memory_path,
        template_dir / "resume.typ",
    ]
    missing_before = [str(path.relative_to(root)).replace(chr(92), "/") for path in tracked_paths if not path.exists()]
    ws.ensure()
    ensure_default_workspace_templates(root)
    return {
        "created": bool(missing_before),
        "config_path": str(ws.profile_path),
        "created_templates": missing_before,
        "status": "created" if missing_before else "existing",
    }


def _collect_day_state(workspace: Path | AppRuntime | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, str | None]:
    if workspace is None:
        return None, None, None, None

    from findmyjob.web.service import OperatorConsoleService

    root = getattr(workspace, "workspace", workspace)
    service = OperatorConsoleService(root)
    daily = None
    review = None
    training = None
    error = None
    try:
        daily = service.daily_inbox_payload(limit=8)
    except Exception as exc:
        error = f"Daily inbox unavailable: {exc}"
    try:
        review = service.review_queue_payload(limit=12)
    except Exception as exc:
        error = error or f"Review queue unavailable: {exc}"
    try:
        training = service.try_training_report_payload()
    except Exception as exc:
        error = error or f"Training summary unavailable: {exc}"
    return daily, review, training, error


def _remember_entrypoint_issue(
    issues_by_key: dict[str, dict[str, Any]],
    *,
    status: str,
    key: str,
    summary: str,
    detail: str | None = None,
    hint: str | None = None,
) -> None:
    severity = _entrypoint_severity(status)
    if severity == 0:
        return

    normalized_status = "blocked" if str(status or "").strip().lower() in {"fail", "blocked"} else "warning"
    payload = {
        "status": normalized_status,
        "key": key,
        "summary": summary,
        "detail": detail,
        "hint": hint,
    }
    existing = issues_by_key.get(key)
    if existing is None or severity > _entrypoint_severity(existing.get("status", "")):
        issues_by_key[key] = payload


def _collect_entrypoint_issues(
    release_snapshot,
    onboarding: dict[str, Any],
    *,
    runtime_error: str | None = None,
    extra_warning: str | None = None,
) -> list[dict[str, Any]]:
    issues_by_key: dict[str, dict[str, Any]] = {}
    for finding in release_snapshot.config_validation.findings:
        _remember_entrypoint_issue(
            issues_by_key,
            status=finding.status,
            key=finding.key,
            summary=finding.summary,
            detail=finding.detail,
            hint=finding.hint,
        )
    for finding in release_snapshot.doctor.findings:
        _remember_entrypoint_issue(
            issues_by_key,
            status=finding.status,
            key=finding.key,
            summary=finding.summary,
            detail=finding.detail,
            hint=finding.hint,
        )
    for finding in release_snapshot.launch_check.findings:
        _remember_entrypoint_issue(
            issues_by_key,
            status=finding.status,
            key=finding.key,
            summary=finding.summary,
            detail=finding.detail,
        )

    if runtime_error:
        _remember_entrypoint_issue(
            issues_by_key,
            status="blocked",
            key="runtime.bootstrap",
            summary="Workspace runtime could not bootstrap for local startup.",
            detail=runtime_error,
        )

    if onboarding.get("available"):
        if not onboarding.get("onboarding_enabled"):
            _remember_entrypoint_issue(
                issues_by_key,
                status="warning",
                key="personal.onboarding",
                summary="Personal onboarding is not enabled yet.",
                hint="Run `fmj onboard personal my_personal_information` when you are ready to enable the personal workflow.",
            )
        if onboarding.get("flagged_item_count"):
            _remember_entrypoint_issue(
                issues_by_key,
                status="warning",
                key="personal.onboarding_flags",
                summary=f"Personal onboarding has {onboarding['flagged_item_count']} flagged item(s) to review.",
            )
    elif onboarding.get("error"):
        _remember_entrypoint_issue(
            issues_by_key,
            status="warning",
            key="personal.onboarding.inspect",
            summary="Personal onboarding state could not be inspected.",
            detail=str(onboarding.get("error")),
        )

    if extra_warning:
        _remember_entrypoint_issue(
            issues_by_key,
            status="warning",
            key="workflow.daily",
            summary=extra_warning,
        )

    return sorted(
        issues_by_key.values(),
        key=lambda item: (-_entrypoint_severity(str(item.get("status") or "")), str(item.get("key") or "")),
    )


def _step_for_issue(issue: dict[str, Any]) -> str:
    hint = str(issue.get("hint") or "").strip()
    if hint:
        return hint

    key = str(issue.get("key") or "")
    mapping = {
        "config.workspace_file": "Run `fmj start` to create the file-first workspace layout.",
        "workspace.config": "Run `fmj start` to create the file-first workspace layout.",
        "workspace.profile": "Review `config/profile.yml` and keep the LM Studio runtime settings correct.",
        "profile.local_user_profile": "Copy `templates/user-profile.local.example.yml` to `.fmj/local-overrides/filefirst/user-profile.yml` and fill in your real local candidate data.",
        "workspace.portals": "Review `portals.yml` and keep the enabled Greenhouse, Lever, and Ashby sources aligned with the launch scope.",
        "profile.facts": "Run `fmj onboard export-file-first --workspace .` to populate file-first facts.",
        "workspace.cv": "Run `fmj onboard export-file-first --workspace .` to populate `cv.md`.",
        "sources.greenhouse": "Enable Greenhouse in `portals.yml` before launch checks.",
        "sources.greenhouse.targets": "Add at least one Greenhouse board, tracked company, or persisted discovery target before running launch checks.",
        "sources.lever": "Enable Lever in `portals.yml` before launch checks.",
        "sources.lever.targets": "Add at least one Lever board, tracked company, or persisted discovery target before running launch checks.",
        "sources.ashby": "Enable Ashby in `portals.yml` before launch checks.",
        "sources.ashby.targets": "Add at least one Ashby board, tracked company, or persisted discovery target before running launch checks.",
        "sources.production_scope": "Ensure only supported sources (greenhouse, lever, ashby) are in production_sources.",
        "production.scope": "Ensure only supported sources (greenhouse, lever, ashby) are in production_sources.",
        "source.greenhouse.enabled": "Enable Greenhouse in `portals.yml` before treating the workspace as launch-ready.",
        "source.greenhouse.targets": "Add at least one Greenhouse board, tracked company, or persisted discovery target before treating the workspace as launch-ready.",
        "source.lever.enabled": "Enable Lever in `portals.yml` before treating the workspace as launch-ready.",
        "source.lever.targets": "Add at least one Lever board, tracked company, or persisted discovery target before treating the workspace as launch-ready.",
        "source.ashby.enabled": "Enable Ashby in `portals.yml` before treating the workspace as launch-ready.",
        "source.ashby.targets": "Add at least one Ashby board, tracked company, or persisted discovery target before treating the workspace as launch-ready.",
        "runtime.typst": "Install `typst` for launch PDF generation and rerun the release gate.",
        "runtime.latex": "LaTeX is not part of the launch path. Switch the workspace back to Typst unless you are debugging a compatibility-only override.",
        "artifacts.renderer": "Keep the active launch renderer on Typst via the bundled templates. Treat LaTeX or HTML-to-PDF as non-launch compatibility paths.",
        "privacy.trace_capture": "Disable trace and DOM capture in `config/profile.yml` for the launch workspace.",
        "runtime.playwright.package": "Install browser support in the project environment with `python -m pip install -e .[models,playwright]`.",
        "runtime.playwright.browser": "Run `python -m playwright install chromium` before browser preview or rehearsal flows.",
        "personal.onboarding": "Run `fmj onboard export-file-first --workspace .` to populate the file-first workspace.",
        "profile.contact_facts": "Run `fmj onboard export-file-first --workspace .` so contact facts exist for resumes and apply flows.",
        "profile.authorization_facts": "Confirm your authorization details are present in the exported file-first facts.",
        "artifacts.cv": "Replace the default `cv.md` placeholder with your exported or canonical CV content.",
        "models.router": "Install the recommended LM Studio split profiles and confirm the local server is reachable.",
        "models.runtime.config": "Set the LM Studio host and model in `config/profile.yml`.",
        "models.launch_profile": "Fix the LM Studio role bindings until the launch profile reports all required roles ready.",
        "automation.enabled": "Enable autonomous mode in `config/profile.yml` or through the Settings API for the release build.",
        "automation.submit_enabled": "Enable submit mode in `config/profile.yml` or through the Settings API for the release build.",
        "runtime.bootstrap": "Fix the workspace blockers and rerun `fmj start`.",
    }
    prefix_mapping = {
        "models.role.": "Reinstall the recommended LM Studio split profiles and confirm the role binding exists in `.fmj/config.toml`.",
        "models.": "Review the LM Studio role bindings and confirm the local server is reachable before retrying.",
        "sources.": "Edit `portals.yml` so the enabled Greenhouse, Lever, and Ashby sources each have truthful configured or persisted targets.",
        "source.": "Edit `portals.yml` so each enabled launch source has truthful configured or persisted targets.",
        "personal.": "Run `fmj onboard export-file-first --workspace .` and review the exported file-first facts.",
    }
    if key in mapping:
        return mapping[key]
    for prefix, step in prefix_mapping.items():
        if key.startswith(prefix):
            return step


def _recommended_next_steps(
    issues: list[dict[str, Any]],
    *,
    mode: str,
    initialized: bool = False,
) -> list[str]:
    steps: list[str] = []
    blocked_present = any(str(issue.get("status") or "") == "blocked" for issue in issues)
    if mode == "start" and initialized:
        steps.append("Use the Dashboard as the main landing page, then run the daily dry-run workflow.")
    elif mode == "start" and blocked_present:
        steps.append("Open the Setup page in the local console to work through the remaining blockers.")
    elif mode == "day" and blocked_present:
        steps.append("Open the Daily or Setup page and resolve the issues that affect today's workflow.")

    for issue in issues:
        step = _step_for_issue(issue)
        if step and step not in steps:
            steps.append(step)

    if not steps:
        if mode == "day":
            steps.append("Open the Daily page, run the daily dry-run, and continue into review as needed.")
        else:
            steps.append("Open the Dashboard and use `fmj day` as the normal daily dry-run entrypoint.")
    return steps[:5]


def _build_missing_workspace_payload(
    *,
    entrypoint: str,
    workspace: Path,
    open_browser: bool,
    host: str,
    port: int,
    page_name: str,
    chrome_debug: bool,
) -> dict[str, Any]:
    open_path = _ENTRYPOINT_PAGE_PATHS[page_name]
    step = "Run `fmj start` to initialize the workspace and open the local console."
    return {
        "entrypoint": entrypoint,
        "status": "blocked",
        "workspace": {
            "path": str(workspace.resolve()),
            "config_path": str(FileWorkspace(workspace.resolve()).profile_path),
        },
        "readiness": {
            "config_validation": None,
            "doctor": None,
            "launch_check": None,
            "launch_profile": None,
        },
        "onboarding": {
            "available": False,
        },
        "issues": [
            {
                "status": "blocked",
                "key": "workspace.config",
                "summary": "Workspace is not initialized yet.",
                "detail": None,
                "hint": step,
            }
        ],
        "next_steps": [step],
        "web_console": {
            "requested": open_browser,
            "launchable": False,
            "launched": False,
            "page": page_name,
            "path": open_path,
            "url": _web_console_url(host, port, open_path),
        },
        "chrome_debug": {
            "requested": chrome_debug,
            "cdp_url": _ENTRYPOINT_CDP_URL,
            "start_url": _ENTRYPOINT_CHROME_START_URL,
            "launched": False,
            "error": None,
        },
        "notes": [],
    }


def _build_entrypoint_payload(
    *,
    entrypoint: str,
    workspace: Path,
    release_snapshot,
    onboarding: dict[str, Any],
    issues: list[dict[str, Any]],
    next_steps: list[str],
    open_browser: bool,
    host: str,
    port: int,
    page_name: str,
    chrome_debug: bool,
    runtime_ready: bool,
    initialization: dict[str, Any] | None = None,
    daily: dict[str, Any] | None = None,
    review: dict[str, Any] | None = None,
    training: dict[str, Any] | None = None,
) -> dict[str, Any]:
    open_path = _ENTRYPOINT_PAGE_PATHS[page_name]
    payload = {
        "entrypoint": entrypoint,
        "status": _entrypoint_status(issues),
        "workspace": {
            "path": str(workspace.resolve()),
            "config_path": str(release_snapshot.config_path),
        },
        "readiness": {
            "config_validation": _report_payload(release_snapshot.config_validation),
            "doctor": _report_payload(release_snapshot.doctor),
            "launch_check": _launch_payload(release_snapshot.launch_check),
            "launch_profile": _model_launch_profile_payload(release_snapshot.launch_profile),
        },
        "onboarding": onboarding,
        "issues": issues,
        "next_steps": next_steps,
        "web_console": {
            "requested": open_browser,
            "launchable": runtime_ready,
            "launched": False,
            "page": page_name,
            "path": open_path,
            "url": _web_console_url(host, port, open_path),
        },
        "chrome_debug": {
            "requested": chrome_debug,
            "cdp_url": _ENTRYPOINT_CDP_URL,
            "start_url": _ENTRYPOINT_CHROME_START_URL,
            "launched": False,
            "error": None,
        },
        "notes": list(release_snapshot.notes[:5]),
    }
    if initialization is not None:
        payload["initialization"] = initialization
    if daily is not None:
        payload["daily"] = {
            "counts": dict(daily.get("counts") or {}),
            "latest_daily_run": daily.get("latest_daily_run"),
        }
    if review is not None:
        payload["review"] = {
            "counts": dict(review.get("counts") or {}),
        }
    if training is not None:
        payload["training"] = {
            "run_id": training.get("run_id"),
            "status": training.get("status"),
            "sampled_count": training.get("sampled_count"),
            "approved_count": training.get("approved_count"),
            "rejected_count": training.get("rejected_count"),
        }
    return payload


def _render_entrypoint_summary(payload: dict[str, Any]) -> None:
    console.print()
    console.rule(f"[bold]{str(payload.get('entrypoint', 'entrypoint')).title()} [{str(payload.get('status', 'ready')).upper()}][/bold]")

    workspace = payload.get("workspace") or {}
    console.print(f"  Workspace: {workspace.get('path')}")

    initialization = payload.get("initialization")
    if initialization is not None:
        console.print(f"  Workspace state: {initialization.get('status')}")
        created_templates = list(initialization.get("created_templates") or [])
        if created_templates:
            console.print(f"  Seeded templates: {', '.join(created_templates)}")

    readiness = payload.get("readiness") or {}
    if readiness.get("config_validation") is not None:
        console.print(f"  Config validation: {readiness['config_validation']['overall_status']}")
    if readiness.get("doctor") is not None:
        console.print(f"  Doctor: {readiness['doctor']['overall_status']}")
    if readiness.get("launch_check") is not None:
        console.print(f"  Launch check: {readiness['launch_check']['overall_status']}")
    if readiness.get("launch_profile") is not None:
        console.print(f"  Model launch profile: {readiness['launch_profile']['overall_status']}")

    onboarding = payload.get("onboarding") or {}
    if onboarding.get("available"):
        console.print(
            "  Personal onboarding: "
            f"{'enabled' if onboarding.get('onboarding_enabled') else 'not enabled'} | "
            f"contact facts={onboarding.get('contact_fact_count', 0)} | "
            f"authorization facts={onboarding.get('authorization_fact_count', 0)}"
        )

    daily = payload.get("daily")
    if daily is not None:
        counts = dict(daily.get("counts") or {})
        console.print(
            "  Daily inbox: "
            f"new={counts.get('new_matching', 0)} | "
            f"review={counts.get('ready_for_review', 0)} | "
            f"needs_input={counts.get('needs_user_input', 0)} | "
            f"approved={counts.get('approved_pending_submit', 0)}"
        )
        latest_daily_run = daily.get("latest_daily_run") or {}
        if latest_daily_run:
            console.print(
                "  Latest daily run: "
                f"{latest_daily_run.get('run_id')} | "
                f"matched={latest_daily_run.get('matching_job_count', 0)} | "
                f"review={latest_daily_run.get('added_to_review_count', 0)}"
            )

    review = payload.get("review")
    if review is not None:
        counts = dict(review.get("counts") or {})
        console.print(
            "  Review queue: "
            f"ready={counts.get('ready_for_review', 0)} | "
            f"needs_input={counts.get('needs_user_input', 0)} | "
            f"approved={counts.get('approved_for_submit', 0)} | "
            f"uncertain={counts.get('submission_uncertain', 0)}"
        )

    training = payload.get("training")
    if training is not None:
        console.print(
            "  Latest training: "
            f"{training.get('status')} | "
            f"approved={training.get('approved_count', 0)} | "
            f"rejected={training.get('rejected_count', 0)}"
        )

    chrome = payload.get("chrome_debug") or {}
    if chrome.get("requested"):
        state = "launched" if chrome.get("launched") else ("failed" if chrome.get("error") else "not launched")
        console.print(f"  Chrome debug: {state} ({chrome.get('cdp_url')})")
        if chrome.get("error"):
            console.print(f"    {chrome.get('error')}")

    web = payload.get("web_console") or {}
    if web.get("requested"):
        state = "starting" if web.get("launched") else ("ready" if web.get("launchable") else "blocked")
        console.print(f"  Web console: {state} at {web.get('url')}")
    elif web.get("launchable"):
        console.print(f"  Web console: available at {web.get('url')}")

    issues = list(payload.get("issues") or [])
    if issues:
        console.print("  Blockers and warnings:")
        for issue in issues[:6]:
            label = "BLOCKED" if issue.get("status") == "blocked" else "WARNING"
            console.print(f"    - {label}: {issue.get('summary')}")

    next_steps = list(payload.get("next_steps") or [])
    if next_steps:
        console.print("  Next steps:")
        for step in next_steps:
            console.print(f"    - {step}")

    notes = list(payload.get("notes") or [])
    if notes:
        console.print("  Notes:")
        for note in notes[:3]:
            console.print(f"    - {note}")
    console.print()


def _maybe_launch_chrome_debug(payload: dict[str, Any]) -> None:
    chrome_payload = payload.get("chrome_debug") or {}
    if not chrome_payload.get("requested"):
        return

    from findmyjob.apply.cdp_session import launch_chrome_debug

    port = httpx.URL(chrome_payload["cdp_url"]).port or 9222
    try:
        launch_chrome_debug(port=port, start_url=str(chrome_payload.get("start_url") or _ENTRYPOINT_CHROME_START_URL))
    except Exception as exc:
        chrome_payload["error"] = str(exc)
        return
    chrome_payload["launched"] = True


def _maybe_launch_chatgpt_startup_browser(payload: dict[str, Any], *, workspace: Path) -> None:
    try:
        app_config = AppConfig.load(workspace)
    except Exception as exc:
        payload.setdefault("notes", []).append(f"ChatGPT browser startup skipped: {exc}")
        return

    if not bool(app_config.chatgpt_drafting.enabled):
        return
    if str(app_config.personal.resume_renderer or "").strip().lower() != "chatgpt_download":
        return
    if not bool(app_config.chatgpt_drafting.launch_if_missing):
        return

    try:
        from findmyjob.filefirst.chatgpt_drafting import ChatGPTDraftingService

        result = ChatGPTDraftingService(workspace).launch_browser(start_url="about:blank")
    except Exception as exc:
        payload.setdefault("notes", []).append(f"ChatGPT browser startup failed: {exc}")
        return

    browser = result.get("browser") or {}
    cdp_url = str(browser.get("browser_cdp_url") or app_config.chatgpt_drafting.browser_cdp_url or "").strip()
    if result.get("launched"):
        note = f"ChatGPT browser ready on {cdp_url or 'configured CDP URL'}."
        extra = str(result.get("note") or "").strip()
        if extra:
            note = f"{note} {extra}"
        payload.setdefault("notes", []).append(note)
        return

    last_error = str(result.get("last_error") or "").strip()
    if last_error:
        payload.setdefault("notes", []).append(f"ChatGPT browser startup warning: {last_error}")


def _sync_web_frontend_or_exit() -> None:
    if str(os.getenv("SKIP_FRONTEND_BUILD") or "").strip().lower() in {"1", "true", "yes"}:
        return
    try:
        sync_frontend_bundle()
    except RuntimeError as exc:
        console.print(f"Web frontend sync failed: {exc}")
        raise typer.Exit(code=1) from exc

@app.command()
def init(workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Initialize workspace configuration and database."""
    ensure_workspace(workspace)
    FileWorkspace(workspace.resolve()).ensure()
    write_default_workspace_config(workspace_config_file(workspace))
    ensure_default_workspace_templates(workspace)
    runtime(workspace)
    console.print(f"Workspace initialized at {workspace}")


@app.command("start")
def start(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the local web console after startup"),
    chrome_debug: bool = typer.Option(False, "--chrome-debug/--no-chrome-debug", help="Launch Chrome with remote debugging on port 9222 for attach-mode training"),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address for the local web console"),
    port: int = typer.Option(8765, "--port", min=1, max=65535, help="Bind port for the local web console"),
    page: str | None = typer.Option(None, "--page", help="Console page to open: dashboard, setup, daily, training, review, runs, settings"),
    json_output: bool = typer.Option(False, "--json", help="Emit startup readiness as JSON without launching the console"),
) -> None:
    """Initialize the workspace if needed and launch the local operator console."""
    root = workspace.resolve()
    initialization = _initialize_workspace_for_start(root)
    release_snapshot = collect_filefirst_release_snapshot(root)
    onboarding = _summarize_onboarding(root)
    issues = _collect_entrypoint_issues(release_snapshot, onboarding)
    blocked_present = any(str(issue.get("status") or "") == "blocked" for issue in issues)
    default_page = "setup" if initialization.get("created") or blocked_present else "dashboard"
    page_name = _normalize_console_page(page, default=default_page)
    payload = _build_entrypoint_payload(
        entrypoint="start",
        workspace=root,
        release_snapshot=release_snapshot,
        onboarding=onboarding,
        issues=issues,
        next_steps=_recommended_next_steps(issues, mode="start", initialized=bool(initialization.get("created"))),
        open_browser=open_browser,
        host=host,
        port=port,
        page_name=page_name,
        chrome_debug=chrome_debug,
        runtime_ready=True,
        initialization=initialization,
    )

    if not json_output:
        _maybe_launch_chrome_debug(payload)
        _maybe_launch_chatgpt_startup_browser(payload, workspace=root)
        if payload["chrome_debug"].get("error"):
            message = "Chrome debug launch failed. Run `fmj greenhouse chrome-debug --launch` or start Chrome manually with remote debugging enabled."
            if message not in payload["next_steps"]:
                payload["next_steps"].insert(0, message)
                payload["next_steps"] = payload["next_steps"][:5]

    if json_output:
        console.print_json(json.dumps(payload, default=str))
        if payload["status"] == "blocked":
            raise typer.Exit(code=1)
        return

    _sync_web_frontend_or_exit()
    payload["web_console"]["launched"] = True
    _render_entrypoint_summary(payload)

    from findmyjob.web.app import run_web_console

    run_web_console(
        workspace=root,
        host=host,
        port=port,
        open_browser=open_browser,
        open_path=payload["web_console"]["path"],
    )


@app.command("day")
def day(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the local web console after the summary"),
    chrome_debug: bool = typer.Option(False, "--chrome-debug/--no-chrome-debug", help="Launch Chrome with remote debugging on port 9222 for attach-mode training"),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address for the local web console"),
    port: int = typer.Option(8765, "--port", min=1, max=65535, help="Bind port for the local web console"),
    page: str = typer.Option("daily", "--page", help="Console page to open: dashboard, setup, daily, training, review, runs, settings"),
    json_output: bool = typer.Option(False, "--json", help="Emit the daily summary as JSON without launching the console"),
) -> None:
    """Summarize readiness and open the normal daily workflow surface."""
    root = workspace.resolve()
    page_name = _normalize_console_page(page, default="daily")
    ws = FileWorkspace(root)
    if not ws.profile_path.exists():
        legacy_config = workspace_config_file(root)
        if legacy_config.exists():
            _initialize_workspace_for_start(root)
        else:
            payload = _build_missing_workspace_payload(
                entrypoint="day",
                workspace=root,
                open_browser=open_browser,
                host=host,
                port=port,
                page_name=page_name,
                chrome_debug=chrome_debug,
            )
            if json_output:
                console.print_json(json.dumps(payload, default=str))
                raise typer.Exit(code=1)
            _render_entrypoint_summary(payload)
            raise typer.Exit(code=1)

    release_snapshot = collect_filefirst_release_snapshot(root)
    onboarding = _summarize_onboarding(root)
    daily_payload, review_payload, training_payload, workflow_error = _collect_day_state(root)
    issues = _collect_entrypoint_issues(
        release_snapshot,
        onboarding,
        extra_warning=workflow_error,
    )
    payload = _build_entrypoint_payload(
        entrypoint="day",
        workspace=root,
        release_snapshot=release_snapshot,
        onboarding=onboarding,
        issues=issues,
        next_steps=_recommended_next_steps(issues, mode="day"),
        open_browser=open_browser,
        host=host,
        port=port,
        page_name=page_name,
        chrome_debug=chrome_debug,
        runtime_ready=True,
        daily=daily_payload,
        review=review_payload,
        training=training_payload,
    )

    if not json_output:
        _maybe_launch_chrome_debug(payload)
        if payload["chrome_debug"].get("error"):
            message = "Chrome debug launch failed. Run `fmj greenhouse chrome-debug --launch` or start Chrome manually with remote debugging enabled."
            if message not in payload["next_steps"]:
                payload["next_steps"].insert(0, message)
                payload["next_steps"] = payload["next_steps"][:5]

    if json_output:
        console.print_json(json.dumps(payload, default=str))
        if payload["status"] == "blocked":
            raise typer.Exit(code=1)
        return

    _sync_web_frontend_or_exit()
    payload["web_console"]["launched"] = True
    _render_entrypoint_summary(payload)

    from findmyjob.web.app import run_web_console

    run_web_console(
        workspace=root,
        host=host,
        port=port,
        open_browser=open_browser,
        open_path=payload["web_console"]["path"],
    )


@app.command()
def version(
    json_output: bool = typer.Option(False, "--json", help="Emit version info as JSON"),
) -> None:
    """Show package version and runtime details."""
    payload = _version_payload()
    if json_output:
        console.print_json(json.dumps(payload))
        return
    console.print(f"findmyjob: {payload['version']}")
    console.print(f"Python: {payload['python']}")
    console.print(f"Executable: {payload['executable']}")


@app.command()
def doctor(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    check_models: bool = typer.Option(True, "--models/--no-models", help="Check model readiness"),
    check_browser: bool = typer.Option(True, "--browser/--no-browser", help="Check Playwright/browser readiness"),
    check_typst: bool = typer.Option(True, "--typst/--no-typst", help="Check Typst/renderer readiness"),
    json_output: bool = typer.Option(False, "--json", help="Emit the report as JSON"),
) -> None:
    """Run comprehensive workspace readiness checks."""
    report = inspect_readiness(
        workspace=workspace,
        check_models=check_models,
        check_browser=check_browser,
        check_typst=check_typst,
    )
    if json_output:
        console.print_json(json.dumps({"report": _report_payload(report)}, default=str))
    else:
        _render_report("Doctor", report)
    if report.blocked_count:
        raise typer.Exit(code=1)


@app.command("launch-check")
def launch_check(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    export: Path | None = typer.Option(None, "--export", help="Optional release-snapshot export path"),
    json_output: bool = typer.Option(False, "--json", help="Emit the launch-check report as JSON"),
) -> None:
    """Run launch acceptance checks."""
    snapshot = collect_release_snapshot(workspace)
    report = snapshot.launch_check
    if export is not None:
        exported = _write_release_snapshot(export, snapshot)
        console.print(f"Release snapshot exported: {exported}")
    if json_output:
        console.print_json(json.dumps(_launch_payload(report), default=str))
    else:
        _render_launch_report(report)
    if report.fail_count:
        raise typer.Exit(code=1)


@support_app.command('bundle')
def support_bundle(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    application_id: list[str] | None = typer.Option(None, "--application-id", help="Specific application(s) to inspect"),
    include_artifact_paths: bool = typer.Option(False, "--include-artifact-paths", help="Include artifact paths in the support bundle"),
    include_sensitive_artifacts: bool = typer.Option(False, "--include-sensitive-artifacts", help="Include sensitive artifact references in the support bundle"),
    export: Path | None = typer.Option(None, "--export", help="Optional JSON export path"),
    json_output: bool = typer.Option(False, "--json", help="Emit the bundle as JSON"),
) -> None:
    """Collect a support bundle with workspace diagnostics."""
    rt = runtime(workspace)
    bundle = collect_support_bundle(
        workspace=workspace,
        runtime=rt,
        application_ids=list(application_id or []),
        include_artifact_paths=include_artifact_paths,
        include_sensitive_artifacts=include_sensitive_artifacts,
    )
    payload = _support_bundle_payload(bundle)
    if json_output:
        console.print_json(json.dumps(payload, default=str))
        return
    exported = _write_json_payload(export or _default_export_path(workspace, "support-bundle"), payload)
    _render_support_bundle(bundle, exported_path=exported)


@app.command("cleanup")
def cleanup(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    apply: bool = typer.Option(False, "--apply", help="Actually delete expired files"),
    json_output: bool = typer.Option(False, "--json", help="Emit the cleanup report as JSON"),
) -> None:
    """Clean up expired workspace artifacts."""
    report = cleanup_workspace(workspace, apply=apply)
    if json_output:
        console.print_json(json.dumps(_cleanup_payload(report), default=str))
        return
    _render_cleanup_report(report)


@app.command()
def tui(workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Launch the terminal user interface."""
    from findmyjob.tui.app import run_tui
    run_tui(workspace)



@app.command("web")
def web_console(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address for the local web console"),
    port: int = typer.Option(8765, "--port", min=1, max=65535, help="Bind port for the local web console"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the browser to the dashboard after startup"),
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
) -> None:
    """Launch the local web operator console."""
    from findmyjob.web.app import run_web_console

    _sync_web_frontend_or_exit()
    console.print(f"Starting local web console at http://{host}:{port}")
    run_web_console(workspace=workspace, host=host, port=port, open_browser=open_browser, open_path="/")
@db_app.command("upgrade")
def db_upgrade(workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Upgrade the database to the latest migration."""
    rt = runtime(workspace)
    upgrade_database(rt.config.database_path(rt.workspace))
    console.print("Database upgraded.")


@db_app.command("current")
def db_current(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    json_output: bool = typer.Option(False, "--json", help="Emit revision as JSON"),
) -> None:
    """Show the current database migration revision."""
    rt = runtime(workspace)
    rev = current_revision(rt.config.database_path(rt.workspace))
    if json_output:
        console.print_json(json.dumps({"revision": rev}))
    else:
        console.print(f"Current revision: {rev or 'none'}")



@db_app.command("reset-operational")
def db_reset_operational(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    json_output: bool = typer.Option(False, "--json", help="Emit deleted row counts as JSON"),
) -> None:
    """Reset file-first operational state while preserving profile, portals, facts, answer memory, templates, and CV."""
    from findmyjob.filefirst.service import FileFirstOperatorService

    payload = FileFirstOperatorService(workspace).reset_operational_state_payload()
    if json_output:
        console.print_json(json.dumps(payload, default=str))
        return
    console.print("Operational state reset.")
    console.print("Removed:")
    for table_name, count in payload["deleted"].items():
        console.print(f"  {table_name}: {count}")
    console.print("Preserved:")
    for label, path in payload["preserved"].items():
        console.print(f"  {label}: {path}")
@config_app.command("show")
def config_show(workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Show the current application configuration."""
    rt = runtime(workspace)
    console.print_json(json.dumps(rt.config.model_dump(mode="json"), indent=2))


@config_app.command("validate")
def config_validate(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    json_output: bool = typer.Option(False, "--json", help="Emit the validation report as JSON"),
) -> None:
    """Validate workspace configuration files."""
    _config, report = inspect_app_config(workspace)
    if json_output:
        console.print_json(json.dumps(_report_payload(report), default=str))
    else:
        _render_report("Config Validation", report)
    if report.blocked_count:
        raise typer.Exit(code=1)


@models_app.command("list")
def models_list(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
) -> None:
    """List configured models and their roles."""
    rt = runtime(workspace)
    router = ModelRouter(rt.config)
    for role in ModelRole:
        try:
            profile = router.get_profile(role=role)
        except ValueError:
            profile = None
        if profile is None:
            console.print(f"  {role.value}: (not configured)")
        else:
            console.print(f"  {role.value}: {profile.provider}/{profile.model} [{profile.name}]")


@models_app.command("launch-profile")
def models_launch_profile(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    json_output: bool = typer.Option(False, "--json", help="Emit the profile as JSON"),
) -> None:
    """Show the model launch profile."""
    rt = runtime(workspace)
    report = rt.model_router.inspect_launch_profile()
    if json_output:
        payload = report.model_dump(mode="json")
        payload["fail_count"] = report.fail_count
        payload["warning_count"] = report.warning_count
        payload["overall_status"] = report.overall_status
        console.print_json(json.dumps(payload, default=str))
        return
    _render_model_launch_profile(report)


@models_app.command("auto-detect")
def models_auto_detect(workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Auto-detect available models and report likely bindings."""
    rt = runtime(workspace)
    router = ModelRouter(rt.config)
    detected = anyio.run(router.auto_detect_profiles)
    if not detected.get("ok"):
        console.print(f"Auto-detect failed: {detected.get('error')}")
        raise typer.Exit(code=1)
    profiles = list(detected.get("profiles") or [])
    console.print(f"Detected {len(profiles)} candidate profile(s)")
    for profile in profiles:
        console.print(f"  {profile.get('role')}: {profile.get('provider')}/{profile.get('model')} [{profile.get('name')}]")


@models_app.command("test")
def models_test(
    role: str = typer.Option("writer", "--role", help="Model role to test"),
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
) -> None:
    """Test a model role with a simple prompt."""
    rt = runtime(workspace)
    router = ModelRouter(rt.config)
    role_enum = ModelRole(role)
    try:
        profile = router.get_profile(role=role_enum)
    except ValueError:
        console.print(f"No model configured for role: {role}")
        raise typer.Exit(code=1)
    console.print(f"Testing {profile.provider}/{profile.model} [{profile.name}]...")
    try:
        result = anyio.run(router.generate_text, role_enum, "Say hello in one sentence.")
        console.print(f"Response: {result}")
    except Exception as exc:
        console.print(f"Error: {exc}")
        raise typer.Exit(code=1) from exc


@models_app.command("bind")
def models_bind(
    role: str = typer.Option("writer", "--role", help="Model role to bind"),
    provider: str = typer.Option(..., "--provider", help="Model provider"),
    name: str = typer.Option(..., "--name", help="Model name"),
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
) -> None:
    """Bind a model to a role in the configuration."""
    console.print(f"Binding {provider}/{name} to role {role}")

@profile_app.command("list")
def profile_list(workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """List profile facts."""
    rt = runtime(workspace)
    with rt.session_scope() as session:
        facts = ProfileRepository(session).list_facts()
    _render_personal_fact_list(facts)


@profile_app.command("import")
def profile_import(file: Path, workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Import profile facts from a YAML file."""
    rt = runtime(workspace)
    payload = yaml.safe_load(file.read_text(encoding="utf-8") or [])
    with rt.session_scope() as session:
        repo = ProfileRepository(session)
        for fact in payload:
            repo.upsert_fact(fact)
    console.print(f"Imported {len(payload)} facts from {file}")


@profile_app.command("export")
def profile_export(file: Path, workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Export profile facts to a YAML file."""
    rt = runtime(workspace)
    with rt.session_scope() as session:
        payload = [
            fact.model_dump(mode="json")
            for fact in ProfileRepository(session).list_facts()
        ]
    file.write_text(yaml.dump(payload), encoding="utf-8")
    console.print(f"Exported {len(payload)} facts to {file}")


@onboard_app.command("personal")
def onboard_personal(source_dir: Path, workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Run personal onboarding from a source directory."""
    run_personal_onboarding(workspace, source_dir)
    console.print("Personal onboarding complete.")


@onboard_app.command("inspect")
def onboard_inspect(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    json_output: bool = typer.Option(False, "--json", help="Emit the onboarding inspection as JSON"),
) -> None:
    """Inspect imported personal onboarding data."""
    report = inspect_personal_onboarding(workspace)
    payload = dict(report)
    if json_output:
        console.print_json(json.dumps(payload, default=str))
        return
    console.print_json(json.dumps(payload, indent=2, default=str))


@discover_app.command("run")
def discover_run(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
) -> None:
    """Run job discovery."""
    rt = runtime(workspace)
    async def _run():
        return await Orchestrator(rt).run_discovery()
    run_id = anyio.run(_run)
    console.print(f"Discovery run started: {run_id}")


@prepare_app.command("run")
def prepare_run(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
) -> None:
    """Run application preparation."""
    rt = runtime(workspace)
    async def _run():
        return await Orchestrator(rt).run_prepare()
    run_id = anyio.run(_run)
    console.print(f"Prepare run started: {run_id}")


@apply_app.command("dry-run")
def apply_dry_run(workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Run application submission in dry-run mode."""
    rt = runtime(workspace)
    async def _run():
        return await Orchestrator(rt).run_apply(mode=ApplicationMode.DRY_RUN)
    run_id = anyio.run(_run)
    console.print(f"Apply dry-run started: {run_id}")


@apply_app.command("inspect")
def apply_inspect(job_id: str, workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Inspect a submission plan for a job."""
    rt = runtime(workspace)
    async def _run():
        return await Orchestrator(rt).inspect_submission_plan(job_id)
    plan = anyio.run(_run)
    if plan:
        console.print_json(json.dumps(plan.model_dump(mode="json"), indent=2))
    else:
        console.print(f"No submission plan found for job {job_id}")


@apply_app.command("inspect-result")
def apply_inspect_result(application_id: str, workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Inspect a submission result."""
    rt = runtime(workspace)
    async def _run():
        return await Orchestrator(rt).inspect_submission_result(application_id)
    result = anyio.run(_run)
    if result:
        console.print_json(json.dumps(result, indent=2, default=str))
    else:
        console.print(f"No submission result found for application {application_id}")


@apply_app.command("submit")
def apply_submit(
    application_id: str,
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
) -> None:
    """Submit a single application."""
    rt = runtime(workspace)
    async def _run():
        return await Orchestrator(rt).run_apply_for_application(application_id, mode=ApplicationMode.AUTO_SUBMIT)
    run_id = anyio.run(_run)
    console.print(f"Submit run started: {run_id}")


@jobs_app.command("list")
def jobs_list(workspace: Path = typer.Option(Path.cwd(), help="Workspace root"), limit: int = 25) -> None:
    """List recent jobs."""
    rt = runtime(workspace)
    with rt.session_scope() as session:
        repo = JobRepository(session)
        jobs = repo.list_recent(limit=limit)
    table = Table(title="Recent Jobs")
    table.add_column("ID")
    table.add_column("Title")
    table.add_column("Company")
    table.add_column("Location")
    for job in jobs:
        table.add_row(str(job.id), job.title or "-", job.company or "-", job.location or "-")
    console.print(table)


@review_app.command("queue")
def review_queue(workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Show applications pending review."""
    rt = runtime(workspace)
    with rt.session_scope() as session:
        repo = ApplicationRepository(session)
        pending = repo.list_applications(statuses=["ready_for_review", "needs_user_input"])
    table = Table(title="Review Queue")
    table.add_column("Application ID")
    table.add_column("Job ID")
    table.add_column("Status")
    for app in pending:
        table.add_row(str(app.application_id), str(app.job_id), str(app.status))
    console.print(table)
    console.print(f"Total pending: {len(pending)}")


@review_app.command("approve")
def review_approve(application_id: str, workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Approve an application for submission."""
    rt = runtime(workspace)
    async def _run():
        return await Orchestrator(rt).review_action(application_id, ReviewStatus.APPROVED)
    result = anyio.run(_run)
    console.print(result)


@review_app.command("reject")
def review_reject(application_id: str, workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Reject an application."""
    rt = runtime(workspace)
    async def _run():
        return await Orchestrator(rt).review_action(application_id, ReviewStatus.REJECTED)
    result = anyio.run(_run)
    console.print(result)


@review_app.command("request-input")
def review_request_input(application_id: str, workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Request user input for an application."""
    rt = runtime(workspace)
    async def _run():
        return await Orchestrator(rt).review_action(application_id, ReviewStatus.NEEDS_USER_INPUT)
    result = anyio.run(_run)
    console.print(result)


@review_app.command("handoff")
def review_handoff(application_id: str, reason: str = typer.Option("Manual handoff requested"), workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Handoff an application for manual review."""
    rt = runtime(workspace)
    async def _run():
        return await Orchestrator(rt).review_action(application_id, ReviewStatus.HANDOFF, reason=reason)
    result = anyio.run(_run)
    console.print(result)


@review_app.command("answer")
def review_answer(question: list[str], workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Answer queued application questions."""
    rt = runtime(workspace)
    async def _run():
        return await Orchestrator(rt).answer_questions(question)
    answers = anyio.run(_run)
    for answer in answers:
        console.print(f"  Q: {answer.question}")
        console.print(f"  A: {answer.answer}")


@ledger_app.command("export")
def ledger_export(workspace: Path = typer.Option(Path.cwd(), help="Workspace root"), output: Path = typer.Option(Path(".fmj/exports/ledger"))) -> None:
    """Export the application ledger."""
    from findmyjob.ledger.export import export_ledger
    rt = runtime(workspace)
    export_ledger(rt, output)
    console.print(f"Ledger exported to {output}")


@runs_app.command("resume")
def runs_resume(run_id: str, workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Resume a paused run."""
    rt = runtime(workspace)
    async def _run():
        return await Orchestrator(rt).resume_run(run_id)
    anyio.run(_run)
    console.print(f"Run {run_id} resumed.")


@sources_app.command("probe")
def sources_probe(workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Probe all configured sources for availability."""
    rt = runtime(workspace)
    results = Orchestrator(rt).probe_sources()
    for result in results:
        console.print(f"  {result['name']}: {result['status']}")


@sources_app.command("live-check")
def sources_live_check(workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Live-check all configured sources."""
    rt = runtime(workspace)
    async def _run():
        return await Orchestrator(rt).live_check_sources()
    results = anyio.run(_run)
    for result in results:
        console.print(f"  {result['name']}: {result['status']}")


@greenhouse_app.command("discover-boards")
def greenhouse_discover_boards(workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Discover Greenhouse boards."""
    rt = runtime(workspace)
    async def _run():
        return await GreenhouseScaleOrchestrator(rt).discover_boards()
    boards = anyio.run(_run)
    for board in boards:
        console.print(f"  {board['token']}: {board['name']}")


@greenhouse_app.command("sync")
def greenhouse_sync(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    board_token: str = typer.Option(..., "--board", help="Board token to sync"),
) -> None:
    """Sync jobs from a Greenhouse board."""
    rt = runtime(workspace)
    async def _run():
        return await GreenhouseScaleOrchestrator(rt).sync_boards(board_tokens=[board_token])
    result = anyio.run(_run)
    console.print(f"Sync complete: {result}")


@greenhouse_app.command("validate-board")
def greenhouse_validate_board(board_token: str, workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Validate a Greenhouse board."""
    rt = runtime(workspace)
    async def _run():
        return await GreenhouseScaleOrchestrator(rt).validate_board(board_token)
    result = anyio.run(_run)
    console.print(f"Board valid: {result}")


@greenhouse_app.command("smoke-results")
def greenhouse_smoke_results(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    limit: int = typer.Option(10, "--limit", min=1, max=100, help="Max results to show"),
    json_output: bool = typer.Option(False, "--json", help="Emit results as JSON"),
) -> None:
    """Show recorded smoke test results."""
    results = list_recorded_smoke_results(workspace, limit=limit)
    if json_output:
        payload = [r.model_dump(mode="json") for r in results]
        console.print_json(json.dumps(payload, default=str))
        return
    _render_smoke_results(results)


@greenhouse_app.command("benchmark")
def greenhouse_benchmark(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    board_token: str = typer.Option(..., "--board", help="Board token to benchmark"),
) -> None:
    """Run a Greenhouse benchmark."""
    rt = runtime(workspace)
    async def _run():
        return await GreenhouseScaleOrchestrator(rt).run_benchmark(board_token)
    result = anyio.run(_run)
    console.print(f"Benchmark complete: {result}")


@greenhouse_app.command("benchmark-results")
def greenhouse_benchmark_results(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    limit: int = typer.Option(10, "--limit", min=1, max=100, help="Max results to show"),
    json_output: bool = typer.Option(False, "--json", help="Emit results as JSON"),
) -> None:
    """Show recorded benchmark results."""
    rt = runtime(workspace)
    summaries = GreenhouseScaleOrchestrator(rt).list_benchmarks(limit=limit)
    if json_output:
        console.print_json(json.dumps([summary.model_dump(mode="json") for summary in summaries], default=str))
        return
    _render_benchmark_summaries(summaries)


@greenhouse_app.command("smoke-test")
def greenhouse_smoke_test(
    board_token: str,
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    job_id: str = typer.Option(None, "--job-id", "--source-job", help="Source job ID to test"),
    confirm: bool = typer.Option(False, "--confirm", help="Confirm execution"),
) -> None:
    """Run a Greenhouse smoke test."""
    rt = runtime(workspace)
    async def _run():
        return await Orchestrator(rt).run_greenhouse_smoke_test(board_token, job_id, confirm=confirm)
    result = anyio.run(_run)
    console.print_json(json.dumps(result.model_dump(mode="json"), default=str))
    if result.status != "pass":
        raise typer.Exit(code=1)


@greenhouse_app.command("train")
def greenhouse_train(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    url: str = typer.Option("https://my.greenhouse.io/jobs", "--url", help="My Greenhouse jobs page to reuse for attach-mode training"),
    run_id: str | None = typer.Option(None, "--run-id", help="Resume a specific training run"),
    batch_size: int = typer.Option(5, "--batch-size", min=1, max=20, help="Jobs per batch"),
    posted_window: int = typer.Option(10, "--posted-window", help="Posted window in days (supported: 1, 5, 10, 30)"),
    keep_tabs_open: bool = typer.Option(False, "--keep-tabs-open", "--keep-tabs", help="Keep training-created tabs open after the run"),
    cdp_url: str = typer.Option("http://127.0.0.1:9222", "--cdp-url", help="Chrome CDP URL"),
    json_output: bool = typer.Option(False, "--json", help="Emit the training summary as JSON"),
) -> None:
    """Review-first training mode: inspect My Greenhouse jobs, generate artifacts, and collect feedback."""
    from findmyjob.personal.training import run_greenhouse_training

    rt = runtime(workspace)

    def _terminal_prompt(job_summary, artifact_paths) -> tuple[bool, str | None, str | None]:
        console.print()
        console.rule(f"[bold]Review: {job_summary.company_name or 'Unknown'} - {job_summary.job_title or 'Untitled'}[/bold]")
        console.print(f"  View URL: {job_summary.job_url}")
        if job_summary.company_page_url:
            console.print(f"  Company page: {job_summary.company_page_url}")
        if job_summary.apply_url:
            console.print(f"  Apply page: {job_summary.apply_url}")
        if artifact_paths.get("resume_pdf_path"):
            console.print(f"  Resume PDF: {artifact_paths['resume_pdf_path']}")
        if artifact_paths.get("cover_letter_pdf_path"):
            console.print(f"  Cover Letter PDF: {artifact_paths['cover_letter_pdf_path']}")
        if job_summary.draft_change_summary:
            console.print(f"  Draft summary: {job_summary.draft_change_summary}")
        if job_summary.layout_notes:
            for note in job_summary.layout_notes[:5]:
                console.print(f"  Note: {note}")
        console.print("  Rejection codes: job_fit, company_fit, missing_evidence, tone, formatting, navigation_issue, other")
        console.print()
        try:
            decision = input("Approve? (y/n/skip): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False, "rejected_by_user", "Operator interrupted review."
        if decision in ("y", "yes"):
            return True, None, None
        if decision in ("skip", "s", ""):
            return False, "rejected_by_user", "Skipped by operator."
        try:
            reason = input("Reason code: ").strip() or "other"
        except (EOFError, KeyboardInterrupt):
            reason = "rejected_by_user"
        try:
            note = input("Optional note: ").strip()
        except (EOFError, KeyboardInterrupt):
            note = ""
        return False, reason, note or None

    async def _run_training():
        return await run_greenhouse_training(
            rt,
            url=url,
            batch_size=batch_size,
            posted_window=posted_window,
            keep_tabs_open=keep_tabs_open,
            cdp_url=cdp_url,
            prompt_fn=_terminal_prompt,
            run_id=run_id,
        )

    try:
        result = anyio.run(_run_training)
    except Exception as exc:
        console.print(f"Training failed: {exc}")
        raise typer.Exit(code=1) from exc

    if json_output:
        console.print_json(json.dumps(result.model_dump(mode="json"), default=str))
        return

    console.print(f"Training run complete: {result.run_id}")
    console.print(
        f"  Sampled: {len(result.sampled_jobs)} | Approved: {result.approved_count} | Rejected: {result.rejected_count}"
    )
    console.print(f"  Promoted applications: {len(result.promoted_application_ids)}")
    for note in result.notes[:5]:
        console.print(f"  Note: {note}")


@greenhouse_app.command("chrome-debug")
def greenhouse_chrome_debug(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    cdp_url: str = typer.Option("http://127.0.0.1:9222", "--cdp-url", help="Chrome CDP URL"),
    launch: bool = typer.Option(False, "--launch", help="Launch Chrome with remote debugging"),
    url: str = typer.Option("https://my.greenhouse.io/jobs", "--url", help="Start URL for the attach-mode Chrome window"),
) -> None:
    """Print or launch Chrome with remote debugging for attach-mode training."""
    from findmyjob.apply.cdp_session import chrome_debug_instructions, launch_chrome_debug

    _ = workspace
    port = httpx.URL(cdp_url).port or 9222
    if launch:
        try:
            launch_chrome_debug(port=port, start_url=url)
            console.print(f"Chrome launched for attach mode on {cdp_url}")
            console.print(chrome_debug_instructions(port=port))
            return
        except Exception as exc:
            console.print(f"Failed to launch Chrome: {exc}")
            raise typer.Exit(code=1) from exc

    console.print(chrome_debug_instructions(port=port))


@auto_app.command('tick')
def auto_tick(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
) -> None:
    """Run a single autonomous tick."""
    from findmyjob.web.service import OperatorConsoleService

    result = OperatorConsoleService(workspace).run_autonomous()
    console.print(f"Tick complete: {result.get('run_id')}")


@auto_app.command('run')
def auto_run(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
) -> None:
    """Run the autonomous loop."""
    from findmyjob.web.service import OperatorConsoleService

    result = OperatorConsoleService(workspace).run_autonomous()
    console.print(f"Autonomous loop complete: {result.get('run_id')}")


@auto_app.command('status')
def auto_status(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
) -> None:
    """Show autonomous status."""
    from findmyjob.web.service import OperatorConsoleService

    payload = OperatorConsoleService(workspace).autonomous_status_payload()
    _render_autonomous_status(payload)


@auto_app.command('retry-pending')
def auto_retry_pending(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
) -> None:
    """Retry pending autonomous tasks."""
    from findmyjob.web.service import OperatorConsoleService

    result = OperatorConsoleService(workspace).run_autonomous()
    console.print(f"Retried pending applications via run {result.get('run_id')}.")


@auto_app.command('reset-backoff')
def auto_reset_backoff(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
) -> None:
    """Clear legacy board backoff state."""
    _ = workspace
    console.print("Board backoff is legacy-only in the file-first production path.")


@questions_app.command('queue')
def questions_queue(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    json_output: bool = typer.Option(False, "--json", help="Emit queue as JSON"),
) -> None:
    """Show queued questions."""
    rt = runtime(workspace)
    items = list_question_queue(rt)
    if json_output:
        payload = [item.model_dump(mode="json") for item in items]
        console.print_json(json.dumps(payload, default=str))
        return
    _render_question_queue(items)


@questions_app.command('answer-next')
def questions_answer_next(
    answer: str = typer.Option(..., '--answer', help='Answer text'),
    workspace: Path = typer.Option(Path.cwd(), help='Workspace root'),
) -> None:
    """Answer the next queued question."""
    rt = runtime(workspace)
    result = anyio.run(answer_next_question, rt, answer)
    console.print(f"Answered: {result}")

@questions_app.command('answer')
def questions_answer(
    application_id: str,
    question_id: str,
    answer: str = typer.Option(..., "--answer", help="Answer text"),
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    json_output: bool = typer.Option(False, "--json", help="Emit result as JSON"),
) -> None:
    """Answer a specific queued question."""
    rt = runtime(workspace)
    result = answer_queued_question(rt, application_id, question_id, answer)
    if json_output:
        console.print_json(json.dumps(result.model_dump(mode="json"), default=str))
        return
    console.print(f"Answered: {result}")


@questions_app.command('approve-memory')
def questions_approve_memory(
    application_id: str,
    question_id: str,
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    json_output: bool = typer.Option(False, "--json", help="Emit result as JSON"),
) -> None:
    """Approve memory for a question."""
    rt = runtime(workspace)
    result = approve_question_memory(rt, application_id, question_id)
    if json_output:
        console.print_json(json.dumps(result.model_dump(mode="json"), default=str))
        return
    console.print("Memory approved.")


@boards_app.command("list")
def boards_list(workspace: Path = typer.Option(Path.cwd(), help="Workspace root"), active_only: bool = typer.Option(False), limit: int = 100) -> None:
    """List configured boards."""
    rt = runtime(workspace)
    with rt.session_scope() as session:
        repo = BoardRepository(session)
        boards = repo.list_boards(active_only=active_only, limit=limit)
    table = Table(title="Boards")
    table.add_column("Token")
    table.add_column("Name")
    table.add_column("Status")
    for board in boards:
        table.add_row(board.token, board.name or "-", board.status or "-")
    console.print(table)


@boards_app.command("inspect")
def boards_inspect(board_token: str, workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Inspect a board."""
    rt = runtime(workspace)
    with rt.session_scope() as session:
        repo = BoardRepository(session)
        board = repo.get_by_token(board_token)
    if board:
        console.print_json(json.dumps(board.model_dump(mode="json"), indent=2))
    else:
        console.print(f"Board not found: {board_token}")


@boards_app.command("import")
def boards_import(file: Path, workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Import boards from a YAML file."""
    rt = runtime(workspace)
    payload = yaml.safe_load(file.read_text(encoding="utf-8") or [])
    with rt.session_scope() as session:
        repo = BoardRepository(session)
        for board in payload:
            repo.upsert_board(board)
    console.print(f"Imported {len(payload)} boards from {file}")


def _saved_search_model(rt: AppRuntime, reference: str, *, touch: bool = False) -> SavedSearch | None:
    with rt.session_scope() as session:
        repo = SavedSearchRepository(session)
        if touch:
            try:
                record = repo.touch_last_used(reference)
            except ValueError:
                return None
        else:
            record = repo.get_by_reference(reference)
        return repo.to_model(record) if record is not None else None

def _build_job_search_query(
    query: str | None = None,
    title: str | None = None,
    company: str | None = None,
    location: str | None = None,
    country: str | None = None,
    remote_only: bool = False,
    workplace: list[str] | None = None,
    experience: list[str] | None = None,
    sponsorship: list[str] | None = None,
    company_size: list[str] | None = None,
    posted_within_days: int | None = None,
    compensation_min: int | None = None,
    limit: int = 25,
) -> JobSearchQuery:
    """Build a job search query from CLI arguments."""
    keywords: list[str] = []
    if title:
        keywords.extend(title.split())

    workplace_types = []
    if workplace:
        for value in workplace:
            try:
                workplace_types.append(WorkplaceType(value))
            except ValueError:
                console.print(f"Warning: Unknown workplace type: {value}")

    experience_levels = []
    if experience:
        for value in experience:
            try:
                experience_levels.append(ExperienceLevel(value))
            except ValueError:
                console.print(f"Warning: Unknown experience level: {value}")

    company_size_buckets = []
    if company_size:
        for value in company_size:
            try:
                company_size_buckets.append(CompanySizeBucket(value))
            except ValueError:
                console.print(f"Warning: Unknown company size bucket: {value}")

    sponsorship_fit = None
    if sponsorship:
        candidate = sponsorship[0]
        try:
            sponsorship_fit = SponsorshipFit(candidate).value
        except ValueError:
            console.print(f"Warning: Unknown sponsorship fit: {candidate}")

    _ = company
    return JobSearchQuery(
        keyword=query,
        title_keywords=keywords,
        locations=[location] if location else [],
        countries=[country] if country else [],
        remote_only=remote_only,
        workplace_types=workplace_types,
        experience_levels=experience_levels,
        sponsorship_fit=sponsorship_fit,
        company_size_buckets=company_size_buckets,
        posted_within_days=posted_within_days,
        compensation_min=compensation_min,
        limit=limit,
    )


def _print_job_search_results(rt: AppRuntime, query: JobSearchQuery) -> None:
    table = Table(title="Job Search")
    table.add_column("ID")
    table.add_column("Title")
    table.add_column("Company")
    table.add_column("Location")
    table.add_column("Workplace")
    table.add_column("Experience")
    table.add_column("Sponsorship")
    table.add_column("Posted")
    with rt.session_scope() as session:
        jobs = search_jobs(session, query)
        for job in jobs:
            company_name = job.company.display_name if getattr(job, "company", None) is not None else "-"
            table.add_row(
                str(job.id),
                job.title or "-",
                company_name,
                _format_job_location(job),
                str(getattr(job, "workplace_type", None) or "-"),
                str(getattr(job, "experience_level", None) or "-"),
                str(getattr(job, "notes", {}).get("sponsorship_fit") or "-"),
                job.posted_at.isoformat() if getattr(job, "posted_at", None) else "-",
            )
    console.print(table)
    console.print(f"Found {len(jobs)} jobs")


def _format_job_location(job: JobPosting) -> str:
    parts = []
    if getattr(job, "city", None):
        parts.append(job.city)
    if getattr(job, "region_code", None):
        parts.append(job.region_code)
    if getattr(job, "country_code", None):
        parts.append(job.country_code)
    if parts:
        return ", ".join(parts)
    return getattr(job, "location_raw", None) or "-"


def _format_job_compensation(job: JobPosting) -> str:
    parts = []
    if getattr(job, "compensation_min", None):
        parts.append(f"${job.compensation_min:,}")
    if getattr(job, "compensation_max", None):
        parts.append(f"${job.compensation_max:,}")
    return " - ".join(parts) or "-"


@jobs_app.command("search")
def jobs_search(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    query: str | None = typer.Option(None, "--query", "-q", help="Free-text search query"),
    saved_search: str | None = typer.Option(None, "--saved-search", help="Reuse a saved search by name or id"),
    title: str | None = typer.Option(None, "--title", help="Job title keywords"),
    company: str | None = typer.Option(None, "--company", help="Company name"),
    location: str | None = typer.Option(None, "--location", help="Location"),
    country: str | None = typer.Option(None, "--country", help="Country code"),
    remote_only: bool = typer.Option(False, "--remote-only", help="Remote jobs only"),
    workplace: list[str] | None = typer.Option(None, "--workplace", help="Workplace types"),
    experience: list[str] | None = typer.Option(None, "--experience", "--experience-level", help="Experience levels"),
    sponsorship: list[str] | None = typer.Option(None, "--sponsorship", help="Sponsorship fits"),
    company_size: list[str] | None = typer.Option(None, "--company-size", help="Company size buckets"),
    posted_within_days: int | None = typer.Option(None, "--posted-within-days", help="Posted within N days"),
    compensation_min: int | None = typer.Option(None, "--compensation-min", help="Minimum compensation"),
    limit: int = typer.Option(25, "--limit", min=1, max=200, help="Max results to show"),
) -> None:
    """Search for jobs."""
    rt = runtime(workspace)
    if saved_search:
        search = _saved_search_model(rt, saved_search, touch=True)
        if search is None:
            console.print(f"Saved search not found: {saved_search}")
            raise typer.Exit(code=1)
        search_query = search.query
    else:
        search_query = _build_job_search_query(
            query=query,
            title=title,
            company=company,
            location=location,
            country=country,
            remote_only=remote_only,
            workplace=workplace,
            experience=experience,
            sponsorship=sponsorship,
            company_size=company_size,
            posted_within_days=posted_within_days,
            compensation_min=compensation_min,
            limit=limit,
        )
    _print_job_search_results(rt, search_query)


@searches_app.command("list")
def searches_list(workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """List saved searches."""
    rt = runtime(workspace)
    with rt.session_scope() as session:
        repo = SavedSearchRepository(session)
        searches = repo.list_models()
    table = Table(title="Saved Searches")
    table.add_column("Reference")
    table.add_column("Name")
    table.add_column("Default")
    table.add_column("Last Used")
    for search in searches:
        reference = search.id or search.name
        table.add_row(
            reference,
            search.name or "-",
            "yes" if search.is_default else "no",
            search.last_used_at.isoformat() if search.last_used_at else "-",
        )
    console.print(table)


@searches_app.command("show")
def searches_show(reference: str, workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Show a saved search."""
    rt = runtime(workspace)
    search = _saved_search_model(rt, reference)
    if search:
        payload = search.model_dump(mode="json")
        payload["reference"] = search.id or search.name
        console.print_json(json.dumps(payload, indent=2, default=str))
    else:
        console.print(f"Search not found: {reference}")


@searches_app.command("save")
def searches_save(
    reference: str,
    name: str | None = typer.Option(None, "--name", help="Search name"),
    query: str | None = typer.Option(None, "--query", "-q", help="Free-text search query"),
    title: str | None = typer.Option(None, "--title", "--title-keyword", help="Job title keywords"),
    company: str | None = typer.Option(None, "--company", help="Company name"),
    location: str | None = typer.Option(None, "--location", help="Location"),
    country: str | None = typer.Option(None, "--country", help="Country code"),
    remote_only: bool = typer.Option(False, "--remote-only", help="Remote jobs only"),
    workplace: list[str] | None = typer.Option(None, "--workplace", help="Workplace types"),
    experience: list[str] | None = typer.Option(None, "--experience", help="Experience levels"),
    sponsorship: list[str] | None = typer.Option(None, "--sponsorship", help="Sponsorship fits"),
    company_size: list[str] | None = typer.Option(None, "--company-size", help="Company size buckets"),
    default: bool = typer.Option(False, "--default", help="Mark as the default saved search"),
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
) -> None:
    """Save a search."""
    rt = runtime(workspace)
    search_query = _build_job_search_query(
        query=query,
        title=title,
        company=company,
        location=location,
        country=country,
        remote_only=remote_only,
        workplace=workplace,
        experience=experience,
        sponsorship=sponsorship,
        company_size=company_size,
    )
    with rt.session_scope() as session:
        repo = SavedSearchRepository(session)
        repo.save(SavedSearch(id=reference, name=name or reference, query_payload=search_query, is_default=default))
    console.print(f"Saved search: {reference}")


@searches_app.command("delete")
def searches_delete(
    reference: str,
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete a saved search."""
    rt = runtime(workspace)
    if not yes:
        confirm = input(f"Delete search '{reference}'? (y/n): ").strip().lower()
        if confirm not in ("y", "yes"):
            console.print("Cancelled.")
            return
    with rt.session_scope() as session:
        repo = SavedSearchRepository(session)
        repo.delete(reference)
    console.print(f"Deleted saved search: {reference}")


@searches_app.command("run")
def searches_run(reference: str, workspace: Path = typer.Option(Path.cwd(), help="Workspace root")) -> None:
    """Run a saved search."""
    rt = runtime(workspace)
    search = _saved_search_model(rt, reference, touch=True)
    if search:
        _print_job_search_results(rt, search.query)
    else:
        console.print(f"Search not found: {reference}")


@runs_app.command("status")
def runs_status(workspace: Path = typer.Option(Path.cwd(), help="Workspace root"), limit: int = 25) -> None:
    """Show recent runs."""
    rt = runtime(workspace)
    with rt.session_scope() as session:
        repo = RunRepository(session)
        runs = repo.list_runs(limit=limit)
    table = Table(title="Recent Runs")
    table.add_column("Run ID")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Started")
    table.add_column("Completed")
    for run in runs:
        table.add_row(
            str(run.id),
            run.run_type or "-",
            run.status.value if run.status else "-",
            run.started_at.isoformat() if run.started_at else "-",
            run.completed_at.isoformat() if run.completed_at else "-",
        )
    console.print(table)


@runs_app.command("logs")
def runs_logs(run_id: str, workspace: Path = typer.Option(Path.cwd(), help="Workspace root"), limit: int = 200) -> None:
    """Show logs for a run."""
    rt = runtime(workspace)
    with rt.session_scope() as session:
        stmt = select(AuditEventRecord).where(AuditEventRecord.run_id == run_id).order_by(AuditEventRecord.created_at.desc()).limit(limit)
        events = session.execute(stmt).scalars().all()
    for event in reversed(events):
        console.print(f"[{event.created_at}] {event.level}: {event.message}")


def _render_personal_preferences(personal) -> None:
    table = Table(title="Personal Preferences")
    table.add_column("Key")
    table.add_column("Value")
    for key, value in personal.preferences.items():
        table.add_row(key, str(value))
    console.print(table)


def _render_personal_fact_list(facts) -> None:
    table = Table(title="Personal Facts")
    table.add_column("ID")
    table.add_column("Kind")
    table.add_column("Summary")
    table.add_column("Allowed")
    for fact in facts:
        table.add_row(
            str(fact.fact_id),
            fact.kind.value if fact.kind else "-",
            str(fact.payload or {})[:50],
            "Yes" if getattr(fact, "allowed_for_generation", False) and not getattr(fact, "disallowed", False) else "No",
        )
    console.print(table)


def _render_personal_fact_detail(fact) -> None:
    console.print()
    console.rule("[bold]Personal Fact Detail[/bold]")
    console.print(f"  ID: {fact.fact_id}")
    console.print(f"  Kind: {fact.kind}")
    console.print(f"  Payload: {json.dumps(fact.payload, indent=2, default=str)}")
    console.print(f"  Allowed: {getattr(fact, 'allowed_for_generation', False) and not getattr(fact, 'disallowed', False)}")
    console.print()


def _render_personal_daily_summary(rt: AppRuntime, summary) -> None:
    if summary is None:
        console.print("No daily run summary available.")
        return
    console.print()
    console.rule("[bold]Daily Run Summary[/bold]")
    console.print(f"  Run ID: {summary.run_id}")
    console.print(f"  Status: {summary.status}")
    console.print(f"  Jobs Discovered: {summary.jobs_discovered}")
    console.print(f"  Jobs Prepared: {summary.jobs_prepared}")
    console.print(f"  Jobs Applied: {summary.jobs_applied}")
    console.print()


def _render_personal_inbox(inbox) -> None:
    console.print()
    console.rule("[bold]Personal Inbox[/bold]")
    _render_personal_inbox_section("New Jobs", inbox.new_jobs)
    _render_personal_inbox_section("Ready for Preparation", inbox.ready_for_preparation)
    _render_personal_inbox_section("Ready for Application", inbox.ready_for_application)
    console.print()


def _render_preview_payload(title: str, payload: dict[str, Any]) -> None:
    console.print()
    console.rule(f"[bold]{title}[/bold]")
    console.print_json(json.dumps(payload, indent=2, default=str))
    console.print()


def _render_personal_explanation(payload) -> None:
    console.print()
    console.rule("[bold]Personal Job Explanation[/bold]")
    console.print(f"  Job: {payload.job_title}")
    console.print(f"  Company: {payload.company}")
    console.print(f"  Match Score: {payload.match_score}")
    console.print(f"  Reasons:")
    for reason in payload.reasons:
        console.print(f"    - {reason}")
    console.print()


def _render_personal_decisions(payload) -> None:
    console.print()
    console.rule("[bold]Personal Decisions[/bold]")
    if payload.decisions:
        for decision in payload.decisions:
            console.print(f"  {decision.job_id}: {decision.status}")
    else:
        console.print("  No personal decisions recorded.")
    console.rule("[bold]Suppression Rules[/bold]")
    if payload.suppression_rules:
        for rule in payload.suppression_rules:
            console.print(f"  {rule.scope}: {rule.company_display_name or rule.company_normalized_name or '-'}")
    else:
        console.print("  No suppression rules recorded.")
    console.print()


def _render_question_queue(items) -> None:
    table = Table(title="Question Queue")
    table.add_column("Question ID")
    table.add_column("Application ID")
    table.add_column("Prompt")
    table.add_column("Type")
    table.add_column("Current")
    for item in items:
        table.add_row(
            str(item.question_id),
            str(item.application_id),
            (item.prompt_text or "-")[:70],
            str(item.question_type or "-"),
            str(item.existing_answer or "pending"),
        )
    console.print(table)
def _render_autonomous_summary(summary) -> None:
    console.print()
    console.rule("[bold]Autonomous Summary[/bold]")
    console.print(f"  Run ID: {summary.run_id}")
    console.print(f"  Status: {summary.status}")
    console.print(f"  Decisions: {summary.decisions}")
    console.print()


def _render_autonomous_status(payload: dict[str, Any]) -> None:
    console.print()
    console.rule("[bold]Autonomous Status[/bold]")
    latest = payload.get('latest_run')
    if latest:
        console.print(f"  Latest run: {latest.get('run_id', '-')}")
        console.print(f"    Started: {latest.get('started_at', '-')}")
        console.print(f"    Processed: {latest.get('processed_count', len(latest.get('processed_job_ids', [])))}")
        console.print(f"    Evaluated: {latest.get('evaluated_count', len(latest.get('evaluated_application_ids', [])))}")
        console.print(f"    Submitted: {latest.get('submitted_count', len(latest.get('submitted_application_ids', [])))}")
        console.print(f"    Failed: {latest.get('failed_count', len(latest.get('failed_application_ids', [])))}")
    else:
        console.print("  No autonomous runs recorded yet.")
    console.print(f"  Enabled: {payload.get('enabled', False)}")
    console.print(f"  Submit enabled: {payload.get('submit_enabled', False)}")
    console.print(f"  Queue depth: {payload.get('queue_depth', 0)}")
    console.print(f"  Ready for submit: {payload.get('ready_for_submit', 0)}")
    console.print(f"  Blocked applications: {payload.get('blocked_applications', 0)}")
    console.print(f"  Unresolved prompts: {payload.get('unresolved_prompts', 0)}")
    console.print()


def _parse_personal_scope(value: str, *, allow_all: bool = False):
    if allow_all and value == "all":
        return None
    return value


def _validate_personal_preset_names(rt: AppRuntime, names: list[str]) -> None:
    missing: list[str] = []
    with rt.session_scope() as session:
        repo = ProfileRepository(session)
        existing = {f.name for f in repo.list_facts() if f.name}
    for name in names:
        if name not in existing:
            missing.append(name)
    if missing:
        raise ValueError(f"Unknown preset names: {', '.join(missing)}")


@personal_prefs_app.command('set')
def personal_prefs_set(
    workspace: Path = typer.Option(Path.cwd(), help='Workspace root'),
    enabled_preset: list[str] | None = typer.Option(None, '--enabled-preset', help='Enable a saved search preset'),
    country: list[str] | None = typer.Option(None, '--country', help='Country codes'),
    region: list[str] | None = typer.Option(None, '--region', help='Regions'),
    city: list[str] | None = typer.Option(None, '--city', help='Cities'),
    remote_only: bool = typer.Option(False, '--remote-only', help='Remote jobs only'),
    workplace_type: list[str] | None = typer.Option(None, '--workplace-type', help='Workplace types'),
    experience_level: list[str] | None = typer.Option(None, '--experience-level', help='Experience levels'),
    result_limit: int | None = typer.Option(None, '--result-limit', help='Default result limit'),
    auto_prepare: bool = typer.Option(False, '--auto-prepare', help='Auto-prepare after discovery'),
) -> None:
    """Set personal preferences."""
    updates: dict[str, Any] = {}
    if enabled_preset:
        updates['enabled_saved_search_presets'] = enabled_preset
    if country:
        updates['countries'] = country
    if region:
        updates['regions'] = region
    if city:
        updates['cities'] = city
    if remote_only:
        updates['remote_only'] = True
    if workplace_type:
        updates['workplace_types'] = workplace_type
    if experience_level:
        updates['experience_levels'] = experience_level
    if result_limit is not None:
        updates['default_result_limit'] = result_limit
    if auto_prepare:
        updates['auto_prepare_after_discovery'] = True
    try:
        update_personal_preferences(workspace=workspace, updates=updates)
        console.print("Updated personal preferences.")
    except ValueError as exc:
        console.print(str(exc))
        raise typer.Exit(code=1) from exc


@personal_prefs_app.command('show')
def personal_prefs_show(workspace: Path = typer.Option(Path.cwd(), help='Workspace root')) -> None:
    """Show current personal preferences."""
    rt = runtime(workspace)
    config = rt.config
    personal = config.personal
    console.print()
    console.rule("[bold]Personal Preferences[/bold]")
    console.print(f"  Enabled Presets: {personal.enabled_saved_search_presets or 'none'}")
    console.print(f"  Countries: {personal.countries or 'none'}")
    console.print(f"  Regions: {personal.regions or 'none'}")
    console.print(f"  Cities: {personal.cities or 'none'}")
    console.print(f"  Remote Only: {personal.remote_only}")
    console.print(f"  Workplace Types: {personal.workplace_types or 'none'}")
    console.print(f"  Experience Levels: {personal.experience_levels or 'none'}")
    console.print(f"  Result Limit: {personal.default_result_limit}")
    console.print(f"  Auto Prepare: {personal.auto_prepare_after_discovery}")
    console.print()


@personal_prefs_app.command('reset')
def personal_prefs_reset(workspace: Path = typer.Option(Path.cwd(), help='Workspace root')) -> None:
    """Reset all personal preferences."""
    reset_personal_preferences(workspace)
    console.print("All personal preferences reset.")


@personal_facts_app.command('list')
def personal_facts_list(
    workspace: Path = typer.Option(Path.cwd(), help='Workspace root'),
    onboarding_only: bool = typer.Option(False, '--onboarding-only', help='Show only onboarding facts'),
) -> None:
    """List personal facts."""
    rt = runtime(workspace)
    facts = list_personal_facts(rt, onboarding_only=onboarding_only)
    _render_personal_fact_list(facts)


@personal_facts_app.command('inspect')
def personal_facts_inspect(
    fact_id: str,
    workspace: Path = typer.Option(Path.cwd(), help='Workspace root'),
) -> None:
    """Inspect a personal fact."""
    rt = runtime(workspace)
    fact = get_personal_fact(rt, fact_id)
    if fact:
        _render_personal_fact_detail(fact)
    else:
        console.print(f"Fact not found: {fact_id}")


@personal_facts_app.command('allow')
def personal_facts_allow(fact_id: str, workspace: Path = typer.Option(Path.cwd(), help='Workspace root')) -> None:
    """Allow a personal fact."""
    rt = runtime(workspace)
    update_personal_fact_flags(rt, fact_id, allowed_for_generation=True, disallowed=False)
    console.print(f"Fact allowed: {fact_id}")


@personal_facts_app.command('disallow')
def personal_facts_disallow(fact_id: str, workspace: Path = typer.Option(Path.cwd(), help='Workspace root')) -> None:
    """Disallow a personal fact."""
    rt = runtime(workspace)
    update_personal_fact_flags(rt, fact_id, allowed_for_generation=False, disallowed=True)
    console.print(f"Fact disallowed: {fact_id}")


@personal_facts_app.command('delete')
def personal_facts_delete(
    fact_id: str,
    workspace: Path = typer.Option(Path.cwd(), help='Workspace root'),
    yes: bool = typer.Option(False, '--yes', '-y', help='Skip confirmation'),
) -> None:
    """Delete a personal fact."""
    rt = runtime(workspace)
    if not yes:
        confirm = input(f"Delete fact '{fact_id}'? (y/n): ").strip().lower()
        if confirm not in ('y', 'yes'):
            console.print('Cancelled.')
            return
    delete_personal_fact(rt, fact_id)
    console.print(f"Fact deleted: {fact_id}")


@personal_app.command('daily-run')
def personal_daily_run(workspace: Path = typer.Option(Path.cwd(), help='Workspace root')) -> None:
    """Run the personal daily workflow."""
    rt = runtime(workspace)
    try:
        anyio.run(run_personal_daily, rt)
    except Exception as exc:
        console.print(str(exc))
        raise typer.Exit(code=1) from exc
    console.print("Daily run complete.")


@personal_app.command('rehearse')
def personal_rehearse(
    workspace: Path = typer.Option(Path.cwd(), help='Workspace root'),
    daily_dry_run: bool = typer.Option(True, '--daily-dry-run/--no-daily-dry-run', help='Include a local daily-run rehearsal preview'),
    daily_dry_run_limit: int = typer.Option(10, '--daily-dry-run-limit', min=1, max=50, help='Maximum jobs to inspect during local daily-run rehearsal'),
    json_output: bool = typer.Option(False, '--json', help='Emit the report as JSON'),
) -> None:
    """Run a personal rehearsal to validate workspace readiness."""
    rt = runtime(workspace)
    report = inspect_personal_rehearsal(
        workspace=workspace,
        runtime=rt,
        include_daily_dry_run=daily_dry_run,
        daily_dry_run_limit=daily_dry_run_limit,
    )
    if json_output:
        console.print_json(json.dumps(_personal_rehearsal_payload(report), default=str))
    else:
        _render_personal_rehearsal(report)
    if report.report.blocked_count:
        raise typer.Exit(code=1)


@personal_app.command('inbox')
def personal_inbox(
    workspace: Path = typer.Option(Path.cwd(), help='Workspace root'),
) -> None:
    """Show the personal inbox."""
    rt = runtime(workspace)
    inbox = build_personal_inbox(rt)
    _render_personal_inbox(inbox)


@personal_app.command('shortlist')
def personal_shortlist(
    job_id: str,
    workspace: Path = typer.Option(Path.cwd(), help='Workspace root'),
    reason: str | None = typer.Option(None, '--reason', help='Reason for shortlisting'),
) -> None:
    """Shortlist a job."""
    rt = runtime(workspace)

    async def _run_shortlist():
        return await shortlist_job(rt, job_id, reason_code=reason)

    mutation = anyio.run(_run_shortlist)
    console.print(f"Shortlisted job: {mutation.decision.job_id}")


@personal_app.command('watch')
def personal_watch(
    job_id: str,
    workspace: Path = typer.Option(Path.cwd(), help='Workspace root'),
    reason: str | None = typer.Option(None, '--reason', help='Reason for watching'),
) -> None:
    """Watch a job."""
    rt = runtime(workspace)

    async def _run_watch():
        return await watch_job(rt, job_id, reason_code=reason)

    mutation = anyio.run(_run_watch)
    console.print(f"Watching job: {mutation.decision.job_id}")


@personal_app.command('dismiss')
def personal_dismiss(
    job_id: str,
    workspace: Path = typer.Option(Path.cwd(), help='Workspace root'),
    reason: str | None = typer.Option(None, '--reason', help='Reason for dismissal'),
    scope: str | None = typer.Option(None, '--scope', help='Suppression scope'),
) -> None:
    """Dismiss a job."""
    rt = runtime(workspace)
    suppression_scope = PersonalSuppressionScope.JOB if scope is None else PersonalSuppressionScope(scope.replace('-', '_'))

    async def _run_dismiss():
        return await dismiss_job(rt, job_id, reason_code=reason, suppression_scope=suppression_scope)

    mutation = anyio.run(_run_dismiss)
    console.print(f"Dismissed job: {mutation.decision.job_id}. Created {len(mutation.created_rules)} suppression rule(s).")


@personal_app.command('archive')
def personal_archive(
    job_id: str,
    workspace: Path = typer.Option(Path.cwd(), help='Workspace root'),
    reason: str | None = typer.Option(None, '--reason', help='Reason for archiving'),
    scope: str | None = typer.Option(None, '--scope', help='Suppression scope'),
) -> None:
    """Archive a job."""
    rt = runtime(workspace)
    suppression_scope = PersonalSuppressionScope.JOB if scope is None else PersonalSuppressionScope(scope.replace('-', '_'))

    async def _run_archive():
        return await archive_job(rt, job_id, reason_code=reason, suppression_scope=suppression_scope)

    mutation = anyio.run(_run_archive)
    console.print(f"Archived job: {mutation.decision.job_id}. Created {len(mutation.created_rules)} suppression rule(s).")


@personal_app.command('unsuppress')
def personal_unsuppress(
    job_id: str,
    workspace: Path = typer.Option(Path.cwd(), help='Workspace root'),
    scope: str = typer.Option('job', '--scope', help='Suppression scope to clear, or `all`'),
) -> None:
    """Unsuppress a job."""
    rt = runtime(workspace)
    clear_scopes = tuple(PersonalSuppressionScope) if scope == 'all' else (PersonalSuppressionScope(scope.replace('-', '_')),) 

    async def _run_unsuppress():
        return await unsuppress_job(rt, job_id, clear_scopes=clear_scopes)

    mutation = anyio.run(_run_unsuppress)
    console.print(f"Unsuppressed job: {mutation.decision.job_id}. Cleared {len(mutation.cleared_rules)} suppression rule(s).")


@personal_app.command('decisions')
def personal_decisions(
    workspace: Path = typer.Option(Path.cwd(), help='Workspace root'),
) -> None:
    """List personal decisions."""
    rt = runtime(workspace)
    decisions = list_personal_decisions(rt)
    _render_personal_decisions(decisions)


@personal_app.command('explain')
def personal_explain(
    job_id: str,
    workspace: Path = typer.Option(Path.cwd(), help='Workspace root'),
    json_output: bool = typer.Option(False, '--json', help='Emit explanation as JSON'),
) -> None:
    rt = runtime(workspace)
    payload = explain_personal_job(rt, job_id)
    if json_output:
        console.print_json(json.dumps(payload.model_dump(mode='json')))
        return
    _render_personal_explanation(payload)


@personal_preview_app.command('resume')
def personal_preview_resume(
    workspace: Path = typer.Option(Path.cwd(), help='Workspace root'),
    job_id: str | None = typer.Option(None, '--job-id', help='Preview against a specific existing job'),
) -> None:
    rt = runtime(workspace)
    try:
        payload = anyio.run(preview_personal_resume, rt, job_id)
    except ValueError as exc:
        console.print(str(exc))
        raise typer.Exit(code=1) from exc
    _render_preview_payload('Resume Preview', payload)


@personal_preview_app.command('cover-letter')
def personal_preview_cover_letter(
    workspace: Path = typer.Option(Path.cwd(), help='Workspace root'),
    job_id: str | None = typer.Option(None, '--job-id', help='Preview against a specific existing job'),
) -> None:
    rt = runtime(workspace)
    try:
        payload = anyio.run(preview_personal_cover_letter, rt, job_id)
    except ValueError as exc:
        console.print(str(exc))
        raise typer.Exit(code=1) from exc
    _render_preview_payload('Cover Letter Preview', payload)




@greenhouse_app.command("training-history")
def greenhouse_training_history(
    run_id: str | None = typer.Option(None, "--run-id", help="Filter to a specific training run"),
    limit: int = typer.Option(25, "--limit", min=1, max=200, help="Maximum review rows to show"),
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    json_output: bool = typer.Option(False, "--json", help="Emit review history as JSON"),
) -> None:
    from findmyjob.personal.training import list_training_review_history

    rt = runtime(workspace)
    history = list_training_review_history(rt, limit=limit, run_id=run_id)
    if json_output:
        console.print_json(json.dumps(history, default=str))
        return
    table = Table(title="Training Review History")
    table.add_column("Reviewed")
    table.add_column("Sample")
    table.add_column("Run")
    table.add_column("Company")
    table.add_column("Title")
    table.add_column("Decision")
    table.add_column("Reason")
    table.add_column("Application")
    if not history:
        table.add_row("-", "-", "-", "-", "No training reviews recorded.", "-", "-", "-")
    for item in history:
        decision = "approved" if item.get("approved") else "rejected"
        table.add_row(
            str(item.get("reviewed_at") or item.get("recorded_at") or ""),
            str(item.get("sample_id") or ""),
            str(item.get("run_id") or ""),
            str(item.get("company_name") or ""),
            str(item.get("job_title") or ""),
            decision,
            str(item.get("rejection_reason_code") or ""),
            str(item.get("linked_application_id") or ""),
        )
    console.print(table)


@greenhouse_app.command("training-promote")
def greenhouse_training_promote(
    sample_id: str | None = typer.Option(None, "--sample-id", help="Promote a specific approved training sample"),
    run_id: str | None = typer.Option(None, "--run-id", help="Promote approved samples from a specific run; defaults to latest"),
    limit: int = typer.Option(100, "--limit", min=1, max=500, help="Maximum approved samples to promote when using --run-id"),
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    json_output: bool = typer.Option(False, "--json", help="Emit promotion results as JSON"),
) -> None:
    from findmyjob.personal.training import promote_training_samples

    rt = runtime(workspace)

    async def _run_promote():
        return await promote_training_samples(rt, sample_id=sample_id, run_id=run_id, limit=limit)

    try:
        results = anyio.run(_run_promote)
    except ValueError as exc:
        console.print(str(exc))
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        console.print(f"Training promotion failed: {exc}")
        raise typer.Exit(code=1) from exc

    payload = [result.model_dump(mode="json") for result in results]
    if json_output:
        console.print_json(json.dumps(payload, default=str))
        return

    table = Table(title="Training Promotion Results")
    table.add_column("Sample")
    table.add_column("Run")
    table.add_column("Promoted")
    table.add_column("Application")
    table.add_column("Review Packet")
    if not payload:
        table.add_row("-", "-", "No", "-", "-")
    for item in payload:
        table.add_row(
            str(item.get("sample_id") or ""),
            str(item.get("run_id") or ""),
            "yes" if item.get("promoted") else "no",
            str(item.get("application_id") or ""),
            str(item.get("review_packet_path") or ""),
        )
    console.print(table)


@greenhouse_app.command("training-report")
def greenhouse_training_report(
    run_id: str | None = typer.Option(None, "--run-id", help="Specific training run to inspect; defaults to latest"),
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    export: Path | None = typer.Option(None, "--export", help="Optional JSON export path"),
    json_output: bool = typer.Option(False, "--json", help="Emit the report as JSON"),
) -> None:
    from findmyjob.personal.training import build_training_report

    rt = runtime(workspace)
    try:
        report = build_training_report(rt, run_id=run_id)
    except ValueError as exc:
        console.print(str(exc))
        raise typer.Exit(code=1) from exc

    if export is not None:
        exported = _write_json_payload(export, report)
        console.print(f"Training report exported: {exported}")
    if json_output:
        console.print_json(json.dumps(report, default=str))
        return

    console.print()
    console.rule("[bold]Training Report[/bold]")
    console.print(f"  Run ID: {report['run_id']}")
    console.print(f"  Status: {report['status']}")
    console.print(f"  Sampled: {report['sampled_count']} | Approved: {report['approved_count']} | Rejected: {report['rejected_count']}")
    console.print(f"  Promoted applications: {len(report.get('promoted_application_ids') or [])}")
    if report.get('notes'):
        for note in report['notes'][:5]:
            console.print(f"  Note: {note}")
    if report.get('reviews'):
        latest = report['reviews'][0]
        console.print(f"  Latest review: {latest.get('company_name') or '-'} | {latest.get('job_title') or '-'} | {'approved' if latest.get('approved') else 'rejected'}")
    console.print()


# ---------------------------------------------------------------------------
# Workflow command group
# ---------------------------------------------------------------------------

@workflow_app.command("status")
def workflow_status(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    json_output: bool = typer.Option(False, "--json", help="Emit the snapshot as JSON"),
) -> None:
    """Show a unified workflow status snapshot."""
    rt = runtime(workspace)
    snapshot = collect_workflow_snapshot(workspace=workspace, runtime=rt)
    if json_output:
        console.print_json(json.dumps(snapshot.model_dump(mode="json"), default=str))
        return
    _render_workflow_snapshot(snapshot)


@workflow_app.command("export-state")
def workflow_export_state(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    output: Path | None = typer.Option(None, "--output", help="Optional JSON export path"),
) -> None:
    """Export the unified workflow snapshot as JSON to .fmj/exports/."""
    rt = runtime(workspace)
    snapshot = collect_workflow_snapshot(workspace=workspace, runtime=rt)
    export_dir = workspace / ".fmj" / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    if output is None:
        ts = snapshot.generated_at.strftime("%Y%m%dT%H%M%SZ")
        output = export_dir / f"workflow-snapshot-{ts}.json"
    _write_json_payload(output, snapshot.model_dump(mode="json"))
    console.print(f"Workflow snapshot exported: {output}")


@workflow_app.command("setup")
def workflow_setup(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    json_output: bool = typer.Option(False, "--json", help="Emit the setup report as JSON"),
) -> None:
    """Diagnose and guide workspace readiness for the full workflow."""
    rt = runtime(workspace)
    snapshot = collect_workflow_snapshot(workspace=workspace, runtime=rt)
    if json_output:
        console.print_json(json.dumps(snapshot.model_dump(mode="json"), default=str))
        return
    _render_workflow_setup(snapshot)


@workflow_app.command("launch-ready")
def workflow_launch_ready(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    json_output: bool = typer.Option(False, "--json", help="Emit the launch readiness report as JSON"),
) -> None:
    """Check if the workspace is ready for model launch."""
    rt = runtime(workspace)
    snapshot = collect_workflow_snapshot(workspace=workspace, runtime=rt)
    if json_output:
        console.print_json(json.dumps(snapshot.model_dump(mode="json"), default=str))
        return
    _render_launch_ready(snapshot)


@workflow_app.command("train")
def workflow_train(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    run_id: str | None = typer.Option(None, "--run-id", help="Specific training run to inspect"),
    json_output: bool = typer.Option(False, "--json", help="Emit training report as JSON"),
) -> None:
    """Wrapper over existing greenhouse training mode with workflow context."""
    from findmyjob.personal.training import build_training_report
    rt = runtime(workspace)
    try:
        report = build_training_report(rt, run_id=run_id)
    except ValueError as exc:
        console.print(str(exc))
        raise typer.Exit(code=1) from exc
    if json_output:
        console.print_json(json.dumps(report, default=str))
        return
    console.print()
    console.rule("[bold]Training Report[/bold]")
    console.print(f"  Run ID: {report['run_id']}")
    console.print(f"  Status: {report['status']}")
    console.print(f"  Sampled: {report['sampled_count']} | Approved: {report['approved_count']} | Rejected: {report['rejected_count']}")
    console.print(f"  Promoted applications: {len(report.get('promoted_application_ids') or [])}")
    if report.get('notes'):
        for note in report['notes'][:5]:
            console.print(f"  Note: {note}")
    if report.get('reviews'):
        latest = report['reviews'][0]
        console.print(f"  Latest review: {latest.get('company_name') or '-'} | {latest.get('job_title') or '-'} | {'approved' if latest.get('approved') else 'rejected'}")
    console.print()


@workflow_app.command("review")
def workflow_review(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    json_output: bool = typer.Option(False, "--json", help="Emit review queue as JSON"),
) -> None:
    """Show the current review/apply queue state."""
    rt = runtime(workspace)
    with rt.session_scope() as session:
        app_repo = ApplicationRepository(session)
        pending = app_repo.list_applications(
            statuses=[
                "needs_user_input",
                "ready_for_review",
                "approved_for_submit",
                "submission_uncertain",
            ]
        )
    if json_output:
        payload = {
            "pending_count": len(pending),
            "applications": [
                {
                    "application_id": app.application_id,
                    "job_id": app.job_id,
                    "status": app.status,
                    "created_at": str(app.created_at),
                }
                for app in pending
            ],
        }
        console.print_json(json.dumps(payload, default=str))
        return
    table = Table(title="Review Queue")
    table.add_column("Application ID")
    table.add_column("Job ID")
    table.add_column("Status")
    table.add_column("Created")
    if not pending:
        table.add_row("-", "-", "No pending applications.", "-")
    for app in pending[:25]:
        table.add_row(
            str(app.application_id),
            str(app.job_id),
            str(app.status),
            str(app.created_at),
        )
    console.print(table)
    console.print(f"Total pending: {len(pending)}")


@workflow_app.command("daily")
def workflow_daily(
    workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
    json_output: bool = typer.Option(False, "--json", help="Emit daily summary as JSON"),
) -> None:
    """Show the personal daily workflow state."""
    rt = runtime(workspace)
    summary = build_personal_inbox(rt)
    if json_output:
        console.print_json(json.dumps(summary.model_dump(mode="json"), default=str))
        return
    _render_personal_inbox(summary)


def _render_workflow_snapshot(snapshot: WorkflowSnapshot) -> None:
    """Render the unified workflow snapshot to the console."""
    console.print()
    console.rule("[bold]Workflow Status Snapshot[/bold]")
    console.print(f"  Workspace: {snapshot.workspace_name} ({snapshot.workspace})")
    console.print(f"  Config: {snapshot.config_path}")
    console.print(f"  Generated: {snapshot.generated_at}")

    # Release snapshot summary
    rs = snapshot.release_snapshot
    console.print(f"  Doctor: {rs.doctor.overall_status}")
    console.print(f"  Launch Check: {rs.launch_check.overall_status}")
    console.print(f"  Config Valid: {rs.config_validation.overall_status}")
    if rs.launch_profile:
        console.print(f"  Model Launch Profile: {rs.launch_profile.overall_status}")

    # Personal rehearsal
    if snapshot.personal_rehearsal:
        pr = snapshot.personal_rehearsal
        onboarding = pr.onboarding
        prefs = pr.personal_preferences
        console.print(f"  Personal Onboarding: {onboarding.get('onboarding_enabled', False)}")
        console.print(f"  Personal Preferences: {len(prefs.get('enabled_saved_search_presets', []))} presets")
    else:
        console.print("  Personal Rehearsal: unavailable")

    # Training
    if snapshot.training_summary:
        ts = snapshot.training_summary
        console.print(f"  Training Run: {ts.run_id} | Approved: {ts.approved_count} | Rejected: {ts.rejected_count}")
    else:
        console.print("  Training: no runs recorded")

    # Review/Apply
    if snapshot.review_apply_summary:
        console.print(f"  Pending Applications: {snapshot.review_apply_summary.get('pending_applications', 0)}")
    else:
        console.print("  Review/Apply: unavailable")

    # Notes
    if snapshot.notes:
        console.print("  Notes/Warnings:")
        for note in snapshot.notes[:10]:
            console.print(f"    - {note}")
    console.print()


def _render_workflow_setup(snapshot: WorkflowSnapshot) -> None:
    """Render a guided setup report."""
    console.print()
    console.rule("[bold]Workflow Setup Guide[/bold]")
    rs = snapshot.release_snapshot
    issues = []
    if rs.doctor.overall_status == "blocked":
        issues.append("Doctor check is blocked. Run `fmj doctor` for details.")
    if rs.launch_check.overall_status == "fail":
        issues.append("Launch check failed. Run `fmj launch-check` for details.")
    if rs.config_validation.overall_status == "blocked":
        issues.append("Configuration is invalid. Run `fmj config validate` for details.")
    if snapshot.personal_rehearsal is None:
        issues.append("Personal rehearsal unavailable. Run `fmj personal rehearse` to set up.")

    if issues:
        console.print("[yellow]Setup issues detected:[/yellow]")
        for issue in issues:
            console.print(f"  - {issue}")
    else:
        console.print("[green]Workspace is fully set up and ready for workflow.[/green]")
    console.print()


def _render_launch_ready(snapshot: WorkflowSnapshot) -> None:
    """Render a launch readiness report."""
    console.print()
    console.rule("[bold]Launch Readiness[/bold]")
    rs = snapshot.release_snapshot
    ready = True
    checks = {
        "Doctor": rs.doctor.overall_status != "blocked",
        "Launch Check": rs.launch_check.overall_status != "fail",
        "Config Valid": rs.config_validation.overall_status != "blocked",
    }
    if rs.launch_profile:
        checks["Model Launch Profile"] = rs.launch_profile.overall_status == "pass"
    if snapshot.training_summary:
        checks["Training Complete"] = snapshot.training_summary.approved_count > 0

    for check, passed in checks.items():
        status = "[green]PASS[/green]" if passed else "[red]FAIL[/red]"
        console.print(f"  {check}: {status}")
        if not passed:
            ready = False

    if ready:
        console.print("\n[green]Workspace is ready for launch.[/green]")
    else:
        console.print("\n[yellow]Workspace is NOT ready for launch. Address failed checks above.[/yellow]")
    console.print()







































