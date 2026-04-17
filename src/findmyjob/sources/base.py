from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from findmyjob.core.enums import ArtifactKind
from findmyjob.core.types import FormFieldBinding, NormalizedJobPosting, SourceCapabilities, SubmissionCapturePolicy, SubmissionPlan, SubmissionResult
from findmyjob.sources.contracts import DiscoveryQuery, ExtractionResult


def _build_captcha_solver_from_notes(notes: dict[str, Any]):
    """Build a CaptchaSolver from job runtime notes if strategy is 'solve' and API key is available."""
    from findmyjob.apply.captcha import CaptchaSolver
    strategy = str(notes.get("captcha_strategy") or "skip").strip().lower()
    if strategy != "solve":
        return strategy, None
    api_key_env = str(notes.get("captcha_api_key_env") or "CAPTCHA_API_KEY")
    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key:
        return strategy, None
    provider = str(notes.get("captcha_provider") or "2captcha").strip().lower()
    timeout = int(notes.get("captcha_solve_timeout") or 300)
    return strategy, CaptchaSolver(api_key=api_key, provider=provider, timeout=float(timeout))


def _preferred_artifact_kinds_for_question(question: Any) -> tuple[ArtifactKind, ArtifactKind]:
    binding = dict(getattr(question, "submission_binding", {}) or {})
    descriptor = " ".join(
        str(part or "").strip().lower()
        for part in (
            getattr(question, "prompt_text", ""),
            getattr(question, "source_field_name", ""),
            getattr(question, "normalized_key", ""),
            binding.get("name"),
            binding.get("id"),
            binding.get("group"),
        )
    )
    preferred_kind = ArtifactKind.COVER_LETTER_PDF if "cover" in descriptor else ArtifactKind.RESUME_PDF
    fallback_kind = ArtifactKind.COVER_LETTER_TEXT if preferred_kind == ArtifactKind.COVER_LETTER_PDF else ArtifactKind.RESUME_TEXT
    return preferred_kind, fallback_kind


def _binding_metadata(question: Any, option_details: list[dict[str, Any]], section: str | None, sensitive: bool) -> dict[str, object]:
    return {
        "source_snapshot_ref": getattr(question, "source_snapshot_ref", None),
        "option_details": list(option_details),
        "section": section,
        "sensitive": sensitive,
        "question_type": getattr(getattr(question, "question_type", None), "value", None),
        "normalized_key": getattr(question, "normalized_key", None),
        "file_constraints": dict(getattr(question, "file_constraints", {}) or {}),
        "submission_binding": dict(getattr(question, "submission_binding", {}) or {}),
    }


def _read_text_artifact(artifact_path: str) -> str:
    candidate = Path(artifact_path)
    if not candidate.exists() or not candidate.is_file():
        return ""
    raw = candidate.read_text(encoding="utf-8", errors="ignore")
    if candidate.suffix.lower() in {".html", ".htm"}:
        raw = re.sub(r"<[^>]+>", " ", raw)
    raw = re.sub(r"\r\n?", "\n", raw)
    raw = re.sub(r"[ \t]+", " ", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    text = raw.strip()
    if len(text) > 12000:
        text = text[:12000].rsplit(" ", 1)[0].rstrip()
    return text


def _normalize_option(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _match_options(chunks: list[str], option_details: list[dict[str, Any]]) -> list[dict[str, str]]:
    resolved: list[dict[str, str]] = []
    for chunk in chunks:
        for option in option_details:
            label = str(option.get("label") or "")
            value = str(option.get("value") or option.get("id") or label)
            if chunk in {_normalize_option(label), _normalize_option(value)}:
                candidate = {"label": label or value, "value": value}
                if candidate not in resolved:
                    resolved.append(candidate)
                break
    return resolved


def _resolve_option_values(raw_answer: str, option_details: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    normalized = _normalize_option(raw_answer)
    if not option_details or not normalized:
        return [], []
    matched = _match_options([normalized], option_details)
    if matched:
        return [item["value"] for item in matched], [item["label"] for item in matched]
    chunks = [_normalize_option(part) for part in raw_answer.replace("|", ",").replace(";", ",").split(",") if part.strip()]
    matched = _match_options(chunks, option_details)
    return [item["value"] for item in matched], [item["label"] for item in matched]


def _generated_text_binding(
    question: Any,
    artifacts_by_kind: dict[str, str],
    option_details: list[dict[str, Any]],
    section: str | None,
    sensitive: bool,
) -> FormFieldBinding | None:
    lowered = f"{getattr(question, 'prompt_text', '')} {getattr(question, 'source_field_name', '')} {getattr(question, 'normalized_key', '') or ''}".lower()
    widget_type = str(getattr(question, "widget_type", "") or "")
    source_field_name = str(getattr(question, "source_field_name", "") or "")
    if widget_type != "textarea" and not source_field_name.endswith("_text"):
        return None
    artifact_path = None
    if "resume" in lowered:
        artifact_path = artifacts_by_kind.get(ArtifactKind.RESUME_TEXT.value)
    elif "cover" in lowered:
        artifact_path = artifacts_by_kind.get(ArtifactKind.COVER_LETTER_TEXT.value)
    if not artifact_path:
        return None
    text_value = _read_text_artifact(artifact_path)
    if not text_value:
        return None
    return FormFieldBinding(
        source_field_name=question.source_field_name,
        widget_type=getattr(question, "widget_type", "textarea"),
        prompt_text=question.prompt_text,
        required=question.required,
        value=text_value,
        metadata=_binding_metadata(question, option_details, section, bool(sensitive)),
    )


def _artifact_companion_satisfied(question: Any, artifacts_by_kind: dict[str, str]) -> bool:
    lowered = f"{getattr(question, 'prompt_text', '')} {getattr(question, 'source_field_name', '')} {getattr(question, 'normalized_key', '') or ''}".lower()
    widget_type = str(getattr(question, "widget_type", "") or "")
    source_field_name = str(getattr(question, "source_field_name", "") or "")
    if widget_type != "textarea" and not source_field_name.endswith("_text"):
        return False
    if "resume" in lowered:
        return bool(artifacts_by_kind.get(ArtifactKind.RESUME_PDF.value) or artifacts_by_kind.get(ArtifactKind.RESUME_TEXT.value))
    if "cover" in lowered:
        return bool(artifacts_by_kind.get(ArtifactKind.COVER_LETTER_PDF.value) or artifacts_by_kind.get(ArtifactKind.COVER_LETTER_TEXT.value))
    return False


def _is_helper_question(question: Any) -> bool:
    submission_binding = dict(getattr(question, "submission_binding", {}) or {})
    raw_type = str(submission_binding.get("raw_type") or "").strip().lower()
    widget_type = str(getattr(question, "widget_type", "") or "").strip().lower()
    section = str(getattr(question, "section", None) or "").strip().lower()
    source_field_name = str(
        getattr(question, "source_field_name", "")
        or submission_binding.get("name")
        or submission_binding.get("id")
        or ""
    ).strip().lower()
    normalized_key = str(getattr(question, "normalized_key", "") or "").strip().lower()
    input_role = str(getattr(question, "input_role", "") or "").strip().lower()
    if input_role == "helper":
        return True
    if raw_type in {"hidden", "input_hidden"} or widget_type == "hidden":
        return True
    if source_field_name in {
        "latitude",
        "longitude",
        "lat",
        "lng",
        "location_lat",
        "location_lng",
        "g-recaptcha-response",
        "h-captcha-response",
        "cf-turnstile-response",
    }:
        return True
    if section == "location" and normalized_key in {"latitude", "longitude"}:
        return True
    return False


class SourceAdapter(ABC):
    adapter_name: str

    def __init__(self, boards: Sequence[str]) -> None:
        self.boards = list(boards)

    @abstractmethod
    def capabilities(self) -> SourceCapabilities:
        raise NotImplementedError

    @abstractmethod
    async def discover(self, client: httpx.AsyncClient, query: DiscoveryQuery) -> list[tuple[NormalizedJobPosting, dict]]:
        raise NotImplementedError

    def translate_filters(self, query: DiscoveryQuery) -> dict[str, Any]:
        return {
            "title_keywords": query.title_keywords,
            "locations": query.locations,
            "countries": query.countries,
            "regions": query.regions,
            "cities": query.cities,
            "workplace_types": query.workplace_types,
            "employment_types": query.employment_types,
            "location_scopes": query.location_scopes,
            "experience_levels": query.experience_levels,
            "company_size_buckets": query.company_size_buckets,
            "posted_within_days": query.posted_within_days,
            "compensation_min": query.compensation_min,
            "compensation_currency": query.compensation_currency,
            "requires_future_sponsorship": query.requires_future_sponsorship,
            "remote_only": query.remote_only,
            "allow_unknown_compensation": query.allow_unknown_compensation,
            "allow_unknown_experience_level": query.allow_unknown_experience_level,
        }

    async def load_application_contract(self, client: httpx.AsyncClient, job: NormalizedJobPosting) -> ExtractionResult:
        url = str(job.apply_url or job.posting_url or "").strip()
        if not url:
            return ExtractionResult(handoff_url=str(job.apply_url or job.posting_url))
        from findmyjob.apply.browser import PlaywrightSubmitter
        from findmyjob.apply.forms import extract_questions_from_lever_fields

        submitter = PlaywrightSubmitter(
            timeout_seconds=int(job.notes.get("browser_timeout_seconds") or 30),
            capture_policy=SubmissionCapturePolicy.model_validate(job.notes.get("capture_policy") or {}),
            browser_attach_enabled=bool(job.notes.get("browser_attach_enabled", False)),
            browser_cdp_url=job.notes.get("browser_cdp_url"),
            browser_mode=str(job.notes.get("browser_mode") or "headless"),
            max_open_tabs=int(job.notes.get("max_open_tabs") or 10),
        )
        try:
            field_rows = await submitter.inspect_form(url)
        except Exception:
            return ExtractionResult(handoff_url=url)
        return extract_questions_from_lever_fields(field_rows, handoff_url=url)

    async def extract_questions(self, client: httpx.AsyncClient, job: NormalizedJobPosting) -> ExtractionResult:
        return await self.load_application_contract(client, job)

    def bind_answers(self, job: NormalizedJobPosting, question_answers, artifacts_by_kind: dict[str, str]) -> SubmissionPlan:
        return SubmissionPlan(source_kind=job.source_kind, application_url=str(job.apply_url or job.posting_url))

    async def prepare_submission(self, job: NormalizedJobPosting, plan: SubmissionPlan, output_dir: Path) -> SubmissionPlan:
        return plan

    async def execute_submission(self, job: NormalizedJobPosting, plan: SubmissionPlan, output_dir: Path) -> SubmissionResult:
        if not self.capabilities().supports_apply:
            return SubmissionResult(
                status=job.lifecycle_status,
                submitted=False,
                uncertain=False,
                message="Submission is not supported for this adapter in the current environment.",
                plan=plan,
            )
        from findmyjob.apply.browser import PlaywrightSubmitter

        captcha_strategy, captcha_solver = _build_captcha_solver_from_notes(job.notes)
        submitter = PlaywrightSubmitter(
            timeout_seconds=int(job.notes.get("browser_timeout_seconds") or 30),
            capture_policy=SubmissionCapturePolicy.model_validate(job.notes.get("capture_policy") or {}),
            browser_attach_enabled=bool(job.notes.get("browser_attach_enabled", False)),
            browser_cdp_url=job.notes.get("browser_cdp_url"),
            browser_mode=str(job.notes.get("browser_mode") or "headless"),
            max_open_tabs=int(job.notes.get("max_open_tabs") or 10),
            captcha_strategy=captcha_strategy,
            captcha_solver=captcha_solver,
        )
        return await submitter.submit_generic_form(str(job.apply_url or job.posting_url), plan, output_dir)

    def verify_submission_result(self, result: SubmissionResult) -> SubmissionResult:
        return result

    async def submit(self, job: NormalizedJobPosting, plan: SubmissionPlan, output_dir: Path) -> SubmissionResult:
        prepared = await self.prepare_submission(job, plan, output_dir)
        result = await self.execute_submission(job, prepared, output_dir)
        return self.verify_submission_result(result)

    async def preview_submission(self, job: NormalizedJobPosting, plan: SubmissionPlan, output_dir: Path) -> SubmissionResult:
        if not self.capabilities().supports_apply:
            return SubmissionResult(
                status=job.lifecycle_status,
                submitted=False,
                uncertain=False,
                message="Preview is not supported for this adapter in the current environment.",
                plan=plan,
            )
        from findmyjob.apply.browser import PlaywrightSubmitter

        captcha_strategy, captcha_solver = _build_captcha_solver_from_notes(job.notes)
        submitter = PlaywrightSubmitter(
            timeout_seconds=int(job.notes.get("browser_timeout_seconds") or 30),
            capture_policy=SubmissionCapturePolicy.model_validate(job.notes.get("capture_policy") or {}),
            browser_attach_enabled=bool(job.notes.get("browser_attach_enabled", False)),
            browser_cdp_url=job.notes.get("browser_cdp_url"),
            browser_mode=str(job.notes.get("browser_mode") or "headless"),
            max_open_tabs=int(job.notes.get("max_open_tabs") or 10),
            captcha_strategy=captcha_strategy,
            captcha_solver=captcha_solver,
        )
        return await submitter.preview_generic_form(str(job.apply_url or job.posting_url), plan, output_dir)

    def manual_handoff_url(self, job: NormalizedJobPosting) -> str:
        return str(job.apply_url or job.posting_url)
