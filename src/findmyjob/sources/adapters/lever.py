from __future__ import annotations

from pathlib import Path

import httpx

from findmyjob.apply.browser import PlaywrightSubmitter
from findmyjob.apply.forms import extract_questions_from_lever_fields
from findmyjob.core.enums import PolicyMode, QuestionType, SourceKind, SourceRisk
from findmyjob.core.retry import http_get_with_retry
from findmyjob.core.filtering import evaluate_job_against_query
from findmyjob.core.types import ArtifactBinding, FormFieldBinding, NormalizedJobPosting, SourceCapabilities, SubmissionCapturePolicy, SubmissionPlan, SubmissionResult
from findmyjob.sources.base import (
    SourceAdapter,
    _artifact_companion_satisfied,
    _binding_metadata,
    _build_captcha_solver_from_notes,
    _generated_text_binding,
    _is_helper_question,
    _preferred_artifact_kinds_for_question,
    _resolve_option_values,
)
from findmyjob.sources.contracts import DiscoveryQuery, ExtractionResult
from findmyjob.sources.normalizer import build_normalized_job


class LeverAdapter(SourceAdapter):
    source_kind = SourceKind.LEVER
    policy_mode = PolicyMode.HUMAN_IN_LOOP_SUBMIT
    adapter_name = "lever"

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
        jobs: list[tuple[NormalizedJobPosting, dict]] = []
        for board in self.boards:
            response = await http_get_with_retry(
                client,
                f"https://api.lever.co/v0/postings/{board}",
                params={"mode": "json"},
            )
            payload = response.json()
            for item in payload:
                categories = item.get("categories", {})
                commitment = categories.get("commitment")
                compensation = item.get("salaryRange") if item.get("salaryRange") else None
                posting = build_normalized_job(
                    company_name=board,
                    title=item.get("text", "Untitled role"),
                    source=self.adapter_name,
                    source_kind=self.source_kind.value,
                    source_job_id=str(item.get("id")),
                    posting_url=item.get("hostedUrl"),
                    apply_url=item.get("applyUrl") or item.get("hostedUrl"),
                    location_raw=categories.get("location"),
                    employment_type=commitment,
                    compensation=compensation,
                    description=item.get("descriptionPlain") or item.get("description") or "",
                    notes={"board": board, "team": categories.get("team"), "capabilities": self.capabilities().model_dump(mode="json")},
                )
                if not self._matches_query(posting, query):
                    continue
                jobs.append((posting, item))
        return jobs

    async def load_application_contract(self, client: httpx.AsyncClient, job: NormalizedJobPosting) -> ExtractionResult:
        submitter = self._submitter_for_job(job)
        field_rows = await submitter.inspect_lever_form(str(job.apply_url or job.posting_url))
        return extract_questions_from_lever_fields(field_rows, handoff_url=str(job.apply_url or job.posting_url))

    def bind_answers(self, job: NormalizedJobPosting, question_answers, artifacts_by_kind: dict[str, str]) -> SubmissionPlan:
        fields: list[FormFieldBinding] = []
        missing: list[str] = []
        notes: list[str] = []
        for question, answer in question_answers:
            binding = self._binding_for_question(question, answer, artifacts_by_kind)
            if binding is None:
                if question.required and not _is_helper_question(question) and not _artifact_companion_satisfied(question, artifacts_by_kind):
                    missing.append(question.prompt_text)
                continue
            fields.append(binding)
        if missing:
            notes.append("Missing required bound fields")
        return SubmissionPlan(
            source_kind=job.source_kind,
            application_url=str(job.apply_url or job.posting_url),
            fields=fields,
            missing_required_fields=missing,
            notes=notes,
        )

    async def execute_submission(self, job: NormalizedJobPosting, plan: SubmissionPlan, output_dir: Path) -> SubmissionResult:
        submitter = self._submitter_for_job(job)
        return await submitter.submit_lever(str(job.apply_url or job.posting_url), plan, output_dir)

    async def preview_submission(
        self,
        job: NormalizedJobPosting,
        plan: SubmissionPlan,
        output_dir: Path,
        *,
        keep_browser_open: bool = False,
    ) -> SubmissionResult:
        submitter = self._submitter_for_job(job)
        return await submitter.preview_generic_form(str(job.apply_url or job.posting_url), plan, output_dir, keep_browser_open=keep_browser_open)

    def _submitter_for_job(self, job: NormalizedJobPosting) -> PlaywrightSubmitter:
        captcha_strategy, captcha_solver = _build_captcha_solver_from_notes(job.notes)
        return PlaywrightSubmitter(
            timeout_seconds=int(job.notes.get("browser_timeout_seconds") or 30),
            capture_policy=SubmissionCapturePolicy.model_validate(job.notes.get("capture_policy") or {}),
            browser_attach_enabled=bool(job.notes.get("browser_attach_enabled", False)),
            browser_cdp_url=job.notes.get("browser_cdp_url"),
            browser_mode=str(job.notes.get("browser_mode") or "headless"),
            max_open_tabs=int(job.notes.get("max_open_tabs") or 10),
            captcha_strategy=captcha_strategy,
            captcha_solver=captcha_solver,
        )

    def _binding_for_question(self, question, answer, artifacts_by_kind: dict[str, str]) -> FormFieldBinding | None:
        option_details = list(getattr(question, "option_details", None) or [])
        section = getattr(question, "section", None)
        sensitive = getattr(question, "sensitive", None)
        metadata = _binding_metadata(question, option_details, section, bool(sensitive))

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
                metadata=metadata,
            )
        generated_text_binding = _generated_text_binding(question, artifacts_by_kind, option_details, section, bool(sensitive))
        if generated_text_binding is not None:
            return generated_text_binding
        if answer is None or answer.needs_user_input:
            return None
        raw_answer = answer.candidate_answer or ""
        option_values, labels = _resolve_option_values(raw_answer, option_details)
        return FormFieldBinding(
            source_field_name=question.source_field_name,
            widget_type=getattr(question, "widget_type", "text"),
            prompt_text=question.prompt_text,
            required=question.required,
            value=raw_answer,
            values=labels if len(labels) > 1 else [],
            option_value=option_values[0] if len(option_values) == 1 else None,
            option_values=option_values if len(option_values) > 1 else [],
            metadata=metadata,
        )

    def _matches_query(self, job: NormalizedJobPosting, query: DiscoveryQuery) -> bool:
        return evaluate_job_against_query(job, query).matched

