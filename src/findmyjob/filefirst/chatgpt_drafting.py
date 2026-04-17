from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anyio

from findmyjob.core.async_compat import run_async
from findmyjob.apply.browser_session import launch_attachable_browser
from findmyjob.core.config import AppConfig
from findmyjob.filefirst.workspace import FileWorkspace
from findmyjob.filefirst.text_utils import strip_html_tags
from findmyjob.sources.normalizer import normalize_text, slugify

_PROMPT_SELECTORS = (
    "#prompt-textarea",
    "div.ProseMirror[contenteditable='true']",
    "[role='textbox'][contenteditable='true']",
    "div[contenteditable='true']",
    "textarea[data-testid='prompt-textarea']",
    "textarea",
)
_SEND_BUTTON_SELECTORS = (
    "button[data-testid='send-button']",
    "button[aria-label*='Send']",
    "button[aria-label*='send']",
    "button[class*='send']",
)
_CHATGPT_HOME_URL = "https://chatgpt.com"
_TEMP_CHAT_ENABLE_SELECTORS = (
    "button[aria-label='Turn on temporary chat']",
    "button[aria-label='Temporary chat']",
    "button[aria-label*='temporary chat']",
)
_TEMP_CHAT_ACTIVE_SELECTORS = (
    "button[aria-label='Turn off temporary chat']",
    "button[aria-label*='temporary chat'][aria-pressed='true']",
)
_ASSISTANT_TURN_SELECTORS = (
    "[data-message-author-role='assistant']",
    "article",
)
_RETRY_LINKS_PROMPT = (
    "The download links were not generated. "
    "Please output ONLY the markers and download links exactly like normal — "
    "nothing else, no explanation. Just:\n"
    "[[PDF_OUTPUT_READY]]\n"
    "resume PDF link\n"
    "cover letter PDF link\n"
    "[[PDF_OUTPUT_COMPLETE]]"
)
_RECOVER_STALLED_PROMPT = (
    "You stopped before completing the requested PDF generation. "
    "Continue from where you left off and finish the task now. "
    "Return the normal final output with the exact markers and both PDF download links:\n"
    "[[PDF_OUTPUT_READY]]\n"
    "resume PDF link\n"
    "cover letter PDF link\n"
    "[[PDF_OUTPUT_COMPLETE]]"
)
_RETRY_ATTACHMENTS_PROMPT = (
    "The previous response did not provide usable downloadable PDF attachments. "
    "Return BOTH files again as actual downloadable PDF attachments or buttons, "
    "not sandbox:/mnt/data paths, not /mnt/data paths, not 'Failed to get upload status' messages, "
    "and not plain text filenames. "
    "Please output ONLY the markers and the two downloadable PDF attachments exactly like normal - "
    "nothing else, no explanation. Just:\n"
    "[[PDF_OUTPUT_READY]]\n"
    "resume PDF attachment\n"
    "cover letter PDF attachment\n"
    "[[PDF_OUTPUT_COMPLETE]]"
)
_RECOVER_ATTACHMENTS_PROMPT = (
    "The previous response did not provide usable downloadable PDF attachments for automation. "
    "Re-send BOTH files in this message as actual downloadable PDF attachments or buttons. "
    "Do NOT output sandbox:/mnt/data paths. "
    "Do NOT output /mnt/data paths. "
    "Do NOT output 'Failed to get upload status' messages. "
    "Do NOT output plain text file names. "
    "The two items must be real clickable file attachments that trigger browser downloads. "
    "Do NOT add explanations. "
    "Return ONLY:\n"
    "[[PDF_OUTPUT_READY]]\n"
    "resume PDF attachment\n"
    "cover letter PDF attachment\n"
    "[[PDF_OUTPUT_COMPLETE]]"
)
_ATTACHMENT_OUTPUT_CONTRACT = (
    "Return only the final completion markers and two real downloadable PDF attachments.\n"
    "Do not output the resume or cover letter as plain text.\n"
    "Do not output LaTeX or source code.\n"
    "Do not output sandbox:/mnt/data paths.\n"
    "Do not output /mnt/data paths.\n"
    "Do not output 'Failed to get upload status' messages.\n"
    "Do not output plain text file names.\n"
    "The two items between the markers must be real clickable PDF attachments or buttons that trigger browser downloads.\n"
    "The first file must be the resume PDF.\n"
    "The second file must be the cover letter PDF.\n"
    "Return exactly:\n"
    "[[PDF_OUTPUT_READY]]\n"
    "resume PDF attachment\n"
    "cover letter PDF attachment\n"
    "[[PDF_OUTPUT_COMPLETE]]"
)

_STATUS_UNSET = object()
_DRAFT_BATCH_TERMINAL_STATUSES = {"drafted", "reused", "failed"}


@dataclass(slots=True)
class ChatGPTDownloadedArtifacts:
    resume_raw_path: Path
    cover_letter_raw_path: Path


async def _sleep_ms(delay_ms: int | float) -> None:
    await anyio.sleep(max(float(delay_ms), 0.0) / 1000.0)


def local_date_string() -> str:
    try:
        now = datetime.now(ZoneInfo("America/Chicago"))
    except ZoneInfoNotFoundError:
        now = datetime.now().astimezone()
    return f"{now.strftime('%B')} {now.day}, {now.year}"


def build_chatgpt_prompt(*, company: str, role: str, job_description: str) -> str:
    return (
        f"Current local date: {local_date_string()}\n"
        f"Company: {company.strip()}\n"
        f"Role: {role.strip()}\n"
        "Job Description:\n"
        f"{normalize_text(strip_html_tags(job_description or '')).strip()}\n"
        "\n"
        "Output contract:\n"
        f"{_ATTACHMENT_OUTPUT_CONTRACT}\n"
    ).strip()


def extract_marked_block(text: str, *, start_marker: str, end_marker: str) -> str:
    body = str(text or "")
    start_index = body.find(start_marker)
    end_index = body.find(end_marker)
    if start_index < 0:
        raise ValueError("completion_start_marker_missing")
    if end_index < 0:
        raise ValueError("completion_end_marker_missing")
    if end_index <= start_index:
        raise ValueError("completion_markers_out_of_order")
    inner = body[start_index + len(start_marker):end_index].strip()
    if not inner:
        raise ValueError("completion_payload_empty")
    return inner


def classify_downloads(paths: list[Path]) -> ChatGPTDownloadedArtifacts:
    pdfs = [path for path in paths if path.suffix.casefold() == ".pdf"]
    if len(pdfs) != 2:
        raise RuntimeError(f"expected_exactly_two_pdfs:{len(pdfs)}")
    resume_path: Path | None = None
    cover_letter_path: Path | None = None
    for path in pdfs:
        lowered = path.name.casefold()
        if re.search(r"_resume(?:-\d+)?\.pdf$", lowered):
            if resume_path is not None:
                raise RuntimeError("duplicate_resume_pdf")
            resume_path = path
        elif re.search(r"_cover_letter(?:-\d+)?\.pdf$", lowered):
            if cover_letter_path is not None:
                raise RuntimeError("duplicate_cover_letter_pdf")
            cover_letter_path = path
        else:
            raise RuntimeError(f"unexpected_pdf_filename:{path.name}")
    if resume_path is None or cover_letter_path is None:
        raise RuntimeError("missing_resume_or_cover_letter_pdf")
    return ChatGPTDownloadedArtifacts(
        resume_raw_path=resume_path,
        cover_letter_raw_path=cover_letter_path,
    )


def _contact_payload(ws: FileWorkspace) -> dict[str, Any]:
    profile = ws.load_profile()
    for fact in ws.load_facts():
        if fact.kind == "contact" and not fact.disallowed:
            return dict(fact.payload)
    return {
        "name": str(profile.candidate.name or "").strip(),
        "email": str(profile.candidate.email or "").strip(),
        "phone": str(profile.candidate.phone or "").strip() or None,
        "linkedin": str(profile.candidate.linkedin or "").strip() or None,
        "website": str(profile.candidate.website or "").strip() or None,
    }


def _validation_context(*, ws: FileWorkspace, company: str, role: str) -> dict[str, Any]:
    return {
        "job": {"company_name": company, "title": role},
        "profile": {
            "contact": [_contact_payload(ws)],
            "education": [],
            "work": [],
            "projects": [],
            "skills": [],
            "preferences": [],
        },
        "selected_fact_ids": [],
    }


def _pdf_artifact_from_existing(
    pipeline: Any,
    *,
    path: Path,
    context: dict[str, Any],
    expect_one_page: bool,
) -> Any:
    from findmyjob.documents.pipeline import RenderedArtifact

    validation: dict[str, Any] = {"selected_fact_ids": context.get("selected_fact_ids", [])}
    try:
        validation.update(pipeline.validate_pdf(path, expect_one_page=expect_one_page, context=context))
    except Exception as exc:
        validation.update({"valid": False, "failure_reason": "invalid_pdf", "error": str(exc)})
    return RenderedArtifact(kind="pdf", path=path, content_hash="", validation_results=validation)


class ChatGPTDraftingService:
    _batch_lock = threading.RLock()
    _cdp_open_lock = threading.RLock()
    _download_claim_lock = threading.RLock()
    _prompt_submit_lock = threading.Lock()
    _active_draft_workers: dict[str, dict[str, Any]] = {}
    _claimed_download_entries: dict[tuple[str, tuple[int, int]], str] = {}
    _deferred_browser_cleanups: list[tuple[Any, Any]] = []
    _active_cdp_clients = 0
    _next_prompt_submit_at = 0.0

    def __init__(self, workspace: Path | FileWorkspace) -> None:
        self.workspace = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
        self.workspace.ensure()
        self.config = AppConfig.load(self.workspace.root)
        self.drafting = self.config.chatgpt_drafting
        self._draft_worker_token: str | None = None

    def status_payload(self) -> dict[str, Any]:
        stored = self.workspace.load_chatgpt_drafting_status()
        batch_payload = dict(stored.get("batch") or {})
        if (
            batch_payload
            and not type(self)._active_draft_workers
            and any(
                str(member.get("status") or "").strip().lower() not in _DRAFT_BATCH_TERMINAL_STATUSES
                for member in list(batch_payload.get("members") or [])
            )
        ):
            self.recover_stale_batch()
            stored = self.workspace.load_chatgpt_drafting_status()
            batch_payload = dict(stored.get("batch") or {})
        if batch_payload:
            with type(self)._batch_lock:
                batch_payload = self._refresh_batch_summary_locked(batch_payload)
        profile_dir = self.config.chatgpt_profile_dir(self.workspace.root)
        downloads_dir = self.config.chatgpt_downloads_dir(self.workspace.root)
        return {
            "enabled": bool(self.drafting.enabled),
            "renderer": self.config.personal.resume_renderer,
            "gpt_url": self.drafting.gpt_url,
            "completion_start_marker": self.drafting.completion_start_marker,
            "completion_end_marker": self.drafting.completion_end_marker,
            "timeout_seconds": int(self.drafting.timeout_seconds),
            "prompt_submit_delay_ms": int(self.drafting.prompt_submit_delay_ms),
            "download_timeout_seconds": int(self.drafting.download_timeout_seconds),
            "max_parallel_jobs": int(self.drafting.max_parallel_jobs),
            "use_temporary_chat": bool(self.drafting.use_temporary_chat),
            "browser": {
                "browser_mode": self.drafting.browser_mode,
                "browser_cdp_url": self.drafting.browser_cdp_url,
                "launch_if_missing": bool(self.drafting.launch_if_missing),
                "profile_dir": str(profile_dir),
                "profile_dir_exists": profile_dir.exists(),
                "downloads_dir": str(downloads_dir),
            },
            "launch_status": {
                "last_browser_launch_ok": stored.get("last_browser_launch_ok"),
                "last_browser_launch_at": stored.get("last_browser_launch_at"),
                "browser_health": stored.get("browser_health"),
            },
            "progress": {
                "status": stored.get("status"),
                "phase": stored.get("phase"),
                "application_id": stored.get("application_id"),
                "job_id": stored.get("job_id"),
                "company": stored.get("company"),
                "role": stored.get("role"),
                "poll_count": stored.get("poll_count"),
                "wait_seconds": stored.get("wait_seconds"),
                "partial_markers_seen": stored.get("partial_markers_seen"),
                "last_observation": stored.get("last_observation"),
                "raw_root": stored.get("raw_root"),
                "active_worker_count": stored.get("active_worker_count"),
                "active_workers": stored.get("active_workers"),
                "temporary_chat_enabled": stored.get("temporary_chat_enabled"),
                "temporary_chat_last_result": stored.get("temporary_chat_last_result"),
                "temporary_chat_checked_at": stored.get("temporary_chat_checked_at"),
            },
            "last_result": stored.get("last_result"),
            "last_error": stored.get("last_error"),
            "batch": batch_payload,
            "updated_at": stored.get("updated_at"),
        }

    def _update_status(self, **updates: Any) -> None:
        with type(self)._batch_lock:
            state = self.workspace.load_chatgpt_drafting_status()
            for key, value in updates.items():
                if value is _STATUS_UNSET:
                    state.pop(key, None)
                else:
                    state[key] = value
            if self._draft_worker_token is not None:
                active_workers = self._update_active_worker_status_locked(updates)
                self._sync_batch_state(state)
                state["active_worker_count"] = len(active_workers)
                state["active_workers"] = active_workers
            else:
                state.pop("active_worker_count", None)
                state.pop("active_workers", None)
            self.workspace.save_chatgpt_drafting_status(state)

    @staticmethod
    def _draft_batch_member(
        *,
        application_id: str,
        job_id: str,
        company: str,
        role: str,
    ) -> dict[str, Any]:
        return {
            "application_id": application_id,
            "job_id": job_id,
            "company": company,
            "role": role,
            "status": "queued",
            "phase": "queued",
            "last_observation": "Queued for ChatGPT drafting.",
            "render_error": None,
            "reused_existing_downloads": False,
        }

    @classmethod
    def _ensure_batch_member_locked(
        cls,
        batch: dict[str, Any],
        *,
        application_id: str,
        job_id: str,
        company: str,
        role: str,
    ) -> dict[str, Any]:
        members = list(batch.get("members") or [])
        for member in members:
            if str(member.get("application_id") or "") == application_id:
                return member
        member = cls._draft_batch_member(
            application_id=application_id,
            job_id=job_id,
            company=company,
            role=role,
        )
        members.append(member)
        members.sort(key=lambda item: str(item.get("application_id") or ""))
        batch["members"] = members
        return member

    @classmethod
    def _refresh_batch_summary_locked(cls, batch: dict[str, Any]) -> dict[str, Any]:
        members = list(batch.get("members") or [])
        completed = [
            member
            for member in members
            if str(member.get("status") or "").strip().lower() in {"drafted", "reused"}
        ]
        failed = [
            member
            for member in members
            if str(member.get("status") or "").strip().lower() == "failed"
        ]
        active = [
            member
            for member in members
            if str(member.get("status") or "").strip().lower() not in _DRAFT_BATCH_TERMINAL_STATUSES
        ]
        batch["member_count"] = len(members)
        batch["target_size"] = max(len(members), int(batch.get("target_size") or len(members) or 1))
        batch["completed_count"] = len(completed)
        batch["failed_count"] = len(failed)
        batch["active_count"] = len(active)
        batch["remaining_count"] = max(0, len(members) - len(completed) - len(failed))
        batch["active_worker_count"] = len(cls._active_draft_workers)
        batch["completed_member_ids"] = [member.get("application_id") for member in completed if member.get("application_id")]
        batch["failed_member_ids"] = [member.get("application_id") for member in failed if member.get("application_id")]
        batch["status"] = (
            "running"
            if batch["remaining_count"] > 0
            else ("completed_with_failures" if batch["failed_count"] > 0 else "completed")
        )
        batch["handoff_status"] = "waiting_for_batch" if batch["remaining_count"] > 0 else "ready_for_prepare"
        batch["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        return batch

    @classmethod
    def _reserve_prompt_submission_slot(cls, spacing_seconds: float) -> float:
        spacing = max(0.0, float(spacing_seconds or 0.0))
        with cls._prompt_submit_lock:
            now = time.monotonic()
            wait_seconds = max(0.0, cls._next_prompt_submit_at - now)
            cls._next_prompt_submit_at = max(cls._next_prompt_submit_at, now) + spacing
            return wait_seconds

    def _sync_batch_state(self, state: dict[str, Any]) -> dict[str, Any] | None:
        with type(self)._batch_lock:
            batch = dict(state.get("batch") or {})
            worker = type(self)._active_draft_workers.get(self._draft_worker_token) if self._draft_worker_token else None
            if worker is None and not batch:
                return None
            if not batch:
                persisted = dict(self.workspace.load_chatgpt_drafting_status().get("batch") or {})
                if persisted:
                    batch = persisted
            if not batch:
                batch = {
                    "run_id": None,
                    "run_type": "manual_draft",
                    "target_size": 1,
                    "members": [],
                    "status": "running",
                    "handoff_status": "waiting_for_batch",
                }
            if worker is not None:
                member = self._ensure_batch_member_locked(
                    batch,
                    application_id=str(worker.get("application_id") or ""),
                    job_id=str(worker.get("job_id") or ""),
                    company=str(worker.get("company") or ""),
                    role=str(worker.get("role") or ""),
                )
                member["status"] = "running"
                member["phase"] = str(worker.get("phase") or member.get("phase") or "queued")
                member["last_observation"] = str(worker.get("last_observation") or member.get("last_observation") or "")
            state["batch"] = self._refresh_batch_summary_locked(batch)
            return state["batch"]

    def start_batch(
        self,
        *,
        run_id: str | None,
        run_type: str | None,
        target_size: int,
        members: list[dict[str, str]],
    ) -> dict[str, Any]:
        with type(self)._batch_lock:
            state = self.workspace.load_chatgpt_drafting_status()
            state["batch"] = self._refresh_batch_summary_locked(
                {
                    "run_id": run_id,
                    "run_type": run_type,
                    "target_size": max(1, int(target_size or len(members) or 1)),
                    "members": [
                        self._draft_batch_member(
                            application_id=str(item.get("application_id") or ""),
                            job_id=str(item.get("job_id") or ""),
                            company=str(item.get("company") or ""),
                            role=str(item.get("role") or ""),
                        )
                        for item in members
                        if str(item.get("application_id") or "").strip()
                    ],
                    "status": "running",
                    "handoff_status": "waiting_for_batch",
                }
            )
            self.workspace.save_chatgpt_drafting_status(state)
            return dict(state["batch"])

    def clear_batch(self) -> None:
        with type(self)._batch_lock:
            state = self.workspace.load_chatgpt_drafting_status()
            state.pop("batch", None)
            self.workspace.save_chatgpt_drafting_status(state)

    def recover_stale_batch(self) -> dict[str, Any] | None:
        state = self.workspace.load_chatgpt_drafting_status()
        batch = dict(state.get("batch") or {})
        if not batch:
            return None
        with type(self)._batch_lock:
            if type(self)._active_draft_workers:
                return self._refresh_batch_summary_locked(batch)
            members = list(batch.get("members") or [])
            if not members:
                return None
            changed = False
            for member in members:
                status = str(member.get("status") or "").strip().lower()
                if status in _DRAFT_BATCH_TERMINAL_STATUSES:
                    continue
                application_id = str(member.get("application_id") or "").strip()
                company = str(member.get("company") or "").strip()
                role = str(member.get("role") or "").strip()
                if not application_id or not company or not role:
                    member["status"] = "failed"
                    member["phase"] = "failed"
                    member["last_observation"] = "Drafting worker was lost before completion."
                    member["render_error"] = "worker_lost_before_completion"
                    changed = True
                    continue
                raw_root = self.config.chatgpt_downloads_dir(self.workspace.root) / (slugify(application_id) or "application") / "raw"
                recovered_downloads = self._reuse_existing_downloads(
                    raw_root=raw_root,
                    company=company,
                    role=role,
                )
                if recovered_downloads is not None:
                    application = self.workspace.find_application(application_id)
                    job_id = str(member.get("job_id") or (application.job_id if application is not None else "")).strip()
                    job = self.workspace.load_job(job_id) if job_id else None
                    normalized = self._normalize_artifacts(
                        raw_paths=list(recovered_downloads),
                        company=company,
                        role=role,
                        application_id=application_id,
                        on_date=(application.date if application is not None else getattr(job, "date", None)),
                    )
                    member["status"] = "reused"
                    member["phase"] = "closed"
                    member["last_observation"] = "Recovered existing ChatGPT PDFs after worker loss."
                    member["render_error"] = None
                    member["reused_existing_downloads"] = True
                    state["last_result"] = {
                        "application_id": application_id,
                        "job_id": job_id or None,
                        "success": True,
                        "renderer": "chatgpt_download",
                        "pdf_path": self.workspace.relative_path(normalized["pdf_path"]),
                        "cover_letter_path": self.workspace.relative_path(normalized["cover_letter_path"]),
                    }
                else:
                    member["status"] = "failed"
                    member["phase"] = "failed"
                    member["last_observation"] = "Drafting worker was lost before downloads were finalized."
                    member["render_error"] = "worker_lost_before_download_completion"
                changed = True
            if not changed:
                return self._refresh_batch_summary_locked(batch)
            batch["members"] = members
            state["batch"] = self._refresh_batch_summary_locked(batch)
            completed_count = int(state["batch"].get("completed_count") or 0)
            failed_count = int(state["batch"].get("failed_count") or 0)
            if completed_count and failed_count:
                state["status"] = "completed_with_failures"
            elif completed_count:
                state["status"] = "completed"
            else:
                state["status"] = "failed"
            state["phase"] = "interrupted"
            state["last_observation"] = "Recovered stale ChatGPT drafting batch after backend worker loss."
            state["last_error"] = (
                None
                if failed_count == 0
                else "stale_draft_batch_recovered_with_failures"
            )
            state.pop("active_worker_count", None)
            state.pop("active_workers", None)
            self.workspace.save_chatgpt_drafting_status(state)
            return dict(state["batch"])

    def current_batch_payload(self) -> dict[str, Any]:
        state = self.workspace.load_chatgpt_drafting_status()
        batch = dict(state.get("batch") or {})
        if not batch:
            return {}
        with type(self)._batch_lock:
            return self._refresh_batch_summary_locked(batch)

    def _register_draft_worker(
        self,
        *,
        application_id: str,
        job_id: str,
        company: str,
        role: str,
    ) -> None:
        worker_token = f"{application_id}:{threading.get_ident()}:{time.time_ns()}"
        self._draft_worker_token = worker_token
        with type(self)._batch_lock:
            type(self)._active_draft_workers[worker_token] = {
                "application_id": application_id,
                "job_id": job_id,
                "company": company,
                "role": role,
                "phase": "queued",
                "status": "running",
                "last_observation": "Queued for ChatGPT drafting.",
                "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            }

    def _update_active_worker_status_locked(self, updates: dict[str, Any]) -> list[dict[str, Any]]:
        with type(self)._batch_lock:
            if self._draft_worker_token is None:
                return []
            worker = type(self)._active_draft_workers.get(self._draft_worker_token)
            if worker is None:
                return self._active_worker_payload_locked()
            for key, value in updates.items():
                if value is _STATUS_UNSET:
                    worker.pop(key, None)
                else:
                    worker[key] = value
            worker["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            return self._active_worker_payload_locked()

    @classmethod
    def _active_worker_payload_locked(cls) -> list[dict[str, Any]]:
        workers = list(cls._active_draft_workers.values())
        workers.sort(
            key=lambda item: (
                str(item.get("application_id") or ""),
                str(item.get("updated_at") or ""),
            )
        )
        return [
            {
                "application_id": item.get("application_id"),
                "job_id": item.get("job_id"),
                "company": item.get("company"),
                "role": item.get("role"),
                "phase": item.get("phase"),
                "status": item.get("status"),
                "last_observation": item.get("last_observation"),
            }
            for item in workers
        ]

    def _complete_draft_worker(self, *, result: dict[str, Any]) -> None:
        with type(self)._batch_lock:
            state = self.workspace.load_chatgpt_drafting_status()
            state["last_result"] = {
                "application_id": result.get("application_id"),
                "job_id": result.get("job_id"),
                "success": bool(result.get("success")),
                "renderer": result.get("renderer"),
                "pdf_path": result.get("pdf_path"),
                "cover_letter_path": result.get("cover_letter_path"),
            }
            state["last_error"] = result.get("render_error")
            batch = dict(state.get("batch") or {})
            if batch:
                member = self._ensure_batch_member_locked(
                    batch,
                    application_id=str(result.get("application_id") or ""),
                    job_id=str(result.get("job_id") or ""),
                    company=str(result.get("company") or ""),
                    role=str(result.get("role") or ""),
                )
                member["status"] = (
                    "failed"
                    if not bool(result.get("success"))
                    else ("reused" if bool((result.get("draft") or {}).get("reused_existing_downloads")) else "drafted")
                )
                member["phase"] = "closed" if bool(result.get("success")) else "failed"
                member["last_observation"] = (
                    "Drafting completed successfully."
                    if bool(result.get("success"))
                    else str(result.get("render_error") or "ChatGPT drafting failed.")
                )
                member["render_error"] = result.get("render_error")
                member["reused_existing_downloads"] = bool((result.get("draft") or {}).get("reused_existing_downloads"))
            if self._draft_worker_token is not None:
                type(self)._active_draft_workers.pop(self._draft_worker_token, None)
            active_workers = self._active_worker_payload_locked()
            if batch:
                state["batch"] = self._refresh_batch_summary_locked(batch)
        if active_workers:
            state["status"] = "running"
            state["phase"] = "batch_running"
            state["active_worker_count"] = len(active_workers)
            state["active_workers"] = active_workers
            state["last_observation"] = (
                f"Drafting finished for application {result.get('application_id')}; "
                f"waiting for {len(active_workers)} other ChatGPT tab(s)."
            )
        else:
            state["status"] = "completed" if bool(result.get("success")) else "failed"
            state["phase"] = "completed" if bool(result.get("success")) else "failed"
            state["last_observation"] = (
                "Drafting completed successfully."
                if bool(result.get("success"))
                else str(result.get("render_error") or "ChatGPT drafting failed.")
            )
            state.pop("active_worker_count", None)
            state.pop("active_workers", None)
        self.workspace.save_chatgpt_drafting_status(state)
        self._draft_worker_token = None

    @classmethod
    def _register_cdp_client(cls) -> None:
        with cls._batch_lock:
            cls._active_cdp_clients += 1

    @classmethod
    def _release_cdp_client(cls, *, browser: Any, playwright: Any) -> list[tuple[Any, Any]]:
        with cls._batch_lock:
            cls._deferred_browser_cleanups.append((browser, playwright))
            cls._active_cdp_clients = max(0, cls._active_cdp_clients - 1)
            if cls._active_cdp_clients > 0:
                return []
            pending = list(cls._deferred_browser_cleanups)
            cls._deferred_browser_cleanups.clear()
            return pending

    @staticmethod
    async def _cleanup_browser_client(browser: Any, playwright: Any) -> None:
        if browser is not None:
            try:
                disconnect = getattr(browser, "disconnect", None)
                if callable(disconnect):
                    await disconnect()
                else:
                    await browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass

    def launch_browser(self, *, close_existing: bool = False, start_url: str | None = None) -> dict[str, Any]:
        from findmyjob.apply.browser_session import _is_cdp_listening, cdp_port
        import subprocess as _sp

        profile_dir = self._resolve_chrome_profile_dir()
        port = cdp_port(self.drafting.browser_cdp_url)
        note: str | None = None
        browser_health: dict[str, Any] | None = None

        if _is_cdp_listening(port):
            browser_health = self._probe_existing_browser_health()
            if bool(browser_health.get("healthy")) and not close_existing:
                state = self.workspace.load_chatgpt_drafting_status()
                state.update({
                    "last_browser_launch_ok": True,
                    "last_browser_launch_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "last_error": None,
                    "browser_health": browser_health,
                })
                self.workspace.save_chatgpt_drafting_status(state)
                payload = self.status_payload()
                payload["launched"] = True
                payload["note"] = f"Chrome already listening on CDP port {port}."
                return payload
            close_existing = True
            note = (
                "Existing ChatGPT browser session on the CDP port failed health checks; "
                "relaunching the dedicated browser."
            )

        if close_existing:
            try:
                _sp.run(["taskkill", "/IM", "chrome.exe", "/F"], capture_output=True, timeout=10)
            except Exception:
                pass
            time.sleep(2)

        launched = launch_attachable_browser(
            browser_cdp_url=self.drafting.browser_cdp_url,
            profile_dir=profile_dir,
            start_url=str(start_url or self.drafting.gpt_url or "https://chatgpt.com"),
        )
        error_msg = None
        if not launched:
            error_msg = (
                "Chrome launched but never responded on CDP port {port}. "
                "This usually means another Chrome instance has the profile locked. "
                "Close all Chrome windows first, or re-run with --close-existing."
            ).format(port=port)
        else:
            browser_health = self._probe_existing_browser_health()
            if not bool(browser_health.get("healthy")):
                launched = False
                error_msg = (
                    "Chrome responded on the CDP port, but ChatGPT health checks still failed: "
                    f"{str(browser_health.get('reason') or 'unknown_browser_health_error').strip()}"
                )
            elif note is None:
                note = f"Chrome launched and passed health checks on CDP port {port}."

        state = self.workspace.load_chatgpt_drafting_status()
        state.update({
            "last_browser_launch_ok": launched,
            "last_browser_launch_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "last_error": error_msg,
            "browser_health": browser_health,
        })
        self.workspace.save_chatgpt_drafting_status(state)
        payload = self.status_payload()
        payload["launched"] = launched
        payload["note"] = note
        return payload

    def test(self, target: str | None = None) -> dict[str, Any]:
        resolved_target = str(target or "").strip()
        if not resolved_target:
            applications = self.workspace.load_applications()
            if applications:
                resolved_target = applications[0].id
            else:
                inbox = self.workspace.load_inbox()
                if not inbox:
                    raise ValueError("No application or job is available for ChatGPT drafting test.")
                resolved_target = inbox[0].job_id
        application = self.workspace.find_application(resolved_target)
        job_id = application.job_id if application is not None else resolved_target
        job = self.workspace.load_job(job_id)
        if job is None:
            raise ValueError(f"Unknown ChatGPT drafting target: {resolved_target}")
        evaluation = self.workspace.load_evaluation(job_id)
        if evaluation is None:
            raise ValueError(f"No evaluation found for job: {job_id}")
        application_id = application.id if application is not None else self.workspace.next_application_id()
        return self.draft(job=job, evaluation=evaluation, application_id=application_id, on_date=application.date if application is not None else None)

    def draft(self, *, job: Any, evaluation: Any, application_id: str, on_date: str | None = None) -> dict[str, Any]:
        self._register_draft_worker(
            application_id=application_id,
            job_id=job.job_id,
            company=str(evaluation.company or job.company),
            role=str(evaluation.role or job.title),
        )
        self._update_status(
            status="running",
            phase="preparing_prompt",
            application_id=application_id,
            job_id=job.job_id,
            company=str(evaluation.company or job.company),
            role=str(evaluation.role or job.title),
            last_error=None,
            last_result=_STATUS_UNSET,
            poll_count=0,
            wait_seconds=0,
            partial_markers_seen=False,
            last_observation="Preparing ChatGPT drafting prompt.",
        )
        try:
            result = self._draft_sync(job=job, evaluation=evaluation, application_id=application_id, on_date=on_date)
        except Exception as exc:
            result = {
                "success": False,
                "job_id": job.job_id,
                "application_id": application_id,
                "renderer": "chatgpt_download",
                "template_bridge_used": False,
                "resume_template_path": None,
                "cover_letter_template_path": None,
                "html_path": None,
                "pdf_path": None,
                "cover_letter_path": None,
                "resume_text_path": None,
                "cover_letter_text_path": None,
                "warnings": [],
                "render_error": str(exc),
                "draft": {
                    "provider": "chatgpt_custom_gpt",
                    "completion_start_marker": self.drafting.completion_start_marker,
                    "completion_end_marker": self.drafting.completion_end_marker,
                },
            }
        self._complete_draft_worker(result=result)
        return result

    def _draft_sync(self, *, job: Any, evaluation: Any, application_id: str, on_date: str | None = None) -> dict[str, Any]:
        raw_root = self.config.chatgpt_downloads_dir(self.workspace.root) / (slugify(application_id) or "application") / "raw"
        raw_root.mkdir(parents=True, exist_ok=True)
        company = str(evaluation.company or job.company)
        role = str(evaluation.role or job.title)
        self._update_status(
            phase="opening_browser",
            raw_root=str(raw_root),
            last_observation="Opening ChatGPT drafting browser session.",
        )
        prompt = build_chatgpt_prompt(
            company=company,
            role=role,
            job_description=str(job.description or ""),
        )
        reused_downloads = self._reuse_existing_downloads(
            raw_root=raw_root,
            company=company,
            role=role,
        )
        if reused_downloads is not None:
            self._update_status(
                phase="reusing_downloads",
                last_observation="Found existing resume and cover letter PDFs; skipping ChatGPT regeneration.",
            )
            download_result = {"downloads": reused_downloads, "assistant_text": ""}
            reused_existing_downloads = True
        else:
            use_temporary_chat = bool(self.drafting.use_temporary_chat)
            if not use_temporary_chat:
                self._update_status(
                    temporary_chat_enabled=False,
                    temporary_chat_last_result="disabled_by_config",
                    temporary_chat_checked_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                )
            launch_payload = self.launch_browser(start_url="about:blank")
            if not bool(launch_payload.get("launched")):
                raise RuntimeError(
                    "chatgpt_browser_launch_failed:"
                    f"{str(launch_payload.get('last_error') or launch_payload.get('note') or 'unknown_browser_launch_error').strip()}"
                )
            current_use_temporary_chat = use_temporary_chat
            temp_retry_used = False
            browser_repair_attempted = False
            while True:
                try:
                    download_result = run_async(
                        self._draft_via_browser,
                        prompt,
                        raw_root,
                        company,
                        role,
                        current_use_temporary_chat,
                    )
                    break
                except Exception as exc:
                    if current_use_temporary_chat and not temp_retry_used and self._should_retry_without_temporary_chat(exc):
                        temp_retry_used = True
                        current_use_temporary_chat = False
                        self._update_status(
                            phase="retrying_without_temporary_chat",
                            last_observation=(
                                "ChatGPT attachments failed while temporary chat was enabled; "
                                "retrying once without temporary chat for this draft."
                            ),
                            temporary_chat_enabled=False,
                            temporary_chat_last_result="fallback_retry_without_temp",
                            temporary_chat_checked_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                        )
                        continue
                    if not browser_repair_attempted and self._is_chatgpt_browser_blocker_error(exc):
                        browser_repair_attempted = True
                        self._update_status(
                            phase="repairing_browser_session",
                            last_observation=(
                                "ChatGPT browser session became unhealthy; relaunching the dedicated browser "
                                "and retrying this draft once."
                            ),
                        )
                        relaunch_payload = self.launch_browser(close_existing=True, start_url="about:blank")
                        if not bool(relaunch_payload.get("launched")):
                            raise RuntimeError(
                                "chatgpt_browser_relaunch_failed:"
                                f"{str(relaunch_payload.get('last_error') or relaunch_payload.get('note') or 'unknown_browser_relaunch_error').strip()}"
                            ) from exc
                        continue
                    raise
            reused_existing_downloads = False
        normalized = self._normalize_artifacts(
            raw_paths=list(download_result["downloads"]),
            company=company,
            role=role,
            application_id=application_id,
            on_date=on_date or getattr(job, "date", None),
        )
        return {
            "success": True,
            "job_id": job.job_id,
            "application_id": application_id,
            "renderer": "chatgpt_download",
            "template_bridge_used": False,
            "resume_template_path": None,
            "cover_letter_template_path": None,
            "html_path": None,
            "pdf_path": self.workspace.relative_path(normalized["pdf_path"]),
            "cover_letter_path": self.workspace.relative_path(normalized["cover_letter_path"]),
            "resume_text_path": self.workspace.relative_path(normalized["resume_text_path"]),
            "cover_letter_text_path": self.workspace.relative_path(normalized["cover_letter_text_path"]),
            "warnings": [],
            "render_error": None,
            "draft": {
                "provider": "chatgpt_custom_gpt",
                "completion_start_marker": self.drafting.completion_start_marker,
                "completion_end_marker": self.drafting.completion_end_marker,
                "reused_existing_downloads": reused_existing_downloads,
            },
            "artifacts": {key: str(value) for key, value in normalized.items()},
            "raw_downloads": [self.workspace.relative_path(path) for path in download_result["downloads"]],
        }

    def _resolve_chrome_profile_dir(self) -> Path:
        """Return the Chrome user-data-dir for ChatGPT drafting.

        Chrome 146+ blocks CDP (``--remote-debugging-port``) when launched with
        the default user-data-dir.  The dedicated ``.fmj/browser/chatgpt-profile``
        works because it is non-default.  The operator must log into ChatGPT once
        in this profile; the session then persists across runs.

        If ``chrome_user_data_dir`` is explicitly set in config, honour it — the
        operator accepts responsibility for ensuring CDP works on that directory.
        """
        custom = str(self.drafting.chrome_user_data_dir or "").strip()
        if custom:
            resolved = Path(custom)
            if not resolved.is_absolute():
                resolved = self.workspace.root / custom
            return resolved
        return self.config.chatgpt_profile_dir(self.workspace.root)

    async def _open_cdp_drafting_page(self, playwright: Any) -> tuple[Any, Any]:
        connect_timeout_seconds = min(45.0, max(10.0, float(self.drafting.timeout_seconds) / 8.0))
        page_timeout_ms = int(self.drafting.timeout_seconds * 1000)
        with type(self)._cdp_open_lock:
            self._update_status(
                phase="connecting_cdp",
                last_observation="Connecting to Chrome over CDP for ChatGPT drafting.",
            )
            browser = await asyncio.wait_for(
                playwright.chromium.connect_over_cdp(self.drafting.browser_cdp_url),
                timeout=connect_timeout_seconds,
            )
            self._register_cdp_client()
            context = browser.contexts[0] if browser.contexts else await asyncio.wait_for(
                browser.new_context(accept_downloads=True),
                timeout=connect_timeout_seconds,
            )
            page = await asyncio.wait_for(
                context.new_page(),
                timeout=connect_timeout_seconds,
            )
            page.set_default_timeout(page_timeout_ms)
        return browser, page

    @staticmethod
    def _should_retry_without_temporary_chat(exc: Exception) -> bool:
        lowered = str(exc or "").strip().casefold()
        return any(
            token in lowered
            for token in (
                "download_timed_out",
                "missing_download_link",
                "download_controls_unavailable",
                "sandbox:/mnt/data/",
                "/mnt/data/",
                "failed to get upload status",
                "download_not_a_valid_pdf",
                "download_too_small_or_missing",
            )
        )

    @staticmethod
    def _is_chatgpt_browser_blocker_error(exc: Any) -> bool:
        lowered = str(exc or "").strip().casefold()
        return lowered.startswith("chatgpt_http_") or "chatgpt_login_required" in lowered

    def _probe_existing_browser_health(self) -> dict[str, Any]:
        try:
            return run_async(self._probe_existing_browser_health_async)
        except Exception as exc:
            return {
                "healthy": False,
                "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "reason": str(exc).strip() or exc.__class__.__name__,
            }

    async def _probe_existing_browser_health_async(self) -> dict[str, Any]:
        from playwright.async_api import async_playwright

        checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        playwright = await async_playwright().start()
        browser = None
        page = None
        cdp_registered = False
        try:
            browser, page = await self._open_cdp_drafting_page(playwright)
            cdp_registered = True
            await self._goto_chatgpt_url(page, _CHATGPT_HOME_URL, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            try:
                body_text = str(await page.locator("body").inner_text(timeout=2_000) or "")
            except Exception:
                body_text = ""
            normalized_body = " ".join(body_text.split())
            lowered = normalized_body.casefold()
            if "log in" in lowered and "sign up" in lowered:
                return {
                    "healthy": False,
                    "checked_at": checked_at,
                    "reason": "chatgpt_login_required_for_prompt_submission",
                    "url": str(getattr(page, "url", "") or ""),
                    "body": normalized_body[:240],
                }
            return {
                "healthy": True,
                "checked_at": checked_at,
                "reason": None,
                "url": str(getattr(page, "url", "") or ""),
            }
        except Exception as exc:
            return {
                "healthy": False,
                "checked_at": checked_at,
                "reason": str(exc).strip() or exc.__class__.__name__,
            }
        finally:
            if page is not None:
                try:
                    if not page.is_closed():
                        await page.close()
                except Exception:
                    pass
            if cdp_registered and browser is not None:
                cleanup_clients = self._release_cdp_client(browser=browser, playwright=playwright)
                for cleanup_browser, cleanup_playwright in cleanup_clients:
                    await self._cleanup_browser_client(cleanup_browser, cleanup_playwright)
            else:
                await self._cleanup_browser_client(browser, playwright)

    async def _translate_chatgpt_navigation_error(self, page: Any, url: str, exc: Exception) -> Exception:
        lowered = str(exc or "").strip().casefold()
        if "err_http_response_code_failure" not in lowered:
            return exc
        page_url = ""
        page_title = ""
        body_excerpt = ""
        try:
            page_url = str(getattr(page, "url", "") or "")
        except Exception:
            page_url = ""
        try:
            page_title = str(await page.title() or "")
        except Exception:
            page_title = ""
        try:
            body_text = str(await page.locator("body").inner_text(timeout=2_000) or "")
            body_excerpt = " ".join(body_text.split())[:400]
        except Exception:
            body_excerpt = ""
        match = re.search(r"HTTP ERROR\s+(\d+)", body_excerpt, flags=re.IGNORECASE)
        if match:
            status_code = match.group(1)
            if status_code == "431":
                return RuntimeError(
                    "chatgpt_http_431:"
                    "ChatGPT is returning HTTP 431 in the dedicated browser session. "
                    "The drafting browser profile is unhealthy and needs a browser/session reset before more drafts can continue. "
                    f"url={url!r}:page_url={page_url!r}:title={page_title!r}:body={body_excerpt!r}"
                )
            return RuntimeError(
                f"chatgpt_http_{status_code}:"
                f"url={url!r}:page_url={page_url!r}:title={page_title!r}:body={body_excerpt!r}"
            )
        return RuntimeError(
            "chatgpt_http_failure:"
            f"url={url!r}:page_url={page_url!r}:title={page_title!r}:body={body_excerpt!r}:original={str(exc).strip()!r}"
        )

    async def _goto_chatgpt_url(self, page: Any, url: str, *, wait_until: str = "domcontentloaded") -> Any:
        try:
            return await page.goto(url, wait_until=wait_until)
        except Exception as exc:
            raise await self._translate_chatgpt_navigation_error(page, url, exc)

    async def _draft_via_browser(
        self,
        prompt: str,
        raw_root: Path,
        company: str,
        role: str,
        use_temporary_chat: bool = False,
    ) -> dict[str, Any]:
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        browser = None
        page = None
        cdp_registered = False
        try:
            browser, page = await self._open_cdp_drafting_page(playwright)
            cdp_registered = True

            base_gpt_url = str(self.drafting.gpt_url or "https://chatgpt.com")
            gpt_url = self._with_temporary_chat_query(base_gpt_url) if use_temporary_chat else base_gpt_url
            if use_temporary_chat:
                # Open a new tab, prime temporary chat on the ChatGPT home surface,
                # then navigate into the custom GPT. The temporary-chat control is
                # currently exposed on the base app surface rather than consistently
                # on custom GPT pages.
                self._update_status(
                    phase="loading_gpt",
                    last_observation="Opening ChatGPT home to enable temporary chat.",
                )
                await self._goto_chatgpt_url(page, _CHATGPT_HOME_URL, wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass
                if not await self._enable_temporary_chat(page):
                    raise RuntimeError("temporary_chat_not_enabled_on_home")
            else:
                self._update_status(
                    phase="loading_gpt",
                    last_observation="Opening ChatGPT GPT without temporary chat fallback.",
                    temporary_chat_enabled=False,
                    temporary_chat_last_result="disabled_for_attachment_retry",
                    temporary_chat_checked_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                )

            self._update_status(
                phase="loading_gpt",
                last_observation=f"Opening ChatGPT GPT at {gpt_url}.",
            )
            await self._goto_chatgpt_url(page, gpt_url, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            if use_temporary_chat and not await self._enable_temporary_chat(page):
                raise RuntimeError("temporary_chat_not_enabled_on_gpt")

            await self._wait_for_prompt_composer(
                page,
                timeout_ms=60_000,
                recovery_url=gpt_url,
            )

            # Submit the prompt
            self._update_status(
                phase="submitting_prompt",
                last_observation="Submitting the drafting prompt to ChatGPT.",
            )
            await self._submit_prompt(page, prompt)

            # ChatGPT navigates to /c/<conversation-id> after the first message.
            # Wait for the URL to change, then re-settle on the page.
            try:
                await page.wait_for_url("**/c/**", timeout=15_000)
            except Exception:
                pass  # URL may not change if reusing an existing conversation

            # Extended thinking + generation can take a long time.
            # Poll with asyncio.sleep (not page.wait_for_timeout which is fragile over CDP).
            self._update_status(
                phase="waiting_for_markers",
                poll_count=0,
                wait_seconds=0,
                partial_markers_seen=False,
                last_observation="Waiting for ChatGPT completion markers.",
            )
            turn = await self._wait_for_completed_turn(page)

            # Extra stability: wait until text stops growing (generation fully finished).
            # Extended thinking + button rendering can take a while after markers appear,
            # so be generous: up to 10 rounds × 3s = 30s of stability checking.
            self._update_status(
                phase="stabilizing_response",
                last_observation="Completion markers found; waiting for ChatGPT output to stabilize.",
            )
            prev_len = len(turn["text"])
            stable_count = 0
            for _ in range(10):
                await anyio.sleep(3)
                try:
                    fresh = str(await turn["locator"].inner_text(timeout=3000) or "")
                except Exception:
                    break
                if len(fresh) == prev_len:
                    stable_count += 1
                    if stable_count >= 2:
                        break
                else:
                    stable_count = 0
                prev_len = len(fresh)
                turn["text"] = fresh

            # ChatGPT needs time to prepare the download files after generation
            # finishes.  Wait 5 seconds so the PDF blobs are ready before clicking.
            self._update_status(
                phase="waiting_for_downloads",
                last_observation="Completion markers found; waiting for download buttons and PDF blobs.",
            )
            await anyio.sleep(5)

            self._update_status(
                phase="downloading_pdfs",
                last_observation="Clicking ChatGPT PDF download controls.",
            )
            downloads = await self._download_pdfs(
                page,
                turn["locator"],
                raw_root,
                company=company,
                role=role,
                use_temporary_chat=use_temporary_chat,
            )

            # Verify every downloaded file is a real PDF before we close anything.
            for path in downloads:
                if not path.exists() or path.stat().st_size < 256:
                    raise RuntimeError(f"download_too_small_or_missing:{path.name}")
                with open(path, "rb") as fh:
                    if fh.read(5) != b"%PDF-":
                        raise RuntimeError(f"download_not_a_valid_pdf:{path.name}")

            self._update_status(
                phase="downloads_captured",
                last_observation="Resume and cover letter PDFs were captured from ChatGPT.",
            )
            return {"downloads": downloads, "assistant_text": turn["text"]}
        finally:
            # Close only the working ChatGPT tab so retries do not accumulate
            # conversation tabs, but keep the parent Chrome session alive.
            if page is not None:
                try:
                    if not page.is_closed():
                        await page.close()
                except Exception:
                    pass
            if cdp_registered and browser is not None:
                cleanup_clients = self._release_cdp_client(browser=browser, playwright=playwright)
                for cleanup_browser, cleanup_playwright in cleanup_clients:
                    await self._cleanup_browser_client(cleanup_browser, cleanup_playwright)
            else:
                await self._cleanup_browser_client(browser, playwright)

    async def _wait_for_prompt_composer(
        self,
        page: Any,
        *,
        timeout_ms: int,
        recovery_url: str | None = None,
    ) -> Any:
        deadline = time.monotonic() + (timeout_ms / 1000)
        reloaded = False
        last_body_excerpt = ""
        while time.monotonic() < deadline:
            remaining_ms = max(1_000, int((deadline - time.monotonic()) * 1000))
            try:
                await page.bring_to_front()
            except Exception:
                pass
            composer = await self._try_find_visible_locator(
                page,
                _PROMPT_SELECTORS,
                timeout_ms=min(2_000, remaining_ms),
            )
            if composer is not None:
                return composer
            try:
                body_text = str(await page.locator("body").inner_text(timeout=1_000) or "").strip()
            except Exception:
                body_text = ""
            if body_text:
                normalized_excerpt = " ".join(body_text.split())
                last_body_excerpt = normalized_excerpt[:240]
                lowered = normalized_excerpt.casefold()
                if "log in" in lowered and "sign up" in lowered:
                    raise RuntimeError("chatgpt_login_required_for_prompt_submission")
            if not reloaded and recovery_url and (deadline - time.monotonic()) > 10:
                reloaded = True
                self._update_status(
                    phase="loading_gpt",
                    last_observation="Prompt composer did not appear; reloading the ChatGPT drafting tab once.",
                )
                try:
                    await page.goto(recovery_url, wait_until="domcontentloaded")
                    try:
                        await page.wait_for_load_state("networkidle", timeout=15_000)
                    except Exception:
                        pass
                except Exception:
                    try:
                        await page.reload(wait_until="domcontentloaded")
                    except Exception:
                        pass
                continue
            await anyio.sleep(1)
        try:
            current_url = str(getattr(page, "url", "") or "")
        except Exception:
            current_url = ""
        try:
            title = str(await page.title() or "")
        except Exception:
            title = ""
        raise RuntimeError(
            "chatgpt_prompt_composer_missing:"
            f"url={current_url!r}:title={title!r}:body={last_body_excerpt!r}"
        )

    async def _submit_prompt(self, page: Any, prompt: str) -> None:
        composer = await self._wait_for_prompt_composer(page, timeout_ms=60_000)
        submit_wait_seconds = type(self)._reserve_prompt_submission_slot(6.0)
        if submit_wait_seconds > 0:
            self._update_status(
                phase="submitting_prompt",
                last_observation=(
                    "Waiting for a ChatGPT submission slot to avoid rate limiting "
                    f"({submit_wait_seconds:.1f}s)."
                ),
            )
            await anyio.sleep(submit_wait_seconds)
        try:
            await page.bring_to_front()
        except Exception:
            pass
        try:
            await composer.click()
        except Exception:
            await composer.evaluate("el => el.focus()")
        await _sleep_ms(300)

        await self._write_prompt_to_composer(page, composer, prompt)

        if self.drafting.prompt_submit_delay_ms:
            await _sleep_ms(int(self.drafting.prompt_submit_delay_ms))

        # Give the composer a moment to register the pasted prompt. If the
        # content did not land, try once more with a direct text insertion path.
        await _sleep_ms(500)
        if not await self._composer_has_prompt_fragment(composer, prompt):
            self._update_status(
                phase="submitting_prompt",
                last_observation="ChatGPT composer did not register the prompt on the first paste; retrying prompt insertion once.",
            )
            try:
                await composer.click()
            except Exception:
                await composer.evaluate("el => el.focus()")
            await page.keyboard.press("Control+A")
            await page.keyboard.insert_text(prompt)
            await _sleep_ms(750)

        send_button = await self._wait_for_enabled_send_button(page, timeout_ms=12_000)
        if send_button is not None:
            await send_button.click()
            return
        if await self._composer_has_prompt_fragment(composer, prompt):
            await page.keyboard.press("Enter")
            return
        raise RuntimeError("chatgpt_prompt_not_pasted_or_send_not_enabled")

    async def _write_prompt_to_composer(self, page: Any, composer: Any, prompt: str) -> None:
        # ProseMirror contenteditable divs don't support fill().
        # Use clipboard paste for reliability with long text.
        is_contenteditable = await composer.evaluate(
            "el => el.getAttribute('contenteditable') === 'true'"
        )
        if is_contenteditable:
            await page.evaluate("text => navigator.clipboard.writeText(text)", prompt)
            await page.keyboard.press("Control+V")
            return
        try:
            await composer.fill(prompt)
        except Exception:
            await page.keyboard.press("Control+A")
            await page.keyboard.insert_text(prompt)

    async def _composer_text(self, composer: Any) -> str:
        try:
            value = await composer.evaluate(
                """el => {
                    const direct = typeof el.value === 'string' ? el.value : '';
                    const text = (el.innerText || el.textContent || '').trim();
                    return direct || text;
                }"""
            )
        except Exception:
            return ""
        return str(value or "").strip()

    async def _composer_has_prompt_fragment(self, composer: Any, prompt: str) -> bool:
        composer_text = " ".join((await self._composer_text(composer)).split()).casefold()
        if not composer_text:
            return False
        fragment = " ".join(str(prompt or "").split())[:80].casefold()
        return bool(fragment) and fragment in composer_text

    async def _wait_for_enabled_send_button(self, page: Any, *, timeout_ms: int) -> Any | None:
        deadline = time.monotonic() + (timeout_ms / 1000)
        while time.monotonic() < deadline:
            for selector in _SEND_BUTTON_SELECTORS:
                locator = page.locator(selector).first
                try:
                    if not await locator.is_visible(timeout=500):
                        continue
                except Exception:
                    continue
                try:
                    if await locator.is_enabled(timeout=500):
                        return locator
                except Exception:
                    continue
            await anyio.sleep(0.5)
        return None

    def _with_temporary_chat_query(self, url: str) -> str:
        parts = urlsplit(str(url or "").strip() or _CHATGPT_HOME_URL)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["temporary-chat"] = "true"
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    async def _temporary_chat_active(self, page: Any) -> bool:
        active_toggle = await self._try_find_visible_locator(page, _TEMP_CHAT_ACTIVE_SELECTORS, timeout_ms=1_000)
        if active_toggle is not None:
            return True
        try:
            current_url = str(getattr(page, "url", "") or "")
        except Exception:
            current_url = ""
        if "temporary-chat=true" in current_url.casefold():
            return True
        try:
            return bool(
                await page.evaluate(
                    """() => {
                        const text = `${document.body?.innerText || ""}\n${document.documentElement?.outerHTML || ""}`;
                        return /temporary\\s+chat/i.test(text);
                    }"""
                )
            )
        except Exception:
            return False

    async def _enable_temporary_chat(self, page: Any) -> bool:
        checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        if await self._temporary_chat_active(page):
            self._update_status(
                phase="loading_gpt",
                last_observation="ChatGPT temporary chat is already enabled.",
                temporary_chat_enabled=True,
                temporary_chat_last_result="already_enabled",
                temporary_chat_checked_at=checked_at,
            )
            return True

        toggle = await self._try_find_visible_locator(page, _TEMP_CHAT_ENABLE_SELECTORS, timeout_ms=3_000)
        if toggle is None:
            self._update_status(
                temporary_chat_last_result="toggle_unavailable",
                temporary_chat_checked_at=checked_at,
            )
            return False

        self._update_status(
            phase="loading_gpt",
            last_observation="Enabling ChatGPT temporary chat for this drafting tab.",
            temporary_chat_checked_at=checked_at,
        )
        try:
            await toggle.click()
            await anyio.sleep(0.5)
            enabled = await self._temporary_chat_active(page)
            self._update_status(
                temporary_chat_enabled=enabled,
                temporary_chat_last_result="enabled" if enabled else "click_not_confirmed",
                temporary_chat_checked_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            )
            return enabled
        except Exception:
            self._update_status(
                temporary_chat_last_result="click_failed",
                temporary_chat_checked_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            )
            return False

    async def _wait_for_completed_turn(self, page: Any) -> dict[str, Any]:
        deadline = time.monotonic() + float(self.drafting.timeout_seconds)
        partial_seen = False
        poll_count = 0
        start_time = time.monotonic()
        latest_unmarked_text = ""
        stable_without_markers = 0
        recovery_attempted = False
        while time.monotonic() < deadline:
            poll_count += 1
            latest_unmarked_turn: dict[str, Any] | None = None
            try:
                turn = await self._latest_marked_turn(page)
                if turn is not None:
                    self._update_status(
                        phase="markers_found",
                        poll_count=poll_count,
                        wait_seconds=round(time.monotonic() - start_time, 1),
                        partial_markers_seen=partial_seen,
                        last_observation="ChatGPT completion markers were detected.",
                    )
                    return turn
                partial_seen = partial_seen or await self._any_partial_marker(page)
                latest_unmarked_turn = await self._latest_assistant_turn(page)
            except Exception:
                # Page may be mid-navigation or temporarily unavailable
                pass
            # Use anyio.sleep instead of page.wait_for_timeout — the latter
            # breaks over CDP when the page navigates after sending a message.
            # Poll every 3s to be patient with extended thinking.
            if not partial_seen and latest_unmarked_turn is not None:
                current_text = latest_unmarked_turn["text"]
                if current_text == latest_unmarked_text:
                    stable_without_markers += 1
                else:
                    latest_unmarked_text = current_text
                    stable_without_markers = 0
            else:
                stable_without_markers = 0
            if (
                not partial_seen
                and not recovery_attempted
                and stable_without_markers >= 4
                and await self._page_shows_stopped_thinking(page)
            ):
                self._update_status(
                    phase="recovering_stalled_generation",
                    poll_count=poll_count,
                    wait_seconds=round(time.monotonic() - start_time, 1),
                    partial_markers_seen=False,
                    last_observation="ChatGPT stopped without emitting markers; sending a recovery prompt.",
                )
                await self._submit_prompt(page, _RECOVER_STALLED_PROMPT)
                recovery_attempted = True
                latest_unmarked_text = ""
                stable_without_markers = 0
                await anyio.sleep(5)
                continue
            if poll_count == 1 or poll_count % 5 == 0:
                self._update_status(
                    phase="waiting_for_completion_after_markers" if partial_seen else "waiting_for_markers",
                    poll_count=poll_count,
                    wait_seconds=round(time.monotonic() - start_time, 1),
                    partial_markers_seen=partial_seen,
                    last_observation=(
                        "ChatGPT started emitting markers; waiting for the full response to finish."
                        if partial_seen
                        else "ChatGPT is still thinking; markers have not appeared yet."
                    ),
                )
            await anyio.sleep(3)
        if partial_seen:
            raise TimeoutError("Timed out waiting for the ChatGPT response to finish after markers started appearing.")
        raise TimeoutError("Timed out waiting for the ChatGPT response completion markers.")

    async def _latest_marked_turn(self, page: Any) -> dict[str, Any] | None:
        for selector in _ASSISTANT_TURN_SELECTORS:
            locator = page.locator(selector)
            try:
                count = await locator.count()
            except Exception:
                continue
            for index in range(count - 1, -1, -1):
                candidate = locator.nth(index)
                try:
                    text = str(await candidate.inner_text(timeout=1000) or "").strip()
                except Exception:
                    continue
                if not text:
                    continue
                try:
                    extract_marked_block(
                        text,
                        start_marker=self.drafting.completion_start_marker,
                        end_marker=self.drafting.completion_end_marker,
                    )
                except ValueError:
                    continue
                return {"locator": candidate, "text": text}
        return None

    async def _latest_assistant_turn(self, page: Any) -> dict[str, Any] | None:
        for selector in _ASSISTANT_TURN_SELECTORS:
            locator = page.locator(selector)
            try:
                count = await locator.count()
            except Exception:
                continue
            for index in range(count - 1, -1, -1):
                candidate = locator.nth(index)
                try:
                    text = str(await candidate.inner_text(timeout=1000) or "").strip()
                except Exception:
                    continue
                if text:
                    return {"locator": candidate, "text": text}
        return None

    async def _page_shows_stopped_thinking(self, page: Any) -> bool:
        try:
            body_text = str(await page.locator("body").inner_text(timeout=2000) or "")
        except Exception:
            return False
        return "Stopped thinking" in body_text

    async def _any_partial_marker(self, page: Any) -> bool:
        for selector in _ASSISTANT_TURN_SELECTORS:
            locator = page.locator(selector)
            try:
                count = await locator.count()
            except Exception:
                continue
            for index in range(count - 1, -1, -1):
                candidate = locator.nth(index)
                try:
                    text = str(await candidate.inner_text(timeout=1000) or "").strip()
                except Exception:
                    continue
                if self.drafting.completion_start_marker in text or self.drafting.completion_end_marker in text:
                    return True
        return False

    def _download_watch_dirs(self, raw_root: Path) -> list[Path]:
        candidates: list[Path] = []

        def _add(path_value: Path | str | None) -> None:
            if path_value in (None, ""):
                return
            try:
                candidate = Path(path_value).expanduser()
                resolved = candidate.resolve() if candidate.exists() else candidate
            except Exception:
                return
            if resolved not in candidates:
                candidates.append(resolved)

        _add(raw_root)
        user_profile = os.environ.get("USERPROFILE") or str(Path.home())
        _add(Path(user_profile) / "Downloads")
        _add(Path.home() / "Downloads")

        profile_root = self._resolve_chrome_profile_dir()
        for preferences_path in (
            profile_root / "Default" / "Preferences",
            profile_root / "Profile 1" / "Preferences",
            profile_root / "Preferences",
        ):
            if not preferences_path.exists():
                continue
            try:
                payload = json.loads(preferences_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            download_payload = payload.get("download") or {}
            savefile_payload = payload.get("savefile") or {}
            for key in ("default_directory", "last_directory"):
                _add(download_payload.get(key))
                _add(savefile_payload.get(key))
        return candidates

    @staticmethod
    def _entry_signature(entry: Path) -> tuple[int, int] | None:
        try:
            stat = entry.stat()
        except OSError:
            return None
        return (int(stat.st_size), int(stat.st_mtime_ns))

    @classmethod
    def _snapshot_download_entries(cls, directories: list[Path]) -> dict[Path, tuple[int, int] | None]:
        seen: dict[Path, tuple[int, int] | None] = {}
        for directory in directories:
            if not directory.exists() or not directory.is_dir():
                continue
            try:
                for entry in directory.iterdir():
                    try:
                        resolved = entry.resolve()
                    except Exception:
                        resolved = entry
                    seen[resolved] = cls._entry_signature(entry)
            except Exception:
                continue
        return seen

    @staticmethod
    def _filename_component(value: str | None) -> str:
        text = str(value or "").strip()
        normalized = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
        return normalized or "Document"

    def _canonical_raw_pdf_path(self, raw_root: Path, *, company: str, role: str, label: str) -> Path:
        contact = _contact_payload(self.workspace)
        candidate = self._filename_component(contact.get("name") or "Candidate")
        company_part = self._filename_component(company)
        role_part = self._filename_component(role)
        suffix = "Resume" if label == "resume" else "Cover_Letter"
        return raw_root / f"{candidate}_{company_part}_{role_part}_{suffix}.pdf"

    @staticmethod
    def _label_in_download_name(normalized_name: str, *, label: str) -> bool:
        if label == "resume":
            return bool(re.search(r"(?:^|-)resume(?:-\d+)?$", normalized_name))
        return bool(re.search(r"(?:^|-)cover-letter(?:-\d+)?$", normalized_name))

    def _download_matches_job(self, entry: Path, *, company: str, role: str, label: str) -> bool:
        normalized_name = slugify(entry.stem)
        expected_company = slugify(company)
        expected_role = slugify(role)
        if not expected_company or not expected_role:
            return False
        if expected_company not in normalized_name or expected_role not in normalized_name:
            return False
        return self._label_in_download_name(normalized_name, label=label)

    @classmethod
    def _claim_download_candidate(cls, entry: Path, *, signature: tuple[int, int], claim_key: str) -> bool:
        try:
            resolved = str(entry.resolve()).casefold()
        except Exception:
            resolved = str(entry).casefold()
        key = (resolved, signature)
        with cls._download_claim_lock:
            owner = cls._claimed_download_entries.get(key)
            if owner is None or owner == claim_key:
                cls._claimed_download_entries[key] = claim_key
                return True
            return False

    def _store_download_into_raw(self, entry: Path, raw_root: Path, *, company: str, role: str, label: str) -> Path:
        target = self._canonical_raw_pdf_path(raw_root, company=company, role=role, label=label)
        try:
            same_file = entry.resolve() == target.resolve()
        except Exception:
            same_file = entry == target
        if same_file:
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        source_signature = self._entry_signature(entry)
        target_signature = self._entry_signature(target) if target.exists() else None
        if source_signature is not None and source_signature == target_signature:
            return target
        shutil.copy2(entry, target)
        return target

    def _find_matching_downloads(self, directories: list[Path], *, company: str, role: str) -> dict[str, Path]:
        matches: dict[str, Path] = {}
        for directory in directories:
            if not directory.exists() or not directory.is_dir():
                continue
            try:
                entries = sorted(directory.glob("*.pdf"), key=lambda item: item.stat().st_mtime_ns, reverse=True)
            except Exception:
                continue
            for label in ("resume", "cover_letter"):
                if label in matches:
                    continue
                for entry in entries:
                    if self._download_matches_job(entry, company=company, role=role, label=label):
                        matches[label] = entry
                        break
            if len(matches) == 2:
                break
        return matches

    def _serial_download_capture_mode(self) -> bool:
        try:
            return int(self.drafting.max_parallel_jobs or 1) <= 1
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _pdf_has_magic_bytes(path: Path) -> bool:
        try:
            with path.open("rb") as handle:
                return handle.read(5) == b"%PDF-"
        except OSError:
            return False

    def _reuse_existing_downloads(self, *, raw_root: Path, company: str, role: str) -> list[Path] | None:
        raw_candidates = sorted(raw_root.glob("*.pdf"))
        if len(raw_candidates) == 2:
            try:
                classified = classify_downloads(raw_candidates)
            except Exception:
                classified = None
            if classified is not None and self._pdf_has_magic_bytes(classified.resume_raw_path) and self._pdf_has_magic_bytes(classified.cover_letter_raw_path):
                return [classified.resume_raw_path, classified.cover_letter_raw_path]

        raw_matches = self._find_matching_downloads([raw_root], company=company, role=role)
        if set(raw_matches) == {"resume", "cover_letter"}:
            resume_path = self._store_download_into_raw(
                raw_matches["resume"],
                raw_root,
                company=company,
                role=role,
                label="resume",
            )
            cover_letter_path = self._store_download_into_raw(
                raw_matches["cover_letter"],
                raw_root,
                company=company,
                role=role,
                label="cover_letter",
            )
            if self._pdf_has_magic_bytes(resume_path) and self._pdf_has_magic_bytes(cover_letter_path):
                return [resume_path, cover_letter_path]

        external_dirs = []
        for directory in self._download_watch_dirs(raw_root):
            try:
                same_dir = raw_root.resolve() == directory.resolve()
            except Exception:
                same_dir = raw_root == directory
            if same_dir:
                continue
            external_dirs.append(directory)

        latest_matches = self._find_matching_downloads(external_dirs, company=company, role=role)
        if set(latest_matches) != {"resume", "cover_letter"}:
            return None
        if not self._pdf_has_magic_bytes(latest_matches["resume"]) or not self._pdf_has_magic_bytes(latest_matches["cover_letter"]):
            return None
        resume_path = self._store_download_into_raw(
            latest_matches["resume"],
            raw_root,
            company=company,
            role=role,
            label="resume",
        )
        cover_letter_path = self._store_download_into_raw(
            latest_matches["cover_letter"],
            raw_root,
            company=company,
            role=role,
            label="cover_letter",
        )
        return [resume_path, cover_letter_path]

    async def _dismiss_obstructive_dialogs(self, page: Any) -> None:
        if page is None or not hasattr(page, "get_by_role"):
            return
        for label in ("Got it", "OK", "Okay"):
            try:
                locator = page.get_by_role("button", name=label)
            except Exception:
                continue
            try:
                count = await locator.count()
            except Exception:
                continue
            if count <= 0:
                continue
            button = locator.first if hasattr(locator, "first") else locator
            try:
                visible = await button.is_visible(timeout=500)
            except Exception:
                visible = True
            if not visible:
                continue
            try:
                await button.click(timeout=2_000)
                await anyio.sleep(0.2)
            except Exception:
                continue

    async def _download_pdfs(
        self,
        page: Any,
        turn_locator: Any,
        raw_root: Path,
        *,
        company: str,
        role: str,
        use_temporary_chat: bool = False,
    ) -> list[Path]:
        # The ChatGPT custom GPT places clickable elements (buttons / links)
        # between the [[PDF_OUTPUT_READY]] and [[PDF_OUTPUT_COMPLETE]] markers.
        # One contains the word "resume", the other "cover".  We locate them by
        # scanning every clickable child of the turn for those keywords, then
        # click each one and wait for the real PDF to land on disk.
        #
        # Playwright's expect_download() does NOT work reliably over CDP for
        # ChatGPT's JavaScript-generated blob downloads — it saves a UUID-named
        # temp artifact instead of the real PDF.  Instead we use Chrome's CDP
        # Page.setDownloadBehavior to direct downloads to a known folder, click
        # the buttons, then poll for .pdf files to appear on disk.

        # Tell Chrome to save downloads into raw_root via CDP.
        try:
            cdp_session = await page.context.new_cdp_session(page)
            await cdp_session.send("Page.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": str(raw_root.resolve()),
            })
        except Exception:
            # Fallback: try via the page's own client if available
            try:
                await page.evaluate("() => {}")  # no-op to ensure page is ready
            except Exception:
                pass

        watch_dirs = self._download_watch_dirs(raw_root)
        download_timeout = float(self.drafting.download_timeout_seconds)
        click_timeout = min(download_timeout, 20.0)
        claim_key = str(raw_root.resolve())
        current_turn = {
            "locator": turn_locator,
            "text": str(await turn_locator.inner_text(timeout=3000) or ""),
        }
        last_error = "download_controls_unavailable"

        for recovery_round in range(3):
            if self._marked_turn_has_sandbox_paths(str(current_turn.get("text") or "")):
                current_turn = await self._request_downloadable_attachments(page, reason="sandbox_paths")

            before = self._snapshot_download_entries(watch_dirs)
            saved_paths: list[Path] = []
            round_failed = False

            for index, label in enumerate(("resume", "cover_letter")):
                await self._dismiss_obstructive_dialogs(page)
                current_turn["locator"], locator = await self._find_download_element_with_retry(page, current_turn["locator"], label)
                self._update_status(
                    phase="downloading_pdfs",
                    last_observation=f"Downloading the {label.replace('_', ' ')} PDF from ChatGPT.",
                )
                try:
                    await page.bring_to_front()
                    await anyio.sleep(0.5)
                except Exception:
                    pass
                try:
                    await locator.scroll_into_view_if_needed()
                except Exception:
                    pass
                await locator.click()
                new_pdf = await self._wait_for_new_pdf(
                    watch_dirs,
                    before,
                    raw_root=raw_root,
                    company=company,
                    role=role,
                    label=label,
                    claim_key=claim_key,
                    timeout=click_timeout,
                )
                if new_pdf is None:
                    last_error = f"download_timed_out:{label}"
                    round_failed = True
                    break
                stored_pdf = self._store_download_into_raw(
                    new_pdf,
                    raw_root,
                    company=company,
                    role=role,
                    label=label,
                )
                saved_paths.append(stored_pdf)
                before = self._snapshot_download_entries(watch_dirs)
                before[stored_pdf.resolve()] = self._entry_signature(stored_pdf)
                if index == 0:
                    # Give Chrome time to finalize the first blob download before
                    # reacquiring and clicking the second control.
                    await anyio.sleep(5)

            if not round_failed:
                classified = classify_downloads(saved_paths)
                return [classified.resume_raw_path, classified.cover_letter_raw_path]

            if use_temporary_chat:
                raise RuntimeError(last_error)
            if recovery_round == 2:
                break
            current_turn = await self._request_downloadable_attachments(page, reason=last_error)

        raise RuntimeError(last_error)

    async def _wait_for_new_pdf(
        self,
        directories: list[Path],
        known: dict[Path, tuple[int, int] | None],
        *,
        raw_root: Path,
        company: str,
        role: str,
        label: str,
        claim_key: str,
        timeout: float,
    ) -> Path | None:
        """Poll the configured download dirs until a new PDF appears."""
        deadline = time.monotonic() + timeout
        allow_generic_capture = self._serial_download_capture_mode()
        try:
            raw_root_resolved = raw_root.resolve()
        except Exception:
            raw_root_resolved = raw_root
        while time.monotonic() < deadline:
            await anyio.sleep(2)
            generic_raw_candidates: list[tuple[Path, tuple[int, int]]] = []
            generic_external_candidates: list[tuple[Path, tuple[int, int]]] = []
            for directory in directories:
                if not directory.exists() or not directory.is_dir():
                    continue
                try:
                    directory_resolved = directory.resolve()
                except Exception:
                    directory_resolved = directory
                try:
                    entries = list(directory.iterdir())
                except Exception:
                    continue
                for entry in entries:
                    try:
                        resolved = entry.resolve()
                    except Exception:
                        resolved = entry
                    if not entry.is_file():
                        continue
                    name_lower = entry.name.casefold()
                    if name_lower.endswith(".crdownload") or name_lower.endswith(".tmp"):
                        continue
                    if not name_lower.endswith(".pdf"):
                        continue
                    current_signature = self._entry_signature(entry)
                    if current_signature is None:
                        continue
                    if known.get(resolved) == current_signature:
                        continue
                    try:
                        size1 = entry.stat().st_size
                    except OSError:
                        continue
                    await anyio.sleep(1)
                    try:
                        if not entry.exists():
                            continue
                        if entry.stat().st_size != size1 or size1 <= 256:
                            continue
                    except OSError:
                        continue
                    current_signature = self._entry_signature(entry)
                    if current_signature is None:
                        continue
                    if self._download_matches_job(entry, company=company, role=role, label=label):
                        if not self._claim_download_candidate(entry, signature=current_signature, claim_key=claim_key):
                            continue
                        return resolved
                    if not allow_generic_capture or not self._pdf_has_magic_bytes(entry):
                        continue
                    candidate = (resolved, current_signature)
                    if directory_resolved == raw_root_resolved:
                        generic_raw_candidates.append(candidate)
                    else:
                        generic_external_candidates.append(candidate)
            for candidates in (generic_raw_candidates, generic_external_candidates):
                unique_candidates: list[tuple[Path, tuple[int, int]]] = []
                seen_candidate_keys: set[tuple[str, tuple[int, int]]] = set()
                for candidate_path, candidate_signature in candidates:
                    try:
                        candidate_key = (str(candidate_path.resolve()).casefold(), candidate_signature)
                    except Exception:
                        candidate_key = (str(candidate_path).casefold(), candidate_signature)
                    if candidate_key in seen_candidate_keys:
                        continue
                    seen_candidate_keys.add(candidate_key)
                    unique_candidates.append((candidate_path, candidate_signature))
                if len(unique_candidates) != 1:
                    continue
                candidate_path, candidate_signature = unique_candidates[0]
                if not self._claim_download_candidate(
                    candidate_path,
                    signature=candidate_signature,
                    claim_key=claim_key,
                ):
                    continue
                return candidate_path
        return None

    def _marked_turn_has_sandbox_paths(self, text: str) -> bool:
        lowered = str(text or "").casefold()
        return any(
            token in lowered
            for token in (
                "sandbox:/mnt/data/",
                "/mnt/data/",
                "failed to get upload status",
            )
        )

    async def _stabilize_turn_text(self, turn: dict[str, Any], *, rounds: int = 6) -> dict[str, Any]:
        prev_len = len(str(turn.get("text") or ""))
        stable = 0
        for _ in range(rounds):
            await anyio.sleep(3)
            try:
                fresh = str(await turn["locator"].inner_text(timeout=3000) or "")
            except Exception:
                break
            if len(fresh) == prev_len:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            prev_len = len(fresh)
            turn["text"] = fresh
        return turn

    async def _request_downloadable_attachments(self, page: Any, *, reason: str) -> dict[str, Any]:
        await self._dismiss_obstructive_dialogs(page)
        self._update_status(
            phase="recovering_download_links",
            last_observation=(
                "ChatGPT did not provide usable downloadable PDF attachments; "
                f"requesting a fresh attachment-only response ({reason})."
            ),
        )
        await self._submit_prompt(page, _RECOVER_ATTACHMENTS_PROMPT)
        retry_turn = await self._wait_for_completed_turn(page)
        retry_turn = await self._stabilize_turn_text(retry_turn)
        await anyio.sleep(5)
        return retry_turn

    async def _find_download_element_with_retry(self, page: Any, turn_locator: Any, label: str) -> tuple[Any, Any]:
        """Find one document download control, retrying in the same conversation if needed."""
        current_turn = turn_locator
        for recovery_round in range(3):
            for _attempt in range(10):
                locator = await self._find_marker_download_element(current_turn, label)
                if locator is not None:
                    return current_turn, locator
                latest_turn = await self._latest_marked_turn(page)
                if latest_turn is not None:
                    current_turn = latest_turn["locator"]
                await anyio.sleep(3)
            if recovery_round == 2:
                break

            self._update_status(
                phase="recovering_download_links",
                last_observation=(
                    f"ChatGPT did not expose the {label.replace('_', ' ')} download control; "
                    f"requesting the links again in the same conversation (retry {recovery_round + 1}/2)."
                ),
            )
            await self._submit_prompt(page, _RETRY_ATTACHMENTS_PROMPT)
            await anyio.sleep(5)

            retry_turn = await self._wait_for_completed_turn(page)
            retry_turn = await self._stabilize_turn_text(retry_turn)
            await anyio.sleep(5)
            current_turn = retry_turn["locator"]

        raise RuntimeError(f"missing_download_link:{label}")

    async def _find_marker_download_elements(self, turn_locator: Any) -> tuple[Any | None, Any | None]:
        """Find the 'resume' and 'cover' clickable elements inside the marked block."""
        return (
            await self._find_marker_download_element(turn_locator, "resume"),
            await self._find_marker_download_element(turn_locator, "cover_letter"),
        )

    async def _find_marker_download_element(self, turn_locator: Any, label: str) -> Any | None:
        # Scan all clickable element types for text containing "resume" or "cover"
        for selector in ("button", "a", "[role='link']", "[role='button']", "span[class*='btn']", "div[role='button']"):
            locator = turn_locator.locator(selector)
            try:
                count = await locator.count()
            except Exception:
                continue
            for index in range(count):
                item = locator.nth(index)
                try:
                    descriptor = await item.evaluate(
                        """
                        (el) => ({
                          text: (el.innerText || el.textContent || '').trim(),
                          aria: (el.getAttribute('aria-label') || '').trim(),
                          title: (el.getAttribute('title') || '').trim(),
                          download: (el.getAttribute('download') || '').trim(),
                        })
                        """
                    )
                except Exception:
                    continue
                haystack = " ".join(
                    str(descriptor.get(key) or "").strip()
                    for key in ("text", "aria", "title", "download")
                ).casefold()
                if not haystack:
                    continue
                if label == "resume" and "resume" in haystack:
                    return item
                if label == "cover_letter" and ("cover letter" in haystack or "cover" in haystack):
                    return item
        return None

    async def _find_visible_locator(self, page: Any, selectors: tuple[str, ...], *, timeout_ms: int) -> Any:
        deadline = time.monotonic() + (timeout_ms / 1000)
        while time.monotonic() < deadline:
            candidate = await self._try_find_visible_locator(page, selectors, timeout_ms=1000)
            if candidate is not None:
                return candidate
        raise RuntimeError(f"Unable to find a visible locator for selectors: {selectors!r}")

    async def _try_find_visible_locator(self, page: Any, selectors: tuple[str, ...], *, timeout_ms: int) -> Any | None:
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                await locator.wait_for(state="visible", timeout=timeout_ms)
                return locator
            except Exception:
                continue
        return None

    def _normalize_artifacts(
        self,
        *,
        raw_paths: list[Path],
        company: str,
        role: str,
        application_id: str,
        on_date: str | None,
    ) -> dict[str, Path]:
        classified = classify_downloads(raw_paths)
        pdf_path = self.workspace.resume_pdf_path_for(application_id, company, on_date)
        cover_letter_path = self.workspace.output_dir / f"cover-letter-{application_id}-{slugify(company)}.pdf"
        shutil.copyfile(classified.resume_raw_path, pdf_path)
        shutil.copyfile(classified.cover_letter_raw_path, cover_letter_path)

        from findmyjob.documents.pipeline import DocumentPipeline, DocumentTemplateConfig

        pipeline = DocumentPipeline(
            artifacts_dir=self.workspace.output_dir,
            template_dir=self.workspace.root / "templates" / "typst",
            template_config=DocumentTemplateConfig(resume_renderer="chatgpt_download"),
        )
        context = _validation_context(ws=self.workspace, company=company, role=role)
        resume_pdf = _pdf_artifact_from_existing(pipeline, path=pdf_path, context=context, expect_one_page=True)
        if not resume_pdf.validation_results.get("valid"):
            raise RuntimeError(f"resume_pdf_invalid:{resume_pdf.validation_results.get('failure_reason')}")
        cover_letter_pdf = _pdf_artifact_from_existing(pipeline, path=cover_letter_path, context=context, expect_one_page=True)
        if not cover_letter_pdf.validation_results.get("valid"):
            raise RuntimeError(f"cover_letter_pdf_invalid:{cover_letter_pdf.validation_results.get('failure_reason')}")

        base_name = pdf_path.stem
        resume_text = pipeline.write_resume_text_from_pdf(base_name, resume_pdf, context)
        if not resume_text.validation_results.get("valid"):
            raise RuntimeError(f"resume_text_invalid:{resume_text.validation_results.get('failure_reason')}")
        cover_letter_text = pipeline.write_cover_letter_text_from_pdf(base_name, cover_letter_pdf, context)
        if not cover_letter_text.validation_results.get("valid"):
            raise RuntimeError(f"cover_letter_text_invalid:{cover_letter_text.validation_results.get('failure_reason')}")

        return {
            "pdf_path": pdf_path,
            "cover_letter_path": cover_letter_path,
            "resume_text_path": resume_text.path,
            "cover_letter_text_path": cover_letter_text.path,
            "raw_resume_path": classified.resume_raw_path,
            "raw_cover_letter_path": classified.cover_letter_raw_path,
        }


__all__ = [
    "ChatGPTDraftingService",
    "ChatGPTDownloadedArtifacts",
    "build_chatgpt_prompt",
    "classify_downloads",
    "extract_marked_block",
    "local_date_string",
]
