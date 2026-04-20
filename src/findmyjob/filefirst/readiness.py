from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from findmyjob.core.runtime import _inspect_playwright
from findmyjob.core.tooling import find_latex_engine, find_typst_executable
from findmyjob.core.types import LaunchCheckReport, ModelLaunchProfileReport, ModelLaunchRoleStatus, ReleaseSnapshotReport, ValidationReport
from findmyjob.filefirst.advanced_models import load_model_router
from findmyjob.filefirst.render import _template_bridge_details
from findmyjob.filefirst.source_targets import SOURCE_ORDER, SUPPORTED_SOURCES, active_sources, requested_sources, scope_targets
from findmyjob.filefirst.workspace import DEFAULT_CV, FileWorkspace
from findmyjob.web.frontend_sync import inspect_frontend_build_readiness

_SUPPORTED_SOURCE_ORDER = SOURCE_ORDER
_SUPPORTED_SOURCES = SUPPORTED_SOURCES
_OPTIONAL_SOURCES: set[str] = set()  # All three are now production-ready


def _workspace(value: Path | FileWorkspace) -> FileWorkspace:
    ws = value if isinstance(value, FileWorkspace) else FileWorkspace(Path(value))
    ws.ensure()
    return ws


def _unique(items: Iterable[str]) -> list[str]:
    ordered: list[str] = []
    for item in items:
        cleaned = str(item or "").strip()
        if cleaned and cleaned not in ordered:
            ordered.append(cleaned)
    return ordered


def _requested_production_sources(ws: FileWorkspace) -> list[str]:
    return requested_sources(ws)


def _production_sources(ws: FileWorkspace) -> list[str]:
    return [source_name for source_name in _requested_production_sources(ws) if source_name in _SUPPORTED_SOURCES]


def _enabled_sources(ws: FileWorkspace) -> list[str]:
    portals = ws.load_portals()
    return [source_name for source_name in _SUPPORTED_SOURCE_ORDER if bool(getattr(portals.sources.get(source_name), "enabled", False))]


def _effective_sources(ws: FileWorkspace) -> list[str]:
    return active_sources(ws)


def _source_scope_finding(source_name: str, scope: dict[str, list[str]]) -> tuple[str, str, str]:
    label = source_name.capitalize()
    configured = list(scope.get("configured") or [])
    persisted = list(scope.get("persisted") or [])
    bootstrap = list(scope.get("bootstrap") or [])
    combined = _unique([*configured, *persisted, *bootstrap])
    detail = (
        f"configured={len(configured)}; persisted={len(persisted)}; "
        f"bootstrap={len(bootstrap)}; sample={', '.join(combined[:12]) or 'none'}"
    )
    if configured or persisted:
        return "ok", f"{label} discovery scope includes configured or persisted targets.", detail
    if bootstrap:
        return "warning", f"{label} discovery scope relies only on bundled seeds (unverified).", detail
    return "blocked", f"{label} discovery scope is empty.", "Add boards, seed URLs, seed domains, tracked companies, or run discovery once to persist boards."


def _contact_ready(ws: FileWorkspace) -> bool:
    profile = ws.load_profile()
    if profile.candidate.name.strip() and str(profile.candidate.email or "").strip():
        return True
    for fact in ws.load_facts():
        if fact.kind != "contact" or fact.disallowed:
            continue
        payload = fact.payload
        if str(payload.get("name") or "").strip() and str(payload.get("email") or "").strip():
            return True
    return False


def _authorization_ready(ws: FileWorkspace) -> bool:
    for fact in ws.load_facts():
        if fact.kind != "authorization" or fact.disallowed:
            continue
        if "is_authorized" in fact.payload:
            return True
    return False


def _experience_fact_count(ws: FileWorkspace) -> int:
    return sum(1 for fact in ws.load_facts() if fact.kind in {"work", "project", "skill"} and not fact.disallowed)


def _cv_ready(ws: FileWorkspace) -> bool:
    body = ws.load_cv().strip()
    return bool(body and body != DEFAULT_CV.strip())


def _launch_status(report: ValidationReport) -> str:
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


def _document_renderer_state(ws: FileWorkspace) -> dict[str, object]:
    bridge = _template_bridge_details(ws)
    if bridge.get("requested"):
        return {
            "renderer": str(bridge.get("resume_renderer") or "latex"),
            "requested": True,
            "configured": bool(bridge.get("configured")),
            "resume_template_path": bridge.get("resume_template_path"),
            "cover_letter_template_path": bridge.get("cover_letter_template_path"),
            "missing_resume_template": bool(bridge.get("missing_resume_template")),
            "chatgpt_drafting": bridge.get("chatgpt_drafting"),
        }
    return {
        "renderer": str(bridge.get("resume_renderer") or "typst"),
        "requested": False,
        "configured": True,
        "resume_template_path": None,
        "cover_letter_template_path": bridge.get("cover_letter_template_path"),
        "missing_resume_template": False,
        "chatgpt_drafting": bridge.get("chatgpt_drafting"),
    }


def inspect_filefirst_config(workspace: Path | FileWorkspace) -> ValidationReport:
    ws = _workspace(workspace)
    report = ValidationReport(
        context="config",
        workspace=str(ws.root),
        loaded_files=[
            str(ws.profile_path),
            str(ws.portals_path),
            str(ws.user_profile_path),
            str(ws.facts_path),
            str(ws.answer_memory_path),
            str(ws.cv_path),
        ],
    )

    for key, summary, path in (
        ("workspace.profile", "Profile config found.", ws.profile_path),
        ("workspace.portals", "Portal config found.", ws.portals_path),
        ("profile.facts", "Facts file found.", ws.facts_path),
        ("profile.answer_memory", "Answer memory file found.", ws.answer_memory_path),
        ("workspace.cv", "CV file found.", ws.cv_path),
    ):
        if path.exists():
            report.add("ok", key, summary, detail=str(path))
        else:
            report.add("blocked", key, f"{summary[:-1]} is missing.", detail=str(path))

    profile_surface = ws.user_profile_surface()
    if profile_surface["mode"] == "sample_mode":
        report.add(
            "warning",
            "profile.local_user_profile",
            "Workspace is still using tracked sample candidate data.",
            detail=f"Create {profile_surface['local_path']} from {profile_surface['public_template_path']} or {profile_surface['local_template_path']}.",
            hint="Open the local Setup page at `/setup` to save your basic profile, or copy `templates/user-profile.local.example.yml` to `.fmj/local-overrides/filefirst/user-profile.yml` for manual editing.",
        )
    elif profile_surface["mode"] == "local_user_profile":
        report.add(
            "ok",
            "profile.local_user_profile",
            "Local user profile is configured.",
            detail=profile_surface["local_path"],
        )
    else:
        report.add(
            "ok",
            "profile.local_user_profile",
            "Advanced local overrides are configured.",
            detail=", ".join(profile_surface["active_advanced_paths"]) or profile_surface["local_path"],
        )

    profile = ws.load_profile()
    portals = ws.load_portals()
    runtime_model = profile.runtime.model
    runtime_provider = str(runtime_model.provider or "").strip().lower()
    runtime_transport = str(runtime_model.transport or "").strip().lower()
    if (
        runtime_provider == "lmstudio"
        and runtime_transport == "local_http"
        and str(runtime_model.base_url or "").strip()
        and str(runtime_model.model or "").strip()
    ):
        report.add(
            "ok",
            "models.runtime.config",
            "Primary runtime model profile matches the LM Studio-local launch contract.",
            detail=(
                f"{runtime_model.base_url} :: {runtime_model.model} "
                f":: preferred_ctx={runtime_model.preferred_context_window}"
            ),
        )
    else:
        report.add(
            "blocked",
            "models.runtime.config",
            "Primary runtime model profile does not satisfy the LM Studio-local launch contract.",
            detail=f"provider={runtime_provider or '-'} :: transport={runtime_transport or '-'} :: base_url={runtime_model.base_url or '-'} :: model={runtime_model.model or '-'}",
            hint="Set `runtime.model.provider=lmstudio`, `runtime.model.transport=local_http`, and configure the local LM Studio host and model in config/profile.yml.",
        )

    automation = profile.runtime.automation
    if automation.capture_traces or automation.capture_dom:
        report.add(
            "blocked",
            "privacy.trace_capture",
            "Trace capture defaults are not privacy-safe for launch.",
            detail=f"capture_traces={automation.capture_traces} :: capture_dom={automation.capture_dom}",
            hint="Set `runtime.automation.capture_traces=false` and `runtime.automation.capture_dom=false` in config/profile.yml.",
        )
    else:
        report.add(
            "ok",
            "privacy.trace_capture",
            "Trace capture defaults are privacy-safe.",
            detail="capture_traces=false :: capture_dom=false",
        )

    requested_sources = _requested_production_sources(ws)
    production_sources = [source_name for source_name in requested_sources if source_name in _SUPPORTED_SOURCES]
    enabled_sources = _enabled_sources(ws)
    unsupported = [s for s in requested_sources if s not in _SUPPORTED_SOURCES]
    if unsupported:
        report.add(
            "warning",
            "sources.production_scope",
            f"Unsupported sources in production_sources: {', '.join(unsupported)}.",
            hint="Only greenhouse, lever, and ashby are supported.",
        )
    else:
        report.add("ok", "sources.production_scope", f"Production sources: {', '.join(production_sources or enabled_sources) or 'none'}.")

    if not enabled_sources:
        report.add(
            "blocked",
            "sources.none_enabled",
            "No production sources are enabled in portals.yml.",
            hint="Enable Greenhouse, Lever, and/or Ashby in portals.yml or from Settings.",
        )

    for source_name in _SUPPORTED_SOURCE_ORDER:
        label = source_name.capitalize()
        portal_config = portals.sources.get(source_name)
        enabled = bool(getattr(portal_config, "enabled", False))
        in_scope = source_name in (production_sources or enabled_sources)
        if not in_scope and not enabled:
            continue
        report.add(
            "ok" if enabled else "blocked",
            f"sources.{source_name}",
            f"{label} is enabled for the production path." if enabled else f"{label} is disabled in the production path.",
            hint=None if enabled else f"Add `{source_name}` to production sources and enable it in portals.yml.",
        )
        if enabled:
            target_scope = scope_targets(ws, source_name)
            scope = {
                "configured": target_scope.configured,
                "persisted": target_scope.persisted,
                "bootstrap": target_scope.bootstrap,
            }
            target_status, target_summary, target_detail = _source_scope_finding(source_name, scope)
            report.add(
                target_status,
                f"sources.{source_name}.targets",
                target_summary,
                detail=target_detail,
                hint=None if target_status == "ok" else f"Configure {label} boards, seeds, or tracked companies before launch.",
            )
    return report


def inspect_filefirst_readiness(
    workspace: Path | FileWorkspace,
    *,
    check_models: bool = True,
    check_browser: bool = True,
    check_typst: bool = True,
) -> ValidationReport:
    ws = _workspace(workspace)
    config_report = inspect_filefirst_config(ws)
    report = ValidationReport(
        context="doctor",
        workspace=str(ws.root),
        loaded_files=list(config_report.loaded_files),
        findings=list(config_report.findings),
    )

    renderer_state = _document_renderer_state(ws)
    if check_typst:
        if renderer_state["renderer"] == "latex":
            latex = find_latex_engine()
            report.add(
                "ok" if latex and renderer_state["configured"] else "blocked",
                "runtime.latex",
                "LaTeX renderer is ready for the active compatibility override."
                if latex and renderer_state["configured"]
                else "LaTeX renderer is required by the active compatibility override but is not ready.",
                detail=latex or (
                    "resume template missing"
                    if renderer_state["missing_resume_template"]
                    else "latex engine not installed"
                ),
            )
            report.add(
                "ok",
                "runtime.typst",
                "Typst launch rendering is currently bypassed by a LaTeX compatibility override.",
                detail=find_typst_executable() or "not installed",
            )
        elif renderer_state["renderer"] == "typst":
            typst = find_typst_executable()
            report.add(
                "ok" if typst else "blocked",
                "runtime.typst",
                "Typst renderer is ready for the active launch workspace."
                if typst
                else "Typst renderer is required by the active launch workspace but is not ready.",
                detail=typst or "typst executable not installed",
            )
        elif renderer_state["renderer"] == "latex_direct":
            report.add(
                "ok",
                "runtime.typst",
                "Typst is not required for the active LaTeX-direct renderer.",
                detail=find_typst_executable() or "not required",
            )
        elif renderer_state["renderer"] == "chatgpt_download":
            report.add(
                "ok",
                "runtime.typst",
                "Typst is not required for the active ChatGPT download renderer.",
                detail=find_typst_executable() or "not required",
            )
        else:
            report.add(
                "warning",
                "runtime.typst",
                "Typst is still the launch renderer even though an HTML-to-PDF compatibility override is configured.",
                detail=find_typst_executable() or "not installed",
            )

    frontend_build = inspect_frontend_build_readiness(ws.root)
    report.add(
        frontend_build.status,
        "runtime.frontend.bundle",
        frontend_build.summary,
        detail=frontend_build.detail,
        hint=frontend_build.hint,
    )

    if check_browser:
        playwright = _inspect_playwright()
        report.add(
            "ok" if playwright["package_ok"] else "blocked",
            "runtime.playwright.package",
            "Playwright package status.",
            detail=playwright["package_detail"],
        )
        report.add(
            "ok" if playwright["browser_ok"] else "blocked",
            "runtime.playwright.browser",
            "Playwright Chromium browser status.",
            detail=playwright["browser_detail"],
        )
        if renderer_state["renderer"] == "chatgpt_download":
            drafting = renderer_state.get("chatgpt_drafting") or {}
            gpt_url = str(drafting.get("gpt_url") or "").strip()
            browser_cdp_url = str(drafting.get("browser_cdp_url") or "").strip()
            profile_dir = drafting.get("profile_dir")
            report.add(
                "ok" if gpt_url and browser_cdp_url else "blocked",
                "chatgpt_drafting.contract",
                "ChatGPT drafting contract is configured."
                if gpt_url and browser_cdp_url
                else "ChatGPT drafting contract is incomplete.",
                detail=f"gpt_url={gpt_url or '-'} :: browser_cdp_url={browser_cdp_url or '-'} :: profile_dir={profile_dir or '-'}",
            )

    if check_models:
        router = load_model_router(ws)
        if router is None:
            report.add(
                "blocked",
                "models.router",
                "Role-based model router is not available.",
                hint="Install or configure the required runtime model profiles for the file-first console.",
            )
        else:
            launch_profile = router.inspect_launch_profile()
            missing_key_envs = sorted(
                {
                    str(model.api_key_env or "").strip()
                    for model in router.list_profiles()
                    if str(model.api_key_env or "").strip() and not os.environ.get(str(model.api_key_env or "").strip())
                }
            )
            detail = (
                f"transport_mix={launch_profile.transport_mix} | "
                f"required_roles={', '.join(launch_profile.required_roles) or 'none'} | "
                f"missing_roles={', '.join(launch_profile.missing_required_roles) or 'none'} | "
                f"missing_env={', '.join(missing_key_envs) or 'none'}"
            )
            report.add(
                "ok" if launch_profile.overall_status == "pass" and not missing_key_envs else "blocked",
                "models.router",
                "Role-based launch profile is ready."
                if launch_profile.overall_status == "pass" and not missing_key_envs
                else "Role-based launch profile is incomplete.",
                detail=detail,
                data=launch_profile.model_dump(mode="json"),
            )
            for role_status in launch_profile.roles:
                report.add(
                    "ok" if role_status.status == "pass" else "warning" if role_status.status == "warning" else "blocked",
                    f"models.role.{role_status.role}",
                    f"{role_status.role} -> {role_status.profile_name or 'unbound'}",
                    detail=f"{role_status.provider or '-'} :: {role_status.model or '-'} :: {role_status.transport or '-'}",
                )

    renderer_status = "blocked"
    renderer_summary = "Active PDF renderer is not ready."
    if renderer_state["renderer"] == "typst" and renderer_state["configured"]:
        renderer_status = "ok"
        renderer_summary = "Active PDF renderer is Typst via bundled local templates."
    elif renderer_state["renderer"] == "chatgpt_download" and renderer_state["configured"]:
        renderer_status = "ok"
        renderer_summary = "Active PDF renderer is ChatGPT download via a managed browser profile."
    elif renderer_state["renderer"] == "latex_direct" and renderer_state["configured"]:
        renderer_status = "ok"
        renderer_summary = "Active PDF renderer is LaTeX-direct via the local resume and cover-letter templates."
    elif renderer_state["renderer"] == "latex" and renderer_state["configured"]:
        renderer_status = "warning"
        renderer_summary = "Active PDF renderer is LaTeX via the local template bridge. This is a compatibility-only path, not the launch renderer."
    elif renderer_state["renderer"] == "html_to_pdf":
        renderer_status = "warning"
        renderer_summary = "Active PDF renderer is HTML-to-PDF via Playwright. This is a compatibility-only path, not the launch renderer."
    report.add(
        renderer_status,
        "artifacts.renderer",
        renderer_summary,
        detail=(
            f"resume_renderer={renderer_state['renderer']} :: template={renderer_state['resume_template_path']}"
            if renderer_state["renderer"] in {"latex", "latex_direct"}
            else (
                f"resume_renderer={renderer_state['renderer']} :: gpt_url={(renderer_state.get('chatgpt_drafting') or {}).get('gpt_url')}"
                if renderer_state["renderer"] == "chatgpt_download"
                else f"resume_renderer={renderer_state['renderer']}"
            )
        ),
    )

    report.add(
        "ok" if _contact_ready(ws) else "warning",
        "profile.contact_facts",
        "Contact facts are ready for file-first resume generation." if _contact_ready(ws) else "Contact facts are missing for file-first resume generation.",
    )
    report.add(
        "ok" if _authorization_ready(ws) else "warning",
        "profile.authorization_facts",
        "Authorization facts are ready for application workflows." if _authorization_ready(ws) else "Authorization facts are missing for application workflows.",
    )
    report.add(
        "ok" if _experience_fact_count(ws) else "warning",
        "profile.experience_facts",
        f"Grounded work/project/skill facts available: {_experience_fact_count(ws)}.",
    )
    report.add(
        "ok" if _cv_ready(ws) else "warning",
        "artifacts.cv",
        "Canonical CV markdown is populated." if _cv_ready(ws) else "Canonical CV markdown still contains the default placeholder.",
        detail=str(ws.cv_path),
    )
    automation = ws.load_profile().runtime.automation
    submit_active = bool(automation.submit_enabled and automation.default_submit_mode == "auto_submit")
    report.add(
        "ok" if automation.enabled else "warning",
        "automation.enabled",
        "Autonomous mode is enabled for the production path." if automation.enabled else "Autonomous mode is disabled in the file-first profile.",
    )
    report.add(
        "ok" if submit_active else "warning",
        "automation.submit_enabled",
        "Autonomous submit mode is enabled for the production path."
        if submit_active
        else "Autonomous submit is not fully enabled; set submit_enabled=true and default_submit_mode=auto_submit.",
        detail=f"submit_enabled={automation.submit_enabled} :: default_submit_mode={automation.default_submit_mode}",
    )
    return report


def build_filefirst_launch_profile(
    workspace: Path | FileWorkspace,
    *,
    doctor_report: ValidationReport | None = None,
) -> ModelLaunchProfileReport:
    ws = _workspace(workspace)
    _ = doctor_report
    router = load_model_router(ws)
    if router is None:
        return ModelLaunchProfileReport(
            required_roles=["classifier", "writer", "question_answerer"],
            roles=[
                ModelLaunchRoleStatus(
                    role="classifier",
                    profile_name=None,
                    transport=None,
                    provider=None,
                    model=None,
                    status="fail",
                    issues=["role-based model router is unavailable"],
                ),
                ModelLaunchRoleStatus(
                    role="writer",
                    profile_name=None,
                    transport=None,
                    provider=None,
                    model=None,
                    status="fail",
                    issues=["role-based model router is unavailable"],
                ),
                ModelLaunchRoleStatus(
                    role="question_answerer",
                    profile_name=None,
                    transport=None,
                    provider=None,
                    model=None,
                    status="fail",
                    issues=["role-based model router is unavailable"],
                ),
            ],
            transport_mix="unbound",
            summary="Role-based launch profile is unavailable.",
        )
    return router.inspect_launch_profile()


def inspect_filefirst_launch_acceptance(
    workspace: Path | FileWorkspace,
    *,
    config_report: ValidationReport | None = None,
    doctor_report: ValidationReport | None = None,
    launch_profile: ModelLaunchProfileReport | None = None,
) -> LaunchCheckReport:
    ws = _workspace(workspace)
    config_report = config_report or inspect_filefirst_config(ws)
    doctor_report = doctor_report or inspect_filefirst_readiness(ws)
    launch_profile = launch_profile or build_filefirst_launch_profile(ws, doctor_report=doctor_report)

    report = LaunchCheckReport(workspace=str(ws.root), checked_at=datetime.now(timezone.utc))
    report.add(
        _launch_status(config_report),
        "config.validation",
        _validation_summary("File-first config validation", config_report),
        detail=", ".join(item.key for item in config_report.findings if item.status in {"blocked", "warning"}) or None,
        data=config_report.model_dump(mode="json"),
    )
    report.add(
        _launch_status(doctor_report),
        "doctor.readiness",
        _validation_summary("File-first doctor readiness", doctor_report),
        detail=", ".join(item.key for item in doctor_report.findings if item.status in {"blocked", "warning"}) or None,
        data=doctor_report.model_dump(mode="json"),
    )
    report.add(
        launch_profile.overall_status if launch_profile.overall_status != "warning" else "warning",
        "models.launch_profile",
        launch_profile.summary or "Role-based launch profile evaluated.",
        detail=ws.load_profile().runtime.model.model,
        data=launch_profile.model_dump(mode="json"),
    )

    automation = ws.load_profile().runtime.automation
    submit_active = bool(automation.submit_enabled and automation.default_submit_mode == "auto_submit")
    requested_sources = _requested_production_sources(ws)
    production_sources = [source_name for source_name in requested_sources if source_name in _SUPPORTED_SOURCES]
    enabled_sources = _enabled_sources(ws)
    effective_sources = _effective_sources(ws)
    unsupported = [s for s in requested_sources if s not in _SUPPORTED_SOURCES]
    if unsupported:
        report.add(
            "fail",
            "production.scope",
            f"Unsupported sources in production scope: {', '.join(unsupported)}.",
        )
    else:
        report.add("pass", "production.scope", f"Production scope: {', '.join(production_sources or enabled_sources or effective_sources) or 'none'}.")

    if not effective_sources:
        report.add(
            "fail",
            "production.sources.enabled",
            "No production sources are enabled for release.",
        )
    for source_name in effective_sources:
        label = source_name.capitalize()
        target_scope = scope_targets(ws, source_name)
        target_status, target_summary, target_detail = _source_scope_finding(
            source_name,
            {
                "configured": target_scope.configured,
                "persisted": target_scope.persisted,
                "bootstrap": target_scope.bootstrap,
            },
        )
        report.add(
            "pass",
            f"source.{source_name}.enabled",
            f"{label} is enabled for release.",
        )
        report.add(
            "pass" if target_status == "ok" else "fail",
            f"source.{source_name}.targets",
            target_summary.replace("scope ", "scope for release "),
            detail=target_detail,
        )
    report.add(
        "pass" if automation.enabled else "fail",
        "automation.enabled",
        "Autonomous mode is enabled." if automation.enabled else "Autonomous mode is disabled.",
    )
    report.add(
        "pass" if submit_active else "fail",
        "automation.submit_enabled",
        "Autonomous submit mode is enabled."
        if submit_active
        else "Autonomous submit is not fully enabled; set submit_enabled=true and default_submit_mode=auto_submit.",
        detail=f"submit_enabled={automation.submit_enabled} :: default_submit_mode={automation.default_submit_mode}",
    )
    report.add(
        "pass" if _contact_ready(ws) else "fail",
        "profile.contact_facts",
        "Contact facts are available for apply flows." if _contact_ready(ws) else "Contact facts for apply flows are missing.",
    )
    profile_surface = ws.user_profile_surface()
    if profile_surface["mode"] == "sample_mode":
        profile_detail = (
            f"{profile_surface['local_path']} :: next step: use /setup to save your real basic profile before treating the workspace as launch-ready."
        )
    elif profile_surface["mode"] == "local_user_profile":
        profile_detail = profile_surface["local_path"]
    else:
        profile_detail = ", ".join(profile_surface["active_advanced_paths"]) or profile_surface["local_path"]
    report.add(
        "pass" if profile_surface["mode"] != "sample_mode" else "fail",
        "profile.local_user_profile",
        "Local candidate profile overrides are configured for launch."
        if profile_surface["mode"] != "sample_mode"
        else "Launch certification requires local candidate data instead of the tracked sample profile.",
        detail=profile_detail,
    )
    report.add(
        "pass" if _authorization_ready(ws) else "fail",
        "profile.authorization_facts",
        "Authorization facts are available for apply flows." if _authorization_ready(ws) else "Authorization facts for apply flows are missing.",
        detail=(
            None
            if _authorization_ready(ws)
            else "Use /setup to record your work authorization and sponsorship defaults for the active workspace."
        ),
    )
    report.add(
        "pass" if _experience_fact_count(ws) else "warning",
        "profile.experience_facts",
        f"Grounded work/project/skill facts available: {_experience_fact_count(ws)}.",
    )
    report.add(
        "pass" if _cv_ready(ws) else "fail",
        "artifacts.cv",
        "Canonical CV markdown is populated." if _cv_ready(ws) else "Canonical CV markdown still contains the default placeholder.",
        detail=str(ws.cv_path),
    )
    renderer_state = _document_renderer_state(ws)
    report.add(
        "pass" if renderer_state["renderer"] in {"typst", "latex_direct", "chatgpt_download"} and renderer_state["configured"] else "fail",
        "artifacts.renderer",
        (
            "Launch PDF renderer is Typst via bundled local templates."
            if renderer_state["renderer"] == "typst" and renderer_state["configured"]
            else "Launch PDF renderer is ChatGPT download via the managed ChatGPT browser."
            if renderer_state["renderer"] == "chatgpt_download" and renderer_state["configured"]
            else "Launch PDF renderer is LaTeX-direct via the local resume and cover-letter templates."
            if renderer_state["renderer"] == "latex_direct" and renderer_state["configured"]
            else "Launch certification requires a configured Typst, ChatGPT download, or LaTeX-direct renderer."
        ),
        detail=(
            f"resume_renderer={renderer_state['renderer']} :: template={renderer_state['resume_template_path']}"
            if renderer_state["renderer"] in {"latex", "latex_direct"}
            else (
                f"resume_renderer={renderer_state['renderer']} :: gpt_url={(renderer_state.get('chatgpt_drafting') or {}).get('gpt_url')}"
                if renderer_state["renderer"] == "chatgpt_download"
                else f"resume_renderer={renderer_state['renderer']}"
            )
        ),
    )
    return report


def collect_filefirst_release_snapshot(workspace: Path | FileWorkspace) -> ReleaseSnapshotReport:
    ws = _workspace(workspace)
    config_report = inspect_filefirst_config(ws)
    doctor_report = inspect_filefirst_readiness(ws)
    launch_profile = build_filefirst_launch_profile(ws, doctor_report=doctor_report)
    launch_check = inspect_filefirst_launch_acceptance(
        ws,
        config_report=config_report,
        doctor_report=doctor_report,
        launch_profile=launch_profile,
    )
    notes: list[str] = []
    return ReleaseSnapshotReport(
        generated_at=datetime.now(timezone.utc),
        workspace=str(ws.root),
        workspace_name=ws.root.name,
        config_path=str(ws.profile_path),
        launch_check=launch_check,
        config_validation=config_report,
        doctor=doctor_report,
        launch_profile=launch_profile,
        notes=notes,
    )


__all__ = [
    "build_filefirst_launch_profile",
    "collect_filefirst_release_snapshot",
    "inspect_filefirst_config",
    "inspect_filefirst_launch_acceptance",
    "inspect_filefirst_readiness",
]
