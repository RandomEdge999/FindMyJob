from __future__ import annotations

import concurrent.futures
from contextlib import contextmanager
import json
import re
import shutil
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from findmyjob.apply.browser import PlaywrightSubmitter
from findmyjob.core.async_compat import run_async
from tomlkit import dumps, item, parse, table

from findmyjob.core.config import AppConfig, write_default_workspace_config
from findmyjob.core.email_otp import fetch_greenhouse_application_receipt
from findmyjob.core.filtering import CANADA_PROVINCE_CODES, US_STATE_CODES, normalize_country_code, normalize_region_code
from findmyjob.core.lmstudio import (
    LMSTUDIO_AUTO_MODEL,
    LMSTUDIO_DEFAULT_HOST,
    LMSTUDIO_PROVIDER,
    probe_lmstudio_base_url,
)
from findmyjob.core.runtime import _inspect_playwright
from findmyjob.core.enums import FactKind, JobLifecycleStatus, ModelRole, Sensitivity, VerificationStatus
from findmyjob.core.types import ApplicationQuestion, FormFieldBinding, GroundedAnswer, ModelProfile, ProfileFact, SubmissionEvidence, SubmissionPlan
from findmyjob.filefirst.advanced_models import advanced_models_payload, delete_workspace_model_profile, install_recommended_split_profiles, load_model_router, save_workspace_model_profile
from findmyjob.filefirst.dossier import candidate_dossier_metadata, regenerate_candidate_dossier
from findmyjob.filefirst.evaluate import evaluate_target
from findmyjob.filefirst.chatgpt_drafting import ChatGPTDraftingService
from findmyjob.filefirst.live_market import discover_live_market
from findmyjob.filefirst.models import ApplicationEntry, BoardDiscoveryState, LiveRunState, LocalModelSettings, RunRecord, SourceBoardConfig, SubmissionQuestion, SubmissionRecord, TrackedCompany, utcnow_iso
from findmyjob.filefirst.operator_support import _workspace_stats, begin_live_run, emit_live_event as operator_emit_live_event, finish_live_run, jobs_table_payload as operator_jobs_table_payload, live_status_payload as operator_live_status_payload
from findmyjob.filefirst.readiness import collect_filefirst_release_snapshot
from findmyjob.filefirst.screening import override_screening, screen_job, screening_payload
from findmyjob.filefirst.render import build_pdf_for_target
from findmyjob.filefirst.tracker import tracker_snapshot
from findmyjob.filefirst.workspace import FileWorkspace, SCAN_HISTORY_COLUMNS
from findmyjob.grounding.service import GroundingService
from findmyjob.sources.normalizer import build_normalized_job, parse_structured_location, slugify
from findmyjob.sources.adapters.ashby import AshbyAdapter
from findmyjob.sources.adapters.greenhouse import GreenhouseAdapter
from findmyjob.sources.adapters.lever import LeverAdapter
from findmyjob.model_router.router import ModelRouter, reset_model_trace_handler, set_model_trace_handler

_SUPPORTED_PRODUCTION_SOURCES = {"greenhouse", "lever", "ashby"}
_BACKGROUND_WORKER_RUN_TYPES = {"autonomous", "discover"}

# Terminal submission states should not continue contributing queue/prompt metrics.
_TERMINAL_SUBMISSION_STATUSES = {"submitted", "failed", "rejected", "submission_failed"}
_INACTIVE_APPLICATION_STATUSES = {"Rejected", "Dismissed", "Archived"}
_MODEL_CHECK_CACHE: dict[str, dict[str, Any]] = {}
_GREENHOUSE_LAUNCH_DEFAULT_SOURCES = ["greenhouse"]
_REGION_CODE_TO_NAME = {code: name.title() for name, code in {**US_STATE_CODES, **CANADA_PROVINCE_CODES}.items()}
_HANDLED_INBOX_STATES = {"screened_out", "dismissed", "archived", "rejected"}

class FileFirstOperatorService:
    _WORKER_LOCK = threading.RLock()
    _ACTIVE_WORKERS: dict[str, dict[str, Any]] = {}
    _MANUAL_HANDOFF_WATCH_LOCK = threading.RLock()
    _MANUAL_HANDOFF_WATCHERS: dict[str, dict[str, Any]] = {}

    def __init__(self, workspace: Path | FileWorkspace | None) -> None:
        self.workspace = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path.cwd() if workspace is None else Path(workspace))
        self.workspace.ensure()
        self._submission_registry_path = self.workspace.fmj_dir / "submission_registry.json"
        self._submission_registry = self._load_submission_registry()
        self._recover_stale_live_state()

    def _load_submission_registry(self) -> dict[str, str]:
        try:
            payload = json.loads(self._submission_registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        registry: dict[str, str] = {}
        for key, value in payload.items():
            normalized_key = str(key or "").strip()
            normalized_value = str(value or "").strip()
            if normalized_key and normalized_value:
                registry[normalized_key] = normalized_value
        return registry

    def _save_submission_registry(self) -> None:
        self.workspace.fmj_dir.mkdir(parents=True, exist_ok=True)
        self._submission_registry_path.write_text(
            json.dumps(self._submission_registry, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _remember_submission(self, application_id: str, submitted_at: str | None = None) -> None:
        self._submission_registry[application_id] = submitted_at or utcnow_iso()
        self._save_submission_registry()

    @staticmethod
    def _local_now() -> datetime:
        return datetime.now().astimezone()

    @staticmethod
    def _parse_recorded_timestamp(value: str | None) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _submitted_local_day_summary(self, *, local_day: Any | None = None) -> dict[str, Any]:
        now = self._local_now()
        target_day = local_day or now.date()
        local_tz = now.tzinfo or timezone.utc
        total = 0
        by_company: dict[str, int] = {}
        seen_application_ids: set[str] = set()

        for submission in self.workspace.load_submissions():
            if str(submission.status or "").strip().casefold() != "submitted":
                continue
            application_id = str(submission.application_id or "").strip()
            if application_id and application_id in seen_application_ids:
                continue
            timestamp = self._parse_recorded_timestamp(
                submission.submitted_at or submission.updated_at or submission.created_at
            )
            if timestamp is None or timestamp.astimezone(local_tz).date() != target_day:
                continue
            if application_id:
                seen_application_ids.add(application_id)
            total += 1
            company_key = str(submission.company or "").casefold().strip()
            if company_key:
                by_company[company_key] = int(by_company.get(company_key, 0) or 0) + 1

        return {
            "day": target_day.isoformat(),
            "total": total,
            "by_company": by_company,
        }

    @staticmethod
    def _normalize_handled_url(value: str | None) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        parsed = urlsplit(raw)
        normalized_path = parsed.path.rstrip("/") or parsed.path
        return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), normalized_path, "", ""))

    @staticmethod
    def _handled_pair(company: str | None, role: str | None) -> dict[str, str] | None:
        company_key = str(company or "").casefold().strip()
        role_key = str(role or "").casefold().strip()
        if not company_key or not role_key:
            return None
        return {"company": company_key, "role": role_key}

    def _handled_jobs_index(self) -> dict[str, set[Any]]:
        payload = self.workspace.load_handled_jobs()
        pairs: set[tuple[str, str]] = set()
        for item in payload.get("pairs") or []:
            if not isinstance(item, dict):
                continue
            company = str(item.get("company") or "").casefold().strip()
            role = str(item.get("role") or "").casefold().strip()
            if company and role:
                pairs.add((company, role))
        return {
            "job_ids": {str(item or "").strip() for item in payload.get("job_ids") or [] if str(item or "").strip()},
            "urls": {self._normalize_handled_url(item) for item in payload.get("urls") or [] if self._normalize_handled_url(item)},
            "pairs": pairs,
            "duplicate_clusters": {str(item or "").strip() for item in payload.get("duplicate_clusters") or [] if str(item or "").strip()},
        }

    def _save_handled_jobs_index(self, index: dict[str, set[Any]]) -> None:
        pairs = [
            {"company": company, "role": role}
            for company, role in sorted(index.get("pairs") or set())
        ]
        self.workspace.save_handled_jobs(
            {
                "job_ids": sorted(index.get("job_ids") or set()),
                "urls": sorted(index.get("urls") or set()),
                "pairs": pairs,
                "duplicate_clusters": sorted(index.get("duplicate_clusters") or set()),
            }
        )

    def _remember_handled_entry(
        self,
        index: dict[str, set[Any]],
        *,
        job_id: str | None = None,
        url: str | None = None,
        company: str | None = None,
        role: str | None = None,
        duplicate_cluster: str | None = None,
    ) -> None:
        normalized_job_id = str(job_id or "").strip()
        if normalized_job_id:
            index["job_ids"].add(normalized_job_id)
        normalized_url = self._normalize_handled_url(url)
        if normalized_url:
            index["urls"].add(normalized_url)
        pair = self._handled_pair(company, role)
        if pair is not None:
            index["pairs"].add((pair["company"], pair["role"]))
        normalized_cluster = str(duplicate_cluster or "").strip()
        if normalized_cluster:
            index["duplicate_clusters"].add(normalized_cluster)

    def _snapshot_handled_jobs(self) -> dict[str, set[Any]]:
        index = self._handled_jobs_index()
        inbox_by_id = {job.job_id: job for job in self.workspace.load_inbox()}
        applications = self.workspace.load_applications()
        applications_by_id = {item.id: item for item in applications}

        for application in applications:
            linked_job = inbox_by_id.get(application.job_id) or self.workspace.load_job(application.job_id)
            self._remember_handled_entry(
                index,
                job_id=application.job_id,
                url=application.url,
                company=application.company,
                role=application.role,
                duplicate_cluster=getattr(linked_job, "duplicate_cluster_key", None),
            )

        for submission in self.workspace.load_submissions():
            linked_application = applications_by_id.get(submission.application_id)
            linked_job = inbox_by_id.get(submission.job_id) or self.workspace.load_job(submission.job_id)
            self._remember_handled_entry(
                index,
                job_id=submission.job_id or getattr(linked_application, "job_id", None),
                url=(getattr(linked_application, "url", None) if linked_application is not None else None) or getattr(linked_job, "url", None),
                company=submission.company or getattr(linked_application, "company", None),
                role=submission.role or getattr(linked_application, "role", None),
                duplicate_cluster=getattr(linked_job, "duplicate_cluster_key", None),
            )

        for job in inbox_by_id.values():
            workflow_state = str(job.workflow_state or "").strip().lower()
            if workflow_state not in _HANDLED_INBOX_STATES:
                continue
            self._remember_handled_entry(
                index,
                job_id=job.job_id,
                url=job.apply_url or job.url,
                company=job.company,
                role=job.title,
                duplicate_cluster=job.duplicate_cluster_key,
            )

        self._save_handled_jobs_index(index)
        return index

    def workflow_snapshot_payload(self) -> dict[str, Any]:
        inbox = self.workspace.load_inbox()
        applications = self.workspace.load_applications()
        submissions = self.workspace.load_submissions()
        return {
            "workspace": str(self.workspace.root),
            "workspace_name": self.workspace.root.name,
            "profile_path": self.workspace.relative_path(self.workspace.profile_path),
            "counts": {
                "inbox": len(inbox),
                "applications": len(applications),
                "submissions": len(submissions),
                "reports": len(list(self.workspace.reports_dir.glob("*.md"))),
                "output_files": len([path for path in self.workspace.output_dir.glob("*") if path.is_file()]),
            },
            "tracker": tracker_snapshot(self.workspace),
        }

    def dashboard_state(self) -> dict[str, Any]:
        return {
            "snapshot": self.workflow_snapshot_payload(),
            "setup": self.setup_readiness_payload(),
            "daily": self.daily_inbox_payload(limit=8),
            "rehearsal": self.rehearsal_payload(limit=8),
            "review": self.review_queue_payload(limit=8),
            "runs": self.runs_history_payload(limit=8),
            "autonomous": self.autonomous_status_payload(),
            "jobs_table": self.jobs_table_payload(limit=100),
            "live": self.live_status_payload(limit=12),
        }

    def dashboard_payload(self) -> dict[str, Any]:
        return self.dashboard_state()

    def jobs_table_payload(self, *, limit: int | None = None, include_rejected: bool = False) -> dict[str, Any]:
        return operator_jobs_table_payload(self.workspace, limit=limit, include_rejected=include_rejected)

    def live_status_payload(self, *, limit: int = 100) -> dict[str, Any]:
        payload = operator_live_status_payload(self.workspace, limit=limit)
        state = dict(payload.get("state") or {})
        if str(state.get("run_type") or "").strip().lower() != "autonomous":
            return payload
        if str(state.get("status") or "").strip().lower() in {"completed", "completed_with_failures", "failed", "interrupted", "blocked"}:
            return payload

        try:
            drafting = ChatGPTDraftingService(self.workspace).status_payload()
        except Exception:
            return payload

        progress = dict(drafting.get("progress") or {})
        batch = dict(drafting.get("batch") or {})
        if str(progress.get("status") or "").strip().lower() != "running":
            return payload

        company = str(progress.get("company") or state.get("current_company") or state.get("company") or "").strip() or None
        role = str(progress.get("role") or state.get("current_role") or state.get("role") or "").strip() or None
        current_title = f"{company} / {role}" if company and role else state.get("current_title")
        state.update(
            {
                "status": "running",
                "stage": "drafting",
                "active_step": "drafting",
                "stream_health": "connected",
                "active_application_id": progress.get("application_id") or state.get("active_application_id"),
                "active_job_id": progress.get("job_id") or state.get("active_job_id"),
                "company": company or state.get("company"),
                "role": role or state.get("role"),
                "current_company": company or state.get("current_company"),
                "current_role": role or state.get("current_role"),
                "current_title": current_title,
                "latest_operator_message": progress.get("last_observation") or state.get("latest_operator_message"),
            }
        )
        payload["state"] = state
        payload["drafting"] = {**progress, "batch": batch}
        return payload

    def live_trace_payload(self, trace_ref: str) -> dict[str, Any]:
        payload = self.workspace.load_live_trace(trace_ref)
        return {"trace_ref": trace_ref, "kind": "summary", "payload": payload}

    def regenerate_candidate_dossier_payload(self) -> dict[str, Any]:
        return regenerate_candidate_dossier(self.workspace)

    def _default_submit_mode(self) -> str:
        return self.workspace.load_profile().runtime.automation.default_submit_mode

    def _configured_supported_sources(self, *, include_disabled: bool = False) -> list[str]:
        portals = self.workspace.load_portals()
        sources: list[str] = []
        for source_name, source_config in portals.sources.items():
            if source_name not in _SUPPORTED_PRODUCTION_SOURCES:
                continue
            if include_disabled or getattr(source_config, 'enabled', False):
                sources.append(source_name)
        return sources

    def _effective_production_sources(self, requested: list[str] | None = None) -> list[str]:
        candidates = [str(item).strip().lower() for item in (requested or []) if str(item).strip()]
        filtered = [item for item in candidates if item in _SUPPORTED_PRODUCTION_SOURCES]
        if filtered:
            return list(dict.fromkeys(filtered))
        enabled = self._configured_supported_sources()
        if enabled:
            return enabled
        existing = [item for item in self.workspace.load_profile().runtime.automation.production_sources if item in _SUPPORTED_PRODUCTION_SOURCES]
        if existing:
            return list(dict.fromkeys(existing))
        return list(_GREENHOUSE_LAUNCH_DEFAULT_SOURCES)

    def _sync_portal_sources(self, active_sources: list[str]) -> dict[str, bool]:
        active = set(active_sources)
        portals = self.workspace.load_portals()
        changed = False
        for source_name, source_config in portals.sources.items():
            if source_name not in _SUPPORTED_PRODUCTION_SOURCES:
                continue
            enabled = source_name in active
            if bool(getattr(source_config, "enabled", False)) != enabled:
                source_config.enabled = enabled
                changed = True
        if changed:
            self.workspace.save_portals(portals)
        return {
            source_name: bool(getattr(source_config, "enabled", False))
            for source_name, source_config in portals.sources.items()
            if source_name in _SUPPORTED_PRODUCTION_SOURCES
        }

    def _load_workspace_config_doc(self) -> tuple[Any, Path]:
        config_path = self.workspace.workspace_config_path
        if not config_path.exists():
            write_default_workspace_config(config_path)
        return parse(config_path.read_text(encoding="utf-8")), config_path

    def _sync_workspace_runtime_config(
        self,
        *,
        automation: Any | None = None,
        active_sources: list[str] | None = None,
        greenhouse_browser_attach_enabled: bool | None = None,
        greenhouse_browser_cdp_url: str | None = None,
    ) -> None:
        profile = self.workspace.load_profile()
        portals = self.workspace.load_portals()
        automation = automation or profile.runtime.automation
        selected_sources = self._effective_production_sources(active_sources or automation.production_sources)
        doc, config_path = self._load_workspace_config_doc()
        submit_active = bool(automation.submit_enabled and automation.default_submit_mode == "auto_submit")

        policy_tbl = doc.get("policy")
        if policy_tbl is None:
            policy_tbl = table()
            doc["policy"] = policy_tbl
        policy_tbl["default_application_mode"] = "auto_submit" if submit_active else "dry_run"
        policy_tbl["require_human_review_for_submit"] = not submit_active
        policy_tbl["default_source_policy"] = "human_in_loop_submit" if submit_active else "review_only"

        autonomous_tbl = doc.get("autonomous")
        if autonomous_tbl is None:
            autonomous_tbl = table()
            doc["autonomous"] = autonomous_tbl
        autonomous_tbl["enabled"] = bool(automation.enabled)
        autonomous_tbl["source"] = selected_sources[0] if selected_sources else "greenhouse"
        autonomous_tbl["browser_mode"] = str(automation.browser_mode or "headed")
        autonomous_tbl["max_open_tabs"] = int(automation.max_open_tabs)
        autonomous_tbl["daily_submit_cap"] = int(automation.daily_submit_cap)
        autonomous_tbl["per_company_daily_cap"] = int(automation.per_company_daily_cap)

        sources_tbl = doc.get("sources")
        if sources_tbl is None:
            sources_tbl = table()
            doc["sources"] = sources_tbl
        for source_name in ("greenhouse", "lever", "ashby"):
            source_tbl = sources_tbl.get(source_name)
            if source_tbl is None:
                source_tbl = table()
                sources_tbl[source_name] = source_tbl
            portal_source = portals.sources.get(source_name)
            enabled = bool(getattr(portal_source, "enabled", False) if portal_source is not None else source_name in selected_sources)
            source_tbl["enabled"] = enabled
            source_tbl["submit_enabled"] = bool(enabled and submit_active)
            if "boards" not in source_tbl:
                source_tbl["boards"] = item([])
            if "seed_urls" not in source_tbl:
                source_tbl["seed_urls"] = item([])
            if "seed_domains" not in source_tbl:
                source_tbl["seed_domains"] = item([])
            if "use_builtin_board_universe" not in source_tbl:
                source_tbl["use_builtin_board_universe"] = True
            if source_name == "greenhouse":
                attach_enabled = (
                    bool(greenhouse_browser_attach_enabled)
                    if greenhouse_browser_attach_enabled is not None
                    else bool(automation.browser_attach_enabled)
                )
                source_tbl["browser_attach_enabled"] = attach_enabled
                resolved_cdp_url = (
                    str(greenhouse_browser_cdp_url or "").strip()
                    if greenhouse_browser_cdp_url is not None
                    else str(automation.browser_cdp_url or "").strip()
                )
                source_tbl["browser_cdp_url"] = resolved_cdp_url or "http://127.0.0.1:9222"
                if "browser_jobs_url" not in source_tbl:
                    source_tbl["browser_jobs_url"] = "https://my.greenhouse.io/jobs"

        config_path.write_text(dumps(doc), encoding="utf-8")

    @staticmethod
    def _normalize_list_payload(value: Any) -> list[str]:
        if isinstance(value, str):
            raw_items = value.replace(",", "\n").splitlines()
        else:
            raw_items = list(value or [])
        normalized: list[str] = []
        for item in raw_items:
            cleaned = str(item or "").strip()
            if cleaned and cleaned not in normalized:
                normalized.append(cleaned)
        return normalized

    @staticmethod
    def _normalize_domain_list(value: Any) -> list[str]:
        return [item.lower() for item in FileFirstOperatorService._normalize_list_payload(value)]

    @staticmethod
    def _resolve_model_role(value: str | None) -> ModelRole:
        if not value:
            return ModelRole.WRITER
        try:
            return ModelRole(str(value).strip())
        except ValueError as exc:
            raise ValueError(f"Unknown model role: {value}") from exc

    def model_router(self):
        return load_model_router(self.workspace)

    def _model_check_state(self) -> dict[str, Any]:
        return _MODEL_CHECK_CACHE.setdefault(self._workspace_key(), {})

    def _record_model_check(self, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        stored = dict(payload)
        stored["checked_at"] = utcnow_iso()
        self._model_check_state()[key] = stored
        return stored

    def _runtime_model_profile(self, payload: dict[str, Any] | None = None, *, name: str = "runtime-model", role: str | None = None) -> ModelProfile:
        runtime_model = self.workspace.load_profile().runtime.model
        merged = runtime_model.model_dump(mode="json")
        if payload:
            merged.update({key: value for key, value in payload.items() if value not in (None, "") or key in {"command"}})
        settings = LocalModelSettings.model_validate(merged)
        return ModelProfile(
            name=name,
            role=self._resolve_model_role(role),
            provider=str(settings.provider or LMSTUDIO_PROVIDER).strip() or LMSTUDIO_PROVIDER,
            model=str(settings.model or "").strip(),
            local=bool(settings.local),
            transport=str(settings.transport or "").strip() or None,
            temperature=float(settings.temperature),
            max_tokens=int(settings.max_tokens) if settings.max_tokens is not None else None,
            supports_structured_output=False,
            fallback_chain=[],
            policy_tags=["runtime_default"],
            base_url=str(settings.base_url or "").strip() or None,
            api_key_env=str(settings.api_key_env or "").strip() or None,
            command=[str(item) for item in list(settings.command or []) if str(item).strip()],
            working_dir=str(settings.working_dir or "").strip() or None,
        )

    @staticmethod
    def _normalize_provider_transport(provider: str | None, transport: str | None) -> tuple[str, str]:
        normalized_provider = str(provider or LMSTUDIO_PROVIDER).strip() or LMSTUDIO_PROVIDER
        normalized_transport = str(transport or "").strip() or ("local_http" if normalized_provider == LMSTUDIO_PROVIDER else "remote_http")
        if normalized_provider == LMSTUDIO_PROVIDER:
            normalized_transport = "local_http"
        return normalized_provider, normalized_transport

    @staticmethod
    def _normalize_local_http_settings(
        *,
        provider: str,
        transport: str,
        base_url: str | None,
        api_key_env: str | None,
    ) -> tuple[str, str, str | None, str | None, bool]:
        normalized_provider, normalized_transport = FileFirstOperatorService._normalize_provider_transport(provider, transport)
        normalized_base_url = str(base_url or "").strip() or None
        normalized_api_key_env = str(api_key_env or "").strip() or None
        if normalized_transport == "local_http":
            resolved = probe_lmstudio_base_url(normalized_base_url or LMSTUDIO_DEFAULT_HOST)
            normalized_base_url = resolved.canonical_base_url
            normalized_api_key_env = None
            return normalized_provider, normalized_transport, normalized_base_url, normalized_api_key_env, True
        return normalized_provider, normalized_transport, normalized_base_url, normalized_api_key_env, normalized_transport == "process"

    def _workspace_key(self) -> str:
        return str(self.workspace.root.resolve())

    @staticmethod
    def _run_metric_summary(metrics: dict[str, Any] | None) -> dict[str, Any]:
        payload = dict(metrics or {})
        summary: dict[str, Any] = {}
        for key, value in payload.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                summary[str(key)] = value
                continue
            if isinstance(value, list):
                summary[f"{key}_count"] = len(value)
                continue
            if isinstance(value, dict):
                nested_summary: dict[str, Any] = {}
                for nested_key, nested_value in value.items():
                    if isinstance(nested_value, (str, int, float, bool)) or nested_value is None:
                        nested_summary[str(nested_key)] = nested_value
                    elif isinstance(nested_value, list):
                        nested_summary[f"{nested_key}_count"] = len(nested_value)
                if nested_summary:
                    summary[str(key)] = nested_summary
        return summary

    def _run_record_summary(self, run: RunRecord | dict[str, Any]) -> dict[str, Any]:
        payload = run.model_dump(mode="json") if isinstance(run, RunRecord) else dict(run or {})
        processed_job_ids = list(payload.get("processed_job_ids") or [])
        evaluated_application_ids = list(payload.get("evaluated_application_ids") or [])
        submitted_application_ids = list(payload.get("submitted_application_ids") or [])
        failed_application_ids = list(payload.get("failed_application_ids") or [])
        notes = [str(item) for item in list(payload.get("notes") or []) if str(item).strip()]
        return {
            "run_id": payload.get("run_id"),
            "run_type": payload.get("run_type"),
            "status": payload.get("status"),
            "event_status": payload.get("event_status"),
            "started_at": payload.get("started_at"),
            "completed_at": payload.get("completed_at"),
            "processed_count": len(processed_job_ids),
            "evaluated_count": len(evaluated_application_ids),
            "submitted_count": len(submitted_application_ids),
            "failed_count": len(failed_application_ids),
            "submitted_application_ids": submitted_application_ids,
            "failed_application_ids": failed_application_ids,
            "notes": notes[:6],
            "note_count": len(notes),
            "metrics": self._run_metric_summary(payload.get("metrics")),
        }

    def _active_worker(self) -> dict[str, Any] | None:
        with self._WORKER_LOCK:
            return dict(self._ACTIVE_WORKERS.get(self._workspace_key()) or {}) or None

    def _recover_stale_live_state(self) -> None:
        state = self.workspace.load_live_state()
        if state.status != 'running' or not state.run_id:
            return
        if str(state.run_type or "").strip().lower() not in _BACKGROUND_WORKER_RUN_TYPES:
            return
        worker = self._active_worker()
        if worker and worker.get('run_id') == state.run_id and worker.get('thread') and worker['thread'].is_alive():
            return
        try:
            ChatGPTDraftingService(self.workspace).recover_stale_batch()
        except Exception:
            pass
        finish_live_run(
            self.workspace,
            run_id=state.run_id,
            run_type=state.run_type or 'unknown',
            status='interrupted',
            stage=state.stage or 'idle',
            message='Run interrupted by server restart or worker loss.',
            latest_error='Stale live state recovered without an active worker.',
        )

    def _register_worker(self, *, run_id: str, run_type: str, thread: threading.Thread) -> None:
        with self._WORKER_LOCK:
            self._ACTIVE_WORKERS[self._workspace_key()] = {'run_id': run_id, 'run_type': run_type, 'thread': thread}

    def _clear_worker(self, *, run_id: str) -> None:
        with self._WORKER_LOCK:
            current = self._ACTIVE_WORKERS.get(self._workspace_key())
            if current and current.get('run_id') == run_id:
                self._ACTIVE_WORKERS.pop(self._workspace_key(), None)

    def _launch_background_run(self, *, run_type: str, stage: str, message: str, runner) -> dict[str, Any]:
        self._recover_stale_live_state()
        current_state = self.workspace.load_live_state()
        if current_state.status == 'running' and current_state.run_id:
            snapshot = self.live_status_payload(limit=50)
            return {
                'accepted': False,
                'duplicate': True,
                'run_id': current_state.run_id,
                'run_type': current_state.run_type,
                'status': current_state.status,
                'state': snapshot['state'],
                'events': snapshot['events'],
            }

        run_id = self._new_run_id(run_type[:4] or run_type)
        begin_live_run(self.workspace, run_id=run_id, run_type=run_type, stage=stage, message=message)

        def _runner() -> None:
            try:
                runner(run_id=run_id, prestarted=True)
            except Exception as exc:  # pragma: no cover - defensive recovery
                state = self.workspace.load_live_state()
                if state.run_id == run_id and state.status == 'running':
                    finish_live_run(
                        self.workspace,
                        run_id=run_id,
                        run_type=run_type,
                        status='failed',
                        stage=state.stage or stage,
                        message=f'{run_type.title()} run failed.',
                        latest_error=str(exc),
                    )
            finally:
                self._clear_worker(run_id=run_id)

        thread = threading.Thread(target=_runner, name=f'fmj-{run_type}-{run_id}', daemon=True)
        self._register_worker(run_id=run_id, run_type=run_type, thread=thread)
        thread.start()
        snapshot = self.live_status_payload(limit=50)
        return {
            'accepted': True,
            'duplicate': False,
            'run_id': run_id,
            'run_type': run_type,
            'status': 'running',
            'state': snapshot['state'],
            'events': snapshot['events'],
        }

    def launch_discovery_run(self) -> dict[str, Any]:
        return self._launch_background_run(run_type='discover', stage='discovery', message='Discovery run started.', runner=self.run_discover)

    def launch_autonomous_run(self) -> dict[str, Any]:
        return self._launch_background_run(run_type='autonomous', stage='queue', message='Autonomous queue run started.', runner=self.run_autonomous)

    def _discovery_progress_message(self, payload: dict[str, Any]) -> str:
        phase = str(payload.get("phase") or "discovery").strip()
        if phase == "seed_crawl":
            targets = sum(len(values) for values in (payload.get("board_targets") or {}).values())
            return f"Seed crawl: {int(payload.get('crawled_pages', 0) or 0)} pages, {targets} board targets found."
        if phase == "seed_complete":
            return (
                f"Seed crawl complete: {int(payload.get('crawled_pages', 0) or 0)} pages, "
                f"{int(payload.get('boards_total', 0) or 0)} source boards queued."
            )
        if phase == "source_board_progress":
            source_name = str(payload.get("source") or "source")
            board = str(payload.get("board") or "board")
            return (
                f"Scanning {source_name}/{board}: "
                f"{int(payload.get('discovered', 0) or 0)} seen, "
                f"{int(payload.get('accepted_count', 0) or 0)} kept so far."
            )
        if phase == "source_board":
            source_name = str(payload.get("source") or "source")
            board = str(payload.get("board") or "board")
            return (
                f"Scanned {source_name}/{board}: "
                f"{int(payload.get('discovered', 0) or 0)} seen, "
                f"{int(payload.get('accepted_count', 0) or 0)} kept."
            )
        if phase == "unsupported_seed":
            return (
                f"Seed ATS fallback: {int(payload.get('unsupported_processed', 0) or 0)}/"
                f"{int(payload.get('unsupported_total', 0) or 0)} processed."
            )
        return "Discovery is scanning live sources."

    def _emit_discovery_progress(self, *, run_id: str, run_type: str, payload: dict[str, Any]) -> None:
        source_metrics = dict(payload.get("source_metrics") or {})
        total_discovered = 0
        total_eligible = 0
        total_rejected = 0
        if source_metrics:
            for item in source_metrics.values():
                if not isinstance(item, dict):
                    continue
                total_discovered += int(item.get("jobs_discovered", 0) or 0)
                total_eligible += int(item.get("eligible_jobs", 0) or 0)
                total_rejected += int(item.get("rejected_jobs", 0) or 0)
        else:
            total_eligible = sum(int(value or 0) for value in dict(payload.get("eligible_by_source") or {}).values())
            total_rejected = sum(int(value or 0) for value in dict(payload.get("rejected_by_source") or {}).values())
        stats = {
            "discovery_scanned": total_discovered or int(payload.get("discovered", 0) or 0),
            "discovery_duplicates": int(payload.get("duplicates", 0) or 0),
            "discovered": total_eligible or int(payload.get("accepted_count", 0) or 0),
            "eligible_after_filters": total_eligible or int(payload.get("eligible_count", 0) or 0),
            "queued_for_apply": total_eligible or int(payload.get("eligible_count", 0) or 0),
            "deterministic_rejects": total_rejected or int(payload.get("rejected_count", 0) or 0),
            "discovery_boards_completed": int(payload.get("boards_completed", 0) or 0),
            "discovery_boards_total": int(payload.get("boards_total", 0) or 0),
            "discovery_seed_pages": int(payload.get("crawled_pages", 0) or 0),
            "discovery_unsupported": int(payload.get("unsupported_urls", 0) or payload.get("unsupported_total", 0) or 0),
            "discovery_errors": int(payload.get("errors_count", 0) or 0),
            "source_mix": dict(payload.get("source_counts") or {}),
            "discovery_eligible_by_source": dict(payload.get("eligible_by_source") or {}),
            "discovery_rejected_by_source": dict(payload.get("rejected_by_source") or {}),
            "discovery_error_counts": dict(payload.get("error_counts") or {}),
            "source_metrics": source_metrics,
            "source_warnings": list(payload.get("warnings") or []),
            "zero_result_sources": list(payload.get("zero_result_sources") or []),
        }
        operator_emit_live_event(
            self.workspace,
            run_id=run_id,
            run_type=run_type,
            event_type=f"{run_type}.discovery.progress",
            stage="discovery",
            status="running",
            message=self._discovery_progress_message(payload),
            source=str(payload.get("source") or "") or None,
            step="discovery",
            payload=payload,
            state_updates={"stats": stats},
        )

    def _run_discovery_scan(self, *, run_id: str, run_type: str, limit: int = 50) -> dict[str, Any]:
        operator_emit_live_event(
            self.workspace,
            run_id=run_id,
            run_type=run_type,
            event_type=f"{run_type}.discovery.started",
            stage="discovery",
            status="running",
            message="Discovering live-market jobs.",
            step="discovery",
        )
        scan_result = run_async(
            lambda: discover_live_market(
                self.workspace,
                limit=limit,
                progress_callback=lambda payload: self._emit_discovery_progress(run_id=run_id, run_type=run_type, payload=payload),
            )
        )
        operator_emit_live_event(
            self.workspace,
            run_id=run_id,
            run_type=run_type,
            event_type=f"{run_type}.discovery.completed",
            stage="discovery",
            status="running",
            message=f"Discovery saved {scan_result.get('new_jobs', 0)} new jobs.",
            payload=scan_result,
            step="discovery",
            state_updates={
                "stats": {
                    "discovery_scanned": int(scan_result.get("discovered", 0) or 0),
                    "discovered": len(scan_result.get("saved_job_ids") or []),
                    "eligible_after_filters": len(scan_result.get("eligible_job_ids") or []),
                    "deterministic_rejects": int(scan_result.get("rejected_count", len(scan_result.get("skipped_job_ids") or [])) or 0),
                    "queued_for_apply": len(scan_result.get("eligible_job_ids") or []),
                    "discovery_duplicates": int(scan_result.get("duplicates", 0) or 0),
                    "discovery_errors": len(scan_result.get("errors") or []),
                    "source_mix": dict(scan_result.get("source_counts") or {}),
                    "discovery_eligible_by_source": dict(scan_result.get("eligible_by_source") or {}),
                    "discovery_rejected_by_source": dict(scan_result.get("rejected_by_source") or {}),
                    "discovery_error_counts": dict(scan_result.get("error_counts") or {}),
                    "source_metrics": dict(scan_result.get("source_metrics") or {}),
                    "source_warnings": list(scan_result.get("warnings") or []),
                    "zero_result_sources": list(scan_result.get("zero_result_sources") or []),
                }
            },
        )
        return scan_result

    def _artifact_paths_from_payload(self, payload: Any) -> list[str]:
        if not isinstance(payload, dict):
            return []
        candidates: list[Any] = []
        for key, value in payload.items():
            if not value:
                continue
            if key.endswith('_path'):
                candidates.append(value)
            elif key.endswith('_paths') and isinstance(value, (list, tuple, set)):
                candidates.extend(list(value))
            elif key == 'artifacts' and isinstance(value, dict):
                candidates.extend(value.values())
        normalized: list[str] = []
        for item in candidates:
            cleaned = str(item or '').strip()
            if cleaned and cleaned not in normalized:
                normalized.append(cleaned)
        return normalized

    def _browser_runtime_blocker(self) -> dict[str, Any] | None:
        inspection = _inspect_playwright()
        if inspection.get('package_ok') and inspection.get('browser_ok'):
            return None
        detail_parts = [
            str(inspection.get('package_detail') or '').strip(),
            str(inspection.get('browser_detail') or '').strip(),
        ]
        return {
            'key': 'runtime.playwright',
            'message': '; '.join([item for item in detail_parts if item]) or 'Playwright browser runtime is not ready.',
            'inspection': inspection,
        }

    @staticmethod
    def _truncate_trace_text(value: Any, *, limit: int = 240) -> str | None:
        text = str(value or '').strip()
        if not text:
            return None
        if len(text) <= limit:
            return text
        return text[: max(limit - 3, 1)].rstrip() + '...'

    @classmethod
    def _artifact_trace_summary(cls, artifacts: Any) -> dict[str, Any] | None:
        if isinstance(artifacts, dict):
            cleaned = {
                str(key): str(value)
                for key, value in artifacts.items()
                if str(key or '').strip() and str(value or '').strip()
            }
            return {
                'count': len(cleaned),
                'keys': sorted(cleaned),
                'paths': cleaned,
            } if cleaned else None
        if isinstance(artifacts, (list, tuple, set)):
            cleaned_list = [str(value) for value in artifacts if str(value or '').strip()]
            return {
                'count': len(cleaned_list),
                'paths': cleaned_list,
            } if cleaned_list else None
        return None

    @classmethod
    def _question_trace_summary(cls, questions: Any) -> dict[str, Any] | None:
        if not isinstance(questions, list):
            return None
        items = [item for item in questions if isinstance(item, dict)]
        if not items:
            return None
        question_types: dict[str, int] = {}
        verification_statuses: dict[str, int] = {}
        required_count = 0
        needs_input_count = 0
        manual_override_count = 0
        sensitive_count = 0
        answered_count = 0
        for item in items:
            question_type = str(item.get('question_type') or 'unknown').strip() or 'unknown'
            verification = str(item.get('verification_status') or 'unknown').strip() or 'unknown'
            question_types[question_type] = question_types.get(question_type, 0) + 1
            verification_statuses[verification] = verification_statuses.get(verification, 0) + 1
            if bool(item.get('required')):
                required_count += 1
            if bool(item.get('needs_user_input')):
                needs_input_count += 1
            if bool(item.get('manual_override')):
                manual_override_count += 1
            if bool(item.get('sensitive')):
                sensitive_count += 1
            if str(item.get('answer') or '').strip():
                answered_count += 1
        return {
            'count': len(items),
            'answered_count': answered_count,
            'required_count': required_count,
            'needs_user_input_count': needs_input_count,
            'manual_override_count': manual_override_count,
            'sensitive_count': sensitive_count,
            'question_types': question_types,
            'verification_statuses': verification_statuses,
        }

    @classmethod
    def _submission_result_summary(cls, payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        summary = {
            'status': str(payload.get('status') or '').strip() or None,
            'submitted': bool(payload.get('submitted')) if 'submitted' in payload else None,
            'uncertain': bool(payload.get('uncertain')) if 'uncertain' in payload else None,
            'external_id': str(payload.get('external_id') or '').strip() or None,
            'message': cls._truncate_trace_text(payload.get('message')),
            'questions': cls._question_trace_summary(payload.get('questions')),
            'artifacts': cls._artifact_trace_summary(payload.get('artifacts')),
            'evidence': cls._artifact_trace_summary(payload.get('evidence')),
        }
        return {key: value for key, value in summary.items() if value not in (None, '', {}, [])} or None

    @classmethod
    def _model_trace_summary(cls, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {
                'trace_kind': 'model_call_summary',
                'summary_version': 1,
                'observed_type': type(payload).__name__,
            }
        summary = {
            'trace_kind': 'model_call_summary',
            'summary_version': 1,
            'observed_keys': sorted(payload.keys()),
            'call_id': str(payload.get('call_id') or '').strip() or None,
            'status': str(payload.get('status') or '').strip() or None,
            'started_at': payload.get('started_at'),
            'completed_at': payload.get('completed_at'),
            'role': str(payload.get('role') or '').strip() or None,
            'mode': str(payload.get('mode') or '').strip() or None,
            'profile_name': str(payload.get('profile_name') or '').strip() or None,
            'provider': str(payload.get('provider') or '').strip() or None,
            'model': str(payload.get('model') or '').strip() or None,
            'transport': str(payload.get('transport') or '').strip() or None,
            'attempt': int(payload.get('attempt') or 0) or None,
            'chain': list(payload.get('chain') or []),
            'fallback_from': str(payload.get('fallback_from') or '').strip() or None,
            'prompt_hash': str(payload.get('prompt_hash') or '').strip() or None,
            'latency_ms': payload.get('latency_ms'),
            'usage': dict(payload.get('usage') or {}),
            'error': payload.get('error') if isinstance(payload.get('error'), dict) else {'message': cls._truncate_trace_text(payload.get('error'))} if payload.get('error') else None,
            'response_kind': str(payload.get('response_kind') or '').strip() or None,
            'response_chars': payload.get('response_chars'),
            'parsed_output_kind': str(payload.get('parsed_output_kind') or '').strip() or None,
            'parsed_output_keys': list(payload.get('parsed_output_keys') or []),
            'parsed_output_count': payload.get('parsed_output_count'),
        }
        return {key: value for key, value in summary.items() if value not in (None, '', {}, [])}

    @classmethod
    def _submission_trace_summary(cls, name: str, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {
                'trace_kind': 'submission_step_summary',
                'summary_version': 1,
                'step_name': name,
                'observed_type': type(payload).__name__,
            }
        blocked = payload.get('blocked')
        blocked_summary = None
        if isinstance(blocked, dict):
            blocked_summary = {
                'key': str(blocked.get('key') or '').strip() or None,
                'message': cls._truncate_trace_text(blocked.get('message')),
                'inspection_keys': sorted((blocked.get('inspection') or {}).keys()) if isinstance(blocked.get('inspection'), dict) else [],
            }
            blocked_summary = {key: value for key, value in blocked_summary.items() if value not in (None, '', {}, [])}
        plan = payload.get('plan')
        plan_summary = None
        if isinstance(plan, dict):
            fields = list(plan.get('fields') or [])
            plan_summary = {
                'field_count': len(fields),
                'missing_required_count': len(list(plan.get('missing_required_fields') or [])),
                'note_count': len(list(plan.get('notes') or [])),
            }
        summary = {
            'trace_kind': 'submission_step_summary',
            'summary_version': 1,
            'step_name': name,
            'observed_keys': sorted(payload.keys()),
            'application_id': str(payload.get('application_id') or '').strip() or None,
            'job_id': str(payload.get('job_id') or '').strip() or None,
            'company': str(payload.get('company') or '').strip() or None,
            'role': str(payload.get('role') or '').strip() or None,
            'source': str(payload.get('source') or '').strip() or None,
            'submit_ready': bool(payload.get('submit_ready')) if 'submit_ready' in payload else None,
            'error': cls._truncate_trace_text(payload.get('error')),
            'preview_issue': cls._truncate_trace_text(payload.get('preview_issue')),
            'blocked': blocked_summary,
            'artifacts': cls._artifact_trace_summary(payload.get('artifacts')),
            'questions': cls._question_trace_summary(payload.get('questions')),
            'missing_required_fields': len(list(payload.get('missing_required_fields') or [])) if payload.get('missing_required_fields') is not None else None,
            'ungrounded_answers': len(list(payload.get('ungrounded_answers') or [])) if payload.get('ungrounded_answers') is not None else None,
            'low_confidence_answers': len(list(payload.get('low_confidence_answers') or [])) if payload.get('low_confidence_answers') is not None else None,
            'plan': plan_summary,
            'result': cls._submission_result_summary(payload.get('result')),
        }
        return {key: value for key, value in summary.items() if value not in (None, '', {}, [])}

    @classmethod
    def _generic_trace_summary(cls, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            summary = {
                'trace_kind': 'summary',
                'summary_version': 1,
                'observed_keys': sorted(payload.keys()),
            }
            for key in ('application_id', 'job_id', 'company', 'role', 'source', 'status'):
                value = str(payload.get(key) or '').strip()
                if value:
                    summary[key] = value
            if payload.get('error'):
                summary['error'] = cls._truncate_trace_text(payload.get('error'))
            return summary
        return {
            'trace_kind': 'summary',
            'summary_version': 1,
            'observed_type': type(payload).__name__,
        }

    @classmethod
    def _trace_payload_summary(cls, *, category: str, name: str, payload: Any) -> dict[str, Any]:
        if category == 'model-calls':
            return cls._model_trace_summary(payload)
        if category == 'submission-steps':
            return cls._submission_trace_summary(name, payload)
        return cls._generic_trace_summary(payload)

    def _persist_trace_payload(self, run_id: str, *, category: str, name: str, payload: dict[str, Any]) -> str | None:
        try:
            summary = self._trace_payload_summary(category=category, name=name, payload=payload)
            return self.workspace.write_live_trace(run_id, category=category, name=name, payload=summary)
        except Exception:
            return None

    def _emit_runtime_event(
        self,
        *,
        run_id: str,
        run_type: str,
        event_type: str,
        message: str,
        stage: str,
        phase: str,
        status: str,
        job_id: str | None = None,
        application_id: str | None = None,
        submission_id: str | None = None,
        company: str | None = None,
        role: str | None = None,
        source: str | None = None,
        model_role: str | None = None,
        model_profile: str | None = None,
        model_call_id: str | None = None,
        artifact_paths: list[str] | dict[str, Any] | None = None,
        error: dict[str, Any] | str | Exception | None = None,
        metrics: dict[str, Any] | None = None,
        trace_ref: str | None = None,
        payload: dict[str, Any] | None = None,
        state_updates: dict[str, Any] | None = None,
    ) -> None:
        merged_state_updates = dict(state_updates or {})
        if error is None and 'latest_error' not in merged_state_updates and status in {'running', 'queued', 'starting', 'completed', 'success', 'skipped'}:
            # Clear stale failures once the workflow is moving or has finished successfully.
            merged_state_updates['latest_error'] = None
        operator_emit_live_event(
            self.workspace,
            run_id=run_id,
            run_type=run_type,
            event_type=event_type,
            phase=phase,
            stage=stage,
            status=status,
            message=message,
            job_id=job_id,
            application_id=application_id,
            submission_id=submission_id,
            company=company,
            role=role,
            source=source,
            model_role=model_role,
            model_profile=model_profile,
            model_call_id=model_call_id,
            artifact_paths=artifact_paths,
            error=error,
            metrics=metrics,
            trace_ref=trace_ref,
            payload=payload,
            state_updates=merged_state_updates,
            step=phase,
        )

    @contextmanager
    def _model_trace_context(
        self,
        *,
        run_id: str,
        run_type: str,
        stage: str,
        phase: str,
        job_id: str | None = None,
        application_id: str | None = None,
        submission_id: str | None = None,
        company: str | None = None,
        role: str | None = None,
        source: str | None = None,
    ):
        def _handler(trace: dict[str, Any]) -> None:
            trace_status = str(trace.get('status') or 'info').strip().lower() or 'info'
            trace_ref = None
            if trace_status in {'completed', 'failed'}:
                trace_ref = self._persist_trace_payload(
                    run_id,
                    category='model-calls',
                    name=f"{phase}-{trace.get('call_id') or uuid.uuid4().hex[:8]}",
                    payload=trace,
                )
            latency_ms = trace.get('latency_ms')
            usage = trace.get('usage') if isinstance(trace.get('usage'), dict) else {}
            metrics = {
                'attempt': int(trace.get('attempt') or 0),
                'latency_ms': latency_ms,
                'prompt_hash': trace.get('prompt_hash'),
                'usage': usage,
                'transport': trace.get('transport'),
                'provider': trace.get('provider'),
                'model': trace.get('model'),
            }
            profile_name = str(trace.get('profile_name') or '').strip() or None
            role_name = str(trace.get('role') or '').strip() or None
            model_message = {
                'started': f"Model call started for {phase} via {profile_name or role_name or 'router' }.",
                'completed': f"Model call completed for {phase} via {profile_name or role_name or 'router' }.",
                'failed': f"Model call failed for {phase} via {profile_name or role_name or 'router' }.",
            }.get(trace_status, f"Model call updated for {phase}.")
            self._emit_runtime_event(
                run_id=run_id,
                run_type=run_type,
                event_type=f"{run_type}.model.{trace_status}",
                message=model_message,
                stage=stage,
                phase=phase,
                status='running' if trace_status == 'started' else ('failed' if trace_status == 'failed' else 'completed'),
                job_id=job_id,
                application_id=application_id,
                submission_id=submission_id,
                company=company,
                role=role,
                source=source,
                model_role=role_name,
                model_profile=profile_name,
                model_call_id=str(trace.get('call_id') or '').strip() or None,
                trace_ref=trace_ref,
                error=trace.get('error'),
                metrics={key: value for key, value in metrics.items() if value not in (None, '', {}, [])},
                payload={
                    'provider': trace.get('provider'),
                    'model': trace.get('model'),
                    'mode': trace.get('mode'),
                    'chain': trace.get('chain'),
                },
            )

        token = set_model_trace_handler(_handler)
        try:
            yield
        finally:
            reset_model_trace_handler(token)

    def _run_pipeline_with_events(self, *, run_id: str, run_type: str, limit: int | None = None, generate_pdf: bool = True, approved_limit: int | None = None) -> dict[str, Any]:
        """Run the screening → evaluation → PDF pipeline.

        Args:
            approved_limit: If set, stop processing after this many jobs have been
                screened-approved and fully processed (evaluated + PDF generated).
                This enables a batch-of-N workflow: screen from the pool until N
                are approved, process those N, then return so the caller can loop.
        """
        pending = sorted(
            [job for job in self.workspace.load_inbox() if job.workflow_state == 'pending'],
            key=self._pending_job_priority,
            reverse=True,
        )
        if limit is not None:
            pending = pending[:limit]
        screened: list[dict[str, Any]] = []
        screened_out: list[dict[str, Any]] = []
        evaluated: list[dict[str, Any]] = []
        pdfs: list[dict[str, Any]] = []
        failed_jobs: list[dict[str, Any]] = []
        approved_processed = 0
        draft_candidates: list[tuple[Any, dict[str, Any]]] = []
        drafting_service = ChatGPTDraftingService(self.workspace)
        drafting_service.clear_batch()

        for job in pending:
            # Stop early if we have processed enough approved jobs
            if approved_limit is not None and approved_processed >= approved_limit:
                break
            self._emit_runtime_event(
                run_id=run_id,
                run_type=run_type,
                event_type=f'{run_type}.screening.started',
                message=f'Screening {job.company} / {job.title}.',
                stage='screening',
                phase='screen',
                status='running',
                job_id=job.job_id,
                company=job.company,
                role=job.title,
                source=job.source,
            )
            try:
                with self._model_trace_context(run_id=run_id, run_type=run_type, stage='screening', phase='screen', job_id=job.job_id, company=job.company, role=job.title, source=job.source):
                    screened_job, screening = screen_job(self.workspace, job.job_id)
            except Exception as exc:
                failed_jobs.append({'job_id': job.job_id, 'application_id': None, 'company': job.company, 'role': job.title, 'stage': 'screening', 'error': str(exc)})
                self._emit_runtime_event(
                    run_id=run_id,
                    run_type=run_type,
                    event_type=f'{run_type}.screening.failed',
                    message=f'Screening failed for {job.company} / {job.title}.',
                    stage='screening',
                    phase='screen',
                    status='failed',
                    job_id=job.job_id,
                    company=job.company,
                    role=job.title,
                    source=job.source,
                    error=exc,
                    payload={'job_id': job.job_id},
                )
                continue
            screen_payload = {
                'job_id': screened_job.job_id,
                'company': screened_job.company,
                'title': screened_job.title,
                'workflow_state': screened_job.workflow_state,
                'screening': screening_payload(screened_job),
            }
            screened.append(screen_payload)
            if not screening.approved:
                screened_out.append(screen_payload)
                self._emit_runtime_event(
                    run_id=run_id,
                    run_type=run_type,
                    event_type=f'{run_type}.screening.completed',
                    message=f'Screened out {screened_job.company} / {screened_job.title}.',
                    stage='screening',
                    phase='screen',
                    status='completed',
                    job_id=screened_job.job_id,
                    company=screened_job.company,
                    role=screened_job.title,
                    source=screened_job.source,
                    payload=screen_payload,
                    metrics={'approved': False},
                )
                continue
            self._emit_runtime_event(
                run_id=run_id,
                run_type=run_type,
                event_type=f'{run_type}.screening.completed',
                message=f'Screening approved {screened_job.company} / {screened_job.title}.',
                stage='screening',
                phase='screen',
                status='completed',
                job_id=screened_job.job_id,
                company=screened_job.company,
                role=screened_job.title,
                source=screened_job.source,
                payload=screen_payload,
                metrics={'approved': True},
            )

            self._emit_runtime_event(
                run_id=run_id,
                run_type=run_type,
                event_type=f'{run_type}.evaluation.started',
                message=f'Evaluating {screened_job.company} / {screened_job.title}.',
                stage='evaluation',
                phase='evaluate',
                status='running',
                job_id=screened_job.job_id,
                company=screened_job.company,
                role=screened_job.title,
                source=screened_job.source,
            )
            try:
                with self._model_trace_context(run_id=run_id, run_type=run_type, stage='evaluation', phase='evaluate', job_id=screened_job.job_id, company=screened_job.company, role=screened_job.title, source=screened_job.source):
                    evaluation = evaluate_target(self.workspace, screened_job.job_id)
            except Exception as exc:
                failed_jobs.append({'job_id': screened_job.job_id, 'application_id': None, 'company': screened_job.company, 'role': screened_job.title, 'stage': 'evaluation', 'error': str(exc)})
                self._emit_runtime_event(
                    run_id=run_id,
                    run_type=run_type,
                    event_type=f'{run_type}.evaluation.failed',
                    message=f'Evaluation failed for {screened_job.company} / {screened_job.title}.',
                    stage='evaluation',
                    phase='evaluate',
                    status='failed',
                    job_id=screened_job.job_id,
                    company=screened_job.company,
                    role=screened_job.title,
                    source=screened_job.source,
                    error=exc,
                )
                continue
            evaluated.append(evaluation)
            self._emit_runtime_event(
                run_id=run_id,
                run_type=run_type,
                event_type=f'{run_type}.evaluation.completed',
                message=f'Evaluated {evaluation.get("company") or screened_job.company} / {evaluation.get("role") or screened_job.title}.',
                stage='evaluation',
                phase='evaluate',
                status='completed',
                job_id=evaluation.get('job_id') or screened_job.job_id,
                application_id=evaluation.get('application_id'),
                company=evaluation.get('company') or screened_job.company,
                role=evaluation.get('role') or screened_job.title,
                source=screened_job.source,
                model_role=str(evaluation.get('model_role') or '') or None,
                model_profile=str(evaluation.get('model_profile') or '') or None,
                artifact_paths=[evaluation.get('report_path')] if evaluation.get('report_path') else None,
                payload=evaluation,
            )

            if not generate_pdf:
                continue
            self._emit_runtime_event(
                run_id=run_id,
                run_type=run_type,
                event_type=f'{run_type}.drafting.queued',
                message=f'Queued artifact build for {evaluation.get("company") or screened_job.company} / {evaluation.get("role") or screened_job.title}.',
                stage='queue',
                phase='draft_queue',
                status='queued',
                job_id=evaluation.get('job_id') or screened_job.job_id,
                application_id=evaluation.get('application_id'),
                company=evaluation.get('company') or screened_job.company,
                role=evaluation.get('role') or screened_job.title,
                source=screened_job.source,
            )
            draft_candidates.append((screened_job, evaluation))
            approved_processed += 1

        if draft_candidates:
            batch_target_size = min(
                len(draft_candidates),
                int(approved_limit or self._draft_batch_size_target() or len(draft_candidates)),
            )
            drafting_service.start_batch(
                run_id=run_id,
                run_type=run_type,
                target_size=batch_target_size,
                members=[
                    {
                        "application_id": str(evaluation.get("application_id") or ""),
                        "job_id": str(evaluation.get("job_id") or screened_job.job_id),
                        "company": str(evaluation.get("company") or screened_job.company),
                        "role": str(evaluation.get("role") or screened_job.title),
                    }
                    for screened_job, evaluation in draft_candidates
                ],
            )
            max_workers = min(len(draft_candidates), self._chatgpt_parallel_jobs())
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_map = {
                    executor.submit(
                        self._draft_single_job,
                        run_id=run_id,
                        run_type=run_type,
                        screened_job=screened_job,
                        evaluation=evaluation,
                    ): (screened_job, evaluation)
                    for screened_job, evaluation in draft_candidates
                }
                for future in concurrent.futures.as_completed(future_map):
                    screened_job, evaluation = future_map[future]
                    try:
                        pdf_result = future.result()
                    except Exception as exc:
                        failed_jobs.append({'job_id': screened_job.job_id, 'application_id': evaluation.get('application_id'), 'company': evaluation.get('company') or screened_job.company, 'role': evaluation.get('role') or screened_job.title, 'stage': 'drafting', 'error': str(exc)})
                        application = self.workspace.find_application(evaluation.get('application_id') or screened_job.job_id)
                        if application is not None:
                            self.workspace.upsert_application(application.model_copy(update={'notes': str(exc)}))
                        self._emit_runtime_event(
                            run_id=run_id,
                            run_type=run_type,
                            event_type=f'{run_type}.drafting.failed',
                            message=f'Artifact build failed for {evaluation.get("company") or screened_job.company} / {evaluation.get("role") or screened_job.title}.',
                            stage='drafting',
                            phase='draft',
                            status='failed',
                            job_id=evaluation.get('job_id') or screened_job.job_id,
                            application_id=evaluation.get('application_id'),
                            company=evaluation.get('company') or screened_job.company,
                            role=evaluation.get('role') or screened_job.title,
                            source=screened_job.source,
                            error=exc,
                        )
                        continue
                    pdfs.append(pdf_result)
                    render_error = str(pdf_result.get('render_error') or '').strip() or None
                    if render_error:
                        failed_jobs.append({'job_id': pdf_result.get('job_id') or screened_job.job_id, 'application_id': pdf_result.get('application_id'), 'company': evaluation.get('company') or screened_job.company, 'role': evaluation.get('role') or screened_job.title, 'stage': 'drafting', 'error': render_error})
                        application = self.workspace.find_application(pdf_result.get('application_id') or screened_job.job_id)
                        if application is not None:
                            self.workspace.upsert_application(application.model_copy(update={'notes': render_error}))
                        self._emit_runtime_event(
                            run_id=run_id,
                            run_type=run_type,
                            event_type=f'{run_type}.drafting.failed',
                            message=f'Artifact build failed for {evaluation.get("company") or screened_job.company} / {evaluation.get("role") or screened_job.title}.',
                            stage='drafting',
                            phase='draft',
                            status='failed',
                            job_id=pdf_result.get('job_id') or screened_job.job_id,
                            application_id=pdf_result.get('application_id'),
                            company=evaluation.get('company') or screened_job.company,
                            role=evaluation.get('role') or screened_job.title,
                            source=screened_job.source,
                            artifact_paths=self._artifact_paths_from_payload(pdf_result),
                            error={'message': render_error},
                            payload=pdf_result,
                        )
                        continue
                    draft_metadata = dict(pdf_result.get('draft') or {})
                    self._emit_runtime_event(
                        run_id=run_id,
                        run_type=run_type,
                        event_type=f'{run_type}.drafting.completed',
                        message=f'Artifacts built for {evaluation.get("company") or screened_job.company} / {evaluation.get("role") or screened_job.title}.',
                        stage='drafting',
                        phase='draft',
                        status='completed',
                        job_id=pdf_result.get('job_id') or screened_job.job_id,
                        application_id=pdf_result.get('application_id'),
                        company=evaluation.get('company') or screened_job.company,
                        role=evaluation.get('role') or screened_job.title,
                        source=screened_job.source,
                        model_role='writer' if draft_metadata.get('writer_profile') else None,
                        model_profile=str(draft_metadata.get('writer_profile') or draft_metadata.get('validation_profile') or '') or None,
                        artifact_paths=self._artifact_paths_from_payload(pdf_result),
                        payload=pdf_result,
                    )
            draft_batch = drafting_service.current_batch_payload()
        else:
            draft_batch = {}

        return {
            'processed': len(pending),
            'approved_processed': approved_processed,
            'approved_limit': approved_limit,
            'screened': screened,
            'screened_out': screened_out,
            'evaluated': evaluated,
            'pdfs': pdfs,
            'draft_batch': draft_batch,
            'failed_jobs': failed_jobs,
        }

    def _emit_pipeline_activity(self, *, run_id: str, run_type: str, pipeline_result: dict[str, Any]) -> None:
        for evaluation in list(pipeline_result.get("evaluated") or []):
            operator_emit_live_event(
                self.workspace,
                run_id=run_id,
                run_type=run_type,
                event_type=f"{run_type}.evaluation.completed",
                stage="evaluation",
                status="running",
                message=f"Evaluated {evaluation.get('company') or 'candidate target'} / {evaluation.get('role') or '-'}.",
                job_id=evaluation.get("job_id"),
                application_id=evaluation.get("application_id"),
                company=evaluation.get("company"),
                role=evaluation.get("role"),
                model_role=str(evaluation.get("model_role") or "") or None,
                model_profile=str(evaluation.get("model_profile") or "") or None,
                step="evaluation",
                payload=evaluation,
            )
        for draft in list(pipeline_result.get("pdfs") or []):
            draft_metadata = dict(draft.get("draft") or {})
            operator_emit_live_event(
                self.workspace,
                run_id=run_id,
                run_type=run_type,
                event_type=f"{run_type}.drafting.completed",
                stage="drafting",
                status="running",
                message=f"Drafted artifacts for application {draft.get('application_id') or '-'}.",
                job_id=draft.get("job_id"),
                application_id=draft.get("application_id"),
                model_role="writer" if draft_metadata.get("writer_profile") else None,
                model_profile=str(draft_metadata.get("writer_profile") or draft_metadata.get("validation_profile") or "") or None,
                step="drafting",
                payload=draft,
                state_updates={
                    "model_activity": {
                        "role": "writer" if draft_metadata.get("writer_profile") else None,
                        "profile": str(draft_metadata.get("writer_profile") or draft_metadata.get("validation_profile") or "") or None,
                        "stage": "drafting",
                        "application_id": draft.get("application_id"),
                    }
                },
            )

    def _application_queue_sort_key(self, application: ApplicationEntry) -> tuple[int, float, str, str]:
        priority = {
            "Preview Ready": 0,
            "Ready to Submit": 1,
            "Needs Input": 2,
            "PDF Ready": 3,
        }
        return (
            priority.get(str(application.status or "").strip(), 99),
            -float(application.score or 0.0),
            str(application.date or ""),
            str(application.id or ""),
        )

    def _application_ready_for_continuation(self, application: ApplicationEntry) -> bool:
        status = str(application.status or "").strip()
        if status in _INACTIVE_APPLICATION_STATUSES or status in {"Applied", "Submit Failed", "Submission Uncertain"}:
            return False
        submission = self.workspace.find_submission(application.id)
        if submission is not None and str(submission.status or "").strip().lower() in _TERMINAL_SUBMISSION_STATUSES | {"preview_failed", "contract_error"}:
            return False
        if status in {"PDF Ready", "Ready to Submit", "Needs Input", "Preview Ready"}:
            return True
        if bool(application.pdf):
            return True
        if submission is None:
            return False
        return str(submission.status or "").strip().lower() in {"ready_for_submit", "needs_user_input", "preview_ready"}


    def _continue_after_ready(self, application_id: str, run_id: str | None) -> SubmissionRecord:
        record = self.workspace.load_submission(application_id)
        if record is None or not record.submit_ready:
            return record if record is not None else run_async(self._prepare_submission_async, application_id, run_id)
        if self._default_submit_mode() == "preview_first":
            try:
                return run_async(self._preview_application_async, application_id, run_id)
            except Exception:
                return record
        if self.workspace.load_profile().runtime.automation.submit_enabled:
            return run_async(self._submit_application_async, application_id, run_id)
        return record

    def setup_readiness_payload(self) -> dict[str, Any]:
        profile = self.workspace.load_profile()
        portals = self.workspace.load_portals()
        snapshot = collect_filefirst_release_snapshot(self.workspace)
        enabled_sources = {name: config.enabled for name, config in portals.sources.items() if name in portals.sources}

        def _normalize_status(value: str) -> str:
            normalized = str(value or "").strip().lower()
            if normalized in {"warnings", "pass_with_warnings"}:
                return "warning"
            if normalized in {"fail", "failed", "error"}:
                return "blocked"
            return normalized

        def _severity(value: str) -> int:
            normalized = _normalize_status(value)
            if normalized in {"blocked"}:
                return 2
            if normalized in {"warning", "warn"}:
                return 1
            return 0

        status_values = [
            _normalize_status(str(snapshot.config_validation.overall_status or "")),
            _normalize_status(str(snapshot.doctor.overall_status or "")),
            _normalize_status(str(snapshot.launch_check.overall_status or "")),
        ]
        rolled_status = max(status_values, key=_severity)

        doctor_payload = snapshot.doctor.model_dump(mode="json")
        doctor_payload.update(
            {
                "overall_status": _normalize_status(str(snapshot.doctor.overall_status or "")),
                "blocked_count": int(getattr(snapshot.doctor, "blocked_count", 0) or 0),
                "warning_count": int(getattr(snapshot.doctor, "warning_count", 0) or 0),
            }
        )

        launch_payload = snapshot.launch_check.model_dump(mode="json")
        launch_payload.update(
            {
                "overall_status": _normalize_status(str(snapshot.launch_check.overall_status or "")),
                "fail_count": int(getattr(snapshot.launch_check, "fail_count", 0) or 0),
                "warning_count": int(getattr(snapshot.launch_check, "warning_count", 0) or 0),
            }
        )

        config_payload = snapshot.config_validation.model_dump(mode="json")
        config_payload.update(
            {
                "overall_status": _normalize_status(str(snapshot.config_validation.overall_status or "")),
                "blocked_count": int(getattr(snapshot.config_validation, "blocked_count", 0) or 0),
                "warning_count": int(getattr(snapshot.config_validation, "warning_count", 0) or 0),
            }
        )

        merged_findings_raw = [
            *(config_payload.get("findings") or []),
            *(doctor_payload.get("findings") or []),
            *(launch_payload.get("findings") or []),
        ]
        seen_findings: set[tuple[str, str, str, str]] = set()
        merged_findings: list[dict[str, Any]] = []
        for finding in merged_findings_raw:
            key = (
                str(finding.get("key") or ""),
                _normalize_status(str(finding.get("status") or "")),
                str(finding.get("summary") or ""),
                str(finding.get("detail") or ""),
            )
            if key in seen_findings:
                continue
            seen_findings.add(key)
            normalized_finding = dict(finding)
            normalized_finding["status"] = key[1] or str(finding.get("status") or "")
            merged_findings.append(normalized_finding)

        return {
            "workspace": {
                "root": str(self.workspace.root),
                "profile_path": self.workspace.relative_path(self.workspace.profile_path),
                "portals_path": self.workspace.relative_path(self.workspace.portals_path),
            },
            "profile_surface": self.workspace.user_profile_surface(),
            "model": profile.runtime.model.model_dump(mode="json"),
            "automation": profile.runtime.automation.model_dump(mode="json"),
            "sources": enabled_sources,
            "config_validation": config_payload,
            "doctor": doctor_payload,
            "launch_check": launch_payload,
            "overall_status": rolled_status,
            "findings": merged_findings,
        }

    def try_training_report_payload(self, *, run_id: str | None = None) -> dict[str, Any] | None:
        _ = run_id
        return None

    def daily_inbox_payload(self, *, limit: int = 12) -> dict[str, Any]:
        jobs = self.workspace.load_inbox()
        applications = {item.job_id: item for item in self.workspace.load_applications()}
        counts: dict[str, int] = {}
        screening_counts = {'approved': 0, 'rejected': 0, 'overridden': 0, 'pending': 0}
        for job in jobs:
            counts[job.workflow_state] = counts.get(job.workflow_state, 0) + 1
            screening = job.screening
            if screening is None:
                screening_counts['pending'] += 1
            elif screening.status == 'approved':
                screening_counts['approved'] += 1
            elif screening.status == 'overridden':
                screening_counts['overridden'] += 1
            else:
                screening_counts['rejected'] += 1
        items = []
        for job in sorted(jobs, key=lambda item: item.discovered_at, reverse=True)[:limit]:
            application = applications.get(job.job_id)
            submission = self.workspace.find_submission(application.id) if application is not None else None
            items.append(
                {
                    'job_id': job.job_id,
                    'application_id': application.id if application is not None else None,
                    'company': job.company,
                    'title': job.title,
                    'source': job.source,
                    'board_family': job.board_family,
                    'automation_tier': job.automation_tier,
                    'ats_family': job.ats_family,
                    'ats_preview_supported': job.ats_preview_supported,
                    'rehearsal_eligible': job.rehearsal_eligible,
                    'hard_reject_reason': job.hard_reject_reason,
                    'auth_reject_reason': job.auth_reject_reason,
                    'login_wall_detected': job.login_wall_detected,
                    'workflow_state': job.workflow_state,
                    'location': job.location,
                    'url': job.url,
                    'status': application.status if application is not None else job.workflow_state,
                    'submission_status': submission.status if submission is not None else None,
                    'blocked': bool(self._submission_blockers(submission)) if submission is not None else False,
                    'screening': screening_payload(job),
                }
            )
        compatible_counts = {
            'shortlisted': counts.get('shortlisted', 0),
            'watching': counts.get('watching', 0),
            'new_matching': counts.get('pending', 0),
            'screened_out': counts.get('screened_out', 0),
            'ready_for_review': sum(1 for item in self.workspace.load_applications() if item.status == 'Ready to Submit'),
            'needs_user_input': sum(1 for item in self.workspace.load_applications() if item.status == 'Needs Input'),
            'preview_ready': sum(1 for item in self.workspace.load_applications() if item.status == 'Preview Ready'),
            'approved_pending_submit': sum(1 for item in self.workspace.load_applications() if item.status == 'Applied'),
            'suppressed': counts.get('dismissed', 0) + counts.get('archived', 0),
        }
        return {'counts': compatible_counts, 'workflow_counts': counts, 'screening_counts': screening_counts, 'items': items}

    def run_daily(self) -> dict[str, Any]:
        run_id = self._new_run_id("daily")
        begin_live_run(self.workspace, run_id=run_id, run_type="daily", stage="discovery", message="Daily discovery started.")
        scan_result = self._run_discovery_scan(run_id=run_id, run_type="daily", limit=50)
        pipeline_result = self._run_pipeline_with_events(run_id=run_id, run_type="daily")
        operator_emit_live_event(
            self.workspace,
            run_id=run_id,
            run_type="daily",
            event_type="daily.pipeline.completed",
            stage="pipeline",
            status="running",
            message=f"Pipeline processed {pipeline_result.get('processed', 0)} jobs.",
            payload={
                "processed": pipeline_result.get("processed", 0),
                "screened_out": len(pipeline_result.get("screened_out", [])),
                "evaluated": len(pipeline_result.get("evaluated", [])),
                "pdfs": len(pipeline_result.get("pdfs", [])),
            },
        )
        run_record = RunRecord(
            run_id=run_id,
            run_type="daily",
            status="completed",
            event_status="completed",
            completed_at=utcnow_iso(),
            processed_job_ids=[item.get("job_id") for item in pipeline_result.get("screened", []) if item.get("job_id")],
            evaluated_application_ids=[item.get("application_id") for item in pipeline_result.get("evaluated", []) if item.get("application_id")],
            notes=[
                f"scan:new={scan_result.get('new_jobs', 0)}",
                f"screened:rejected={len(pipeline_result.get('screened_out', []))}",
                f"pipeline:processed={pipeline_result.get('processed', 0)}",
            ],
            metrics={"scan": scan_result, "pipeline": pipeline_result},
        )
        self.workspace.save_run(run_record)
        finish_live_run(self.workspace, run_id=run_id, run_type="daily", status="completed", stage="complete", message="Daily run completed.")
        return {"run_id": run_id, "scan": scan_result, "pipeline": pipeline_result}

    def run_discover(self, *, run_id: str | None = None, prestarted: bool = False) -> dict[str, Any]:
        run_id = run_id or self._new_run_id("discover")
        if not prestarted:
            begin_live_run(self.workspace, run_id=run_id, run_type="discover", stage="discovery", message="Discovery run started.")

        def _extract_model_profile(notes: str | None) -> str | None:
            text_value = str(notes or '').strip()
            if not text_value:
                return None
            for token in text_value.split('|'):
                token = token.strip()
                if token.startswith('classifier_profile='):
                    return token.split('=', 1)[1].strip() or None
            return None

        scan_result = self._run_discovery_scan(run_id=run_id, run_type="discover", limit=50)

        screened: list[dict[str, Any]] = []
        screened_approved: list[dict[str, Any]] = []
        screened_rejected: list[dict[str, Any]] = []
        for job_id in list(scan_result.get('saved_job_ids') or []):
            job = self.workspace.load_job(job_id)
            if job is None or job.workflow_state != 'pending':
                continue
            operator_emit_live_event(
                self.workspace,
                run_id=run_id,
                run_type='discover',
                event_type='discover.screening.started',
                stage='screening',
                status='running',
                message=f"Screening {job.company} / {job.title}.",
                job_id=job.job_id,
                company=job.company,
                role=job.title,
                source=job.source,
                step='screening',
            )
            screened_job, screening = screen_job(self.workspace, job.job_id, force=False)
            profile_name = _extract_model_profile(screening.notes)
            record = {
                'job_id': screened_job.job_id,
                'company': screened_job.company,
                'title': screened_job.title,
                'workflow_state': screened_job.workflow_state,
                'hard_reject_reason': screened_job.hard_reject_reason,
                'auth_reject_reason': screened_job.auth_reject_reason,
                'screening': screening_payload(screened_job),
                'model_profile': profile_name,
            }
            screened.append(record)
            if screening.approved:
                screened_approved.append(record)
            else:
                screened_rejected.append(record)
            operator_emit_live_event(
                self.workspace,
                run_id=run_id,
                run_type='discover',
                event_type='discover.screening.completed',
                stage='screening',
                status='running',
                message=f"Screened {job.company} / {job.title}: {screening.status}.",
                job_id=screened_job.job_id,
                company=screened_job.company,
                role=screened_job.title,
                source=screened_job.source,
                model_role='classifier',
                model_profile=profile_name,
                step='screening',
                payload=record,
                state_updates={
                    'stats': {
                        'screened': len(screened),
                        'classifier_approved': len(screened_approved),
                        'classifier_rejected': len(screened_rejected),
                        'queued_for_apply': len(screened_approved),
                    },
                    'model_activity': {
                        'role': 'classifier',
                        'profile': profile_name,
                        'stage': 'screening',
                        'job_id': screened_job.job_id,
                    },
                },
            )

        result = {
            'run_id': run_id,
            'scan': scan_result,
            'screened_count': len(screened),
            'screened_approved_count': len(screened_approved),
            'screened_rejected_count': len(screened_rejected),
            'screened_jobs': screened,
            'screened_approved_jobs': screened_approved,
            'screened_rejected_jobs': screened_rejected,
        }
        self.workspace.save_run(
            RunRecord(
                run_id=run_id,
                run_type='discover',
                status='completed',
                event_status='completed',
                completed_at=utcnow_iso(),
                processed_job_ids=[item.get('job_id') for item in screened if item.get('job_id')],
                notes=[
                    f"scan:new={scan_result.get('new_jobs', 0)}",
                    f"screened={len(screened)}",
                    f"approved={len(screened_approved)}",
                    f"rejected={len(screened_rejected)}",
                ],
                metrics=result,
            )
        )
        finish_live_run(self.workspace, run_id=run_id, run_type='discover', status='completed', stage='complete', message='Discovery run completed.')
        return result

    def rehearsal_payload(self, *, limit: int = 5) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        jobs = sorted(
            self.workspace.load_inbox(),
            key=lambda item: (item.rehearsal_rank, item.discovered_at),
            reverse=True,
        )
        for job in jobs[:limit]:
            application = self.workspace.find_application(job.job_id)
            submission = self.workspace.find_submission(application.id) if application is not None else None
            items.append(
                {
                    'job_id': job.job_id,
                    'company': job.company,
                    'title': job.title,
                    'source': job.source,
                    'workflow_state': job.workflow_state,
                    'ats_family': job.ats_family,
                    'ats_preview_supported': job.ats_preview_supported,
                    'rehearsal_eligible': job.rehearsal_eligible,
                    'rehearsal_rank': job.rehearsal_rank,
                    'hard_reject_reason': job.hard_reject_reason,
                    'auth_reject_reason': job.auth_reject_reason,
                    'login_wall_detected': job.login_wall_detected,
                    'screening': screening_payload(job),
                    'application_id': application.id if application is not None else None,
                    'application_status': application.status if application is not None else None,
                    'preview_status': submission.status if submission is not None else None,
                }
            )
        return {
            'count': len(items),
            'approved_count': sum(1 for item in items if (item.get('screening') or {}).get('status') in {'approved', 'overridden'}),
            'rejected_count': sum(1 for item in items if (item.get('screening') or {}).get('status') == 'rejected'),
            'eligible_count': sum(1 for item in items if item.get('rehearsal_eligible')),
            'items': items,
        }

    @staticmethod
    def _screened_job_priority(item: dict[str, Any]) -> tuple[int, int, float]:
        screening_status = str((item.get('screening') or {}).get('status') or '')
        if screening_status in {'approved', 'overridden'}:
            screening_score = 3
        elif item.get('rehearsal_eligible'):
            screening_score = 2
        elif screening_status == 'rejected':
            screening_score = 1
        else:
            screening_score = 0
        preview_score = 1 if item.get('ats_preview_supported') else 0
        rank = float(item.get('rehearsal_rank') or 0.0)
        return (screening_score, preview_score, rank)

    @staticmethod
    def _pending_job_priority(job: Any) -> tuple[int, int, float, str]:
        title = str(getattr(job, 'title', '') or '').casefold()
        positive_markers = (
            'software engineer',
            'engineer',
            'developer',
            'backend',
            'frontend',
            'full-stack',
            'full stack',
            'fullstack',
            'platform',
            'security',
            'data engineer',
            'data scientist',
            'machine learning',
            'ml',
            'ai',
            'python',
            'sre',
            'research',
        )
        negative_markers = (
            'senior',
            'staff',
            'principal',
            'manager',
            'director',
            'head',
            'lead',
            'intern',
            'account executive',
            'sales',
            'policy',
            'compliance',
            'audit',
            'finance',
            'recruiter',
            'support',
            'customer',
            'legal',
        )
        positive_score = sum(1 for marker in positive_markers if marker in title)
        negative_score = sum(1 for marker in negative_markers if marker in title)
        rank = float(getattr(job, 'rehearsal_rank', 0.0) or 0.0)
        discovered_at = str(getattr(job, 'discovered_at', '') or '')
        return (positive_score, -negative_score, rank, discovered_at)

    def start_launch_rehearsal(self, *, limit: int = 5) -> dict[str, Any]:
        async def _run_scan() -> dict[str, Any]:
            return await discover_live_market(self.workspace, limit=limit)

        scan_result = run_async(_run_scan)
        _TERMINAL_STATES = {'screened_out', 'applied', 'submitted', 'rejected', 'withdrawn', 'archived'}
        saved_job_ids = list(scan_result.get('saved_job_ids') or [])
        jobs = [self.workspace.load_job(job_id) for job_id in saved_job_ids]
        jobs = [job for job in jobs if job is not None and job.workflow_state not in _TERMINAL_STATES]
        if not jobs:
            jobs = [
                job
                for job in sorted(
                    self.workspace.load_inbox(),
                    key=lambda item: (item.rehearsal_rank, item.discovered_at),
                    reverse=True,
                )
                if str(job.discovery_method or '').startswith('live_market:')
                and job.workflow_state not in _TERMINAL_STATES
            ][: max(limit * 2, limit)]
        screened_jobs: list[dict[str, Any]] = []
        for job in jobs:
            screened_job, _screening = screen_job(self.workspace, job.job_id, force=False)
            screened_jobs.append(
                {
                    'job_id': screened_job.job_id,
                    'company': screened_job.company,
                    'title': screened_job.title,
                    'source': screened_job.source,
                    'workflow_state': screened_job.workflow_state,
                    'ats_family': screened_job.ats_family,
                    'ats_preview_supported': screened_job.ats_preview_supported,
                    'rehearsal_eligible': screened_job.rehearsal_eligible,
                    'rehearsal_rank': screened_job.rehearsal_rank,
                    'hard_reject_reason': screened_job.hard_reject_reason,
                    'auth_reject_reason': screened_job.auth_reject_reason,
                    'login_wall_detected': screened_job.login_wall_detected,
                    'screening': screening_payload(screened_job),
                    'url': screened_job.url,
                }
            )
        screened_jobs.sort(key=self._screened_job_priority, reverse=True)
        screened_jobs = screened_jobs[:limit]
        suggested = next(
            (
                item['job_id']
                for item in screened_jobs
                if item.get('rehearsal_eligible') and (item.get('screening') or {}).get('status') in {'approved', 'overridden'}
            ),
            None,
        )
        run_id = self._new_run_id('rehearsal')
        self.workspace.save_run(
            RunRecord(
                run_id=run_id,
                run_type='rehearsal_scan',
                status='completed',
                completed_at=utcnow_iso(),
                processed_job_ids=[item['job_id'] for item in screened_jobs],
                notes=[
                    f"discovered={scan_result.get('discovered', 0)}",
                    f"saved={len(scan_result.get('saved_job_ids') or [])}",
                    f"eligible={len(scan_result.get('eligible_job_ids') or [])}",
                ],
                metrics={'scan': scan_result, 'screened_jobs': screened_jobs, 'suggested_job_id': suggested},
            )
        )
        return {
            'run_id': run_id,
            'scan': scan_result,
            'screened_jobs': screened_jobs,
            'suggested_job_id': suggested,
            'next_step': 'Run launch rehearsal again with --job-id <id> after you choose the target live job.',
        }

    def override_job_screening(self, *, job_id: str, approved: bool = True, note: str | None = None) -> dict[str, Any]:
        job, screening = override_screening(self.workspace, job_id, approved=approved, note=note)
        return {
            'job_id': job.job_id,
            'workflow_state': job.workflow_state,
            'screening': screening_payload(job),
        }

    def run_launch_rehearsal(self, *, job_id: str, override_rejected: bool = False) -> dict[str, Any]:
        job = self.workspace.load_job(job_id)
        if job is None:
            raise ValueError(f'Unknown job for rehearsal: {job_id}')
        if job.source not in _SUPPORTED_PRODUCTION_SOURCES:
            raise ValueError(f'Launch rehearsal preview is not supported for source: {job.source}.')
        if job.screening is None:
            job, _ = screen_job(self.workspace, job.job_id)
        if job.screening is None:
            raise ValueError('Job screening did not produce a decision.')
        if not job.screening.approved:
            if not override_rejected:
                raise ValueError('Selected job is screened out. Override it explicitly before rehearsal.')
            job, _ = override_screening(self.workspace, job.job_id, approved=True, note='Launch rehearsal override')

        run_id = self._new_run_id('rehearsal')
        evaluation = self._launch_rehearsal_evaluation(job.job_id)
        pdf_result = build_pdf_for_target(self.workspace, job.job_id)
        artifact_issues = self._launch_artifact_issues(job.job_id, evaluation, pdf_result)
        application = self.workspace.find_application(job.job_id)
        if application is None:
            raise ValueError(f'No application record was created for rehearsal job: {job.job_id}')
        submission = run_async(self._prepare_submission_async, application.id, run_id)
        blockers = self._submission_blockers(submission)
        preview_record = None
        if not artifact_issues:
            preview_record = run_async(self._preview_application_async, application.id, run_id)
            blockers = self._submission_blockers(preview_record)
        run_status = 'completed' if not blockers and not artifact_issues else 'completed_with_failures'
        self.workspace.save_run(
            RunRecord(
                run_id=run_id,
                run_type='rehearsal',
                status=run_status,
                completed_at=utcnow_iso(),
                processed_job_ids=[job.job_id],
                evaluated_application_ids=[application.id],
                notes=[*artifact_issues, *(item['label'] for item in blockers)],
                metrics={
                    'screening': screening_payload(job),
                    'evaluation': evaluation,
                    'pdf': pdf_result,
                    'submission': submission.model_dump(mode='json'),
                    'preview': preview_record.model_dump(mode='json') if preview_record is not None else None,
                },
            )
        )
        return {
            'run_id': run_id,
            'job': job.model_dump(mode='json'),
            'screening': screening_payload(job),
            'evaluation': evaluation,
            'pdf': pdf_result,
            'artifact_issues': artifact_issues,
            'submission': submission.model_dump(mode='json'),
            'preview': preview_record.model_dump(mode='json') if preview_record is not None else None,
            'ready_to_review': preview_record is not None and preview_record.status == 'preview_ready',
            'remaining_blockers': blockers,
        }

    def _launch_rehearsal_evaluation(self, job_id: str) -> dict[str, Any]:
        job = self.workspace.load_job(job_id)
        if job is None:
            raise ValueError(f'Unknown job for rehearsal evaluation: {job_id}')
        cached = self.workspace.load_evaluation(job_id)
        if cached is None:
            return evaluate_target(self.workspace, job_id)

        application = self.workspace.find_application(job_id)
        display_id = application.id if application is not None else self.workspace.next_application_id()
        report_path = self.workspace.report_path_for(display_id, cached.company, on_date=cached.evaluated_at[:10])
        if not report_path.exists():
            report_path.write_text(cached.report_markdown.rstrip() + "\n", encoding="utf-8")
        report_ref = self.workspace.relative_path(report_path)
        if application is None:
            self.workspace.upsert_application(
                ApplicationEntry(
                    id=display_id,
                    job_id=job_id,
                    date=cached.evaluated_at[:10],
                    company=cached.company,
                    role=cached.role,
                    score=cached.score,
                    grade=cached.grade,
                    status='Evaluated',
                    pdf=False,
                    report=report_ref,
                    url=cached.url,
                    source=cached.source,
                )
            )
        self.workspace.update_inbox_state(job_id, 'evaluated')
        return {
            'application_id': display_id,
            'job_id': job_id,
            'company': cached.company,
            'role': cached.role,
            'score': cached.score,
            'grade': cached.grade,
            'report_path': report_ref,
            'model_profile': None,
            'model_role': None,
            'reused_saved_evaluation': True,
        }

    def autonomous_status_payload(self) -> dict[str, Any]:
        profile = self.workspace.load_profile()
        runs = self.workspace.load_runs()
        stats = self._autonomous_queue_stats()
        live_state = self.workspace.load_live_state()
        draft_status = self.chatgpt_drafting_status_payload()
        draft_batch = dict(draft_status.get("batch") or {})
        production_sources = self._effective_production_sources(profile.runtime.automation.production_sources)
        experimental_enabled = [source for source in production_sources if source != "greenhouse"]
        if live_state and live_state.run_type == "autonomous" and str(live_state.status or "").strip().lower() not in {"idle", "completed", "completed_with_failures", "failed", "interrupted"}:
            live_stats = dict(live_state.stats or {})
            for key in (
                "queue_depth",
                "discovered",
                "screened_out",
                "evaluated",
                "drafted",
                "ready_to_apply",
                "blocked_by_questions",
                "pending_questions",
                "submitted",
                "source_metrics",
                "source_warnings",
                "zero_result_sources",
            ):
                if key in live_stats and live_stats.get(key) is not None:
                    stats[key] = live_stats.get(key)
        # Merge live state failures into the reported failed count so that
        # model/screening failures are visible even when no submission records exist.
        submission_failed = int(stats["failed"])
        live_failed = int(live_state.failed_count or 0) if live_state else 0
        total_failed = max(submission_failed, live_failed)
        latest_run = self._run_record_summary(runs[0]) if runs else None
        live_summary = None
        if live_state and live_state.run_id:
            live_summary = {
                "run_id": live_state.run_id,
                "run_type": live_state.run_type,
                "status": live_state.status,
                "started_at": live_state.run_started_at or live_state.started_at,
                "completed_at": live_state.completed_at,
                "processed_count": 0,
                "evaluated_count": 0,
                "submitted_count": int(live_state.submitted_count or 0),
                "failed_count": live_failed,
                "submitted_application_ids": [],
                "failed_application_ids": [],
                "notes": [live_state.latest_operator_message] if live_state.latest_operator_message else ([live_state.latest_error] if live_state.latest_error else []),
                "note_count": 1 if (live_state.latest_operator_message or live_state.latest_error) else 0,
                "metrics": {"failed_count": live_failed, "event_count": live_state.event_count},
            }
        # If no persisted run record exists but live state shows a run, synthesize
        # a minimal latest_run entry so the UI is not blank.
        if latest_run is None and live_summary is not None:
            latest_run = live_summary
        elif (
            latest_run is not None
            and live_summary is not None
            and str(latest_run.get("run_id") or "").strip() == str(live_summary.get("run_id") or "").strip()
            and str(latest_run.get("run_type") or "").strip() != str(live_summary.get("run_type") or "").strip()
        ):
            latest_run = live_summary
        daily_submit_summary = self._submitted_local_day_summary()
        daily_submit_cap = int(profile.runtime.automation.daily_submit_cap or 0)
        daily_submitted_today = int(daily_submit_summary.get("total") or 0)
        return {
            "enabled": profile.runtime.automation.enabled,
            "submit_enabled": profile.runtime.automation.submit_enabled,
            "default_submit_mode": profile.runtime.automation.default_submit_mode,
            "drafting_mode": "serial" if self._serial_chatgpt_mode() else "parallel",
            "configured_ready_to_apply_threshold": self._configured_ready_to_apply_threshold(),
            "ready_to_apply_threshold": self._ready_to_apply_threshold(),
            "drafting_parallel_limit": self._chatgpt_parallel_jobs(),
            "production_sources": production_sources,
            "source_mode": "greenhouse_launch_mode" if production_sources == ["greenhouse"] else "mixed_experimental_mode",
            "experimental_sources_available": ["lever", "ashby"],
            "experimental_sources_enabled": experimental_enabled,
            "queue_depth": int(stats["queue_depth"]),
            "blocked_applications": int(stats["blocked_by_questions"]),
            "ready_for_submit": int(stats["ready_to_apply"]),
            "ready_to_apply": int(stats["ready_to_apply"]),
            "discovered": int(stats["discovered"]),
            "screened_out": int(stats["screened_out"]),
            "evaluated": int(stats["evaluated"]),
            "drafted": int(stats["drafted"]),
            "submitted": int(stats["submitted"]),
            "daily_submit_cap": daily_submit_cap,
            "daily_submitted_today": daily_submitted_today,
            "daily_remaining_capacity": max(0, daily_submit_cap - daily_submitted_today) if daily_submit_cap > 0 else 0,
            "daily_submit_day": str(daily_submit_summary.get("day") or ""),
            "blocked_by_questions": int(stats["blocked_by_questions"]),
            "failed": total_failed,
            "unresolved_prompts": int(stats["pending_questions"]),
            "source_metrics": dict(stats.get("source_metrics") or {}),
            "source_warnings": list(stats.get("source_warnings") or []),
            "zero_result_sources": list(stats.get("zero_result_sources") or []),
            "drafting_batch": draft_batch,
            "latest_run": latest_run,
            "latest_error": live_state.latest_error if live_state else None,
            "live": self.live_status_payload(limit=8),
        }

    def _configured_ready_to_apply_threshold(self) -> int:
        automation = self.workspace.load_profile().runtime.automation
        try:
            return max(1, int(getattr(automation, "ready_to_apply_threshold", 10) or 10))
        except (TypeError, ValueError):
            return 10

    def _serial_chatgpt_mode(self) -> bool:
        return self._chatgpt_parallel_jobs() <= 1

    def _ready_to_apply_threshold(self) -> int:
        configured = self._configured_ready_to_apply_threshold()
        if self._serial_chatgpt_mode():
            return 1
        return configured

    def _draft_batch_size_target(self) -> int:
        if self._serial_chatgpt_mode():
            return 1
        return max(1, self._chatgpt_parallel_jobs())

    @staticmethod
    def _submission_ready_to_apply(submission: SubmissionRecord | None) -> bool:
        if submission is None:
            return False
        return bool(submission.submit_ready) and str(submission.status or "").strip().lower() in {"ready_for_submit", "preview_ready"}

    def _autonomous_queue_stats(self) -> dict[str, Any]:
        jobs = list(self.workspace.load_inbox())
        applications = [item for item in self.workspace.load_applications() if item.status not in _INACTIVE_APPLICATION_STATUSES]
        application_status_by_id = {item.id: item.status for item in applications}
        submissions = [
            item
            for item in self.workspace.load_submissions()
            if application_status_by_id.get(item.application_id) not in _INACTIVE_APPLICATION_STATUSES
        ]
        active_submissions = [item for item in submissions if self._is_active_submission(item)]
        queued_submissions = [
            item
            for item in active_submissions
            if str(item.status or "").strip().lower() not in _TERMINAL_SUBMISSION_STATUSES
        ]
        visible_jobs = [
            job
            for job in jobs
            if str(job.workflow_state or "").strip().lower() not in {"dismissed", "archived", "rejected"}
        ]
        discovered = sum(1 for job in visible_jobs if str(job.workflow_state or "").strip().lower() != "screened_out")
        screened_out = sum(1 for job in jobs if str(job.workflow_state or "").strip().lower() == "screened_out")
        drafted_statuses = {"PDF Ready", "Ready to Submit", "Needs Input", "Preview Ready", "Applied", "Submit Failed", "Submission Uncertain"}
        ready_to_apply = sum(1 for item in active_submissions if self._submission_ready_to_apply(item))
        blocked_by_questions = sum(
            1
            for item in queued_submissions
            if item.missing_required_fields
            or item.ungrounded_answers
            or item.low_confidence_answers
            or any(question.needs_user_input for question in item.questions)
        )
        pending_questions = sum(1 for item in queued_submissions for question in item.questions if question.needs_user_input)
        failed = sum(
            1
            for item in active_submissions
            if str(item.status or "").strip().lower() in {"failed", "submission_failed", "preview_failed", "contract_error"}
        )
        source_snapshot = _workspace_stats(self.workspace)
        return {
            "queue_depth": len(active_submissions),
            "discovered": discovered,
            "screened_out": screened_out,
            "evaluated": len(applications),
            "drafted": sum(1 for item in applications if bool(item.pdf) or str(item.status or "").strip() in drafted_statuses),
            "ready_to_apply": ready_to_apply,
            "submitted": sum(1 for item in submissions if str(item.status or "").strip().lower() == "submitted"),
            "blocked_by_questions": blocked_by_questions,
            "failed": failed,
            "pending_questions": pending_questions,
            "source_metrics": dict(source_snapshot.get("source_metrics") or {}),
            "source_warnings": list(source_snapshot.get("source_warnings") or []),
            "zero_result_sources": list(source_snapshot.get("zero_result_sources") or []),
        }

    def _is_already_submitted(self, application: ApplicationEntry | str) -> bool:
        """Check if an application has already been submitted or preserved as handled."""
        application_id = application.id if isinstance(application, ApplicationEntry) else str(application or "").strip()
        if application_id and application_id in self._submission_registry:
            return True
        for submission in self.workspace.load_submissions():
            if submission.application_id == application_id and submission.status == "submitted":
                self._remember_submission(application_id, submission.submitted_at or utcnow_iso())
                return True
        if isinstance(application, ApplicationEntry):
            handled = self._handled_jobs_index()
            if application.job_id and application.job_id in handled["job_ids"]:
                return True
            normalized_url = self._normalize_handled_url(application.url)
            if normalized_url and normalized_url in handled["urls"]:
                return True
            pair = self._handled_pair(application.company, application.role)
            if pair is not None and (pair["company"], pair["role"]) in handled["pairs"]:
                return True
            linked_job = self.workspace.load_job(application.job_id)
            duplicate_cluster = str(getattr(linked_job, "duplicate_cluster_key", "") or "").strip()
            if duplicate_cluster and duplicate_cluster in handled["duplicate_clusters"]:
                return True
        return False

    def _ready_to_apply_applications(self) -> list[ApplicationEntry]:
        ready: list[ApplicationEntry] = []
        submissions = {item.application_id: item for item in self.workspace.load_submissions()}
        for application in self.workspace.load_applications():
            if application.status in _INACTIVE_APPLICATION_STATUSES or application.status in {"Applied", "Submit Failed", "Submission Uncertain"}:
                continue
            submission = submissions.get(application.id)
            if not self._submission_ready_to_apply(submission):
                continue
            job = self.workspace.load_job(application.job_id)
            if job is None:
                continue
            if str(job.workflow_state or "").strip().lower() in {"screened_out", "dismissed", "archived", "rejected"}:
                continue
            if job.screening and not job.screening.approved:
                continue
            ready.append(application)
        ready.sort(key=self._application_queue_sort_key)
        return ready

    def _applications_requiring_prepare(self) -> list[ApplicationEntry]:
        pending_prepare: list[ApplicationEntry] = []
        submissions = {item.application_id: item for item in self.workspace.load_submissions()}
        terminal_statuses = _TERMINAL_SUBMISSION_STATUSES | {"preview_failed", "contract_error", "unsupported_source", "ready_for_submit", "preview_ready", "needs_user_input"}
        for application in self.workspace.load_applications():
            if application.status in _INACTIVE_APPLICATION_STATUSES or application.status in {"Applied", "Submit Failed", "Submission Uncertain"}:
                continue
            job = self.workspace.load_job(application.job_id)
            if job is None:
                continue
            if str(job.workflow_state or "").strip().lower() in {"screened_out", "dismissed", "archived", "rejected"}:
                continue
            if job.screening and not job.screening.approved:
                continue
            submission = submissions.get(application.id)
            if submission is not None and str(submission.status or "").strip().lower() in terminal_statuses:
                continue
            if not bool(application.pdf):
                continue
            pending_prepare.append(application)
        pending_prepare.sort(key=self._application_queue_sort_key)
        return pending_prepare

    def _get_approved_applications(self) -> list[ApplicationEntry]:
        """Return active applications that can still advance in the file-first loop."""
        approved: dict[str, ApplicationEntry] = {}
        for application in [*self._ready_to_apply_applications(), *self._applications_requiring_prepare()]:
            approved[application.id] = application
        return sorted(approved.values(), key=self._application_queue_sort_key)

    def _autonomous_apply_step(
        self,
        *,
        run_id: str,
        application: ApplicationEntry,
        ready_queue_size: int,
        per_company_cap: int,
        company_submitted_today: dict[str, int],
        submitted: list[str],
        failed: list[str],
        notes: list[str],
        deferred_ids: set[str],
        attempted_ids: set[str],
    ) -> dict[str, Any]:
        if self._default_submit_mode() != "preview_first" and not self.workspace.load_profile().runtime.automation.submit_enabled:
            notes.append("Ready-to-apply queue reached the threshold, but submit is disabled.")
            operator_emit_live_event(
                self.workspace,
                run_id=run_id,
                run_type="autonomous",
                event_type="autonomous.apply.waiting",
                phase="submit",
                stage="submit",
                status="waiting",
                message="Ready-to-apply queue reached the threshold, but submit is disabled.",
                step="submit",
                state_updates={"stats": self._autonomous_queue_stats()},
            )
            return {"stop": True, "terminal_error": None, "applied": False, "submitted_now": False}

        browser_blocker = self._browser_runtime_blocker()
        if browser_blocker is not None:
            stage_name = "preview" if self._default_submit_mode() == "preview_first" else "submit"
            notes.append(str(browser_blocker["message"]))
            self._emit_runtime_event(
                run_id=run_id,
                run_type="autonomous",
                event_type=f"autonomous.{stage_name}.blocked",
                message=f"{stage_name.title()} blocked: {browser_blocker['message']}",
                stage=stage_name,
                phase=stage_name,
                status="blocked",
                error=browser_blocker,
                metrics={"ready_to_apply": ready_queue_size},
                state_updates={"stats": self._autonomous_queue_stats()},
            )
            return {"stop": True, "terminal_error": str(browser_blocker["message"]), "applied": False, "submitted_now": False}

        if self._is_already_submitted(application):
            deferred_ids.add(application.id)
            operator_emit_live_event(
                self.workspace,
                run_id=run_id,
                run_type="autonomous",
                event_type="autonomous.application.skipped_duplicate",
                phase="submit",
                stage="submit",
                status="skipped",
                message=f"Skipping duplicate: {application.company} / {application.role}.",
                job_id=application.job_id,
                application_id=application.id,
                company=application.company,
                role=application.role,
                source=application.source,
                step="submit",
                state_updates={"stats": self._autonomous_queue_stats()},
            )
            return {"stop": False, "terminal_error": None, "applied": False, "submitted_now": False}

        company_key = str(application.company or "").casefold().strip()
        company_submitted = int(company_submitted_today.get(company_key, 0) or 0)
        if per_company_cap > 0 and company_submitted >= per_company_cap:
            deferred_ids.add(application.id)
            notes.append(f"{application.company}: per-company daily cap reached.")
            operator_emit_live_event(
                self.workspace,
                run_id=run_id,
                run_type="autonomous",
                event_type="autonomous.application.deferred",
                phase="submit",
                stage="submit",
                status="waiting",
                message=f"Per-company daily cap reached for {application.company}; deferring {application.role}.",
                job_id=application.job_id,
                application_id=application.id,
                company=application.company,
                role=application.role,
                source=application.source,
                step="submit",
                state_updates={"stats": self._autonomous_queue_stats()},
            )
            return {"stop": False, "terminal_error": None, "applied": False, "submitted_now": False}

        attempted_ids.add(application.id)
        try:
            record = self._continue_after_ready(application.id, run_id)
        except Exception as exc:
            failed.append(application.id)
            notes.append(f"{application.id}: {exc}")
            operator_emit_live_event(
                self.workspace,
                run_id=run_id,
                run_type="autonomous",
                event_type="autonomous.application.failed",
                phase="submit",
                stage="submit",
                status="failed",
                message=f"Failed to apply {application.company} / {application.role}.",
                job_id=application.job_id,
                application_id=application.id,
                company=application.company,
                role=application.role,
                source=application.source,
                payload={"error": str(exc)},
                step="submit",
                state_updates={"latest_error": str(exc), "stats": self._autonomous_queue_stats()},
            )
            return {"stop": False, "terminal_error": None, "applied": False, "submitted_now": False}

        if record.status == "submitted":
            submitted.append(application.id)
            self._remember_submission(application.id, utcnow_iso())
            operator_emit_live_event(
                self.workspace,
                run_id=run_id,
                run_type="autonomous",
                event_type="autonomous.application.submitted",
                phase="submit",
                stage="submit",
                status="completed",
                message=f"Submitted: {application.company} / {application.role}.",
                job_id=application.job_id,
                application_id=application.id,
                company=application.company,
                role=application.role,
                source=application.source,
                step="submit",
                state_updates={"stats": self._autonomous_queue_stats()},
            )
            return {"stop": False, "terminal_error": None, "applied": True, "submitted_now": True}
        elif record.status in {"failed", "submission_failed", "preview_failed", "contract_error"}:
            failed.append(application.id)
            operator_emit_live_event(
                self.workspace,
                run_id=run_id,
                run_type="autonomous",
                event_type="autonomous.application.failed",
                phase="submit",
                stage="submit",
                status="failed",
                message=f"Failed: {application.company} / {application.role}.",
                job_id=application.job_id,
                application_id=application.id,
                company=application.company,
                role=application.role,
                source=application.source,
                payload={"status": record.status},
                step="submit",
                state_updates={"stats": self._autonomous_queue_stats()},
            )
        else:
            operator_emit_live_event(
                self.workspace,
                run_id=run_id,
                run_type="autonomous",
                event_type="autonomous.application.queued",
                phase="preview" if record.status == "preview_ready" else "submit",
                stage="preview" if record.status == "preview_ready" else "submit",
                status="completed" if record.status == "preview_ready" else "running",
                message=f"{application.company} / {application.role} moved to {record.status}.",
                job_id=application.job_id,
                application_id=application.id,
                company=application.company,
                role=application.role,
                source=application.source,
                payload={"status": record.status},
                step="preview" if record.status == "preview_ready" else "submit",
                state_updates={"stats": self._autonomous_queue_stats()},
            )
        return {"stop": False, "terminal_error": None, "applied": True, "submitted_now": False}

    def _autonomous_prepare_step(
        self,
        *,
        run_id: str,
        application: ApplicationEntry,
        failed: list[str],
        notes: list[str],
    ) -> dict[str, Any]:
        operator_emit_live_event(
            self.workspace,
            run_id=run_id,
            run_type="autonomous",
            event_type="autonomous.application.processing",
            phase="prepare",
            stage="prepare",
            status="running",
            message=f"Preparing {application.company} / {application.role}.",
            job_id=application.job_id,
            application_id=application.id,
            company=application.company,
            role=application.role,
            source=application.source,
            step="prepare",
            state_updates={"stats": self._autonomous_queue_stats()},
        )
        try:
            record = run_async(self._prepare_submission_async, application.id, run_id)
        except Exception as exc:
            failed.append(application.id)
            notes.append(f"{application.id}: {exc}")
            operator_emit_live_event(
                self.workspace,
                run_id=run_id,
                run_type="autonomous",
                event_type="autonomous.application.failed",
                phase="prepare",
                stage="prepare",
                status="failed",
                message=f"Failed to prepare {application.company} / {application.role}.",
                job_id=application.job_id,
                application_id=application.id,
                company=application.company,
                role=application.role,
                source=application.source,
                payload={"error": str(exc)},
                step="prepare",
                state_updates={"latest_error": str(exc), "stats": self._autonomous_queue_stats()},
            )
            return {"stop": False, "paused_for_questions": False}

        if not record.submit_ready:
            notes.append(f"{application.id}: blocked for manual input")
            manual_handoff_opened = False
            try:
                parked = run_async(self._open_manual_handoff_preview_async, application.id, run_id)
                manual_handoff_opened = bool(parked)
            except Exception as exc:
                notes.append(f"{application.id}: manual handoff preview failed: {exc}")
            operator_emit_live_event(
                self.workspace,
                run_id=run_id,
                run_type="autonomous",
                event_type="autonomous.application.blocked",
                phase="prepare",
                stage="prepare",
                status="blocked",
                message=f"Blocked: {application.company} / {application.role} needs manual input.",
                job_id=application.job_id,
                application_id=application.id,
                company=application.company,
                role=application.role,
                source=application.source,
                payload={"blockers": self._submission_blockers(record), "manual_handoff_opened": manual_handoff_opened},
                step="prepare",
                state_updates={"stats": self._autonomous_queue_stats()},
            )
            return {"stop": False, "paused_for_questions": True, "manual_handoff_opened": manual_handoff_opened}

        operator_emit_live_event(
            self.workspace,
            run_id=run_id,
            run_type="autonomous",
            event_type="autonomous.prepare.completed",
            phase="prepare",
            stage="prepare",
            status="completed",
            message=f"Prepared {application.company} / {application.role} for apply.",
            job_id=application.job_id,
            application_id=application.id,
            company=application.company,
            role=application.role,
            source=application.source,
            payload={"status": record.status},
            step="prepare",
            state_updates={"stats": self._autonomous_queue_stats()},
        )
        return {"stop": False, "paused_for_questions": False}

    async def _open_manual_handoff_preview_async(self, application_id: str, run_id: str | None) -> bool:
        application = self.workspace.find_application(application_id)
        if application is None:
            return False
        job = self.workspace.load_job(application.job_id)
        if job is None:
            return False
        record = await self._prepare_submission_async(application.id, run_id)
        if record is None or not record.plan:
            return False
        browser_blocker = self._browser_runtime_blocker()
        if browser_blocker is not None:
            return False
        adapter = self._adapter_for_job(job)
        preview_submission = getattr(adapter, "preview_submission", None)
        if not callable(preview_submission):
            return False
        output_dir = self.workspace.output_dir / "submissions" / application.id / "manual-handoff"
        output_dir.mkdir(parents=True, exist_ok=True)
        plan = SubmissionPlan.model_validate(dict(record.plan))
        posting = self._normalized_job(job)
        result = await preview_submission(posting, plan, output_dir, keep_browser_open=True)
        evidence = getattr(result, "evidence", None) if result is not None else None
        browser_left_open = bool(getattr(evidence, "browser_left_open", False))
        merged_artifacts = dict(record.artifacts)
        if evidence is not None:
            preview_paths = {
                "manual_handoff_pre_submit_snapshot": evidence.pre_submit_snapshot_path,
                "manual_handoff_final_snapshot": evidence.final_snapshot_path,
                "manual_handoff_dom_snapshot": evidence.dom_snapshot_path,
                "manual_handoff_post_submit_dom_snapshot": evidence.post_submit_dom_snapshot_path,
                "manual_handoff_trace": evidence.trace_path,
            }
            merged_artifacts.update({key: value for key, value in preview_paths.items() if value})
            if evidence.final_url:
                merged_artifacts["manual_handoff_final_url"] = evidence.final_url
        handoff_note = (
            "Partially filled application left open for manual completion."
            if browser_left_open
            else "Prepared manual-handoff preview, but the browser could not be kept open automatically."
        )
        result_payload = self._merge_result_payload(record, result.model_dump(mode="json"))
        updated = record.model_copy(
            update={
                "artifacts": merged_artifacts,
                "warnings": self._dedupe_strings(list(record.warnings) + [handoff_note]),
                "notes": self._dedupe_strings(list(record.notes) + [handoff_note]),
                "result": result_payload,
                "event_status": "manual_handoff_ready",
                "updated_at": utcnow_iso(),
                "run_id": run_id,
            }
        )
        updated = self._append_review_history_event_to_record(
            updated,
            event_type="review.manual_handoff.opened",
            summary=(
                "Opened a partially filled browser page for manual completion."
                if browser_left_open
                else "Prepared a manual-handoff preview, but the browser could not be kept open."
            ),
            actor="system",
            metadata={
                "browser_left_open": browser_left_open,
                "manual_handoff_final_url": merged_artifacts.get("manual_handoff_final_url"),
            },
        )
        self.workspace.save_submission(updated)
        self.workspace.upsert_application(application.model_copy(update={"status": "Needs Input", "pdf": True, "notes": handoff_note}))
        if browser_left_open:
            self._start_manual_handoff_watcher(application.id)
        else:
            self._stop_manual_handoff_watcher(application.id, status="manual_handoff_unavailable")
        trace_ref = self._persist_trace_payload(
            run_id or "manual",
            category="submission-steps",
            name=f"manual-handoff-{application.id}",
            payload={
                "application_id": application.id,
                "job_id": application.job_id,
                "company": application.company,
                "role": application.role,
                "source": application.source,
                "result": result.model_dump(mode="json"),
            },
        )
        self._emit_runtime_event(
            run_id=run_id or "manual",
            run_type="submission",
            event_type="submission.manual_handoff.ready",
            message=(
                f"Left {application.company} / {application.role} open for manual completion."
                if browser_left_open
                else f"Prepared a manual-handoff preview for {application.company} / {application.role}, but the browser could not stay open."
            ),
            stage="question_resolution",
            phase="question_resolution",
            status="warning" if browser_left_open else "failed",
            job_id=application.job_id,
            application_id=application.id,
            submission_id=application.id,
            company=application.company,
            role=application.role,
            source=application.source,
            artifact_paths=list(merged_artifacts.values()),
            trace_ref=trace_ref,
            payload={
                "manual_handoff_final_url": merged_artifacts.get("manual_handoff_final_url"),
                "browser_left_open": browser_left_open,
            },
        )
        return browser_left_open

    def _autonomous_pipeline_step(self, *, run_id: str, notes: list[str]) -> dict[str, Any]:
        try:
            pipeline_result = self._run_pipeline_with_events(
                run_id=run_id,
                run_type="autonomous",
                approved_limit=self._draft_batch_size_target(),
            )
        except Exception as exc:
            notes.append(f"Pipeline error: {exc}")
            operator_emit_live_event(
                self.workspace,
                run_id=run_id,
                run_type="autonomous",
                event_type="autonomous.pipeline.error",
                phase="pipeline",
                stage="drafting",
                status="failed",
                message=f"Pipeline failed: {exc}",
                step="pipeline",
                state_updates={"latest_error": str(exc), "stats": self._autonomous_queue_stats()},
            )
            return {"stop": True, "terminal_error": str(exc), "evaluated": 0, "drafted": 0}

        evaluated_count = len(pipeline_result.get("evaluated") or [])
        drafted_count = len(pipeline_result.get("pdfs") or [])
        operator_emit_live_event(
            self.workspace,
            run_id=run_id,
            run_type="autonomous",
            event_type="autonomous.pipeline.completed",
            phase="pipeline",
            stage="drafting",
            status="completed",
            message=f"Pipeline advanced {evaluated_count} applications and drafted {drafted_count} artifacts.",
            payload={"pipeline": pipeline_result},
            step="pipeline",
            state_updates={"stats": self._autonomous_queue_stats()},
        )
        draft_batch = dict(pipeline_result.get("draft_batch") or {})
        if draft_batch:
            operator_emit_live_event(
                self.workspace,
                run_id=run_id,
                run_type="autonomous",
                event_type="autonomous.drafting.batch.completed",
                phase="draft",
                stage="drafting",
                status="completed_with_failures" if int(draft_batch.get("failed_count", 0) or 0) else "completed",
                message=(
                    f"Draft batch finished: {int(draft_batch.get('completed_count', 0) or 0)} / "
                    f"{int(draft_batch.get('member_count', 0) or 0)} succeeded."
                ),
                payload={"draft_batch": draft_batch},
                step="drafting",
                state_updates={"stats": self._autonomous_queue_stats()},
            )
        if pipeline_result.get("failed_jobs"):
            notes.extend(
                [
                    f"{item.get('job_id')}: {item.get('error')}"
                    for item in list(pipeline_result.get("failed_jobs") or [])
                    if str(item.get("error") or "").strip()
                ]
            )
        chatgpt_blocker = next(
            (
                str(item.get("error") or "").strip()
                for item in list(pipeline_result.get("failed_jobs") or [])
                if self._is_chatgpt_browser_blocker_error(item.get("error"))
            ),
            None,
        )
        if chatgpt_blocker:
            operator_emit_live_event(
                self.workspace,
                run_id=run_id,
                run_type="autonomous",
                event_type="autonomous.drafting.blocked",
                phase="draft",
                stage="drafting",
                status="blocked",
                message=(
                    "ChatGPT drafting is blocked by a browser/session error. "
                    "Autonomous run paused to avoid wasting more applications."
                ),
                payload={"error": chatgpt_blocker},
                step="drafting",
                state_updates={"latest_error": chatgpt_blocker, "stats": self._autonomous_queue_stats()},
            )
            return {
                "stop": True,
                "terminal_error": chatgpt_blocker,
                "evaluated": evaluated_count,
                "drafted": drafted_count,
                "blocked_for_chatgpt": True,
            }
        return {"stop": False, "terminal_error": None, "evaluated": evaluated_count, "drafted": drafted_count, "blocked_for_chatgpt": False}

    def _autonomous_discovery_batch_size(self) -> int:
        # Greenhouse-first launch mode needs enough raw candidates to actually
        # produce a full approved draft batch. A tiny discovery buffer causes
        # the run to stall at 3-4 approved jobs and never reach the 10-job
        # drafting target the UI is configured around.
        threshold = max(5, int(self._ready_to_apply_threshold() or 5))
        return max(50, min(250, threshold * 10))

    @staticmethod
    def _is_chatgpt_browser_blocker_error(error: Any) -> bool:
        lowered = str(error or "").strip().casefold()
        return lowered.startswith("chatgpt_http_") or "chatgpt_login_required" in lowered

    def _autonomous_discovery_step(self, *, run_id: str, notes: list[str]) -> dict[str, Any]:
        try:
            scan_result = self._run_discovery_scan(
                run_id=run_id,
                run_type="autonomous",
                limit=self._autonomous_discovery_batch_size(),
            )
        except Exception as exc:
            notes.append(f"Discovery error: {exc}")
            operator_emit_live_event(
                self.workspace,
                run_id=run_id,
                run_type="autonomous",
                event_type="autonomous.discovery.error",
                phase="discovery",
                stage="discovery",
                status="warning",
                message=f"Discovery failed: {exc}",
                step="discovery",
                state_updates={"latest_error": str(exc), "stats": self._autonomous_queue_stats()},
            )
            return {"stop": True, "terminal_error": str(exc), "discovery_exhausted": False, "new_jobs": 0}

        discovery_exhausted = not bool(
            int(scan_result.get("new_jobs", 0) or 0)
            or int(scan_result.get("updated_jobs", 0) or 0)
            or len(scan_result.get("saved_job_ids") or [])
            or len(scan_result.get("eligible_job_ids") or [])
        )
        operator_emit_live_event(
            self.workspace,
            run_id=run_id,
            run_type="autonomous",
            event_type="autonomous.discovery.cycle",
            phase="discovery",
            stage="discovery",
            status="completed",
            message=(
                "Discovery reached the current frontier with no new queue additions."
                if discovery_exhausted
                else f"Discovery queued {len(scan_result.get('eligible_job_ids') or [])} potential jobs."
            ),
            payload=scan_result,
            step="discovery",
            state_updates={"stats": self._autonomous_queue_stats()},
        )
        return {
            "stop": False,
            "terminal_error": None,
            "discovery_exhausted": discovery_exhausted,
            "new_jobs": int(scan_result.get("new_jobs", 0) or 0),
        }

    def _run_autonomous_v2(self, *, run_id: str, prestarted: bool) -> dict[str, Any]:
        if not prestarted:
            begin_live_run(self.workspace, run_id=run_id, run_type="autonomous", stage="queue", message="Autonomous queue run started.")

        submitted: list[str] = []
        failed: list[str] = []
        notes: list[str] = []
        profile = self.workspace.load_profile()
        daily_cap = max(1, profile.runtime.automation.daily_submit_cap)
        per_company_cap = max(1, profile.runtime.automation.per_company_daily_cap)
        ready_threshold = self._ready_to_apply_threshold()
        local_day_summary = self._submitted_local_day_summary()
        daily_submitted_count = int(local_day_summary.get("total") or 0)
        company_submitted_today = {
            str(company or "").casefold().strip(): int(count or 0)
            for company, count in dict(local_day_summary.get("by_company") or {}).items()
            if str(company or "").strip()
        }

        discovery_count = 0
        apply_count = 0
        total_evaluated = 0
        total_drafted = 0
        deferred_ids: set[str] = set()
        discovery_exhausted = False
        paused_for_questions = False
        manual_blocker_event_emitted = False
        blocked_for_chatgpt = False
        terminal_error: str | None = None
        loop_iterations = 0
        active_apply_batch_remaining = 0
        require_discovery_refresh = False
        attempted_apply_ids: set[str] = set()
        daily_cap_event_emitted = False
        max_loop_iterations = max(12, min(500, daily_cap * max(2, ready_threshold)))

        while loop_iterations < max_loop_iterations:
            loop_iterations += 1
            current_local_day = self._local_now().date()
            if str(local_day_summary.get("day") or "") != current_local_day.isoformat():
                local_day_summary = self._submitted_local_day_summary(local_day=current_local_day)
                daily_submitted_count = int(local_day_summary.get("total") or 0)
                company_submitted_today = {
                    str(company or "").casefold().strip(): int(count or 0)
                    for company, count in dict(local_day_summary.get("by_company") or {}).items()
                    if str(company or "").strip()
                }
                daily_cap_event_emitted = False
            queue_stats = self._autonomous_queue_stats()
            ready_queue = [
                item
                for item in self._ready_to_apply_applications()
                if item.id not in deferred_ids and item.id not in attempted_apply_ids
            ]
            prepare_queue = [item for item in self._applications_requiring_prepare() if item.id not in deferred_ids]
            pending_jobs = [job for job in self.workspace.load_inbox() if job.workflow_state == "pending"]

            if daily_cap > 0 and daily_submitted_count >= daily_cap:
                if not daily_cap_event_emitted:
                    notes.append(f"Daily submit cap reached ({daily_cap}) for {local_day_summary.get('day')}.")
                    operator_emit_live_event(
                        self.workspace,
                        run_id=run_id,
                        run_type="autonomous",
                        event_type="autonomous.submit.cap_reached",
                        phase="submit",
                        stage="submit",
                        status="completed",
                        message=f"Daily submit cap reached ({daily_cap}); stopping autonomous submissions for {local_day_summary.get('day')}.",
                        step="submit",
                        payload={
                            "daily_submit_cap": daily_cap,
                            "daily_submitted_today": daily_submitted_count,
                            "daily_submit_day": local_day_summary.get("day"),
                        },
                        state_updates={"stats": self._autonomous_queue_stats()},
                    )
                    daily_cap_event_emitted = True
                break

            if int(queue_stats.get("blocked_by_questions", 0) or 0) > 0:
                paused_for_questions = True
                if not manual_blocker_event_emitted:
                    notes.append("One or more applications need manual answers; continuing with the rest of the queue.")
                    operator_emit_live_event(
                        self.workspace,
                        run_id=run_id,
                        run_type="autonomous",
                        event_type="autonomous.questions.pending",
                        phase="question_resolution",
                        stage="question_resolution",
                        status="warning",
                        message="Some applications need manual answers. Autonomous run will continue with other eligible jobs.",
                        step="question_resolution",
                        state_updates={"stats": self._autonomous_queue_stats()},
                    )
                    manual_blocker_event_emitted = True

            if require_discovery_refresh:
                if pending_jobs:
                    result = self._autonomous_pipeline_step(run_id=run_id, notes=notes)
                    total_evaluated += int(result.get("evaluated", 0) or 0)
                    total_drafted += int(result.get("drafted", 0) or 0)
                    terminal_error = result.get("terminal_error") or terminal_error
                    blocked_for_chatgpt = bool(result.get("blocked_for_chatgpt")) or blocked_for_chatgpt
                    require_discovery_refresh = False
                    if result.get("stop"):
                        break
                    continue
                if prepare_queue:
                    result = self._autonomous_prepare_step(
                        run_id=run_id,
                        application=prepare_queue[0],
                        failed=failed,
                        notes=notes,
                    )
                    paused_for_questions = bool(result.get("paused_for_questions"))
                    require_discovery_refresh = False
                    if result.get("stop"):
                        break
                    continue
                if not discovery_exhausted:
                    result = self._autonomous_discovery_step(run_id=run_id, notes=notes)
                    discovery_count += int(result.get("new_jobs", 0) or 0)
                    discovery_exhausted = bool(result.get("discovery_exhausted"))
                    terminal_error = result.get("terminal_error") or terminal_error
                    require_discovery_refresh = False
                    if result.get("stop"):
                        break
                    continue
                require_discovery_refresh = False

            remaining_daily_capacity = max(0, daily_cap - len(submitted))
            if active_apply_batch_remaining <= 0 and ready_queue and remaining_daily_capacity > 0:
                if len(ready_queue) >= ready_threshold:
                    active_apply_batch_remaining = min(ready_threshold, len(ready_queue), remaining_daily_capacity)
                elif discovery_exhausted or (not pending_jobs and not prepare_queue):
                    active_apply_batch_remaining = min(len(ready_queue), remaining_daily_capacity)

            should_apply = bool(ready_queue) and active_apply_batch_remaining > 0
            if should_apply:
                result = self._autonomous_apply_step(
                    run_id=run_id,
                    application=ready_queue[0],
                    ready_queue_size=len(ready_queue),
                    per_company_cap=per_company_cap,
                    company_submitted_today=company_submitted_today,
                    submitted=submitted,
                    failed=failed,
                    notes=notes,
                    deferred_ids=deferred_ids,
                    attempted_ids=attempted_apply_ids,
                )
                apply_count += int(result.get("applied", 0) or 0)
                terminal_error = result.get("terminal_error") or terminal_error
                if result.get("submitted_now"):
                    daily_submitted_count += 1
                    company_key = str(ready_queue[0].company or "").casefold().strip()
                    if company_key:
                        company_submitted_today[company_key] = int(company_submitted_today.get(company_key, 0) or 0) + 1
                active_apply_batch_remaining = max(0, active_apply_batch_remaining - 1)
                if active_apply_batch_remaining == 0 and not discovery_exhausted:
                    require_discovery_refresh = True
                if result.get("stop"):
                    break
                continue

            if prepare_queue:
                result = self._autonomous_prepare_step(
                    run_id=run_id,
                    application=prepare_queue[0],
                    failed=failed,
                    notes=notes,
                )
                paused_for_questions = bool(result.get("paused_for_questions"))
                if result.get("stop"):
                    break
                continue

            if pending_jobs:
                result = self._autonomous_pipeline_step(run_id=run_id, notes=notes)
                total_evaluated += int(result.get("evaluated", 0) or 0)
                total_drafted += int(result.get("drafted", 0) or 0)
                terminal_error = result.get("terminal_error") or terminal_error
                blocked_for_chatgpt = bool(result.get("blocked_for_chatgpt")) or blocked_for_chatgpt
                if result.get("stop"):
                    break
                continue

            if not discovery_exhausted and len(ready_queue) < ready_threshold:
                result = self._autonomous_discovery_step(run_id=run_id, notes=notes)
                discovery_count += int(result.get("new_jobs", 0) or 0)
                discovery_exhausted = bool(result.get("discovery_exhausted"))
                terminal_error = result.get("terminal_error") or terminal_error
                if result.get("stop"):
                    break
                continue

            if ready_queue:
                discovery_exhausted = True
                continue

            if not notes:
                notes.append("No eligible applications are ready for continuation.")
            break

        status = "blocked" if blocked_for_chatgpt else ("completed_with_failures" if failed or terminal_error or paused_for_questions else "completed")
        run_record = RunRecord(
            run_id=run_id,
            run_type="autonomous",
            status=status,
            event_status=status,
            completed_at=utcnow_iso(),
            processed_job_ids=[item.job_id for item in self.workspace.load_inbox()],
            evaluated_application_ids=[item.id for item in self.workspace.load_applications()],
            submitted_application_ids=submitted,
            failed_application_ids=failed,
            notes=notes,
            metrics={
                "discovery_count": discovery_count,
                "apply_count": apply_count,
                "evaluated_count": total_evaluated,
                "drafted_count": total_drafted,
                "submitted_count": len(submitted),
                "failed_count": len(failed),
                "paused_for_questions": paused_for_questions,
                "discovery_exhausted": discovery_exhausted,
                "submit_mode": self._default_submit_mode(),
                "ready_to_apply_threshold": ready_threshold,
                "queue_stats": self._autonomous_queue_stats(),
            },
        )
        self.workspace.save_run(run_record)
        finish_live_run(
            self.workspace,
            run_id=run_id,
            run_type="autonomous",
            status=status,
            stage="drafting" if blocked_for_chatgpt else ("question_resolution" if paused_for_questions else "complete"),
            message=(
                "Autonomous run paused because the ChatGPT browser session is unhealthy."
                if blocked_for_chatgpt
                else (
                    "Autonomous run finished with manual answers still pending."
                    if paused_for_questions
                    else "Autonomous run finished."
                )
            ),
            latest_error=terminal_error or ("; ".join(notes[:3]) if status == "completed_with_failures" and notes else None),
        )
        return {
            "started": True,
            "run_id": run_id,
            "discovery_count": discovery_count,
            "apply_count": apply_count,
            "default_submit_mode": self._default_submit_mode(),
            "ready_to_apply_threshold": ready_threshold,
            "paused_for_questions": paused_for_questions,
            "blocked_for_chatgpt": blocked_for_chatgpt,
            "queue_stats": self._autonomous_queue_stats(),
            "submitted_application_ids": submitted,
            "failed_application_ids": failed,
            "notes": notes,
        }

    def run_autonomous(self, *, run_id: str | None = None, prestarted: bool = False) -> dict[str, Any]:
        run_id = run_id or self._new_run_id("auto")
        return self._run_autonomous_v2(run_id=run_id, prestarted=prestarted)

    def _get_resume_text(self, application: ApplicationEntry) -> str:
        """Get the resume text for an application."""
        html_path = self.workspace.resume_html_path_for(application.id, application.company, application.date)
        if html_path.exists():
            return html_path.read_text(encoding="utf-8", errors="ignore")
        # Fall back to output artifacts
        for path in self.workspace.output_dir.iterdir():
            if path.is_file() and path.name.endswith('.resume.txt'):
                lowered = path.name.casefold()
                company_slug = application.company.casefold().replace(' ', '-')
                if company_slug in lowered:
                    return path.read_text(encoding="utf-8", errors="ignore")
        return ""

    def _get_cover_letter_text(self, application: ApplicationEntry) -> str:
        """Get the cover letter text for an application."""
        for path in self.workspace.output_dir.iterdir():
            if path.is_file() and path.name.endswith('.cover_letter.txt'):
                lowered = path.name.casefold()
                company_slug = application.company.casefold().replace(' ', '-')
                if company_slug in lowered:
                    return path.read_text(encoding="utf-8", errors="ignore")
        return ""

    def triage_job(self, *, job_id: str, action: str, reason_code: str | None = None, note: str | None = None, scope: str = "job") -> dict[str, Any]:
        _ = reason_code
        _ = note
        _ = scope
        job = self.workspace.load_job(job_id)
        if job is None:
            raise ValueError(f"Unknown job: {job_id}")
        mapping = {"shortlist": "shortlisted", "watch": "watching", "dismiss": "dismissed", "archive": "archived", "unsuppress": "pending"}
        if action not in mapping:
            raise ValueError(f"Unsupported action: {action}")
        state = mapping[action]
        self.workspace.update_inbox_state(job_id, state)
        application = self.workspace.find_application(job_id)
        if application is not None:
            status_map = {"shortlisted": "Shortlisted", "watching": "Watching", "dismissed": "Dismissed", "archived": "Archived", "pending": application.status}
            self.workspace.upsert_application(application.model_copy(update={"status": status_map[state]}))
        return {"decision": {"job_id": job_id, "status": state}}

    def question_queue_payload(self, *, limit: int = 20) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        approved_memory = {item.canonical_question for item in self.workspace.load_answer_memory() if item.approved}
        applications = {item.id: item for item in self.workspace.load_applications()}
        for record in self.workspace.load_submissions():
            if not self._is_active_submission(record):
                continue
            application = applications.get(record.application_id)
            if application is not None and application.status in _INACTIVE_APPLICATION_STATUSES:
                continue
            for question in record.questions:
                if not question.needs_user_input:
                    continue
                canonical_question = question.normalized_key or slugify(str(question.prompt_text or ""))
                items.append(
                    {
                        "application_id": record.application_id,
                        "question_id": question.question_id,
                        "job_id": record.job_id,
                        "company": record.company,
                        "title": record.role,
                        "prompt_text": question.prompt_text,
                        "normalized_key": question.normalized_key,
                        "canonical_question": canonical_question,
                        "question_type": question.question_type,
                        "widget_type": question.widget_type,
                        "required": question.required,
                        "source_adapter": record.source,
                        "option_signature": list(question.options),
                        "option_details": list(question.option_details),
                        "existing_answer": question.existing_answer,
                        "has_approved_memory": canonical_question in approved_memory,
                    }
                )
        items.sort(key=lambda item: (str(item.get("company") or ""), str(item.get("title") or ""), str(item.get("prompt_text") or "")))
        return {"count": len(items), "items": items[:limit]}

    @staticmethod
    def _find_submission_question(record: SubmissionRecord, question_id: str) -> tuple[SubmissionQuestion | None, int | None]:
        for index, question in enumerate(record.questions):
            if question.question_id == question_id:
                return question, index
        return None, None

    @classmethod
    def _is_transient_submission_question(
        cls,
        question: SubmissionQuestion | None = None,
        *,
        question_id: str | None = None,
        normalized_key: str | None = None,
        prompt_text: str | None = None,
    ) -> bool:
        resolved_question_id = str(question.question_id if question is not None else question_id or "").strip().casefold()
        resolved_normalized = str(question.normalized_key if question is not None else normalized_key or "").strip().casefold()
        resolved_prompt = str(question.prompt_text if question is not None else prompt_text or "").strip().casefold()
        if resolved_question_id == cls._email_verification_question_id():
            return True
        if resolved_normalized == cls._email_verification_question_id():
            return True
        transient_tokens = (
            "verification code",
            "security code",
            "one-time code",
            "one time code",
            "otp",
        )
        return any(token in resolved_prompt for token in transient_tokens)

    def _clear_transient_submission_answer(
        self,
        record: SubmissionRecord,
        *,
        question_id: str,
        confidence_reason: str = "transient_answer_not_persisted",
    ) -> tuple[SubmissionRecord, SubmissionQuestion | None]:
        found_question, found_index = self._find_submission_question(record, question_id)
        manual_answers = dict(record.manual_answers)
        manual_answers.pop(question_id, None)
        updated_record = record
        updated_question: SubmissionQuestion | None = found_question
        if found_question is not None and found_index is not None:
            updated_question = found_question.model_copy(
                update={
                    "existing_answer": None,
                    "confidence": 0.0 if found_question.needs_user_input else found_question.confidence,
                    "confidence_reason": confidence_reason,
                }
            )
            updated_questions = list(record.questions)
            updated_questions[found_index] = updated_question
            updated_record = record.model_copy(
                update={
                    "questions": updated_questions,
                    "manual_answers": manual_answers,
                    "updated_at": utcnow_iso(),
                }
            )
            return updated_record, updated_question
        return record.model_copy(update={"manual_answers": manual_answers, "updated_at": utcnow_iso()}), updated_question

    def _apply_manual_answer_to_record(
        self,
        record: SubmissionRecord,
        *,
        question_id: str,
        answer_text: str,
        confidence_reason: str = "manual_override",
        verification_status: str = "verified",
        event_status: str = "manual_answer_recorded",
    ) -> tuple[SubmissionRecord, SubmissionQuestion]:
        cleaned_answer = str(answer_text or "").strip()
        found_question, found_index = self._find_submission_question(record, question_id)
        if found_question is None or found_index is None:
            raise ValueError(f"Unknown question: {question_id}")
        if found_question.required and not cleaned_answer:
            raise ValueError("Answer cannot be empty for required questions.")

        updated_question = found_question.model_copy(
            update={
                "existing_answer": cleaned_answer,
                "confidence": 1.0,
                "confidence_reason": confidence_reason,
                "needs_user_input": False,
                "verification_status": verification_status,
            }
        )
        updated_questions = list(record.questions)
        updated_questions[found_index] = updated_question

        blocker_aliases = {slugify(question_id)}
        if found_question.normalized_key:
            blocker_aliases.add(slugify(found_question.normalized_key))
        prompt_text = str(found_question.prompt_text or "").strip()
        if prompt_text:
            blocker_aliases.add(slugify(prompt_text))

        def _is_answered_blocker(value: str) -> bool:
            cleaned = str(value or "").strip()
            if not cleaned:
                return False
            if prompt_text and cleaned.casefold() == prompt_text.casefold():
                return True
            return slugify(cleaned) in blocker_aliases

        manual_answers = dict(record.manual_answers)
        manual_answers[question_id] = cleaned_answer
        updated_record = record.model_copy(
            update={
                "questions": updated_questions,
                "manual_answers": manual_answers,
                "missing_required_fields": [item for item in record.missing_required_fields if not _is_answered_blocker(item)],
                "ungrounded_answers": [item for item in record.ungrounded_answers if not _is_answered_blocker(item)],
                "low_confidence_answers": [item for item in record.low_confidence_answers if not _is_answered_blocker(item)],
                "updated_at": utcnow_iso(),
                "event_status": event_status,
            }
        )
        return updated_record, updated_question

    def _store_approved_answer_memory_entry(
        self,
        *,
        record: SubmissionRecord,
        question: SubmissionQuestion,
        answer_text: str,
    ) -> None:
        cleaned_answer = str(answer_text or "").strip()
        if not cleaned_answer:
            return
        if self._is_transient_submission_question(question):
            return
        from findmyjob.filefirst.models import AnswerMemoryEntry

        canonical_question = question.normalized_key or question.question_id
        option_signature = "|".join(
            sorted(str(option).strip().lower() for option in question.options if str(option).strip())
        )
        context_constraints = {
            "question_type": str(question.question_type or "unknown"),
            "source_adapter": str(record.source or "").strip(),
            "option_signature": option_signature,
        }
        answers = list(self.workspace.load_answer_memory())
        duplicate = any(
            item.canonical_question == canonical_question
            and dict(item.context_constraints or {}) == context_constraints
            and str(item.answer_text or "").strip() == cleaned_answer
            for item in answers
        )
        if duplicate:
            return
        answers.append(
            AnswerMemoryEntry(
                canonical_question=canonical_question,
                context_constraints=context_constraints,
                answer_text=cleaned_answer,
                grounded_fact_ids=[],
                approved=True,
            )
        )
        self.workspace.save_answer_memory(answers)

    def _manual_handoff_submitter(self) -> PlaywrightSubmitter:
        automation = self.workspace.load_profile().runtime.automation
        browser_mode = str(automation.browser_mode or "headed").strip().lower() or "headed"
        timeout_seconds = 60 if browser_mode in {"headed", "attached"} else 30
        return PlaywrightSubmitter(
            timeout_seconds=timeout_seconds,
            browser_attach_enabled=True,
            browser_cdp_url=str(automation.browser_cdp_url or "").strip() or "http://127.0.0.1:9222",
            browser_mode=browser_mode,
            max_open_tabs=int(automation.max_open_tabs or 10),
        )

    @staticmethod
    def _submission_question_binding(question: SubmissionQuestion) -> FormFieldBinding:
        metadata = {
            "question_id": question.question_id,
            "section": question.section,
            "group": question.section,
            "option_details": list(question.option_details),
            "submission_binding": dict(question.submission_binding or {}),
            "input_type": question.question_type,
        }
        return FormFieldBinding(
            source_field_name=str(question.source_field_name or question.question_id),
            widget_type=str(question.widget_type or "text"),
            prompt_text=question.prompt_text,
            required=question.required,
            metadata=metadata,
        )

    def _load_manual_handoff_watch_state(self, application_id: str) -> dict[str, Any]:
        record = self.workspace.load_submission(application_id)
        if record is None:
            return {}
        payload = dict(record.result or {}).get("manual_handoff_watch")
        return dict(payload) if isinstance(payload, dict) else {}

    def _update_manual_handoff_watch_state(self, application_id: str, **updates: Any) -> dict[str, Any] | None:
        with self._MANUAL_HANDOFF_WATCH_LOCK:
            record = self.workspace.load_submission(application_id)
            if record is None:
                return None
            result_payload = dict(record.result or {})
            state = dict(result_payload.get("manual_handoff_watch") or {})
            if updates.get("active") and not state.get("started_at") and "started_at" not in updates:
                state["started_at"] = utcnow_iso()
            state.update(updates)
            result_payload["manual_handoff_watch"] = state
            self.workspace.save_submission(
                record.model_copy(
                    update={
                        "result": result_payload,
                        "updated_at": utcnow_iso(),
                    }
                )
            )
            return state

    @staticmethod
    def _review_history_entries(record: SubmissionRecord | None) -> list[dict[str, Any]]:
        if record is None:
            return []
        payload = dict(record.result or {})
        history = payload.get("review_history")
        if not isinstance(history, list):
            return []
        entries: list[dict[str, Any]] = []
        for item in history:
            if isinstance(item, dict):
                entries.append(dict(item))
        return entries

    def _merge_result_payload(self, record: SubmissionRecord, payload: dict[str, Any]) -> dict[str, Any]:
        merged = dict(record.result or {})
        merged.update(payload)
        return merged

    def _append_review_history_event_to_record(
        self,
        record: SubmissionRecord,
        *,
        event_type: str,
        summary: str,
        actor: str = "operator",
        metadata: dict[str, Any] | None = None,
        timestamp: str | None = None,
    ) -> SubmissionRecord:
        result_payload = dict(record.result or {})
        history = self._review_history_entries(record)
        history.append(
            {
                "timestamp": timestamp or utcnow_iso(),
                "type": str(event_type or "").strip() or "review.event",
                "actor": str(actor or "").strip() or "operator",
                "summary": str(summary or "").strip() or "Review event recorded.",
                "metadata": dict(metadata or {}),
            }
        )
        result_payload["review_history"] = history[-80:]
        return record.model_copy(update={"result": result_payload, "updated_at": utcnow_iso()})

    def _append_review_history_event(
        self,
        application_id: str,
        *,
        event_type: str,
        summary: str,
        actor: str = "operator",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        record = self.workspace.load_submission(application_id)
        if record is None:
            application = self.workspace.find_application(application_id)
            if application is None:
                return
            job = self.workspace.load_job(application.job_id)
            record = SubmissionRecord(
                application_id=application.id,
                job_id=application.job_id,
                company=application.company,
                role=application.role,
                source=application.source,
                apply_url=(getattr(job, "apply_url", None) or application.url),
            )
        updated = self._append_review_history_event_to_record(
            record,
            event_type=event_type,
            summary=summary,
            actor=actor,
            metadata=metadata,
        )
        self.workspace.save_submission(updated)

    def _manual_handoff_summary(self, submission: SubmissionRecord | None) -> dict[str, Any]:
        watch = dict((submission.result or {}).get("manual_handoff_watch") or {}) if submission is not None else {}
        return {
            "active": bool(watch.get("active")),
            "status": str(watch.get("status") or ("watching" if watch.get("active") else "idle")),
            "last_synced_at": watch.get("last_synced_at"),
            "pending_count": int(watch.get("pending_count") or 0),
            "sync_count": int(watch.get("sync_count") or 0),
            "synced_question_count": int(watch.get("synced_question_count") or 0),
            "filled_blank_count": int(watch.get("filled_blank_count") or 0),
            "corrected_answer_count": int(watch.get("corrected_answer_count") or 0),
        }

    def _review_summary(
        self,
        *,
        application: ApplicationEntry,
        job: Any,
        submission: SubmissionRecord | None,
    ) -> dict[str, Any]:
        blockers = self._submission_blockers(submission)
        hard_blockers = [item for item in blockers if str(item.get("category") or "").strip() != "warning"]
        warnings = list(submission.warnings) if submission is not None else []
        unresolved_question_count = sum(
            1 for item in (submission.questions if submission is not None else [])
            if item.needs_user_input
        )
        manual_handoff = self._manual_handoff_summary(submission)
        review_status = str(submission.status if submission is not None else "not_prepared")
        ready_for_submit = bool(
            (submission.submit_ready if submission is not None else False)
            or application.status == "Ready to Submit"
            or review_status in {"preview_ready", "ready_to_submit"}
        )

        if manual_handoff["active"]:
            next_action = "sync_manual_input"
            next_action_reason = "A parked browser page is being watched. Sync any manual edits back into answer memory."
        elif hard_blockers:
            next_action = "open_manual_input" if review_status in {"needs_user_input", "blocked", "preview_ready"} else "save_answers"
            next_action_reason = "Required questions or low-confidence answers still block submission."
        elif unresolved_question_count:
            next_action = "save_answers"
            next_action_reason = "Unresolved prompts are still waiting for operator answers."
        elif ready_for_submit:
            next_action = "approve"
            next_action_reason = "No remaining blockers are recorded. The application is ready to submit."
        elif warnings:
            next_action = "review_summary"
            next_action_reason = "Warnings remain, but there are no hard blockers."
        else:
            next_action = "approve"
            next_action_reason = "The application is ready for operator review."

        if hard_blockers:
            severity = "danger"
        elif warnings or manual_handoff["active"] or bool(getattr(job, "login_wall_detected", False)):
            severity = "warning"
        elif ready_for_submit:
            severity = "success"
        else:
            severity = "neutral"

        screening = screening_payload(job) if job is not None else None
        screening = screening if isinstance(screening, dict) else {}
        classification = {
            "board_family": getattr(job, "board_family", None) if job is not None else None,
            "automation_tier": getattr(job, "automation_tier", None) if job is not None else None,
            "ats_family": getattr(job, "ats_family", None) if job is not None else None,
            "ats_preview_supported": getattr(job, "ats_preview_supported", None) if job is not None else None,
            "rehearsal_eligible": getattr(job, "rehearsal_eligible", None) if job is not None else None,
        }
        return {
            "severity": severity,
            "review_status": review_status,
            "application_status": application.status,
            "ready_for_submit": ready_for_submit,
            "blocker_count": len(hard_blockers),
            "warning_count": len(warnings),
            "missing_required_count": len(submission.missing_required_fields) if submission is not None else 0,
            "ungrounded_count": len(submission.ungrounded_answers) if submission is not None else 0,
            "low_confidence_count": len(submission.low_confidence_answers) if submission is not None else 0,
            "unresolved_question_count": unresolved_question_count,
            "next_action": next_action,
            "next_action_reason": next_action_reason,
            "screening_status": screening.get("status"),
            "screening_approved": bool(screening.get("approved")),
            "classification": classification,
            "login_wall_detected": bool(getattr(job, "login_wall_detected", False)) if job is not None else False,
            "hard_reject_reason": getattr(job, "hard_reject_reason", None) if job is not None else None,
            "auth_reject_reason": getattr(job, "auth_reject_reason", None) if job is not None else None,
            "blocker_labels": [item.get("label") for item in hard_blockers if item.get("label")],
            "warning_labels": list(warnings),
        }

    def _workspace_file_ref(self, value: Any) -> dict[str, Any] | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        lowered = raw.lower()
        if lowered.startswith("http://") or lowered.startswith("https://"):
            return {
                "target": raw,
                "href": raw,
                "relative_path": None,
                "exists": True,
                "external": True,
            }
        candidate = Path(raw)
        resolved = candidate if candidate.is_absolute() else (self.workspace.root / candidate)
        exists = resolved.exists()
        relative = self.workspace.relative_path(resolved).replace("\\", "/")
        return {
            "target": str(resolved if resolved.is_absolute() else candidate),
            "href": f"/files/{quote(relative, safe='/')}" if exists else None,
            "relative_path": relative,
            "exists": exists,
            "external": False,
        }

    def _normalized_artifacts(
        self,
        *,
        application: ApplicationEntry,
        submission: SubmissionRecord | None,
    ) -> list[dict[str, Any]]:
        combined: dict[str, Any] = {}
        combined.update(self._artifact_map(application))
        if application.report:
            combined.setdefault("evaluation_report", application.report)
        if application.url:
            combined.setdefault("job_posting", application.url)
        if submission is not None:
            combined.update(dict(submission.artifacts or {}))

        label_map = {
            "resume_pdf": ("resume_pdf", "Resume PDF", "primary"),
            "cover_letter_pdf": ("cover_letter_pdf", "Cover Letter PDF", "primary"),
            "evaluation_report": ("evaluation_report", "Evaluation Report", "primary"),
            "job_posting": ("job_posting", "Job Posting", "primary"),
            "resume_text": ("resume_text", "Resume Source", "supporting"),
            "cover_letter_text": ("cover_letter_text", "Cover Letter Source", "supporting"),
            "manual_handoff_final_url": ("manual_handoff_page", "Manual Handoff Page", "debug"),
            "manual_handoff_pre_submit_snapshot": ("manual_handoff_pre_submit_snapshot", "Manual Handoff Pre-Submit Snapshot", "debug"),
            "manual_handoff_final_snapshot": ("manual_handoff_final_snapshot", "Manual Handoff Final Snapshot", "debug"),
            "manual_handoff_dom_snapshot": ("manual_handoff_dom_snapshot", "Manual Handoff DOM Snapshot", "debug"),
            "manual_handoff_post_submit_dom_snapshot": ("manual_handoff_post_submit_dom_snapshot", "Manual Handoff Post-Submit DOM Snapshot", "debug"),
            "manual_handoff_trace": ("manual_handoff_trace", "Manual Handoff Trace", "debug"),
            "snapshot_path": ("submission_snapshot", "Submission Snapshot", "debug"),
            "trace_path": ("submission_trace", "Submission Trace", "debug"),
            "pre_submit_snapshot": ("pre_submit_snapshot", "Pre-Submit Snapshot", "debug"),
            "final_snapshot": ("final_snapshot", "Final Snapshot", "debug"),
            "dom_snapshot": ("dom_snapshot", "DOM Snapshot", "debug"),
            "post_submit_dom_snapshot": ("post_submit_dom_snapshot", "Post-Submit DOM Snapshot", "debug"),
            "browser_trace": ("browser_trace", "Browser Trace", "debug"),
        }
        ordered_keys = [
            "resume_pdf",
            "cover_letter_pdf",
            "evaluation_report",
            "job_posting",
            "resume_text",
            "cover_letter_text",
            "manual_handoff_final_url",
            "manual_handoff_pre_submit_snapshot",
            "manual_handoff_final_snapshot",
            "manual_handoff_dom_snapshot",
            "manual_handoff_post_submit_dom_snapshot",
            "manual_handoff_trace",
            "snapshot_path",
            "trace_path",
            "pre_submit_snapshot",
            "final_snapshot",
            "dom_snapshot",
            "post_submit_dom_snapshot",
            "browser_trace",
        ]
        artifacts: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for key in ordered_keys:
            value = combined.get(key)
            ref = self._workspace_file_ref(value)
            if ref is None:
                continue
            kind, label, group = label_map.get(key, (key, key.replace("_", " ").title(), "supporting"))
            dedupe_key = (kind, str(ref.get("target") or ""))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            artifacts.append(
                {
                    "kind": kind,
                    "label": label,
                    "group": group,
                    **ref,
                }
            )
        return artifacts

    async def _capture_manual_handoff_answers_async(self, application_id: str) -> dict[str, Any]:
        record = self.workspace.load_submission(application_id)
        if record is None:
            return {
                "application_id": application_id,
                "page_found": False,
                "page_url": None,
                "answers": [],
                "error": "Submission record not found.",
            }
        application = self.workspace.find_application(application_id)
        target_url = (
            str(record.artifacts.get("manual_handoff_final_url") or "").strip()
            or str(record.apply_url or "").strip()
            or str(application.url if application is not None else "").strip()
        )
        bindings = [
            self._submission_question_binding(question)
            for question in record.questions
            if str(question.question_type or "").strip().lower() != "file"
        ]
        if not bindings:
            return {
                "application_id": application_id,
                "page_found": False,
                "page_url": target_url or None,
                "answers": [],
                "error": "No capture-ready questions are available for this handoff.",
            }
        submitter = self._manual_handoff_submitter()
        captured = await submitter.capture_manual_handoff_answers(
            application_url=target_url,
            bindings=bindings,
        )
        captured["application_id"] = application_id
        return captured

    def _persist_manual_handoff_captures(
        self,
        *,
        application_id: str,
        captures: list[dict[str, Any]],
        approve_memory: bool = True,
        confidence_reason: str = "manual_handoff_sync",
    ) -> dict[str, Any]:
        record = self.workspace.load_submission(application_id)
        if record is None:
            raise ValueError(f"Unknown submission record: {application_id}")
        updated_questions: list[dict[str, Any]] = []
        filled_blank_count = 0
        corrected_answer_count = 0
        record_dirty = False

        for capture in captures:
            question_id = str(capture.get("question_id") or "").strip()
            answer_text = str(capture.get("answer_text") or "").strip()
            if not question_id or not answer_text:
                continue
            question, _index = self._find_submission_question(record, question_id)
            if question is None:
                continue
            if self._is_transient_submission_question(question):
                record, _ = self._clear_transient_submission_answer(record, question_id=question_id)
                record_dirty = True
                continue
            previous_answer = str(record.manual_answers.get(question_id) or question.existing_answer or "").strip()
            if previous_answer == answer_text:
                continue
            record, updated_question = self._apply_manual_answer_to_record(
                record,
                question_id=question_id,
                answer_text=answer_text,
                confidence_reason=confidence_reason,
                verification_status="verified",
                event_status="manual_handoff_synced",
            )
            if approve_memory:
                self._store_approved_answer_memory_entry(
                    record=record,
                    question=updated_question,
                    answer_text=answer_text,
                )
            if previous_answer:
                corrected_answer_count += 1
            else:
                filled_blank_count += 1
            record_dirty = True
            updated_questions.append(
                {
                    "question_id": question_id,
                    "prompt_text": updated_question.prompt_text,
                    "previous_answer": previous_answer,
                    "answer_text": answer_text,
                    "widget_type": updated_question.widget_type,
                    "filled_blank": not previous_answer,
                    "corrected_answer": bool(previous_answer),
                    "change_type": "filled_blank" if not previous_answer else "corrected_answer",
                }
            )

        if record_dirty:
            if updated_questions:
                record = self._append_review_history_event_to_record(
                    record,
                    event_type="review.manual_handoff.synced",
                    summary=f"Learned {len(updated_questions)} answer(s) from the parked browser page.",
                    actor="watcher" if confidence_reason == "manual_handoff_watch" else "operator",
                    metadata={
                        "source": confidence_reason,
                        "updated_count": len(updated_questions),
                        "filled_blank_count": filled_blank_count,
                        "corrected_answer_count": corrected_answer_count,
                        "updated_questions": updated_questions,
                    },
                )
            self.workspace.save_submission(record)
        if updated_questions:
            application = self.workspace.find_application(application_id)
            if application is not None and application.status not in _INACTIVE_APPLICATION_STATUSES and application.status != "Applied":
                blockers = self._submission_blockers(record)
                next_status = "Ready to Submit" if not blockers else "Needs Input"
                self.workspace.upsert_application(application.model_copy(update={"status": next_status}))
            operator_emit_live_event(
                self.workspace,
                run_id=record.run_id or "manual",
                run_type="submission",
                event_type="submission.manual_handoff.synced",
                stage="question_resolution",
                status="running",
                message=f"Captured {len(updated_questions)} manual browser answer(s) for {record.company} / {record.role}.",
                job_id=record.job_id,
                application_id=record.application_id,
                company=record.company,
                role=record.role,
                source=record.source,
                payload={
                    "updated_questions": updated_questions,
                    "filled_blank_count": filled_blank_count,
                    "corrected_answer_count": corrected_answer_count,
                },
            )

        return {
            "application_id": application_id,
            "updated_count": len(updated_questions),
            "filled_blank_count": filled_blank_count,
            "corrected_answer_count": corrected_answer_count,
            "updated_questions": updated_questions,
            "remaining_blockers": self._submission_blockers(record),
            "status": record.status,
        }

    async def _sync_manual_handoff_answers_async(
        self,
        application_id: str,
        approve_memory: bool = True,
        source: str = "manual_handoff_sync",
    ) -> dict[str, Any]:
        captured = await self._capture_manual_handoff_answers_async(application_id)
        current_state = self._load_manual_handoff_watch_state(application_id)
        now = utcnow_iso()
        update_payload: dict[str, Any] = {
            "last_source": source,
            "last_sync_attempt_at": now,
            "last_error": str(captured.get("error") or "").strip() or None,
            "status": "page_not_found",
            "active": bool(current_state.get("active")),
            "sync_count": int(current_state.get("sync_count") or 0),
            "synced_question_count": int(current_state.get("synced_question_count") or 0),
            "filled_blank_count": int(current_state.get("filled_blank_count") or 0),
            "corrected_answer_count": int(current_state.get("corrected_answer_count") or 0),
            "recent_answers": list(current_state.get("recent_answers") or []),
        }
        persisted = {
            "application_id": application_id,
            "updated_count": 0,
            "filled_blank_count": 0,
            "corrected_answer_count": 0,
            "updated_questions": [],
            "remaining_blockers": self._submission_blockers(self.workspace.load_submission(application_id)),
            "status": None,
        }

        if captured.get("page_found"):
            update_payload.update(
                {
                    "active": True,
                    "status": "watching",
                    "last_error": None,
                    "last_page_url": captured.get("page_url"),
                    "last_page_seen_at": now,
                    "last_synced_at": now,
                    "sync_count": int(update_payload["sync_count"]) + 1,
                }
            )
            persisted = self._persist_manual_handoff_captures(
                application_id=application_id,
                captures=list(captured.get("answers") or []),
                approve_memory=approve_memory,
                confidence_reason=source,
            )
            update_payload["synced_question_count"] = int(update_payload["synced_question_count"]) + int(persisted["updated_count"])
            update_payload["filled_blank_count"] = int(update_payload["filled_blank_count"]) + int(persisted["filled_blank_count"])
            update_payload["corrected_answer_count"] = int(update_payload["corrected_answer_count"]) + int(
                persisted["corrected_answer_count"]
            )
            if persisted["updated_questions"]:
                update_payload["recent_answers"] = list(persisted["updated_questions"][-5:])
                update_payload["last_saved_at"] = now

        watch_state = self._update_manual_handoff_watch_state(application_id, **update_payload)
        return {
            **captured,
            **persisted,
            "watch_state": watch_state,
        }

    def _is_manual_handoff_watch_terminal(self, application_id: str) -> bool:
        record = self.workspace.load_submission(application_id)
        if record is None or not self._is_active_submission(record):
            return True
        if str(record.status or "").strip().casefold() in _TERMINAL_SUBMISSION_STATUSES:
            return True
        application = self.workspace.find_application(application_id)
        if application is not None and (application.status in _INACTIVE_APPLICATION_STATUSES or application.status == "Applied"):
            return True
        return False

    def _manual_handoff_watch_loop(self, application_id: str, stop_event: threading.Event) -> None:
        missing_page_count = 0
        pending_text_answers: dict[str, dict[str, Any]] = {}
        discrete_widgets = {"select", "dropdown", "checkbox", "checkbox_group", "radio", "radio_group"}
        while not stop_event.wait(2.0):
            if self._is_manual_handoff_watch_terminal(application_id):
                self._update_manual_handoff_watch_state(
                    application_id,
                    active=False,
                    status="stopped",
                    last_error=None,
                    pending_count=0,
                )
                break
            try:
                captured = run_async(self._capture_manual_handoff_answers_async, application_id)
            except Exception as exc:
                self._update_manual_handoff_watch_state(
                    application_id,
                    active=True,
                    status="sync_failed",
                    last_error=str(exc),
                    last_sync_attempt_at=utcnow_iso(),
                )
                continue
            now = utcnow_iso()
            if not captured.get("page_found"):
                missing_page_count += 1
                self._update_manual_handoff_watch_state(
                    application_id,
                    active=missing_page_count < 3,
                    status="page_not_found" if missing_page_count < 3 else "page_closed",
                    last_error=str(captured.get("error") or "").strip() or None,
                    last_sync_attempt_at=now,
                    pending_count=0,
                )
                if missing_page_count >= 3:
                    self._stop_manual_handoff_watcher(application_id, status="page_closed")
                    break
                continue
            missing_page_count = 0
            record = self.workspace.load_submission(application_id)
            question_lookup = {question.question_id: question for question in (record.questions if record is not None else [])}
            stable_captures: list[dict[str, Any]] = []
            seen_text_question_ids: set[str] = set()
            for capture in list(captured.get("answers") or []):
                question_id = str(capture.get("question_id") or "").strip()
                if not question_id:
                    continue
                answer_text = str(capture.get("answer_text") or "").strip()
                if not answer_text:
                    pending_text_answers.pop(question_id, None)
                    continue
                widget_type = str(
                    capture.get("widget_type")
                    or (question_lookup.get(question_id).widget_type if question_id in question_lookup else "")
                ).strip().lower()
                if widget_type in discrete_widgets:
                    stable_captures.append(capture)
                    pending_text_answers.pop(question_id, None)
                    continue
                seen_text_question_ids.add(question_id)
                pending = dict(pending_text_answers.get(question_id) or {})
                if str(pending.get("answer_text") or "") == answer_text:
                    pending["seen_count"] = int(pending.get("seen_count") or 0) + 1
                else:
                    pending = {"answer_text": answer_text, "seen_count": 1}
                pending_text_answers[question_id] = pending
                if int(pending.get("seen_count") or 0) >= 2:
                    stable_captures.append(capture)
            for question_id in list(pending_text_answers):
                if question_id not in seen_text_question_ids:
                    pending_text_answers.pop(question_id, None)

            current_state = self._load_manual_handoff_watch_state(application_id)
            update_payload: dict[str, Any] = {
                "active": True,
                "status": "watching",
                "last_error": None,
                "last_source": "manual_handoff_watch",
                "last_sync_attempt_at": now,
                "last_synced_at": now,
                "last_page_seen_at": now,
                "last_page_url": captured.get("page_url"),
                "sync_count": int(current_state.get("sync_count") or 0) + 1,
                "synced_question_count": int(current_state.get("synced_question_count") or 0),
                "filled_blank_count": int(current_state.get("filled_blank_count") or 0),
                "corrected_answer_count": int(current_state.get("corrected_answer_count") or 0),
                "recent_answers": list(current_state.get("recent_answers") or []),
                "pending_count": len(pending_text_answers),
            }
            if stable_captures:
                persisted = self._persist_manual_handoff_captures(
                    application_id=application_id,
                    captures=stable_captures,
                    approve_memory=True,
                    confidence_reason="manual_handoff_watch",
                )
                update_payload["synced_question_count"] = int(update_payload["synced_question_count"]) + int(persisted["updated_count"])
                update_payload["filled_blank_count"] = int(update_payload["filled_blank_count"]) + int(persisted["filled_blank_count"])
                update_payload["corrected_answer_count"] = int(update_payload["corrected_answer_count"]) + int(
                    persisted["corrected_answer_count"]
                )
                if persisted["updated_questions"]:
                    update_payload["recent_answers"] = list(persisted["updated_questions"][-5:])
                    update_payload["last_saved_at"] = now
                for capture in stable_captures:
                    pending_text_answers.pop(str(capture.get("question_id") or "").strip(), None)
                update_payload["pending_count"] = len(pending_text_answers)
            self._update_manual_handoff_watch_state(application_id, **update_payload)

    def _start_manual_handoff_watcher(self, application_id: str) -> bool:
        with self._MANUAL_HANDOFF_WATCH_LOCK:
            existing = self._MANUAL_HANDOFF_WATCHERS.get(application_id)
            if existing is not None:
                thread = existing.get("thread")
                if thread is not None and thread.is_alive():
                    self._update_manual_handoff_watch_state(
                        application_id,
                        active=True,
                        status="watching",
                        last_error=None,
                    )
                    return False
                self._MANUAL_HANDOFF_WATCHERS.pop(application_id, None)
            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._manual_handoff_watch_loop,
                args=(application_id, stop_event),
                name=f"fmj-manual-handoff-{application_id}",
                daemon=True,
            )
            self._MANUAL_HANDOFF_WATCHERS[application_id] = {
                "thread": thread,
                "stop_event": stop_event,
            }
            self._update_manual_handoff_watch_state(
                application_id,
                active=True,
                status="watching",
                last_error=None,
                started_at=utcnow_iso(),
                pending_count=0,
            )
            thread.start()
            return True

    def _stop_manual_handoff_watcher(self, application_id: str, *, status: str = "stopped") -> None:
        with self._MANUAL_HANDOFF_WATCH_LOCK:
            existing = self._MANUAL_HANDOFF_WATCHERS.pop(application_id, None)
        if existing is not None:
            stop_event = existing.get("stop_event")
            if isinstance(stop_event, threading.Event):
                stop_event.set()
        self._update_manual_handoff_watch_state(
            application_id,
            active=False,
            status=status,
            pending_count=0,
        )

    def answer_question(self, *, application_id: str, question_id: str, answer_text: str, approve_memory: bool = False, auto_retry: bool = True) -> dict[str, Any]:
        record = self.workspace.load_submission(application_id)
        if record is None:
            raise ValueError(f"Unknown submission record: {application_id}")
        if not self._is_active_submission(record):
            raise ValueError(f"Submission is not active: {application_id}")
        application = self.workspace.find_application(application_id)
        if application is not None and application.status in _INACTIVE_APPLICATION_STATUSES:
            raise ValueError(f"Application is not actionable: {application_id}")
        found_question, _found_index = self._find_submission_question(record, question_id)
        if found_question is None:
            raise ValueError(f"Unknown question: {question_id}")
        cleaned_answer = str(answer_text or "").strip()
        transient_question = self._is_transient_submission_question(found_question)
        if transient_question:
            record, updated_question = self._clear_transient_submission_answer(
                record,
                question_id=question_id,
                confidence_reason="transient_answer_not_persisted",
            )
            record = record.model_copy(update={"event_status": "manual_answer_recorded"})
        else:
            record, updated_question = self._apply_manual_answer_to_record(
                record,
                question_id=question_id,
                answer_text=cleaned_answer,
                confidence_reason="manual_override",
                verification_status="verified",
                event_status="manual_answer_recorded",
            )
        record = self._append_review_history_event_to_record(
            record,
            event_type="review.answer.saved",
            summary=(
                f"Recorded a transient verification step for {found_question.prompt_text}."
                if transient_question
                else f"Saved an answer for {found_question.prompt_text}."
            ),
            actor="operator",
            metadata={
                "question_id": question_id,
                "prompt_text": found_question.prompt_text,
                "transient": transient_question,
                "approved_memory": bool(approve_memory and cleaned_answer and not transient_question),
            },
        )
        self.workspace.save_submission(record)
        operator_emit_live_event(
            self.workspace,
            run_id=record.run_id or "manual",
            run_type="submission",
            event_type="submission.question.answered",
            stage="question_resolution",
            status="running",
            message=f"Manual answer saved for {record.company} / {record.role}.",
            job_id=record.job_id,
            application_id=record.application_id,
            company=record.company,
            role=record.role,
            source=record.source,
            payload={"question_id": question_id, "transient": transient_question},
        )
        if approve_memory and cleaned_answer and not transient_question:
            self._store_approved_answer_memory_entry(
                record=record,
                question=updated_question,
                answer_text=cleaned_answer,
            )
        retry_payload: dict[str, Any] | None = None
        if auto_retry:
            try:
                refreshed = run_async(self._prepare_submission_async, application_id, record.run_id)
                if refreshed.submit_ready:
                    refreshed = self._continue_after_ready(application_id, record.run_id)
                retry_payload = {"status": refreshed.status, "submitted_application_ids": [application_id] if refreshed.status == "submitted" else [], "still_pending_application_ids": [] if refreshed.status in {"submitted", "preview_ready"} else [application_id], "error": None}
            except Exception as exc:
                operator_emit_live_event(
                    self.workspace,
                    run_id=record.run_id or "manual",
                    run_type="submission",
                    event_type="submission.question.retry_failed",
                    stage="question_resolution",
                    status="warning",
                    message=f"Retry after manual answer failed for {record.company} / {record.role}.",
                    job_id=record.job_id,
                    application_id=record.application_id,
                    company=record.company,
                    role=record.role,
                    source=record.source,
                    payload={"question_id": question_id, "error": str(exc)},
                    state_updates={"latest_error": str(exc)},
                )
                retry_payload = {"status": "retry_failed", "submitted_application_ids": [], "still_pending_application_ids": [application_id], "error": str(exc)}
        saved_record = self.workspace.load_submission(application_id)
        return {
            "application_id": application_id,
            "question": updated_question.model_dump(mode="json"),
            "remaining_blockers": self._submission_blockers(saved_record),
            "retry": retry_payload,
        }

    def review_queue_payload(self, *, limit: int = 40) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for application in self.workspace.load_applications():
            job = self.workspace.load_job(application.job_id)
            if job is None or application.status in {"Archived", "Rejected", "Dismissed"}:
                continue
            submission = self.workspace.find_submission(application.id)
            if submission is not None and str(submission.status or "").strip().lower() == "submitted":
                continue
            review_summary = self._review_summary(application=application, job=job, submission=submission)
            manual_handoff = self._manual_handoff_summary(submission)
            items.append(
                {
                    "application_id": application.id,
                    "job_id": application.job_id,
                    "company": application.company,
                    "title": application.role,
                    "status": application.status,
                    "review_status": submission.status if submission is not None else "not_prepared",
                    "classification": {
                        "board_family": job.board_family,
                        "automation_tier": job.automation_tier,
                        "ats_family": job.ats_family,
                        "ats_preview_supported": job.ats_preview_supported,
                        "rehearsal_eligible": job.rehearsal_eligible,
                    },
                    "hard_reject_reason": job.hard_reject_reason,
                    "auth_reject_reason": job.auth_reject_reason,
                    "login_wall_detected": job.login_wall_detected,
                    "screening": screening_payload(job),
                    "gate": {
                        "missing_required_fields": list(submission.missing_required_fields) if submission is not None else [],
                        "ungrounded_answers": list(submission.ungrounded_answers) if submission is not None else [],
                        "low_confidence_answers": list(submission.low_confidence_answers) if submission is not None else [],
                        "warnings": list(submission.warnings) if submission is not None else [],
                    },
                    "remaining_blockers": self._submission_blockers(submission),
                    "report": application.report,
                    "review_summary": review_summary,
                    "manual_handoff": manual_handoff,
                }
            )
        items.sort(key=lambda item: (item["status"], item["company"], item["title"]))
        compatible_counts = {
            "ready_for_review": sum(1 for item in items if item["status"] == "Ready to Submit"),
            "needs_user_input": sum(1 for item in items if item["status"] == "Needs Input"),
            "preview_ready": sum(1 for item in items if item["status"] == "Preview Ready"),
            "approved_pending_submit": sum(1 for item in items if item["status"] == "Applied"),
        }
        return {"count": len(items), "counts": compatible_counts, "items": items[:limit]}

    def review_application(self, *, application_id: str, action: str, reason: str | None = None) -> dict[str, Any]:
        application = self.workspace.find_application(application_id)
        if application is None:
            raise ValueError(f"Unknown application: {application_id}")
        if action == "mark_submitted":
            self._stop_manual_handoff_watcher(application_id, status="submitted")
            return self._mark_application_submitted_manually(application, reason=reason)
        if action == "reject":
            self._stop_manual_handoff_watcher(application_id, status="rejected")
            self.workspace.upsert_application(application.model_copy(update={"status": "Rejected"}))
            note = str(reason or "").strip() or "Rejected from the review console."
            record = self.workspace.load_submission(application_id)
            if record is None:
                job = self.workspace.load_job(application.job_id)
                record = SubmissionRecord(
                    application_id=application.id,
                    job_id=application.job_id,
                    company=application.company,
                    role=application.role,
                    source=application.source,
                    apply_url=(getattr(job, "apply_url", None) or application.url),
                )
            record = record.model_copy(
                update={
                    "status": "rejected",
                    "reviewed": True,
                    "notes": self._dedupe_strings(list(record.notes) + [note]),
                    "updated_at": utcnow_iso(),
                }
            )
            record = self._append_review_history_event_to_record(
                record,
                event_type="review.action.reject",
                summary="Rejected the application from the review queue.",
                actor="operator",
                metadata={"reason": note},
            )
            self.workspace.save_submission(record)
            return {"application_id": application_id, "status": "rejected", "blocked": False, "remaining_blockers": []}
        if action == "sync_manual_input":
            result = run_async(
                self._sync_manual_handoff_answers_async,
                application_id,
                True,
                "manual_handoff_console_sync",
            )
            self._append_review_history_event(
                application_id,
                event_type="review.action.sync_manual_input",
                summary=(
                    "Synced browser changes from the parked manual-handoff page."
                    if result.get("page_found")
                    else "Tried to sync the parked manual-handoff page, but no live tab was found."
                ),
                actor="operator",
                metadata={
                    "page_found": bool(result.get("page_found")),
                    "updated_count": int(result.get("updated_count") or 0),
                    "filled_blank_count": int(result.get("filled_blank_count") or 0),
                    "corrected_answer_count": int(result.get("corrected_answer_count") or 0),
                },
            )
            return {
                "application_id": application_id,
                "status": result.get("status"),
                "blocked": bool(result.get("remaining_blockers")),
                "page_found": bool(result.get("page_found")),
                "synced_count": int(result.get("updated_count") or 0),
                "filled_blank_count": int(result.get("filled_blank_count") or 0),
                "corrected_answer_count": int(result.get("corrected_answer_count") or 0),
                "watch_state": result.get("watch_state"),
                "remaining_blockers": list(result.get("remaining_blockers") or []),
            }
        if action == "request_input":
            self.workspace.upsert_application(application.model_copy(update={"status": "Needs Input"}))
            record = run_async(self._prepare_submission_async, application_id, None)
            manual_handoff_opened = False
            try:
                manual_handoff_opened = bool(run_async(self._open_manual_handoff_preview_async, application_id, None))
            except Exception:
                manual_handoff_opened = False
            self._append_review_history_event(
                application_id,
                event_type="review.action.request_input",
                summary="Opened manual input mode from the review queue.",
                actor="operator",
                metadata={"manual_handoff_opened": manual_handoff_opened, "reason": str(reason or "").strip() or None},
            )
            return {
                "application_id": application_id,
                "status": record.status,
                "blocked": True,
                "manual_handoff_opened": manual_handoff_opened,
                "remaining_blockers": self._submission_blockers(record),
            }
        if action != "approve":
            raise ValueError(f"Unsupported review action: {action}")
        record = run_async(self._prepare_submission_async, application_id, None)
        if self._submission_blockers(record):
            self.workspace.upsert_application(application.model_copy(update={"status": "Needs Input"}))
            manual_handoff_opened = False
            try:
                manual_handoff_opened = bool(run_async(self._open_manual_handoff_preview_async, application_id, None))
            except Exception:
                manual_handoff_opened = False
            self._append_review_history_event(
                application_id,
                event_type="review.action.approve",
                summary="Approve / Apply was attempted, but blockers still require manual review.",
                actor="operator",
                metadata={"blocked": True, "manual_handoff_opened": manual_handoff_opened, "reason": str(reason or "").strip() or None},
            )
            return {
                "application_id": application_id,
                "status": record.status,
                "blocked": True,
                "manual_handoff_opened": manual_handoff_opened,
                "remaining_blockers": self._submission_blockers(record),
            }
        if record.submit_ready:
            record = self._continue_after_ready(application_id, None)
        else:
            self.workspace.upsert_application(application.model_copy(update={"status": "Ready to Submit"}))
        self._append_review_history_event(
            application_id,
            event_type="review.action.approve",
            summary="Approve / Apply advanced the application.",
            actor="operator",
            metadata={"blocked": False, "status": record.status, "reason": str(reason or "").strip() or None},
        )
        return {"application_id": application_id, "status": record.status, "blocked": False, "auto_submitted": record.status == "submitted", "remaining_blockers": []}

    def application_detail_payload(self, application_id: str) -> dict[str, Any]:
        application = self.workspace.find_application(application_id)
        if application is None:
            raise ValueError(f"Unknown application: {application_id}")
        job = self.workspace.load_job(application.job_id)
        evaluation = self.workspace.load_evaluation(application.job_id)
        submission = self.workspace.find_submission(application.id)
        report_text = None
        if application.report:
            report_path = (self.workspace.root / application.report).resolve()
            if report_path.exists():
                report_text = report_path.read_text(encoding="utf-8")
        summary = self._review_summary(application=application, job=job, submission=submission)
        artifacts = self._normalized_artifacts(application=application, submission=submission)
        history = sorted(
            self._review_history_entries(submission),
            key=lambda item: str(item.get("timestamp") or ""),
            reverse=True,
        )
        return {
            "application": {"application_id": application.id, "job_id": application.job_id, "company": application.company, "role": application.role, "status": application.status, "score": application.score, "grade": application.grade, "report": application.report, "url": application.url, "source": application.source},
            "job": job.model_dump(mode="json") if job is not None else None,
            "evaluation": evaluation.model_dump(mode="json") if evaluation is not None else None,
            "questions": [item.model_dump(mode="json") for item in (submission.questions if submission is not None else [])],
            "blockers": self._submission_blockers(submission),
            "submission": submission.model_dump(mode="json") if submission is not None else None,
            "manual_handoff_watch": dict((submission.result or {}).get("manual_handoff_watch") or {}) if submission is not None else {},
            "summary": summary,
            "artifacts": artifacts,
            "history": history,
            "report_markdown": report_text,
        }

    def runs_history_payload(self, *, limit: int = 20) -> dict[str, Any]:
        runs = [self._run_record_summary(run) for run in self.workspace.load_runs()[:limit]]
        return {"count": len(runs), "items": runs, "activity": runs[:6]}

    def reset_operational_state_payload(self) -> dict[str, Any]:
        self.workspace.ensure()
        def _clear_tree(directory: Path) -> int:
            removed = 0
            for path in sorted(directory.rglob("*"), key=lambda item: len(item.parts), reverse=True):
                try:
                    if path.is_file() or path.is_symlink():
                        path.unlink()
                    elif path.is_dir():
                        path.rmdir()
                    else:
                        continue
                    removed += 1
                except OSError:
                    continue
            return removed

        handled_before_reset = self._snapshot_handled_jobs()
        deleted: dict[str, int] = {
            "applications": len(self.workspace.load_applications()),
            "inbox_rows": len(self.workspace.load_inbox()),
            "scan_history_rows": len(self.workspace.load_scan_history()),
            "live_events": len(self.workspace.load_live_events()),
        }
        self.workspace.save_applications([])
        self.workspace.save_inbox([])
        self.workspace._write_tsv(self.workspace.scan_history_path, SCAN_HISTORY_COLUMNS, [])
        self.workspace.save_live_state(LiveRunState())
        self.workspace.live_events_path.write_text("", encoding="utf-8")

        for key, directory in (
            ("jobs", self.workspace.jobs_dir),
            ("evaluations", self.workspace.evaluations_dir),
            ("submissions", self.workspace.submissions_dir),
            ("runs", self.workspace.runs_dir),
        ):
            removed = 0
            for path in directory.glob("*.json"):
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
            deleted[key] = removed

        for key, directory in (
            ("reports", self.workspace.reports_dir),
            ("output_files", self.workspace.output_dir),
            ("chatgpt_runtime", self.workspace.runtime_dir),
        ):
            deleted[key] = _clear_tree(directory)

        # Clear stale board discovery metrics so post-reset UI is truthful.
        self.workspace.save_board_discovery_state(BoardDiscoveryState())
        deleted["board_discovery"] = 1

        # Clear live run traces (contain PII/model call data).
        deleted["live_run_traces"] = _clear_tree(self.workspace.live_runs_dir)

        self._submission_registry.clear()
        self._save_submission_registry()
        def _portable(path: Path) -> str:
            return self.workspace.relative_path(path).replace("\\", "/")

        preserved = {
            "profile": _portable(self.workspace.profile_path),
            "portals": _portable(self.workspace.portals_path),
            "facts": _portable(self.workspace.facts_path),
            "answer_memory": _portable(self.workspace.answer_memory_path),
            "cv": _portable(self.workspace.cv_path),
            "candidate_dossier": _portable(self.workspace.candidate_dossier_path),
            "workspace_model_config": _portable(self.workspace.workspace_config_path),
            "handled_jobs": _portable(self.workspace.handled_jobs_path),
            "modes_dir": _portable(self.workspace.modes_dir),
        }
        return {
            "reset": True,
            "deleted": deleted,
            "preserved": preserved,
            "handled_jobs": {
                "job_ids": len(handled_before_reset.get("job_ids") or []),
                "urls": len(handled_before_reset.get("urls") or []),
                "pairs": len(handled_before_reset.get("pairs") or []),
                "duplicate_clusters": len(handled_before_reset.get("duplicate_clusters") or []),
            },
            "autonomous": self.autonomous_status_payload(),
            "jobs_table": self.jobs_table_payload(limit=25, include_rejected=False),
        }

    def settings_payload(self) -> dict[str, Any]:
        profile = self.workspace.load_profile()
        portals = self.workspace.load_portals()
        app_config = AppConfig.load(self.workspace.root)
        captcha_settings = self._captcha_runtime_settings(
            configured_strategy=app_config.captcha.strategy,
            provider=app_config.captcha.provider,
            api_key_env=app_config.captcha.api_key_env,
            solve_timeout_seconds=app_config.captcha.solve_timeout_seconds,
            browser_mode=profile.runtime.automation.browser_mode,
        )
        setup = self.setup_readiness_payload()
        advanced_models = advanced_models_payload(self.workspace)
        launch_profile = dict(advanced_models.get("launch_profile") or {})
        runtime_model = profile.runtime.model.model_dump(mode="json")
        autonomous_payload = profile.runtime.automation.model_dump(mode="json")
        autonomous_payload.update(
            {
                "captcha_strategy": captcha_settings["captcha_strategy"],
                "captcha_strategy_effective": captcha_settings["captcha_strategy_effective"],
                "captcha_provider": captcha_settings["captcha_provider"],
                "captcha_api_key_env": captcha_settings["captcha_api_key_env"],
                "captcha_solve_timeout_seconds": captcha_settings["captcha_solve_timeout_seconds"],
            }
        )
        return {
            "profile": profile.model_dump(mode="json"),
            "portals": portals.model_dump(mode="json"),
            "tracked_companies": [item.model_dump(mode="json") for item in portals.tracked_companies],
            "autonomous": autonomous_payload,
            "captcha": captcha_settings,
            "runtime_model": runtime_model,
            "local_model": runtime_model,
            "chatgpt_drafting": ChatGPTDraftingService(self.workspace).status_payload(),
            "model_strategy": {
                "mode": "lm_studio_local",
                "provider": profile.runtime.model.provider,
                "transport": profile.runtime.model.transport,
                "base_url": profile.runtime.model.base_url,
                "model": profile.runtime.model.model,
                "api_key_env": profile.runtime.model.api_key_env,
                "launch_transport_mix": launch_profile.get("transport_mix"),
                "role_bindings": advanced_models.get("role_bindings", {}),
            },
            "drafting_strategy": {
                "renderer": app_config.personal.resume_renderer,
                "chatgpt_enabled": bool(app_config.chatgpt_drafting.enabled),
                "gpt_url": app_config.chatgpt_drafting.gpt_url,
                "screening_model": profile.runtime.model.model,
                "question_answerer_mode": "lm_studio_local",
            },
            "submit_mode": profile.runtime.automation.default_submit_mode,
            "dossier": candidate_dossier_metadata(self.workspace),
            "advanced_models": advanced_models,
            "last_model_checks": self._model_check_state(),
            "live_feed": {"enabled": True, "status_path": "/api/live/status", "events_path": "/api/live/events"},
            "readiness": {
                "config_validation": setup.get("config_validation"),
                "doctor": setup.get("doctor"),
                "launch_check": setup.get("launch_check"),
                "findings": setup.get("findings", []),
            },
        }

    def test_config(self) -> dict[str, Any]:
        readiness = self.setup_readiness_payload()
        findings = list(readiness.get("findings") or [])
        return {
            "overall_status": readiness["overall_status"],
            "blocked_count": sum(1 for item in findings if item["status"] == "blocked"),
            "warning_count": sum(1 for item in findings if item["status"] == "warning"),
            "findings": findings,
        }

    def save_greenhouse_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        portals = self.workspace.load_portals()
        greenhouse = portals.sources["greenhouse"]
        greenhouse.enabled = bool(payload.get("enabled", greenhouse.enabled))
        greenhouse.boards = self._normalize_list_payload(payload.get("boards", []))
        self.workspace.save_portals(portals)
        profile = self.workspace.load_profile()
        production_sources = self._effective_production_sources(profile.runtime.automation.production_sources)
        automation = profile.runtime.automation.model_copy(update={"submit_enabled": bool(payload.get("submit_enabled", profile.runtime.automation.submit_enabled)), "browser_attach_enabled": bool(payload.get("browser_attach_enabled", profile.runtime.automation.browser_attach_enabled)), "browser_cdp_url": str(payload.get("browser_cdp_url") or profile.runtime.automation.browser_cdp_url or "").strip() or None, "production_sources": production_sources})
        self.workspace.save_profile(profile.model_copy(update={"runtime": profile.runtime.model_copy(update={"automation": automation})}))
        self._sync_workspace_runtime_config(
            automation=automation,
            active_sources=production_sources,
            greenhouse_browser_attach_enabled=bool(payload.get("browser_attach_enabled", automation.browser_attach_enabled)),
            greenhouse_browser_cdp_url=str(payload.get("browser_cdp_url") or automation.browser_cdp_url or "").strip() or None,
        )
        return {"saved": True, "greenhouse": greenhouse.model_dump(mode="json")}

    def save_portal_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        portals = self.workspace.load_portals()
        incoming_sources = payload.get("sources") or {}
        for source_name in _SUPPORTED_PRODUCTION_SOURCES:
            source_payload = dict(incoming_sources.get(source_name) or {})
            source_config = portals.sources.setdefault(source_name, SourceBoardConfig())
            source_config.enabled = bool(source_payload.get("enabled", getattr(source_config, "enabled", True)))
            source_config.boards = self._normalize_list_payload(source_payload.get("boards", getattr(source_config, "boards", [])))
            source_config.seed_urls = self._normalize_list_payload(source_payload.get("seed_urls", getattr(source_config, "seed_urls", [])))
            source_config.seed_domains = self._normalize_domain_list(source_payload.get("seed_domains", getattr(source_config, "seed_domains", [])))
        portals.tracked_companies = [
            TrackedCompany(
                name=str(item.get("name") or "").strip(),
                careers_url=str(item.get("careers_url") or "").strip() or None,
                source=str(item.get("source") or "").strip().lower() or None,
                board=str(item.get("board") or "").strip() or None,
                api=str(item.get("api") or "").strip() or None,
                enabled=bool(item.get("enabled", True)),
                notes=str(item.get("notes") or "").strip() or None,
            )
            for item in list(payload.get("tracked_companies") or [])
            if str(item.get("name") or "").strip()
        ]
        self.workspace.save_portals(portals)

        profile = self.workspace.load_profile()
        enabled_sources = [source_name for source_name in ("greenhouse", "lever", "ashby") if portals.sources.get(source_name) and portals.sources[source_name].enabled]
        automation = profile.runtime.automation.model_copy(update={"production_sources": enabled_sources})
        self.workspace.save_profile(profile.model_copy(update={"runtime": profile.runtime.model_copy(update={"automation": automation})}))
        self._sync_workspace_runtime_config(automation=automation, active_sources=enabled_sources)
        return {
            "saved": True,
            "portals": portals.model_dump(mode="json"),
            "autonomous": automation.model_dump(mode="json"),
        }

    def save_autonomous_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile = self.workspace.load_profile()
        production_sources = self._effective_production_sources(payload.get("production_sources"))
        automation = profile.runtime.automation.model_copy(update={"enabled": bool(payload.get("enabled", profile.runtime.automation.enabled)), "submit_enabled": bool(payload.get("submit_enabled", profile.runtime.automation.submit_enabled)), "default_submit_mode": str(payload.get("default_submit_mode") or profile.runtime.automation.default_submit_mode), "ready_to_apply_threshold": int(payload.get("ready_to_apply_threshold", profile.runtime.automation.ready_to_apply_threshold)), "browser_attach_enabled": bool(payload.get("browser_attach_enabled", profile.runtime.automation.browser_attach_enabled)), "browser_cdp_url": str(payload.get("browser_cdp_url") or profile.runtime.automation.browser_cdp_url or "").strip() or None, "browser_mode": str(payload.get("browser_mode") or profile.runtime.automation.browser_mode), "max_open_tabs": int(payload.get("max_open_tabs", profile.runtime.automation.max_open_tabs)), "daily_submit_cap": int(payload.get("daily_submit_cap", profile.runtime.automation.daily_submit_cap)), "per_company_daily_cap": int(payload.get("per_company_daily_cap", profile.runtime.automation.per_company_daily_cap)), "production_sources": production_sources})
        updated_profile = profile.model_copy(update={"runtime": profile.runtime.model_copy(update={"automation": automation})})
        self.workspace.save_profile(updated_profile)
        portal_sources = self._sync_portal_sources(production_sources)
        self._sync_workspace_runtime_config(
            automation=automation,
            active_sources=production_sources,
            greenhouse_browser_cdp_url=str(payload.get("browser_cdp_url") or automation.browser_cdp_url or "").strip() or None,
        )
        doc, config_path = self._load_workspace_config_doc()
        captcha_tbl = doc.get("captcha")
        if captcha_tbl is None:
            captcha_tbl = table()
            doc["captcha"] = captcha_tbl
        captcha_tbl["strategy"] = str(payload.get("captcha_strategy") or AppConfig.load(self.workspace.root).captcha.strategy or "skip")
        captcha_tbl["provider"] = str(payload.get("captcha_provider") or AppConfig.load(self.workspace.root).captcha.provider or "2captcha")
        captcha_tbl["api_key_env"] = str(payload.get("captcha_api_key_env") or AppConfig.load(self.workspace.root).captcha.api_key_env or "CAPTCHA_API_KEY")
        captcha_tbl["solve_timeout_seconds"] = int(payload.get("captcha_solve_timeout_seconds", AppConfig.load(self.workspace.root).captcha.solve_timeout_seconds))
        config_path.write_text(dumps(doc), encoding="utf-8")
        app_config = AppConfig.load(self.workspace.root)
        captcha_settings = self._captcha_runtime_settings(
            configured_strategy=app_config.captcha.strategy,
            provider=app_config.captcha.provider,
            api_key_env=app_config.captcha.api_key_env,
            solve_timeout_seconds=app_config.captcha.solve_timeout_seconds,
            browser_mode=automation.browser_mode,
        )
        autonomous_payload = automation.model_dump(mode="json")
        autonomous_payload.update(
            {
                "captcha_strategy": captcha_settings["captcha_strategy"],
                "captcha_strategy_effective": captcha_settings["captcha_strategy_effective"],
                "captcha_provider": captcha_settings["captcha_provider"],
                "captcha_api_key_env": captcha_settings["captcha_api_key_env"],
                "captcha_solve_timeout_seconds": captcha_settings["captcha_solve_timeout_seconds"],
            }
        )
        return {"saved": True, "autonomous": autonomous_payload, "captcha": captcha_settings, "portal_sources": portal_sources}

    def chatgpt_drafting_status_payload(self) -> dict[str, Any]:
        return ChatGPTDraftingService(self.workspace).status_payload()

    def launch_chatgpt_browser(self, *, close_existing: bool = False, start_blank: bool = True) -> dict[str, Any]:
        start_url = "about:blank" if start_blank else None
        return ChatGPTDraftingService(self.workspace).launch_browser(close_existing=close_existing, start_url=start_url)

    def test_chatgpt_drafting(self, target: str | None = None) -> dict[str, Any]:
        return ChatGPTDraftingService(self.workspace).test(target)

    def save_chatgpt_drafting_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        doc, config_path = self._load_workspace_config_doc()
        personal_tbl = doc.get("personal")
        if personal_tbl is None:
            personal_tbl = table()
            doc["personal"] = personal_tbl
        chatgpt_tbl = doc.get("chatgpt_drafting")
        if chatgpt_tbl is None:
            chatgpt_tbl = table()
            doc["chatgpt_drafting"] = chatgpt_tbl

        if payload.get("make_default", True):
            personal_tbl["resume_renderer"] = "chatgpt_download"

        for key in (
            "enabled",
            "gpt_url",
            "completion_start_marker",
            "completion_end_marker",
            "profile_dir",
            "downloads_dir",
            "browser_mode",
            "browser_cdp_url",
            "launch_if_missing",
            "use_temporary_chat",
            "timeout_seconds",
            "prompt_submit_delay_ms",
            "download_timeout_seconds",
            "max_parallel_jobs",
        ):
            if key in payload:
                chatgpt_tbl[key] = payload[key]

        config_path.write_text(dumps(doc), encoding="utf-8")
        return {"saved": True, "chatgpt_drafting": self.chatgpt_drafting_status_payload()}

    def save_smoke_allowlist(self, urls: list[str] | str) -> dict[str, Any]:
        parsed = [item.strip() for item in urls.splitlines()] if isinstance(urls, str) else [str(item).strip() for item in urls]
        parsed = [item for item in parsed if item]
        return {"saved": True, "urls": parsed, "note": "Smoke allowlists are deprecated and not part of the file-first launch control plane."}

    def save_runtime_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile = self.workspace.load_profile()
        current_model = profile.runtime.model
        model_name = str(payload.get("model") or current_model.model).strip() or LMSTUDIO_AUTO_MODEL
        base_url = current_model.base_url
        if "base_url" in payload:
            base_url = str(payload.get("base_url") or "").strip() or None
        provider, transport, base_url, api_key_env, local = self._normalize_local_http_settings(
            provider=LMSTUDIO_PROVIDER,
            transport="local_http",
            base_url=base_url,
            api_key_env=None,
        )
        if transport == "local_http":
            try:
                resolved = probe_lmstudio_base_url(base_url or LMSTUDIO_DEFAULT_HOST)
                base_url = resolved.canonical_base_url
            except Exception:
                pass
        model = profile.runtime.model.model_copy(
            update={
                "provider": provider,
                "transport": transport,
                "base_url": base_url,
                "api_key_env": None,
                "model": model_name,
                "temperature": float(payload.get("temperature", current_model.temperature)),
                "max_tokens": int(payload.get("max_tokens", current_model.max_tokens) or current_model.max_tokens),
                "preferred_context_window": int(
                    payload.get("preferred_context_window", current_model.preferred_context_window)
                    or current_model.preferred_context_window
                ),
                "local": local,
                "command": [],
                "working_dir": None,
            }
        )
        self.workspace.save_profile(profile.model_copy(update={"runtime": profile.runtime.model_copy(update={"model": model})}))
        return {"saved": True, "runtime_model": model.model_dump(mode="json"), "local_model": model.model_dump(mode="json")}

    def save_model_profile(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if name == "runtime-model":
            result = self.save_runtime_model(payload)
            result["saved"] = True
            return result
        sanitized_payload = dict(payload)
        sanitized_payload["model"] = str(sanitized_payload.get("model") or "").strip() or LMSTUDIO_AUTO_MODEL
        sanitized_payload.update(
            {
                "provider": LMSTUDIO_PROVIDER,
                "transport": "local_http",
                "api_key_env": None,
                "command": [],
                "working_dir": None,
                "local": True,
            }
        )
        saved_profile = save_workspace_model_profile(self.workspace, name, sanitized_payload)
        return {"saved": True, "model_profile": saved_profile.model_dump(mode="json"), "advanced_models": advanced_models_payload(self.workspace)}

    def ping_model_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        router = self.model_router()
        profile_name = str(payload.get("profile_name") or "").strip() or None
        if profile_name:
            if router is None:
                raise ValueError("Model router is not configured for saved profile pings.")
            profile = router.get_profile(name=profile_name)
            provider, transport, base_url, api_key_env, local = self._normalize_local_http_settings(
                provider=LMSTUDIO_PROVIDER,
                transport="local_http",
                base_url=profile.base_url,
                api_key_env=None,
            )
            profile = profile.model_copy(
                update={
                    "provider": provider,
                    "transport": transport,
                    "base_url": base_url,
                    "api_key_env": api_key_env,
                    "local": local,
                    "command": [],
                    "working_dir": None,
                }
            )
            cache_key = profile_name
        else:
            profile = self._runtime_model_profile(
                payload,
                name=str(payload.get("name") or "runtime-model").strip() or "runtime-model",
                role=payload.get("role"),
            )
            provider, transport, base_url, api_key_env, local = self._normalize_local_http_settings(
                provider=LMSTUDIO_PROVIDER,
                transport="local_http",
                base_url=profile.base_url,
                api_key_env=None,
            )
            profile = profile.model_copy(
                update={
                    "provider": provider,
                    "transport": transport,
                    "base_url": base_url,
                    "api_key_env": api_key_env,
                    "local": local,
                    "command": [],
                    "working_dir": None,
                }
            )
            cache_key = profile.name
            if router is None:
                router = ModelRouter(AppConfig())
        result = run_async(lambda: router.test_profile(profile))
        return self._record_model_check(cache_key, result)

    def install_recommended_models(self) -> dict[str, Any]:
        result = install_recommended_split_profiles(self.workspace)
        result["advanced_models"] = advanced_models_payload(self.workspace)
        return result

    def delete_model_profile(self, name: str) -> dict[str, Any]:
        return delete_workspace_model_profile(self.workspace, name)


    async def _prepare_submission_async(self, application_id: str, run_id: str | None) -> SubmissionRecord:
        application = self.workspace.find_application(application_id)
        if application is None:
            raise ValueError(f"Unknown application: {application_id}")
        job = self.workspace.load_job(application.job_id)
        if job is None:
            raise ValueError(f"Unknown job: {application.job_id}")
        run_token = run_id or "manual"
        existing = self.workspace.load_submission(application.id)
        manual_answers = dict(existing.manual_answers) if existing is not None else {}
        self._emit_runtime_event(
            run_id=run_token,
            run_type="submission",
            event_type="submission.prepare.started",
            message=f"Preparing application contract for {application.company} / {application.role}.",
            stage="prepare",
            phase="prepare",
            status="running",
            job_id=application.job_id,
            application_id=application.id,
            submission_id=application.id,
            company=application.company,
            role=application.role,
            source=application.source,
        )
        if application.source and application.source not in _SUPPORTED_PRODUCTION_SOURCES:
            warning_message = f"{application.source} is not enabled for unattended production submit."
            artifacts = self._artifact_map(application)
            record = SubmissionRecord(
                application_id=application.id,
                job_id=application.job_id,
                company=application.company,
                role=application.role,
                source=application.source,
                apply_url=job.apply_url,
                status="unsupported_source",
                event_status="unsupported_source",
                warnings=[warning_message],
                manual_answers=manual_answers,
                artifacts=artifacts,
                run_id=run_id,
            )
            self.workspace.save_submission(record)
            self.workspace.upsert_application(application.model_copy(update={"status": "Needs Input", "pdf": bool(application.pdf), "notes": warning_message}))
            self._emit_runtime_event(
                run_id=run_token,
                run_type="submission",
                event_type="submission.prepare.unsupported_source",
                message=f"Unsupported source for {application.company} / {application.role}.",
                stage="prepare",
                phase="prepare",
                status="blocked",
                job_id=application.job_id,
                application_id=application.id,
                submission_id=application.id,
                company=application.company,
                role=application.role,
                source=application.source,
                artifact_paths=artifacts,
                error={"message": warning_message},
                metrics={"supported": False},
            )
            return record
    
        if not application.pdf:
            self._emit_runtime_event(
                run_id=run_token,
                run_type="submission",
                event_type="submission.render.started",
                message=f"Generating artifacts for {application.company} / {application.role} before prepare.",
                stage="drafting",
                phase="render",
                status="running",
                job_id=application.job_id,
                application_id=application.id,
                submission_id=application.id,
                company=application.company,
                role=application.role,
                source=application.source,
            )
            try:
                with self._model_trace_context(
                    run_id=run_token,
                    run_type="submission",
                    stage="drafting",
                    phase="render",
                    job_id=application.job_id,
                    application_id=application.id,
                    submission_id=application.id,
                    company=application.company,
                    role=application.role,
                    source=application.source,
                ):
                    pdf_result = build_pdf_for_target(self.workspace, application.id)
            except Exception as exc:
                artifacts = self._artifact_map(application)
                trace_ref = self._persist_trace_payload(
                    run_token,
                    category="submission-steps",
                    name=f"render-{application.id}",
                    payload={
                        "application_id": application.id,
                        "job_id": application.job_id,
                        "company": application.company,
                        "role": application.role,
                        "source": application.source,
                        "error": str(exc),
                        "artifacts": artifacts,
                    },
                )
                updated_record = (existing or SubmissionRecord(
                    application_id=application.id,
                    job_id=application.job_id,
                    company=application.company,
                    role=application.role,
                    source=application.source,
                    apply_url=job.apply_url,
                    manual_answers=manual_answers,
                )).model_copy(update={
                    "status": "blocked",
                    "event_status": "render_failed",
                    "submit_ready": False,
                    "warnings": self._dedupe_strings(list((existing.warnings if existing is not None else [])) + ["Artifact generation failed."]),
                    "last_error": str(exc),
                    "artifacts": artifacts,
                    "run_id": run_id,
                    "updated_at": utcnow_iso(),
                })
                self.workspace.save_submission(updated_record)
                self.workspace.upsert_application(application.model_copy(update={"status": "Needs Input", "pdf": False, "notes": str(exc)}))
                self._emit_runtime_event(
                    run_id=run_token,
                    run_type="submission",
                    event_type="submission.render.failed",
                    message=f"Artifact generation failed for {application.company} / {application.role}.",
                    stage="drafting",
                    phase="render",
                    status="failed",
                    job_id=application.job_id,
                    application_id=application.id,
                    submission_id=application.id,
                    company=application.company,
                    role=application.role,
                    source=application.source,
                    artifact_paths=artifacts,
                    error=exc,
                    trace_ref=trace_ref,
                    metrics={"artifact_generation": 0},
                )
                return updated_record
            application = self.workspace.find_application(application.id) or application
            render_error = str((pdf_result or {}).get("render_error") or "").strip() or None
            render_artifacts = self._artifact_paths_from_payload(pdf_result)
            trace_ref = self._persist_trace_payload(
                run_token,
                category="submission-steps",
                name=f"render-{application.id}",
                payload={
                    "application_id": application.id,
                    "job_id": application.job_id,
                    "company": application.company,
                    "role": application.role,
                    "source": application.source,
                    "result": pdf_result,
                },
            )
            if render_error:
                artifacts = self._artifact_map(application)
                updated_record = (existing or SubmissionRecord(
                    application_id=application.id,
                    job_id=application.job_id,
                    company=application.company,
                    role=application.role,
                    source=application.source,
                    apply_url=job.apply_url,
                    manual_answers=manual_answers,
                )).model_copy(update={
                    "status": "blocked",
                    "event_status": "render_failed",
                    "submit_ready": False,
                    "warnings": self._dedupe_strings(list((existing.warnings if existing is not None else [])) + ["Artifact generation failed."]),
                    "last_error": render_error,
                    "artifacts": artifacts,
                    "run_id": run_id,
                    "updated_at": utcnow_iso(),
                })
                self.workspace.save_submission(updated_record)
                self.workspace.upsert_application(application.model_copy(update={"status": "Needs Input", "pdf": False, "notes": render_error}))
                self._emit_runtime_event(
                    run_id=run_token,
                    run_type="submission",
                    event_type="submission.render.failed",
                    message=f"Artifact generation failed for {application.company} / {application.role}.",
                    stage="drafting",
                    phase="render",
                    status="failed",
                    job_id=application.job_id,
                    application_id=application.id,
                    submission_id=application.id,
                    company=application.company,
                    role=application.role,
                    source=application.source,
                    artifact_paths=artifacts or render_artifacts,
                    error={"message": render_error},
                    trace_ref=trace_ref,
                    payload=pdf_result,
                    metrics={"artifact_generation": 0},
                )
                return updated_record
            self._emit_runtime_event(
                run_id=run_token,
                run_type="submission",
                event_type="submission.render.completed",
                message=f"Artifacts generated for {application.company} / {application.role}.",
                stage="drafting",
                phase="render",
                status="completed",
                job_id=application.job_id,
                application_id=application.id,
                submission_id=application.id,
                company=application.company,
                role=application.role,
                source=application.source,
                artifact_paths=render_artifacts,
                trace_ref=trace_ref,
                payload=pdf_result,
                metrics={"artifact_generation": 1},
            )
    
        posting = self._normalized_job(job)
        adapter = self._adapter_for_job(job)
        existing = self.workspace.load_submission(application.id)
        manual_answers = dict(existing.manual_answers) if existing is not None else {}
        grounding = self._grounding_service()
        facts = self._grounding_facts_for_application(application, job)
        answer_memory = [item.model_dump(mode="json") for item in self.workspace.load_answer_memory()]
        artifacts = self._artifact_map(application)
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                extraction = await adapter.load_application_contract(client, posting)
        except Exception as exc:
            trace_ref = self._persist_trace_payload(
                run_token,
                category="submission-steps",
                name=f"contract-{application.id}",
                payload={
                    "application_id": application.id,
                    "job_id": application.job_id,
                    "company": application.company,
                    "role": application.role,
                    "source": application.source,
                    "error": str(exc),
                    "artifacts": artifacts,
                },
            )
            record = SubmissionRecord(
                application_id=application.id,
                job_id=application.job_id,
                company=application.company,
                role=application.role,
                source=application.source,
                apply_url=job.apply_url,
                status="contract_error",
                event_status="contract_error",
                last_error=str(exc),
                warnings=["Form inspection failed; check Playwright/browser setup."],
                manual_answers=manual_answers,
                artifacts=artifacts,
                run_id=run_id,
            )
            self.workspace.save_submission(record)
            self.workspace.upsert_application(application.model_copy(update={"status": "Needs Input", "pdf": bool(application.pdf), "notes": str(exc)}))
            self._emit_runtime_event(
                run_id=run_token,
                run_type="submission",
                event_type="submission.prepare.failed",
                message=f"Form inspection failed for {application.company} / {application.role}.",
                stage="prepare",
                phase="prepare",
                status="failed",
                job_id=application.job_id,
                application_id=application.id,
                submission_id=application.id,
                company=application.company,
                role=application.role,
                source=application.source,
                artifact_paths=artifacts,
                error=exc,
                trace_ref=trace_ref,
                state_updates={"latest_error": str(exc)},
            )
            return record
    
        question_answers: list[tuple[ApplicationQuestion, Any]] = []
        questions: list[SubmissionQuestion] = []
        missing_required: list[str] = []
        ungrounded: list[str] = []
        low_confidence: list[str] = []
        trace_payload = {
            "application_id": application.id,
            "job_id": application.job_id,
            "company": application.company,
            "role": application.role,
            "source": application.source,
            "questions": [],
        }
        try:
            with self._model_trace_context(
                run_id=run_token,
                run_type="submission",
                stage="prepare",
                phase="prepare",
                job_id=application.job_id,
                application_id=application.id,
                submission_id=application.id,
                company=application.company,
                role=application.role,
                source=application.source,
            ):
                for index, question in enumerate(extraction.questions):
                    question_id = self._question_id(question, index)
                    if question_id in manual_answers:
                        grounded = GroundedAnswer(question=question.prompt_text, question_type=question.question_type, answer=manual_answers[question_id], confidence=1.0, reason="manual_override", verification_status=VerificationStatus.VERIFIED)
                        verification_status = "verified"
                    else:
                        option_signature = "|".join(sorted(str(option).strip().lower() for option in question.options if str(option).strip()))
                        grounded = await grounding.answer_question(
                            question.prompt_text,
                            facts,
                            options=question.options,
                            normalized_key=question.normalized_key,
                            answer_memory=answer_memory,
                            memory_context={
                                "question_type": question.question_type.value,
                                "source_adapter": application.source,
                                "option_signature": option_signature,
                            },
                            allow_sensitive_fallback=bool(question.required),
                        )
                        verification_status = getattr(grounded.verification_status, "value", str(grounded.verification_status))
                    answer_record = SimpleNamespace(candidate_answer=grounded.answer, needs_user_input=grounded.needs_user_input, confidence=float(grounded.confidence or 0.0), confidence_reason=grounded.reason, verification_status=verification_status)
                    question_answers.append((question, answer_record))
                    companion_satisfied = self._artifact_companion_satisfied(question, artifacts)
                    needs_user_input = bool(question.required and grounded.needs_user_input and question.question_type.value != "file" and not companion_satisfied)
                    if question.required and question.question_type.value != "file" and not str(grounded.answer or "").strip() and not companion_satisfied:
                        missing_required.append(question.prompt_text)
                    if needs_user_input and question.required:
                        ungrounded.append(question.prompt_text)
                    verification_lowered = str(verification_status or '').strip().lower()
                    if str(grounded.answer or "").strip() and float(grounded.confidence or 0.0) < 0.5 and verification_lowered not in {"verified", "review_required"}:
                        low_confidence.append(question.prompt_text)
                    trace_payload["questions"].append({
                        "question_id": question_id,
                        "prompt_text": question.prompt_text,
                        "normalized_key": question.normalized_key,
                        "required": question.required,
                        "question_type": question.question_type.value,
                        "widget_type": question.widget_type,
                        "answer": grounded.answer,
                        "confidence": float(grounded.confidence or 0.0),
                        "reason": grounded.reason,
                        "needs_user_input": needs_user_input,
                        "verification_status": verification_status,
                        "manual_override": question_id in manual_answers,
                    })
                    questions.append(SubmissionQuestion(question_id=question_id, source_field_name=question.source_field_name, prompt_text=question.prompt_text, normalized_key=question.normalized_key, question_type=question.question_type.value, widget_type=question.widget_type, section=question.section, required=question.required, sensitive=question.sensitive, options=list(question.options), option_details=list(question.option_details), submission_binding=dict(question.submission_binding or {}), existing_answer=grounded.answer, confidence=float(grounded.confidence or 0.0), confidence_reason=grounded.reason, needs_user_input=needs_user_input, verification_status=verification_status))
            plan = adapter.bind_answers(posting, question_answers, artifacts)
        except Exception as exc:
            trace_payload["error"] = str(exc)
            trace_payload["artifacts"] = artifacts
            trace_ref = self._persist_trace_payload(run_token, category="submission-steps", name=f"prepare-{application.id}", payload=trace_payload)
            blocked_record = (existing or SubmissionRecord(
                application_id=application.id,
                job_id=application.job_id,
                company=application.company,
                role=application.role,
                source=application.source,
                apply_url=job.apply_url,
                manual_answers=manual_answers,
            )).model_copy(update={
                "status": "blocked",
                "event_status": "prepare_failed",
                "submit_ready": False,
                "warnings": self._dedupe_strings(list((existing.warnings if existing is not None else [])) + ["Question answering failed."]),
                "last_error": str(exc),
                "artifacts": artifacts,
                "manual_answers": manual_answers,
                "run_id": run_id,
                "updated_at": utcnow_iso(),
            })
            self.workspace.save_submission(blocked_record)
            self.workspace.upsert_application(application.model_copy(update={"status": "Needs Input", "pdf": bool(application.pdf), "notes": str(exc)}))
            self._emit_runtime_event(
                run_id=run_token,
                run_type="submission",
                event_type="submission.prepare.failed",
                message=f"Prepare failed for {application.company} / {application.role}.",
                stage="prepare",
                phase="prepare",
                status="failed",
                job_id=application.job_id,
                application_id=application.id,
                submission_id=application.id,
                company=application.company,
                role=application.role,
                source=application.source,
                artifact_paths=artifacts,
                error=exc,
                trace_ref=trace_ref,
                state_updates={"latest_error": str(exc)},
            )
            return blocked_record
    
        missing_required = self._dedupe_strings(missing_required + list(plan.missing_required_fields))
        ungrounded = self._dedupe_strings(ungrounded)
        low_confidence = self._dedupe_strings(low_confidence)
        submit_ready = not missing_required and not ungrounded and not low_confidence
        status = "ready_for_submit" if submit_ready else "needs_user_input"
        record = SubmissionRecord(
            application_id=application.id,
            job_id=application.job_id,
            company=application.company,
            role=application.role,
            source=application.source,
            apply_url=plan.application_url,
            status=status,
            event_status=status,
            submit_ready=submit_ready,
            questions=questions,
            missing_required_fields=missing_required,
            ungrounded_answers=ungrounded,
            low_confidence_answers=low_confidence,
            warnings=[],
            notes=list(plan.notes),
            artifacts=artifacts,
            manual_answers=manual_answers,
            plan=plan.model_dump(mode="json"),
            run_id=run_id,
        )
        self.workspace.save_submission(record)
        self.workspace.upsert_application(application.model_copy(update={"status": "Ready to Submit" if submit_ready else "Needs Input", "pdf": True}))
        trace_payload.update({
            "submit_ready": submit_ready,
            "missing_required_fields": missing_required,
            "ungrounded_answers": ungrounded,
            "low_confidence_answers": low_confidence,
            "artifacts": artifacts,
            "plan": record.plan,
        })
        trace_ref = self._persist_trace_payload(run_token, category="submission-steps", name=f"prepare-{application.id}", payload=trace_payload)
        self._emit_runtime_event(
            run_id=run_token,
            run_type="submission",
            event_type="submission.prepare.completed",
            message=(f"Submission is ready for {application.company} / {application.role}." if submit_ready else f"Submission needs user input for {application.company} / {application.role}."),
            stage="prepare",
            phase="prepare",
            status="completed" if submit_ready else "blocked",
            job_id=application.job_id,
            application_id=application.id,
            submission_id=application.id,
            company=application.company,
            role=application.role,
            source=application.source,
            artifact_paths=artifacts,
            trace_ref=trace_ref,
            metrics={"question_count": len(questions), "missing_required": len(missing_required), "ungrounded": len(ungrounded), "low_confidence": len(low_confidence), "submit_ready": submit_ready},
            payload={"missing_required_fields": missing_required, "ungrounded_answers": ungrounded, "low_confidence_answers": low_confidence},
        )
        return record
    
    @staticmethod
    def _email_verification_question_id() -> str:
        return "email_verification_code"

    def _with_email_verification_code_binding(self, plan: SubmissionPlan, record: SubmissionRecord) -> SubmissionPlan:
        _ = record
        # Email verification codes are one-time credentials. The submitter should
        # always fetch the current code from the inbox when the gate appears
        # instead of reusing anything previously typed by a human.
        return plan

    def _email_verification_required_message(self, result: Any) -> str | None:
        evidence = getattr(result, "evidence", None)
        failure_reason = str(getattr(evidence, "failure_reason", "") or "").strip().casefold()
        if failure_reason != "email_verification_required":
            return None
        return str(getattr(result, "message", "") or "").strip() or "Email verification code required"

    def _submission_requires_email_verification(
        self,
        *,
        record: SubmissionRecord,
        application: ApplicationEntry,
        result: Any,
        merged_artifacts: dict[str, str],
        run_id: str | None,
    ) -> SubmissionRecord:
        question_id = self._email_verification_question_id()
        prompt = "Enter the 8-character verification code sent to your email to continue submission."
        record, _ = self._clear_transient_submission_answer(record, question_id=question_id)
        existing_answer = None
        verification_question = SubmissionQuestion(
            question_id=question_id,
            source_field_name=question_id,
            prompt_text=prompt,
            normalized_key=question_id,
            question_type="text",
            widget_type="text",
            section="verification",
            required=True,
            sensitive=False,
            existing_answer=existing_answer,
            confidence=0.0,
            confidence_reason="email_verification_required",
            needs_user_input=True,
            verification_status="needs_user_input",
        )
        updated_questions = [item for item in record.questions if item.question_id != question_id]
        updated_questions.append(verification_question)
        remaining_missing = [item for item in record.missing_required_fields if slugify(item) not in {slugify(prompt), slugify(question_id)}]
        remaining_ungrounded = [item for item in record.ungrounded_answers if slugify(item) not in {slugify(prompt), slugify(question_id)}]
        remaining_low_confidence = [item for item in record.low_confidence_answers if slugify(item) not in {slugify(prompt), slugify(question_id)}]
        remaining_missing.append(prompt)
        message = self._email_verification_required_message(result) or "Email verification code required"
        warnings = self._dedupe_strings(list(record.warnings) + [message])
        result_payload = self._merge_result_payload(record, result.model_dump(mode="json"))
        updated = record.model_copy(
            update={
                "status": "needs_user_input",
                "event_status": "email_verification_required",
                "submit_ready": False,
                "preview_ready": False,
                "questions": updated_questions,
                "missing_required_fields": remaining_missing,
                "ungrounded_answers": remaining_ungrounded,
                "low_confidence_answers": remaining_low_confidence,
                "result": result_payload,
                "artifacts": merged_artifacts,
                "warnings": warnings,
                "last_error": message,
                "run_id": run_id,
                "updated_at": utcnow_iso(),
            }
        )
        self.workspace.save_submission(updated)
        self.workspace.upsert_application(application.model_copy(update={"status": "Needs Input", "pdf": True, "notes": message}))
        return updated

    def _confirm_submission_via_email(
        self,
        *,
        application: ApplicationEntry,
        result: Any,
        issued_after: datetime,
    ):
        if str(application.source or "").strip().casefold() != "greenhouse":
            return result
        evidence = getattr(result, "evidence", None)
        failure_reason = str(getattr(evidence, "failure_reason", "") or "").strip().casefold()
        if failure_reason == "email_verification_required":
            return result
        recipient = None
        submission = self.workspace.load_submission(application.id)
        if submission is not None:
            recipient = str((submission.manual_answers or {}).get("email") or "").strip() or None
            if not recipient:
                plan_fields: list[Any] = []
                if isinstance(submission.plan, SubmissionPlan):
                    plan_fields = list(submission.plan.fields)
                elif isinstance(submission.plan, dict):
                    plan_fields = list(submission.plan.get("fields") or [])
                for field in plan_fields:
                    field_name = str((field.source_field_name if hasattr(field, "source_field_name") else field.get("source_field_name")) or "").strip().casefold()
                    if field_name == "email":
                        value = field.value if hasattr(field, "value") else field.get("value")
                        recipient = str(value or "").strip() or None
                        if recipient:
                            break
        receipt = fetch_greenhouse_application_receipt(
            company=application.company,
            role=application.role,
            recipient=recipient,
            issued_after=issued_after,
            timeout_seconds=45,
        )
        if receipt is None:
            return result
        evidence_model = evidence.model_copy(deep=True) if isinstance(evidence, SubmissionEvidence) else SubmissionEvidence()
        markers = list(evidence_model.matched_confirmation_markers or [])
        if "email_receipt" not in markers:
            markers.append("email_receipt")
        evidence_model = evidence_model.model_copy(
            update={
                "confirmation_text": evidence_model.confirmation_text or receipt.subject or receipt.body_snippet,
                "confirmation_strategy": "email_receipt",
                "matched_confirmation_markers": markers,
            }
        )
        return result.model_copy(
            update={
                "status": JobLifecycleStatus.SUBMITTED,
                "submitted": True,
                "uncertain": False,
                "message": "Submitted",
                "evidence": evidence_model,
            }
        )

    async def _submit_application_async(self, application_id: str, run_id: str | None) -> SubmissionRecord:
        application = self.workspace.find_application(application_id)
        if application is None:
            raise ValueError(f"Unknown application: {application_id}")
        job = self.workspace.load_job(application.job_id)
        if job is None:
            raise ValueError(f"Unknown job: {application.job_id}")
        run_token = run_id or "manual"
        record = self.workspace.load_submission(application.id)
        if record is None or not record.plan:
            record = await self._prepare_submission_async(application_id, run_id)
        if not record.submit_ready:
            return record
        browser_blocker = self._browser_runtime_blocker()
        if browser_blocker is not None:
            warnings = self._dedupe_strings(list(record.warnings) + [str(browser_blocker["message"])])
            updated = record.model_copy(update={
                "status": "blocked",
                "event_status": "runtime_blocked",
                "submit_ready": False,
                "warnings": warnings,
                "last_error": str(browser_blocker["message"]),
                "run_id": run_id,
                "updated_at": utcnow_iso(),
            })
            self.workspace.save_submission(updated)
            self.workspace.upsert_application(application.model_copy(update={"pdf": True, "notes": str(browser_blocker["message"])}))
            trace_ref = self._persist_trace_payload(run_token, category="submission-steps", name=f"submit-{application.id}", payload={
                "application_id": application.id,
                "job_id": application.job_id,
                "company": application.company,
                "role": application.role,
                "source": application.source,
                "blocked": browser_blocker,
            })
            self._emit_runtime_event(
                run_id=run_token,
                run_type="submission",
                event_type="submission.submit.blocked",
                message=f"Submit blocked for {application.company} / {application.role}: {browser_blocker['message']}",
                stage="submit",
                phase="submit",
                status="blocked",
                job_id=application.job_id,
                application_id=application.id,
                submission_id=application.id,
                company=application.company,
                role=application.role,
                source=application.source,
                artifact_paths=updated.artifacts,
                error=browser_blocker,
                trace_ref=trace_ref,
                metrics={"blocked": 1},
            )
            return updated
        self._emit_runtime_event(
            run_id=run_token,
            run_type="submission",
            event_type="submission.submit.started",
            message=f"Submitting {application.company} / {application.role}.",
            stage="submit",
            phase="submit",
            status="running",
            job_id=application.job_id,
            application_id=application.id,
            submission_id=application.id,
            company=application.company,
            role=application.role,
            source=application.source,
            artifact_paths=record.artifacts,
        )
        posting = self._normalized_job(job)
        adapter = self._adapter_for_job(job)
        output_dir = self.workspace.output_dir / "submissions" / application.id
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            submit_started_at = datetime.now(timezone.utc) - timedelta(seconds=5)
            plan = SubmissionPlan.model_validate(dict(record.plan))
            plan = self._with_email_verification_code_binding(plan, record)
            result = await adapter.submit(posting, plan, output_dir)
            merged_artifacts = dict(record.artifacts)
            for key in ("snapshot_path", "trace_path"):
                value = getattr(result, key, None)
                if value:
                    merged_artifacts[key] = value
            evidence = getattr(result, "evidence", None)
            if evidence is not None:
                evidence_paths = {
                    "pre_submit_snapshot": evidence.pre_submit_snapshot_path,
                    "final_snapshot": evidence.final_snapshot_path,
                    "dom_snapshot": evidence.dom_snapshot_path,
                    "post_submit_dom_snapshot": evidence.post_submit_dom_snapshot_path,
                    "browser_trace": evidence.trace_path,
                }
                merged_artifacts.update({key: value for key, value in evidence_paths.items() if value})
            if self._email_verification_required_message(result) is not None:
                updated = self._submission_requires_email_verification(
                    record=record,
                    application=application,
                    result=result,
                    merged_artifacts=merged_artifacts,
                    run_id=run_id,
                )
                trace_ref = self._persist_trace_payload(run_token, category="submission-steps", name=f"submit-{application.id}", payload={
                    "application_id": application.id,
                    "job_id": application.job_id,
                    "company": application.company,
                    "role": application.role,
                    "source": application.source,
                    "result": result.model_dump(mode="json"),
                    "action_required": "email_verification_code",
                })
                self._emit_runtime_event(
                    run_id=run_token,
                    run_type="submission",
                    event_type="submission.submit.blocked",
                    message=f"Submit blocked for {application.company} / {application.role}: {updated.last_error}",
                    stage="submit",
                    phase="submit",
                    status="blocked",
                    job_id=application.job_id,
                    application_id=application.id,
                    submission_id=application.id,
                    company=application.company,
                    role=application.role,
                    source=application.source,
                    artifact_paths=merged_artifacts,
                    trace_ref=trace_ref,
                    payload={"failure_reason": "email_verification_required"},
                    metrics={"blocked": 1},
                )
                return updated
            result = self._confirm_submission_via_email(
                application=application,
                result=result,
                issued_after=submit_started_at,
            )
            status = "submitted" if result.submitted else ("submission_uncertain" if result.uncertain else "submission_failed")
            result_payload = self._merge_result_payload(record, result.model_dump(mode="json"))
            updated = record.model_copy(update={
                "status": status,
                "event_status": status,
                "submit_ready": False,
                "result": result_payload,
                "artifacts": merged_artifacts,
                "warnings": list(record.warnings),
                "last_error": None if result.submitted or result.uncertain else (result.message or record.last_error),
                "run_id": run_id,
                "submitted_at": utcnow_iso() if result.submitted else None,
                "updated_at": utcnow_iso(),
            })
            self.workspace.save_submission(updated)
            self.workspace.upsert_application(application.model_copy(update={"status": "Applied" if result.submitted else ("Submission Uncertain" if result.uncertain else "Submit Failed"), "pdf": True, "notes": result.message or application.notes}))
            self.workspace.update_inbox_state(application.job_id, "applied" if result.submitted else "pdf_ready")
            trace_ref = self._persist_trace_payload(run_token, category="submission-steps", name=f"submit-{application.id}", payload={
                "application_id": application.id,
                "job_id": application.job_id,
                "company": application.company,
                "role": application.role,
                "source": application.source,
                "result": result.model_dump(mode="json"),
            })
            self._emit_runtime_event(
                run_id=run_token,
                run_type="submission",
                event_type="submission.submit.completed",
                message=f"Submission finished for {application.company} / {application.role} with status {status}.",
                stage="submit",
                phase="submit",
                status="completed" if result.submitted else ("warning" if result.uncertain else "failed"),
                job_id=application.job_id,
                application_id=application.id,
                submission_id=application.id,
                company=application.company,
                role=application.role,
                source=application.source,
                artifact_paths=merged_artifacts,
                trace_ref=trace_ref,
                metrics={"submitted": bool(result.submitted), "uncertain": bool(result.uncertain)},
                payload={"submitted": result.submitted, "uncertain": result.uncertain, "external_id": result.external_id, "message": result.message},
            )
            return updated
        except Exception as exc:
            updated = record.model_copy(update={
                "status": "failed",
                "event_status": "failed",
                "last_error": str(exc),
                "submit_ready": False,
                "run_id": run_id,
                "updated_at": utcnow_iso(),
            })
            self.workspace.save_submission(updated)
            self.workspace.upsert_application(application.model_copy(update={"status": "Submit Failed", "pdf": True, "notes": str(exc)}))
            trace_ref = self._persist_trace_payload(run_token, category="submission-steps", name=f"submit-{application.id}", payload={
                "application_id": application.id,
                "job_id": application.job_id,
                "company": application.company,
                "role": application.role,
                "source": application.source,
                "error": str(exc),
                "artifacts": updated.artifacts,
            })
            self._emit_runtime_event(
                run_id=run_token,
                run_type="submission",
                event_type="submission.submit.failed",
                message=f"Submission failed for {application.company} / {application.role}.",
                stage="submit",
                phase="submit",
                status="failed",
                job_id=application.job_id,
                application_id=application.id,
                submission_id=application.id,
                company=application.company,
                role=application.role,
                source=application.source,
                artifact_paths=updated.artifacts,
                error=exc,
                trace_ref=trace_ref,
                state_updates={"latest_error": str(exc)},
            )
            return updated
    
    
    async def _preview_application_async(self, application_id: str, run_id: str | None) -> SubmissionRecord:
        application = self.workspace.find_application(application_id)
        if application is None:
            raise ValueError(f"Unknown application: {application_id}")
        job = self.workspace.load_job(application.job_id)
        if job is None:
            raise ValueError(f"Unknown job: {application.job_id}")
        run_token = run_id or "manual"
        record = self.workspace.load_submission(application.id)
        if record is None or not record.plan:
            record = await self._prepare_submission_async(application_id, run_id)
        if not record.submit_ready:
            return record
        browser_blocker = self._browser_runtime_blocker()
        if browser_blocker is not None:
            warnings = self._dedupe_strings(list(record.warnings) + [str(browser_blocker["message"])])
            updated = record.model_copy(update={
                "status": "blocked",
                "event_status": "runtime_blocked",
                "submit_ready": False,
                "preview_ready": False,
                "warnings": warnings,
                "last_error": str(browser_blocker["message"]),
                "run_id": run_id,
                "updated_at": utcnow_iso(),
            })
            self.workspace.save_submission(updated)
            self.workspace.upsert_application(application.model_copy(update={"pdf": True, "notes": str(browser_blocker["message"])}))
            trace_ref = self._persist_trace_payload(run_token, category="submission-steps", name=f"preview-{application.id}", payload={
                "application_id": application.id,
                "job_id": application.job_id,
                "company": application.company,
                "role": application.role,
                "source": application.source,
                "blocked": browser_blocker,
            })
            self._emit_runtime_event(
                run_id=run_token,
                run_type="submission",
                event_type="submission.preview.blocked",
                message=f"Preview blocked for {application.company} / {application.role}: {browser_blocker['message']}",
                stage="preview",
                phase="preview",
                status="blocked",
                job_id=application.job_id,
                application_id=application.id,
                submission_id=application.id,
                company=application.company,
                role=application.role,
                source=application.source,
                artifact_paths=updated.artifacts,
                error=browser_blocker,
                trace_ref=trace_ref,
                metrics={"blocked": 1},
            )
            return updated
        adapter = self._adapter_for_job(job)
        if not hasattr(adapter, 'preview_submission'):
            message = f"Preview submission is not supported for source: {job.source}"
            warnings = self._dedupe_strings(list(record.warnings) + [message])
            updated = record.model_copy(update={
                "status": "blocked",
                "event_status": "preview_unsupported",
                "submit_ready": False,
                "preview_ready": False,
                "warnings": warnings,
                "last_error": message,
                "run_id": run_id,
                "updated_at": utcnow_iso(),
            })
            self.workspace.save_submission(updated)
            self.workspace.upsert_application(application.model_copy(update={"pdf": True, "notes": message}))
            trace_ref = self._persist_trace_payload(run_token, category="submission-steps", name=f"preview-{application.id}", payload={
                "application_id": application.id,
                "job_id": application.job_id,
                "company": application.company,
                "role": application.role,
                "source": application.source,
                "error": message,
            })
            self._emit_runtime_event(
                run_id=run_token,
                run_type="submission",
                event_type="submission.preview.blocked",
                message=message,
                stage="preview",
                phase="preview",
                status="blocked",
                job_id=application.job_id,
                application_id=application.id,
                submission_id=application.id,
                company=application.company,
                role=application.role,
                source=application.source,
                artifact_paths=updated.artifacts,
                error={"message": message},
                trace_ref=trace_ref,
            )
            return updated
        self._emit_runtime_event(
            run_id=run_token,
            run_type="submission",
            event_type="submission.preview.started",
            message=f"Previewing {application.company} / {application.role}.",
            stage="preview",
            phase="preview",
            status="running",
            job_id=application.job_id,
            application_id=application.id,
            submission_id=application.id,
            company=application.company,
            role=application.role,
            source=application.source,
            artifact_paths=record.artifacts,
        )
        posting = self._normalized_job(job)
        output_dir = self.workspace.output_dir / 'submissions' / application.id / 'preview'
        output_dir.mkdir(parents=True, exist_ok=True)
        from findmyjob.core.types import SubmissionPlan
    
        try:
            plan = SubmissionPlan.model_validate(dict(record.plan))
            result = await adapter.preview_submission(posting, plan, output_dir)
            preview_issue = self._preview_failure_reason(result)
            merged_artifacts = dict(record.artifacts)
            evidence = getattr(result, 'evidence', None) if result is not None else None
            if evidence is not None:
                preview_paths = {
                    'preview_pre_submit_snapshot': evidence.pre_submit_snapshot_path,
                    'preview_final_snapshot': evidence.final_snapshot_path,
                    'preview_dom_snapshot': evidence.dom_snapshot_path,
                    'preview_post_submit_dom_snapshot': evidence.post_submit_dom_snapshot_path,
                    'preview_trace': evidence.trace_path,
                }
                merged_artifacts.update({key: value for key, value in preview_paths.items() if value})
            status = 'preview_ready' if preview_issue is None and result.status == JobLifecycleStatus.READY_FOR_REVIEW else 'preview_failed'
            warnings = list(record.warnings)
            if preview_issue is not None and preview_issue not in warnings:
                warnings.append(preview_issue)
            result_payload = self._merge_result_payload(record, result.model_dump(mode='json'))
            updated = record.model_copy(update={
                'status': status,
                'event_status': status,
                'submit_ready': False,
                'preview_ready': status == 'preview_ready',
                'result': result_payload,
                'artifacts': merged_artifacts,
                'warnings': warnings,
                'last_error': None if status == 'preview_ready' else preview_issue,
                'run_id': run_id,
                'previewed_at': utcnow_iso(),
                'updated_at': utcnow_iso(),
            })
            self.workspace.save_submission(updated)
            app_status = 'Preview Ready' if status == 'preview_ready' else 'Preview Failed'
            self.workspace.upsert_application(application.model_copy(update={'status': app_status, 'pdf': True, 'notes': preview_issue or application.notes}))
            self.workspace.update_inbox_state(application.job_id, 'preview_ready' if status == 'preview_ready' else 'pdf_ready')
            trace_ref = self._persist_trace_payload(run_token, category='submission-steps', name=f'preview-{application.id}', payload={
                'application_id': application.id,
                'job_id': application.job_id,
                'company': application.company,
                'role': application.role,
                'source': application.source,
                'preview_issue': preview_issue,
                'result': result.model_dump(mode='json'),
            })
            self._emit_runtime_event(
                run_id=run_token,
                run_type='submission',
                event_type='submission.preview.completed',
                message=f"Preview finished for {application.company} / {application.role} with status {status}.",
                stage='preview',
                phase='preview',
                status='completed' if status == 'preview_ready' else 'failed',
                job_id=application.job_id,
                application_id=application.id,
                submission_id=application.id,
                company=application.company,
                role=application.role,
                source=application.source,
                artifact_paths=merged_artifacts,
                trace_ref=trace_ref,
                payload={'preview_issue': preview_issue},
                metrics={'preview_ready': status == 'preview_ready'},
            )
            return updated
        except Exception as exc:
            updated = record.model_copy(update={
                'status': 'preview_failed',
                'event_status': 'preview_failed',
                'submit_ready': False,
                'preview_ready': False,
                'last_error': str(exc),
                'run_id': run_id,
                'updated_at': utcnow_iso(),
            })
            self.workspace.save_submission(updated)
            self.workspace.upsert_application(application.model_copy(update={'status': 'Preview Failed', 'pdf': True, 'notes': str(exc)}))
            trace_ref = self._persist_trace_payload(run_token, category='submission-steps', name=f'preview-{application.id}', payload={
                'application_id': application.id,
                'job_id': application.job_id,
                'company': application.company,
                'role': application.role,
                'source': application.source,
                'error': str(exc),
                'artifacts': updated.artifacts,
            })
            self._emit_runtime_event(
                run_id=run_token,
                run_type='submission',
                event_type='submission.preview.failed',
                message=f"Preview failed for {application.company} / {application.role}.",
                stage='preview',
                phase='preview',
                status='failed',
                job_id=application.job_id,
                application_id=application.id,
                submission_id=application.id,
                company=application.company,
                role=application.role,
                source=application.source,
                artifact_paths=updated.artifacts,
                error=exc,
                trace_ref=trace_ref,
                state_updates={'latest_error': str(exc)},
            )
            return updated
    
    def _preview_failure_reason(self, result) -> str | None:
        if result is None:
            return 'Preview result is None; browser preview failed.'
        evidence = getattr(result, 'evidence', None)
        if getattr(result, 'status', None) != JobLifecycleStatus.READY_FOR_REVIEW:
            return getattr(result, 'message', None) or 'Browser preview did not reach the pre-submit ready state.'
        if evidence is None:
            return 'Browser preview did not capture evidence.'
        if evidence.failure_reason:
            return evidence.failure_reason
        for audit in evidence.field_audit:
            if audit.get('required') and audit.get('status') in {'missing', 'error'}:
                return f"Required browser binding failed: {audit.get('prompt') or audit.get('field') or 'unknown field'}"
        return None

    def _launch_artifact_issues(self, job_id: str, evaluation: dict[str, Any], pdf_result: dict[str, Any]) -> list[str]:
        issues: list[str] = []
        report_path = evaluation.get('report_path')
        if report_path:
            report_candidate = (self.workspace.root / report_path).resolve()
            if self._artifact_contains_redaction(report_candidate):
                issues.append('Generated report still contains redaction placeholders.')
        if pdf_result.get('render_error'):
            issues.append(f"PDF render failed: {pdf_result['render_error']}")
        if pdf_result.get('renderer') == 'latex' and not pdf_result.get('template_bridge_used'):
            issues.append('Launch rehearsal did not use the configured resume template bridge.')
        for key, label in (
            ('resume_text_path', 'resume'),
            ('cover_letter_text_path', 'cover letter'),
        ):
            candidate = pdf_result.get(key)
            if candidate and self._artifact_contains_redaction((self.workspace.root / candidate).resolve()):
                issues.append(f'Generated {label} contains redaction placeholders.')
        return issues

    def _artifact_contains_redaction(self, path: Path | None) -> bool:
        if path is None or not path.exists() or not path.is_file():
            return False
        if path.suffix.lower() not in {'.md', '.txt', '.html', '.json'}:
            return False
        return '[redacted-' in path.read_text(encoding='utf-8', errors='ignore').casefold()

    def _artifact_map(self, application: ApplicationEntry) -> dict[str, str]:
        artifacts: dict[str, str] = {}
        job = self.workspace.load_job(application.job_id)
        company_slug = application.company.casefold().replace(' ', '-')
        source_job_id = str(getattr(job, 'source_job_id', '') or '')
        pdf_path = self.workspace.resume_pdf_path_for(application.id, application.company, application.date)
        if pdf_path.exists():
            artifacts["resume_pdf"] = str(
                self._upload_artifact_alias(
                    pdf_path,
                    kind='resume',
                    company=application.company,
                    application_id=application.id,
                )
            )
        html_path = self.workspace.resume_html_path_for(application.id, application.company, application.date)
        resume_text_candidate = self._latest_output_artifact('.resume.txt', company_slug=company_slug, source_job_id=source_job_id)
        if html_path.exists():
            artifacts["resume_text"] = str(html_path)
        elif resume_text_candidate is not None and resume_text_candidate.exists():
            artifacts["resume_text"] = str(resume_text_candidate)
        else:
            artifacts["resume_text"] = str(self.workspace.cv_path)
        cover_letter_md = self.workspace.output_dir / f"cover-letter-{application.id}-{application.company.casefold().replace(' ', '-')}.md"
        cover_letter_pdf = self.workspace.output_dir / f"cover-letter-{application.id}-{application.company.casefold().replace(' ', '-')}.pdf"
        cover_letter_txt = self._latest_output_artifact('.cover_letter.txt', company_slug=company_slug, source_job_id=source_job_id)
        if cover_letter_md.exists():
            artifacts["cover_letter_text"] = str(cover_letter_md)
        elif cover_letter_txt is not None and cover_letter_txt.exists():
            artifacts["cover_letter_text"] = str(cover_letter_txt)
        if cover_letter_pdf.exists():
            artifacts["cover_letter_pdf"] = str(
                self._upload_artifact_alias(
                    cover_letter_pdf,
                    kind='cover_letter',
                    company=application.company,
                    application_id=application.id,
                )
            )
        return artifacts

    def _upload_artifact_alias(self, source_path: Path, *, kind: str, company: str, application_id: str) -> Path:
        if not source_path.exists():
            return source_path
        candidate_name = str(getattr(self.workspace.load_profile().candidate, 'name', '') or 'Candidate').strip()
        name_token = re.sub(r'[^A-Za-z0-9]+', '_', candidate_name).strip('_') or 'Candidate'
        company_token = re.sub(r'[^A-Za-z0-9]+', '_', str(company or '')).strip('_') or 'Company'
        application_token = re.sub(r'[^A-Za-z0-9]+', '_', str(application_id or '')).strip('_') or 'application'
        kind_token = 'Resume' if kind == 'resume' else 'Cover_Letter'
        target = self.workspace.output_dir / f"{name_token}_{kind_token}_{company_token}_{application_token}{source_path.suffix}"
        if source_path.resolve() != target.resolve():
            shutil.copyfile(source_path, target)
        return target


    def _profile_facts(self) -> list[ProfileFact]:
        facts: list[ProfileFact] = []
        for item in self.workspace.load_facts():
            try:
                kind = FactKind(item.kind)
            except ValueError:
                kind = FactKind.PERSONAL
            try:
                sensitivity = Sensitivity(item.sensitivity)
            except ValueError:
                sensitivity = Sensitivity.MEDIUM
            facts.append(ProfileFact(fact_id=item.fact_id, kind=kind, payload=item.payload, sensitivity=sensitivity, allowed_for_generation=item.allowed_for_generation, disallowed=item.disallowed, provenance=item.provenance, confirmed=item.confirmed))
        return facts

    def _candidate_location_payload(self) -> dict[str, Any]:
        profile = self.workspace.load_profile()
        location_text = str(profile.candidate.location or "").strip()
        if not location_text:
            return {}
        structured = parse_structured_location(location_text)
        payload: dict[str, Any] = {"display": location_text}
        city = str(structured.city or "").strip()
        if city:
            payload["city"] = city
        region_code = normalize_region_code(structured.region_code)
        if region_code:
            payload["region_code"] = region_code
            region_name = _REGION_CODE_TO_NAME.get(region_code)
            if region_name:
                payload["region"] = region_name
        country_code = normalize_country_code(structured.country_code)
        if country_code:
            payload["country_code"] = country_code
        return payload

    def _merge_runtime_location_fact(self, facts: list[ProfileFact]) -> list[ProfileFact]:
        runtime_payload = self._candidate_location_payload()
        if not runtime_payload:
            return facts
        for index, fact in enumerate(facts):
            if fact.kind != FactKind.LOCATION:
                continue
            merged = dict(runtime_payload)
            merged.update(dict(fact.payload or {}))
            region_code = normalize_region_code(merged.get("region_code") or merged.get("region"))
            if region_code:
                merged["region_code"] = region_code
                merged.setdefault("region", _REGION_CODE_TO_NAME.get(region_code))
            country_code = normalize_country_code(merged.get("country_code"))
            if country_code:
                merged["country_code"] = country_code
            facts[index] = fact.model_copy(update={"payload": merged})
            return facts
        facts.append(
            ProfileFact(
                fact_id="runtime.profile.location",
                kind=FactKind.LOCATION,
                payload=runtime_payload,
                sensitivity=Sensitivity.MEDIUM,
                allowed_for_generation=True,
                disallowed=False,
                provenance="runtime:profile_candidate_location",
                confirmed=True,
            )
        )
        return facts

    def _grounding_facts_for_application(self, application: Any, job: Any) -> list[ProfileFact]:
        facts = self._merge_runtime_location_fact(self._profile_facts())
        evaluation = self.workspace.load_evaluation(application.job_id)
        summary_parts = [
            f"Target application: {job.title} at {job.company}.",
            f"Location: {job.location}." if str(job.location or "").strip() else "",
        ]
        bullets: list[str] = []
        if evaluation is not None:
            if str(evaluation.summary or "").strip():
                summary_parts.append(str(evaluation.summary).strip())
            bullets.extend(str(item).strip() for item in list(evaluation.fit_reasons or [])[:4] if str(item).strip())
            bullets.extend(
                f"Resume strategy: {str(item).strip()}"
                for item in list(getattr(evaluation, "custom_bullets", []) or [])[:2]
                if str(item).strip()
            )
        facts.append(
            ProfileFact(
                fact_id=f"runtime.application.{application.id}.job-context",
                kind=FactKind.PERSONAL,
                payload={
                    "company": str(job.company or "").strip(),
                    "title": f"{str(job.title or '').strip()} at {str(job.company or '').strip()}".strip(),
                    "summary": " ".join(part for part in summary_parts if part).strip(),
                    "bullets": bullets,
                    "description": str(job.description or "").strip()[:2000],
                },
                sensitivity=Sensitivity.LOW,
                allowed_for_generation=True,
                disallowed=False,
                provenance="runtime:application_context",
                confirmed=True,
            )
        )
        return facts

    def _grounding_service(self) -> GroundingService:
        return GroundingService(router=load_model_router(self.workspace))

    def _normalized_job(self, job) -> Any:
        note_payload = dict(job.notes or {})
        profile = self.workspace.load_profile()
        app_config = AppConfig.load(self.workspace.root)
        captcha_settings = self._captcha_runtime_settings(
            configured_strategy=app_config.captcha.strategy,
            provider=app_config.captcha.provider,
            api_key_env=app_config.captcha.api_key_env,
            solve_timeout_seconds=app_config.captcha.solve_timeout_seconds,
            browser_mode=profile.runtime.automation.browser_mode,
        )
        note_payload.update(
            {
                "browser_mode": profile.runtime.automation.browser_mode,
                "browser_attach_enabled": profile.runtime.automation.browser_attach_enabled,
                "browser_cdp_url": profile.runtime.automation.browser_cdp_url,
                "max_open_tabs": profile.runtime.automation.max_open_tabs,
                "captcha_strategy": captcha_settings["captcha_strategy_effective"],
                "captcha_provider": captcha_settings["captcha_provider"],
                "captcha_api_key_env": captcha_settings["captcha_api_key_env"],
                "captcha_solve_timeout": captcha_settings["captcha_solve_timeout_seconds"],
            }
        )
        note_payload["board"] = note_payload.get("board") or self._board_from_url(job.source, job.apply_url or job.url) or job.company_key or slugify(job.company)
        return build_normalized_job(company_name=job.company, title=job.title, source=job.source, source_kind=job.source_kind, source_job_id=job.source_job_id, posting_url=job.url, apply_url=job.apply_url or job.url, location_raw=job.location, employment_type=None, compensation=None, description=job.description or "", posted_at=self._parse_dt(job.posted_at), notes=note_payload)

    @staticmethod
    def _captcha_runtime_settings(
        *,
        configured_strategy: str | None,
        provider: str | None,
        api_key_env: str | None,
        solve_timeout_seconds: int | None,
        browser_mode: str | None,
    ) -> dict[str, Any]:
        configured = str(configured_strategy or "skip").strip().lower() or "skip"
        effective = configured
        if effective == "skip" and str(browser_mode or "").strip().lower() == "headed":
            effective = "manual"
        return {
            "captcha_strategy": configured,
            "captcha_strategy_effective": effective,
            "captcha_provider": str(provider or "2captcha").strip() or "2captcha",
            "captcha_api_key_env": str(api_key_env or "CAPTCHA_API_KEY").strip() or "CAPTCHA_API_KEY",
            "captcha_solve_timeout_seconds": int(solve_timeout_seconds or 300),
        }

    def _chatgpt_parallel_jobs(self) -> int:
        app_config = AppConfig.load(self.workspace.root)
        if str(app_config.personal.resume_renderer or "").strip() != "chatgpt_download":
            return 1
        return max(1, int(app_config.chatgpt_drafting.max_parallel_jobs or 1))

    def _draft_single_job(self, *, run_id: str, run_type: str, screened_job: Any, evaluation: dict[str, Any]) -> dict[str, Any]:
        self._emit_runtime_event(
            run_id=run_id,
            run_type=run_type,
            event_type=f"{run_type}.drafting.started",
            message=f'Building artifacts for {evaluation.get("company") or screened_job.company} / {evaluation.get("role") or screened_job.title}.',
            stage="drafting",
            phase="draft",
            status="running",
            job_id=evaluation.get("job_id") or screened_job.job_id,
            application_id=evaluation.get("application_id"),
            company=evaluation.get("company") or screened_job.company,
            role=evaluation.get("role") or screened_job.title,
            source=screened_job.source,
        )
        with self._model_trace_context(
            run_id=run_id,
            run_type=run_type,
            stage="drafting",
            phase="draft",
            job_id=evaluation.get("job_id") or screened_job.job_id,
            application_id=evaluation.get("application_id"),
            company=evaluation.get("company") or screened_job.company,
            role=evaluation.get("role") or screened_job.title,
            source=screened_job.source,
        ):
            return build_pdf_for_target(self.workspace.root, screened_job.job_id)

    def _adapter_for_job(self, job):
        board = self._board_from_url(job.source, job.apply_url or job.url) or job.company_key or slugify(job.company)
        if job.source == "greenhouse":
            return GreenhouseAdapter([board])
        if job.source == "lever":
            return LeverAdapter([board])
        if job.source == "ashby":
            return AshbyAdapter([board])
        raise ValueError(f"Unsupported source: {job.source}")

    def _artifact_companion_kind(self, question: ApplicationQuestion) -> str | None:
        source_field_name = str(question.source_field_name or '')
        widget_type = str(getattr(question, 'widget_type', '') or '')
        if widget_type != 'textarea' and not source_field_name.endswith('_text'):
            return None
        lowered = f"{question.prompt_text} {source_field_name} {question.normalized_key or ''}".lower()
        if 'resume' in lowered or source_field_name == 'resume_text':
            return 'resume'
        if 'cover' in lowered:
            return 'cover_letter'
        return None

    def _artifact_companion_satisfied(self, question: ApplicationQuestion, artifacts: dict[str, str]) -> bool:
        kind = self._artifact_companion_kind(question)
        if kind == 'resume':
            return bool(artifacts.get('resume_pdf') or artifacts.get('resume_text'))
        if kind == 'cover_letter':
            return bool(artifacts.get('cover_letter_pdf') or artifacts.get('cover_letter_text'))
        return False

    def _latest_output_artifact(self, suffix: str, *, company_slug: str, source_job_id: str | None = None) -> Path | None:
        candidates: list[tuple[int, float, Path]] = []
        for path in self.workspace.output_dir.iterdir():
            if not path.is_file() or not path.name.endswith(suffix):
                continue
            lowered = path.name.casefold()
            score = 0
            if company_slug and company_slug in lowered:
                score += 2
            elif company_slug:
                continue
            if source_job_id and source_job_id.casefold() in lowered:
                score += 3
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            candidates.append((score, mtime, path))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return candidates[0][2]

    def _submission_blockers(self, submission: SubmissionRecord | None) -> list[dict[str, str]]:
        if submission is None:
            return []
        blockers = [{"category": "missing_required_field", "label": item} for item in submission.missing_required_fields]
        blockers += [{"category": "ungrounded_answer", "label": item} for item in submission.ungrounded_answers]
        blockers += [{"category": "low_confidence_answer", "label": item} for item in submission.low_confidence_answers]
        blockers += [{"category": "warning", "label": item} for item in submission.warnings]
        if submission.last_error:
            blockers.append({"category": "runtime_error", "label": submission.last_error})
        return blockers

    @staticmethod
    def _append_note(existing: str | None, note: str) -> str:
        lines = [line.strip() for line in str(existing or "").splitlines() if line.strip()]
        cleaned = str(note or "").strip()
        if cleaned and cleaned not in lines:
            lines.append(cleaned)
        return "\n".join(lines)

    def _mark_application_submitted_manually(
        self,
        application: ApplicationEntry,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        submitted_at = utcnow_iso()
        note = str(reason or "").strip() or "Manually confirmed as submitted from the review console."
        existing = self.workspace.load_submission(application.id)
        job = self.workspace.load_job(application.job_id)
        base_record = existing or SubmissionRecord(
            application_id=application.id,
            job_id=application.job_id,
            company=application.company,
            role=application.role,
            source=application.source,
            apply_url=(getattr(job, "apply_url", None) or application.url),
        )
        result_payload = dict(base_record.result or {})
        result_payload.update(
            {
                "submitted": True,
                "uncertain": False,
                "manual_confirmation": True,
                "message": note,
                "submitted_at": submitted_at,
            }
        )
        updated_record = base_record.model_copy(
            update={
                "status": "submitted",
                "event_status": "submitted",
                "submit_ready": False,
                "preview_ready": False,
                "missing_required_fields": [],
                "ungrounded_answers": [],
                "low_confidence_answers": [],
                "result": result_payload,
                "notes": self._dedupe_strings(list(base_record.notes) + [note]),
                "last_error": None,
                "reviewed": True,
                "run_id": "manual-review",
                "submitted_at": base_record.submitted_at or submitted_at,
                "updated_at": utcnow_iso(),
            }
        )
        updated_record = self._append_review_history_event_to_record(
            updated_record,
            event_type="review.action.mark_submitted",
            summary="Recorded a manual submission from the review queue.",
            actor="operator",
            metadata={"reason": note, "submitted_at": updated_record.submitted_at or submitted_at},
        )
        self.workspace.save_submission(updated_record)
        self.workspace.upsert_application(
            application.model_copy(
                update={
                    "status": "Applied",
                    "pdf": True,
                    "notes": self._append_note(application.notes, note),
                }
            )
        )
        self.workspace.update_inbox_state(application.job_id, "applied")
        self._remember_submission(application.id, updated_record.submitted_at or submitted_at)

        run_id = self._new_run_id("manual-review")
        trace_ref = self._persist_trace_payload(
            run_id,
            category="submission-steps",
            name=f"manual-submit-{application.id}",
            payload={
                "application_id": application.id,
                "job_id": application.job_id,
                "company": application.company,
                "role": application.role,
                "source": application.source,
                "manual_confirmation": True,
                "submitted_at": updated_record.submitted_at or submitted_at,
                "notes": updated_record.notes,
            },
        )
        self._emit_runtime_event(
            run_id=run_id,
            run_type="review",
            event_type="review.manual_submit.recorded",
            message=f"Recorded manual submission for {application.company} / {application.role}.",
            stage="review",
            phase="review",
            status="completed",
            job_id=application.job_id,
            application_id=application.id,
            submission_id=application.id,
            company=application.company,
            role=application.role,
            source=application.source,
            artifact_paths=updated_record.artifacts,
            trace_ref=trace_ref,
            metrics={"submitted": True, "manual_confirmation": True},
            payload={"manual_confirmation": True, "submitted_at": updated_record.submitted_at or submitted_at},
        )
        return {
            "application_id": application.id,
            "status": updated_record.status,
            "blocked": False,
            "manual_submitted": True,
            "submitted_at": updated_record.submitted_at or submitted_at,
            "remaining_blockers": [],
        }

    def _is_active_submission(self, submission: SubmissionRecord | None) -> bool:
        if submission is None:
            return False
        return str(submission.status or "").strip().lower() not in _TERMINAL_SUBMISSION_STATUSES

    def _question_id(self, question: ApplicationQuestion, index: int) -> str:
        base = question.normalized_key or question.source_field_name or f"question-{index + 1}"
        return slugify(base) or f"question-{index + 1}"

    def _board_from_url(self, source: str, url: str | None) -> str | None:
        value = str(url or "").strip().lower()
        if not value:
            return None
        if source == "greenhouse" and ".greenhouse.io/" in value:
            return value.split(".greenhouse.io/", 1)[1].split("/", 1)[0]
        if source == "lever" and "jobs.lever.co/" in value:
            return value.split("jobs.lever.co/", 1)[1].split("/", 1)[0]
        if source == "ashby" and "jobs.ashbyhq.com/" in value:
            return value.split("jobs.ashbyhq.com/", 1)[1].split("/", 1)[0]
        return None

    def _new_run_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:8]}"

    def _parse_dt(self, value: str | None) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _dedupe_strings(self, values: list[str]) -> list[str]:
        unique: list[str] = []
        for value in values:
            cleaned = str(value or "").strip()
            if cleaned and cleaned not in unique:
                unique.append(cleaned)
        return unique


__all__ = ["FileFirstOperatorService"]














