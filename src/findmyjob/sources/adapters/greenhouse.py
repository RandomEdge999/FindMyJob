from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse, urlunparse

import httpx

from findmyjob.apply.browser import PlaywrightSubmitter
from findmyjob.apply.forms import extract_questions_from_greenhouse_payload, extract_questions_from_lever_fields, merge_extraction_results
from findmyjob.core.enums import ArtifactKind, PolicyMode, QuestionType, SourceKind, SourceRisk
from findmyjob.core.filtering import evaluate_job_against_query
from findmyjob.core.retry import http_get_with_retry
from findmyjob.core.types import ArtifactBinding, FormFieldBinding, NormalizedJobPosting, SourceCapabilities, SubmissionCapturePolicy, SubmissionPlan, SubmissionResult
from findmyjob.sources.base import SourceAdapter, _build_captcha_solver_from_notes, _preferred_artifact_kinds_for_question
from findmyjob.sources.contracts import DiscoveryQuery, ExtractionResult
from findmyjob.sources.normalizer import build_normalized_job, extract_posted_at, extract_source_updated_at


class GreenhouseAdapter(SourceAdapter):
    source_kind = SourceKind.GREENHOUSE
    policy_mode = PolicyMode.HUMAN_IN_LOOP_SUBMIT
    adapter_name = "greenhouse"

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            adapter_name=self.adapter_name,
            source_kind=self.source_kind.value,
            policy_mode=self.policy_mode,
            risk=SourceRisk.LOW,
            supports_discovery=True,
            supports_apply=True,
            supports_auto_submit=True,
            supported_filters=[
                "title_keywords",
                "locations",
                "countries",
                "regions",
                "cities",
                "workplace_types",
                "employment_types",
                "location_scopes",
                "experience_levels",
                "posted_within_days",
                "compensation_min",
                "compensation_currency",
                "company_size_buckets",
                "remote_only",
                "requires_future_sponsorship",
            ],
            supported_question_types=[QuestionType.DETERMINISTIC, QuestionType.BOOLEAN, QuestionType.SELECT, QuestionType.NUMERIC, QuestionType.DATE, QuestionType.FILE],
        )

    async def discover(self, client: httpx.AsyncClient, query: DiscoveryQuery) -> list[tuple[NormalizedJobPosting, dict]]:
        results: list[tuple[NormalizedJobPosting, dict]] = []
        for board in self.boards:
            response = await http_get_with_retry(
                client,
                f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs",
                params={"content": "true"},
            )
            payload = response.json()
            items = payload if isinstance(payload, list) else list(payload.get("jobs", []))
            for item in items:
                if not isinstance(item, dict):
                    continue
                description = item.get("content") or ""
                absolute_url = item.get("absolute_url")
                posting = build_normalized_job(
                    company_name=board,
                    title=item.get("title", "Untitled role"),
                    source=self.adapter_name,
                    source_kind=self.source_kind.value,
                    source_job_id=str(item.get("id")),
                    posting_url=self._greenhouse_url(board, absolute_url, item.get("id"), fragment=None),
                    apply_url=self._greenhouse_url(board, absolute_url, item.get("id"), fragment="application"),
                    location_raw=self._greenhouse_location_text(item),
                    employment_type=self._employment_type(item),
                    compensation=item.get("pay_input_ranges"),
                    description=description,
                    posted_at=extract_posted_at(item),
                    source_updated_at=extract_source_updated_at(item),
                    notes={"board": board, "capabilities": self.capabilities().model_dump(mode="json")},
                )
                if not self._matches_query(posting, query):
                    continue
                results.append((posting, item))
        return results

    async def load_application_contract(self, client: httpx.AsyncClient, job: NormalizedJobPosting) -> ExtractionResult:
        board = str(job.notes.get("board") or job.company_key)
        response = await http_get_with_retry(
            client,
            f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job.source_job_id}",
            params={"questions": "true"},
        )
        payload = response.json()
        if isinstance(payload, list):
            payload = next((item for item in payload if isinstance(item, dict)), {})
        elif not isinstance(payload, dict):
            payload = {}
        handoff_url = self._greenhouse_url(board, str(job.apply_url or job.posting_url), job.source_job_id, fragment="application")
        extracted = extract_questions_from_greenhouse_payload(payload, handoff_url=handoff_url)
        try:
            submitter = self._submitter_for_job(job)
            inspected_rows = await submitter.inspect_form(handoff_url)
            fallback = extract_questions_from_lever_fields(inspected_rows, handoff_url=handoff_url)
            extracted = merge_extraction_results(extracted, fallback)
        except Exception:
            pass
        return extracted

    def bind_answers(self, job: NormalizedJobPosting, question_answers, artifacts_by_kind: dict[str, str]) -> SubmissionPlan:
        fields: list[FormFieldBinding] = []
        missing: list[str] = []
        notes: list[str] = []
        for question, answer in question_answers:
            binding = self._binding_for_question(question, answer, artifacts_by_kind)
            if binding is None:
                if question.required and not self._is_helper_question(question) and not self._artifact_companion_satisfied(question, artifacts_by_kind):
                    missing.append(question.prompt_text)
                continue
            fields.append(binding)
        if missing:
            notes.append("Missing required bound fields")
        board = str(job.notes.get("board") or job.company_key)
        return SubmissionPlan(
            source_kind=job.source_kind,
            application_url=self._greenhouse_url(board, str(job.apply_url or job.posting_url), job.source_job_id, fragment="application"),
            fields=fields,
            missing_required_fields=missing,
            notes=notes,
        )

    async def execute_submission(self, job: NormalizedJobPosting, plan: SubmissionPlan, output_dir: Path) -> SubmissionResult:
        submitter = self._submitter_for_job(job)
        board = str(job.notes.get("board") or job.company_key)
        application_url = self._greenhouse_url(board, str(job.apply_url or job.posting_url), job.source_job_id, fragment="application")
        return await submitter.submit_greenhouse(application_url, plan, output_dir)

    async def preview_submission(
        self,
        job: NormalizedJobPosting,
        plan: SubmissionPlan,
        output_dir: Path,
        *,
        keep_browser_open: bool = False,
    ) -> SubmissionResult:
        submitter = self._submitter_for_job(job)
        board = str(job.notes.get("board") or job.company_key)
        application_url = self._greenhouse_url(board, str(job.apply_url or job.posting_url), job.source_job_id, fragment="application")
        return await submitter.preview_greenhouse(application_url, plan, output_dir, keep_browser_open=keep_browser_open)

    def _submitter_for_job(self, job: NormalizedJobPosting) -> PlaywrightSubmitter:
        timeout_seconds = int(job.notes.get("browser_timeout_seconds") or 30)
        capture_policy = SubmissionCapturePolicy.model_validate(job.notes.get("capture_policy") or {})
        captcha_strategy, captcha_solver = _build_captcha_solver_from_notes(job.notes)
        return PlaywrightSubmitter(
            timeout_seconds=timeout_seconds,
            capture_policy=capture_policy,
            browser_attach_enabled=bool(job.notes.get("browser_attach_enabled", False)),
            browser_cdp_url=job.notes.get("browser_cdp_url"),
            browser_mode=str(job.notes.get("browser_mode") or "headless"),
            max_open_tabs=int(job.notes.get("max_open_tabs") or 10),
            captcha_strategy=captcha_strategy,
            captcha_solver=captcha_solver,
        )

    def _greenhouse_url(self, board: str | None, raw_url: str | None, source_job_id: str | None, *, fragment: str | None) -> str:
        value = str(raw_url or "").strip()
        board_token = str(board or "").strip().strip("/")
        job_id = str(source_job_id or "").strip()
        parsed = urlparse(value) if value else None
        if parsed is not None and parsed.query:
            gh_jid = parse_qs(parsed.query).get("gh_jid", [None])[0]
            if gh_jid:
                job_id = str(gh_jid).strip() or job_id
        host = (parsed.netloc or "").lower() if parsed is not None else ""
        if board_token and job_id and (host.endswith("greenhouse.io") or host.endswith("careerpuck.com") or host == ""):
            base = f"https://job-boards.greenhouse.io/{board_token}/jobs/{job_id}"
            return f"{base}#{fragment}" if fragment else base
        if parsed is not None:
            return urlunparse(parsed._replace(fragment=fragment or ""))
        if value:
            return f"{value}#{fragment}" if fragment else value
        base = f"https://job-boards.greenhouse.io/{board_token}/jobs/{job_id}"
        return f"{base}#{fragment}" if fragment else base

    def _binding_for_question(self, question, answer, artifacts_by_kind: dict[str, str]) -> FormFieldBinding | None:
        field_config = getattr(question, "field_config", None) or {}
        if self._is_helper_question(question):
            return None
        option_details = list(getattr(question, "option_details", None) or field_config.get("option_details", []))
        section = getattr(question, "section", None) or field_config.get("section")
        sensitive = getattr(question, "sensitive", None)
        if sensitive is None:
            sensitive = bool(field_config.get("sensitive"))

        if question.question_type.value == "file":
            preferred_kind, fallback_kind = _preferred_artifact_kinds_for_question(question)
            artifact_path = artifacts_by_kind.get(preferred_kind.value) or artifacts_by_kind.get(fallback_kind.value)
            if not artifact_path:
                return None
            mime_type = "application/pdf" if artifact_path.endswith(".pdf") else "text/plain"
            return FormFieldBinding(
                source_field_name=question.source_field_name,
                widget_type=getattr(question, "widget_type", "file"),
                prompt_text=question.prompt_text,
                required=question.required,
                artifact_binding=ArtifactBinding(
                    artifact_kind=preferred_kind if artifact_path.endswith(".pdf") else fallback_kind,
                    source_artifact_kind=preferred_kind,
                    path=artifact_path,
                    mime_type=mime_type,
                ),
                metadata=self._binding_metadata(question, option_details, section, bool(sensitive)),
            )

        generated_text_binding = self._generated_text_binding(question, artifacts_by_kind, option_details, section, bool(sensitive))
        if generated_text_binding is not None:
            return generated_text_binding

        if answer is None or answer.needs_user_input:
            return None

        raw_answer = answer.candidate_answer or ""
        option_values, labels = self._resolve_option_values(raw_answer, option_details)
        return FormFieldBinding(
            source_field_name=question.source_field_name,
            widget_type=getattr(question, "widget_type", "text"),
            prompt_text=question.prompt_text,
            required=question.required,
            value=raw_answer,
            values=labels if len(labels) > 1 else [],
            option_value=option_values[0] if len(option_values) == 1 else None,
            option_values=option_values if len(option_values) > 1 else [],
            metadata=self._binding_metadata(question, option_details, section, bool(sensitive)),
        )

    def _binding_metadata(self, question, option_details: list[dict], section: str | None, sensitive: bool) -> dict[str, object]:
        field_config = getattr(question, "field_config", None) or {}
        return {
            "source_snapshot_ref": question.source_snapshot_ref,
            "option_details": list(option_details),
            "section": section,
            "sensitive": sensitive,
            "question_type": question.question_type.value,
            "normalized_key": question.normalized_key,
            "file_constraints": dict(getattr(question, "file_constraints", {}) or {}),
            "submission_binding": dict(field_config.get("submission_binding") or getattr(question, "submission_binding", {}) or {}),
        }

    def _artifact_companion_satisfied(self, question, artifacts_by_kind: dict[str, str]) -> bool:
        lowered = f"{question.prompt_text} {question.source_field_name} {getattr(question, 'normalized_key', '') or ''}".lower()
        widget_type = str(getattr(question, 'widget_type', '') or '')
        source_field_name = str(getattr(question, 'source_field_name', '') or '')
        if widget_type != 'textarea' and not source_field_name.endswith('_text'):
            return False
        if 'resume' in lowered:
            return bool(artifacts_by_kind.get(ArtifactKind.RESUME_PDF.value) or artifacts_by_kind.get(ArtifactKind.RESUME_TEXT.value))
        if 'cover' in lowered:
            return bool(artifacts_by_kind.get(ArtifactKind.COVER_LETTER_PDF.value) or artifacts_by_kind.get(ArtifactKind.COVER_LETTER_TEXT.value))
        return False

    def _generated_text_binding(self, question, artifacts_by_kind: dict[str, str], option_details: list[dict], section: str | None, sensitive: bool) -> FormFieldBinding | None:
        lowered = f"{question.prompt_text} {question.source_field_name} {getattr(question, 'normalized_key', '') or ''}".lower()
        widget_type = str(getattr(question, 'widget_type', '') or '')
        source_field_name = str(getattr(question, 'source_field_name', '') or '')
        if widget_type != 'textarea' and not source_field_name.endswith('_text'):
            return None
        artifact_path = None
        if 'resume' in lowered:
            artifact_path = artifacts_by_kind.get(ArtifactKind.RESUME_TEXT.value)
        elif 'cover' in lowered:
            artifact_path = artifacts_by_kind.get(ArtifactKind.COVER_LETTER_TEXT.value)
        if not artifact_path:
            return None
        text_value = self._read_text_artifact(artifact_path)
        if not text_value:
            return None
        return FormFieldBinding(
            source_field_name=question.source_field_name,
            widget_type=getattr(question, 'widget_type', 'textarea'),
            prompt_text=question.prompt_text,
            required=question.required,
            value=text_value,
            metadata=self._binding_metadata(question, option_details, section, bool(sensitive)),
        )

    def _read_text_artifact(self, artifact_path: str) -> str:
        candidate = Path(artifact_path)
        if not candidate.exists() or not candidate.is_file():
            return ''
        raw = candidate.read_text(encoding='utf-8', errors='ignore')
        if candidate.suffix.lower() in {'.html', '.htm'}:
            raw = re.sub(r'<[^>]+>', ' ', raw)
        raw = re.sub(r'\r\n?', '\n', raw)
        raw = re.sub(r'[ \t]+', ' ', raw)
        raw = re.sub(r'\n{3,}', '\n\n', raw)
        text = raw.strip()
        if len(text) > 12000:
            text = text[:12000].rsplit(' ', 1)[0].rstrip()
        return text

    def _resolve_option_values(self, raw_answer: str, option_details: list[dict]) -> tuple[list[str], list[str]]:
        normalized = self._normalize_option(raw_answer)
        if not option_details or not normalized:
            return [], []
        matched = self._match_options([normalized], option_details)
        if matched:
            return [item["value"] for item in matched], [item["label"] for item in matched]
        chunks = [self._normalize_option(part) for part in raw_answer.replace("|", ",").replace(";", ",").split(",") if part.strip()]
        matched = self._match_options(chunks, option_details)
        return [item["value"] for item in matched], [item["label"] for item in matched]

    def _match_options(self, chunks: list[str], option_details: list[dict]) -> list[dict[str, str]]:
        resolved: list[dict[str, str]] = []
        for chunk in chunks:
            for option in option_details:
                label = str(option.get("label") or "")
                value = str(option.get("value") or option.get("id") or label)
                if chunk in {self._normalize_option(label), self._normalize_option(value)}:
                    candidate = {"label": label or value, "value": value}
                    if candidate not in resolved:
                        resolved.append(candidate)
                    break
        return resolved

    @staticmethod
    def _normalize_option(value: str) -> str:
        return " ".join(str(value or "").strip().lower().split())

    def _is_helper_question(self, question) -> bool:
        field_config = getattr(question, "field_config", None) or {}
        if str(field_config.get("input_role") or "").strip().lower() == "helper":
            return True
        submission_binding = dict(field_config.get("submission_binding") or getattr(question, "submission_binding", {}) or {})
        raw_type = str(submission_binding.get("raw_type") or "").strip().lower()
        widget_type = str(getattr(question, "widget_type", "") or "").strip().lower()
        section = str(getattr(question, "section", None) or field_config.get("section") or "").strip().lower()
        source_field_name = str(
            getattr(question, "source_field_name", "")
            or submission_binding.get("name")
            or submission_binding.get("id")
            or ""
        ).strip().lower()
        normalized_key = str(getattr(question, "normalized_key", "") or "").strip().lower()
        if raw_type in {"hidden", "input_hidden"} or widget_type == "hidden":
            return True
        if source_field_name in {"latitude", "longitude", "lat", "lng", "location_lat", "location_lng"}:
            return True
        if section == "location" and normalized_key in {"latitude", "longitude"}:
            return True
        return False

    def _matches_query(self, job: NormalizedJobPosting, query: DiscoveryQuery) -> bool:
        return evaluate_job_against_query(job, query).matched

    def _employment_type(self, payload: dict[str, object] | object) -> str | None:
        if not isinstance(payload, dict):
            return None
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            return None
        value = metadata.get("employment_type")
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None
    def _greenhouse_location_text(self, payload: dict[str, object]) -> str | None:
        location = payload.get("location")
        if isinstance(location, dict):
            name = str(location.get("name") or "").strip()
            if name:
                return name
        elif isinstance(location, str) and location.strip():
            return location.strip()
        offices = [str(office.get("name") or "").strip() for office in payload.get("offices", []) if isinstance(office, dict) and str(office.get("name") or "").strip()]
        if offices:
            return " | ".join(offices)
        return None


