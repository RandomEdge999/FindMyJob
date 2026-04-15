from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from findmyjob.core.enums import ModelRole
from findmyjob.core.lmstudio import LMSTUDIO_DEFAULT_HOST, LMSTUDIO_PROVIDER, probe_lmstudio_base_url
from findmyjob.web.service import OperatorConsoleService

router = APIRouter()


class DailyTriageRequest(BaseModel):
    job_id: str
    action: str
    reason_code: str | None = None
    note: str | None = None
    scope: str = "job"


class ReviewActionRequest(BaseModel):
    application_id: str
    action: str
    reason: str | None = None


class QuestionAnswerRequest(BaseModel):
    application_id: str
    question_id: str
    answer_text: str
    approve_memory: bool = False
    auto_retry: bool = True


class GreenhouseSettingsRequest(BaseModel):
    enabled: bool = True
    submit_enabled: bool = False
    boards: list[str] = Field(default_factory=list)
    browser_attach_enabled: bool = False
    browser_cdp_url: str = "http://127.0.0.1:9222"


class PortalSourceSettingsRequest(BaseModel):
    enabled: bool = True
    boards: list[str] = Field(default_factory=list)
    seed_urls: list[str] = Field(default_factory=list)
    seed_domains: list[str] = Field(default_factory=list)


class TrackedCompanyRequest(BaseModel):
    name: str
    careers_url: str | None = None
    source: str | None = None
    board: str | None = None
    api: str | None = None
    enabled: bool = True
    notes: str | None = None


class PortalSettingsRequest(BaseModel):
    sources: dict[str, PortalSourceSettingsRequest] = Field(default_factory=dict)
    tracked_companies: list[TrackedCompanyRequest] = Field(default_factory=list)


class AutonomousSettingsRequest(BaseModel):
    enabled: bool = True
    daily_submit_cap: int = Field(default=100, ge=1, le=5000)
    per_company_daily_cap: int = Field(default=2, ge=1, le=1000)
    ready_to_apply_threshold: int = Field(default=10, ge=1, le=5000)
    browser_mode: str = "headed"
    max_open_tabs: int = Field(default=6, ge=1, le=50)
    submit_enabled: bool = False
    browser_attach_enabled: bool = False
    browser_cdp_url: str = "http://127.0.0.1:9222"
    default_submit_mode: str = "preview_first"
    production_sources: list[str] = Field(default_factory=list)
    captcha_strategy: str = "skip"
    captcha_provider: str = "2captcha"
    captcha_api_key_env: str = "CAPTCHA_API_KEY"
    captcha_solve_timeout_seconds: int = Field(default=300, ge=30, le=600)


class SmokeAllowlistRequest(BaseModel):
    urls: list[str] = Field(default_factory=list)


class ChatGPTDraftingSettingsRequest(BaseModel):
    enabled: bool = True
    gpt_url: str
    completion_start_marker: str = "[[PDF_OUTPUT_READY]]"
    completion_end_marker: str = "[[PDF_OUTPUT_COMPLETE]]"
    profile_dir: str = ".fmj/browser/chatgpt-profile"
    downloads_dir: str = ".fmj/runtime/chatgpt-downloads"
    browser_mode: str = "attached"
    browser_cdp_url: str = "http://127.0.0.1:9333"
    launch_if_missing: bool = True
    use_temporary_chat: bool = False
    timeout_seconds: int = Field(default=900, ge=15, le=1800)
    prompt_submit_delay_ms: int = Field(default=300, ge=0, le=10000)
    download_timeout_seconds: int = Field(default=300, ge=10, le=1800)
    max_parallel_jobs: int = Field(default=10, ge=1, le=20)
    make_default: bool = True


class ChatGPTDraftingTestRequest(BaseModel):
    target: str | None = None


class ChatGPTBrowserLaunchRequest(BaseModel):
    close_existing: bool = False
    start_blank: bool = True


class RehearsalStartRequest(BaseModel):
    limit: int = Field(default=5, ge=1, le=10)


class RehearsalRunRequest(BaseModel):
    job_id: str
    override_rejected: bool = False


class ScreeningOverrideRequest(BaseModel):
    job_id: str
    approved: bool = True
    note: str | None = None


class ModelProfileRequest(BaseModel):
    name: str
    role: str | None = None
    provider: str = LMSTUDIO_PROVIDER
    model: str
    base_url: str | None = None
    transport: str | None = None
    api_key_env: str | None = None
    temperature: float = 0.2
    max_tokens: int | None = None
    preferred_context_window: int | None = Field(default=None, ge=8192, le=262144)
    supports_structured_output: bool = False
    fallback_chain: list[str] = Field(default_factory=list)
    policy_tags: list[str] = Field(default_factory=list)
    local: bool = False
    command: list[str] = Field(default_factory=list)
    working_dir: str | None = None


class RuntimeModelRequest(BaseModel):
    provider: str = LMSTUDIO_PROVIDER
    transport: str = "local_http"
    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    temperature: float = 0.2
    max_tokens: int | None = None
    preferred_context_window: int | None = Field(default=None, ge=8192, le=262144)
    local: bool = False
    command: list[str] = Field(default_factory=list)
    working_dir: str | None = None


class ModelPingRequest(BaseModel):
    profile_name: str | None = None
    role: str | None = None
    provider: str | None = None
    transport: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    preferred_context_window: int | None = Field(default=None, ge=8192, le=262144)
    local: bool = False
    command: list[str] = Field(default_factory=list)
    working_dir: str | None = None


class DeleteModelProfileRequest(BaseModel):
    name: str


def _console(request: Request) -> OperatorConsoleService:
    return OperatorConsoleService(request.app.state.workspace)


@router.get("/dashboard")
def dashboard(request: Request) -> dict:
    return _console(request).dashboard_payload()


class RejectJobRequest(BaseModel):
    job_id: str
    note: str | None = None


@router.post("/jobs/reject")
def reject_job(request: Request, body: RejectJobRequest) -> dict:
    """Manually reject a job so it won't be processed or applied to."""
    from findmyjob.filefirst.screening import override_screening
    from findmyjob.filefirst.workspace import FileWorkspace

    ws = FileWorkspace(request.app.state.workspace)
    ws.ensure()
    job = ws.load_job(body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {body.job_id}")
    updated_job, screening = override_screening(ws, body.job_id, approved=False, note=body.note or "Manually rejected by operator")
    ws.update_inbox_state(body.job_id, "screened_out")
    return {
        "job_id": body.job_id,
        "company": updated_job.company,
        "title": updated_job.title,
        "workflow_state": "screened_out",
        "screening_status": screening.status,
    }


@router.post("/jobs/purge-rejected")
def purge_rejected_jobs(request: Request) -> dict:
    """Delete all screened_out/rejected jobs from disk and inbox, keeping scan_history for dedup."""
    service = _console(request)
    ws = service.workspace
    inbox = ws.load_inbox()
    rejected_ids = {job.job_id for job in inbox if job.workflow_state in ("screened_out", "rejected")}
    for job_id in rejected_ids:
        ws.delete_job(job_id)
    removed = ws.remove_from_inbox(rejected_ids)
    return {"purged": len(rejected_ids), "removed_from_inbox": removed}


@router.get("/jobs/table")
def jobs_table(request: Request, limit: int | None = None) -> dict:
    return _console(request).jobs_table_payload(limit=limit, include_rejected=False)

@router.get("/jobs/applicable")
def applicable_jobs(request: Request, limit: int | None = None) -> dict:
    """Return only applicable jobs (excluding rejected/screened-out)."""
    return _console(request).jobs_table_payload(limit=limit, include_rejected=False)


@router.get("/live/status")
def live_status(request: Request, limit: int = 100) -> dict:
    return _console(request).live_status_payload(limit=limit)


@router.get("/live/traces")
def live_trace(request: Request, ref: str) -> dict:
    try:
        return _console(request).live_trace_payload(ref)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/live/events")
async def live_events(request: Request, limit: int = 100):
    console = _console(request)

    async def event_stream():
        snapshot = console.live_status_payload(limit=limit)
        yield f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"
        last_count = int(snapshot.get("state", {}).get("event_count", 0) or 0)
        while True:
            if await request.is_disconnected():
                break
            payload = console.live_status_payload(limit=limit)
            state = payload.get("state", {})
            event_count = int(state.get("event_count", 0) or 0)
            if event_count != last_count:
                last_count = event_count
                yield f"event: update\ndata: {json.dumps(payload)}\n\n"
            else:
                yield "event: heartbeat\ndata: {}\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/profile/dossier/regenerate")
def dossier_regenerate(request: Request) -> dict:
    return _console(request).regenerate_candidate_dossier_payload()


@router.get("/workflow/snapshot")
def workflow_snapshot(request: Request) -> dict:
    return _console(request).workflow_snapshot_payload()


@router.get("/setup/readiness")
def setup_readiness(request: Request) -> dict:
    return _console(request).setup_readiness_payload()


@router.post("/workspace/reset-operational")
def workspace_reset_operational(request: Request) -> dict:
    return _console(request).reset_operational_state_payload()


@router.get("/daily/inbox")
def daily_inbox(request: Request, limit: int = 12) -> dict:
    return _console(request).daily_inbox_payload(limit=limit)


@router.post("/daily/run")
def daily_run(request: Request) -> dict:
    return _console(request).run_daily()


@router.get("/autonomous/status")
def autonomous_status_get(request: Request) -> dict:
    return _console(request).autonomous_status_payload()


@router.get("/rehearsal")
def rehearsal_get(request: Request, limit: int = 5) -> dict:
    return _console(request).rehearsal_payload(limit=limit)


@router.post("/rehearsal/start")
def rehearsal_start(request: Request, payload: RehearsalStartRequest) -> dict:
    return _console(request).start_launch_rehearsal(limit=payload.limit)


@router.post("/rehearsal/override")
def rehearsal_override(request: Request, payload: ScreeningOverrideRequest) -> dict:
    try:
        return _console(request).override_job_screening(job_id=payload.job_id, approved=payload.approved, note=payload.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/rehearsal/run")
def rehearsal_run(request: Request, payload: RehearsalRunRequest) -> dict:
    try:
        return _console(request).run_launch_rehearsal(job_id=payload.job_id, override_rejected=payload.override_rejected)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/discover")
def discover_jobs(request: Request) -> JSONResponse:
    """Run job discovery across all sources (Greenhouse, Lever, Ashby) using the expanded board lists."""
    payload = _console(request).launch_discovery_run()
    return JSONResponse(status_code=202, content=payload)


@router.post("/autonomous/run")
def autonomous_run(request: Request) -> JSONResponse:
    payload = _console(request).launch_autonomous_run()
    return JSONResponse(status_code=202, content=payload)


@router.post("/daily/triage")
def daily_triage(request: Request, payload: DailyTriageRequest) -> dict:
    try:
        return _console(request).triage_job(job_id=payload.job_id, action=payload.action, reason_code=payload.reason_code, note=payload.note, scope=payload.scope)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/questions/queue")
def questions_queue(request: Request, limit: int = 20) -> dict:
    return _console(request).question_queue_payload(limit=limit)


@router.post("/questions/answer")
def question_answer(request: Request, payload: QuestionAnswerRequest) -> dict:
    try:
        return _console(request).answer_question(application_id=payload.application_id, question_id=payload.question_id, answer_text=payload.answer_text, approve_memory=payload.approve_memory, auto_retry=payload.auto_retry)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/review/queue")
def review_queue(request: Request, limit: int = 40) -> dict:
    return _console(request).review_queue_payload(limit=limit)


@router.post("/review/action")
def review_action(request: Request, payload: ReviewActionRequest) -> dict:
    try:
        return _console(request).review_application(application_id=payload.application_id, action=payload.action, reason=payload.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/applications/{application_id}")
def application_detail(request: Request, application_id: str) -> dict:
    try:
        return _console(request).application_detail_payload(application_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/history")
def runs_history(request: Request, limit: int = 20) -> dict:
    return _console(request).runs_history_payload(limit=limit)


@router.get("/settings")
def settings_get(request: Request) -> dict:
    return _console(request).settings_payload()


@router.get("/chatgpt-drafting/status")
def chatgpt_drafting_status(request: Request) -> dict:
    return _console(request).chatgpt_drafting_status_payload()


@router.post("/chatgpt-drafting/browser/launch")
def chatgpt_drafting_browser_launch(request: Request, payload: ChatGPTBrowserLaunchRequest | None = None) -> dict:
    close_existing = bool(payload.close_existing) if payload is not None else False
    start_blank = bool(payload.start_blank) if payload is not None else True
    return _console(request).launch_chatgpt_browser(close_existing=close_existing, start_blank=start_blank)


@router.post("/chatgpt-drafting/test")
def chatgpt_drafting_test(request: Request, payload: ChatGPTDraftingTestRequest | None = None) -> dict:
    try:
        return _console(request).test_chatgpt_drafting(payload.target if payload is not None else None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/settings/test")
def settings_test(request: Request) -> dict:
    return _console(request).test_config()


@router.post("/settings/greenhouse")
def settings_greenhouse(request: Request, payload: GreenhouseSettingsRequest) -> dict:
    try:
        return _console(request).save_greenhouse_settings(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/settings/portals")
def settings_portals(request: Request, payload: PortalSettingsRequest) -> dict:
    try:
        return _console(request).save_portal_settings(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/settings/autonomous")
def settings_autonomous(request: Request, payload: AutonomousSettingsRequest) -> dict:
    try:
        return _console(request).save_autonomous_settings(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/settings/chatgpt-drafting")
def settings_chatgpt_drafting(request: Request, payload: ChatGPTDraftingSettingsRequest) -> dict:
    try:
        return _console(request).save_chatgpt_drafting_settings(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/settings/runtime-model")
def settings_runtime_model(request: Request, payload: RuntimeModelRequest) -> dict:
    try:
        return _console(request).save_runtime_model(payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/settings/smoke-allowlist")
def settings_smoke_allowlist(request: Request, payload: SmokeAllowlistRequest) -> dict:
    return _console(request).save_smoke_allowlist(payload.urls)


@router.post("/settings/models")
def settings_models(request: Request, payload: ModelProfileRequest) -> dict:
    try:
        return _console(request).save_model_profile(payload.name, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/settings/models/ping")
def settings_models_ping(request: Request, payload: ModelPingRequest) -> dict:
    try:
        return _console(request).ping_model_profile(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _float_or_zero(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _normalize_generic_models(raw: dict[str, Any]) -> list[dict[str, Any]]:
    models: list[dict[str, Any]] = []
    for entry in raw.get("data", []) or raw.get("models", []) or []:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("id") or entry.get("name") or "").strip()
        if not model_id:
            continue
        name = str(entry.get("name") or model_id).strip() or model_id
        context_length = int(
            entry.get("context_length")
            or entry.get("n_ctx")
            or (entry.get("meta") or {}).get("n_ctx_train")
            or 0
        )
        models.append(
            {
                "id": model_id,
                "name": name,
                "label": f"{name} ({model_id})" if name != model_id else model_id,
                "canonical_slug": model_id,
                "description": str(entry.get("description") or "").strip(),
                "tier": "local",
                "context_length": context_length,
                "prompt_price": 0.0,
                "completion_price": 0.0,
                "request_price": 0.0,
                "supports_tools": False,
                "supports_structured_output": False,
            }
        )
    models.sort(key=lambda item: (item["name"].lower(), item["id"].lower()))
    return models


def _fetch_local_http_models(*, base_url: str | None, provider: str | None) -> dict[str, Any]:
    try:
        resolved = probe_lmstudio_base_url(base_url or LMSTUDIO_DEFAULT_HOST)
        models = _normalize_generic_models(resolved.models_payload)
        return {
            "models": models,
            "count": len(models),
            "live": True,
            "source": str(provider or LMSTUDIO_PROVIDER).strip() or LMSTUDIO_PROVIDER,
            "key_scoped": False,
            "api_key_configured": False,
            "cached": False,
            "transport": "local_http",
            "base_url": resolved.canonical_base_url,
        }
    except Exception as exc:
        return {
            "models": [],
            "count": 0,
            "live": True,
            "source": str(provider or LMSTUDIO_PROVIDER).strip() or LMSTUDIO_PROVIDER,
            "key_scoped": False,
            "api_key_configured": False,
            "cached": False,
            "transport": "local_http",
            "base_url": str(base_url or "").strip() or LMSTUDIO_DEFAULT_HOST,
            "error": str(exc),
        }


@router.get("/settings/models/available")
def settings_models_available(
    request: Request,
    refresh: bool = False,
    provider: str | None = None,
    transport: str | None = None,
    base_url: str | None = None,
    api_key_env: str | None = None,
    profile_name: str | None = None,
) -> dict:
    """Return the live LM Studio model catalog for the launch runtime."""
    if profile_name:
        try:
            router = _console(request).model_router()
            if router is not None:
                profile = router.get_profile(name=profile_name)
                provider = provider or profile.provider
                transport = transport or profile.transport
                base_url = base_url or profile.base_url
                api_key_env = api_key_env or profile.api_key_env
        except ValueError:
            pass
    resolved_base_url = str(base_url or "").strip()
    resolved_transport = str(transport or "").strip().lower()
    resolved_provider = LMSTUDIO_PROVIDER
    if resolved_transport and resolved_transport != "local_http":
        resolved_transport = "local_http"
    return _fetch_local_http_models(
        base_url=resolved_base_url or LMSTUDIO_DEFAULT_HOST,
        provider=resolved_provider,
    )


@router.post("/settings/models/recommended")
def settings_models_recommended(request: Request) -> dict:
    return _console(request).install_recommended_models()


@router.delete("/settings/models")
def settings_models_delete(request: Request, payload: DeleteModelProfileRequest) -> dict:
    try:
        return _console(request).delete_model_profile(payload.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
