from __future__ import annotations

from pathlib import Path
from typing import Any

from findmyjob.core.enums import ArtifactKind, JobLifecycleStatus, PolicyMode, QuestionType
from findmyjob.core.types import SubmissionGateReport
from findmyjob.db.models import ApplicationRecord, JobPosting
from findmyjob.db.repositories import ApplicationRepository, JobRepository

_NON_BLOCKING_ARTIFACT_FAILURE_REASONS = {
    "plain_text_contains_unsupported_lines",
    "cover_letter_missing_required_fields",
    "resume_text_missing_contact_fields",
    "resume_text_empty",
}
_HELPER_FIELD_TOKENS = {
    "latitude",
    "longitude",
    "lat",
    "lng",
    "location_lat",
    "location_lng",
}


class ApplicationService:
    def __init__(self, job_repository: JobRepository, application_repository: ApplicationRepository) -> None:
        self.job_repository = job_repository
        self.application_repository = application_repository

    def submission_gate(
        self,
        job: JobPosting,
        application: ApplicationRecord,
        artifact_kinds: set[ArtifactKind],
        ungrounded_answers: list[str],
        source_policy: PolicyMode,
        missing_required_fields: list[str] | None = None,
        artifact_validation_failures: list[str] | None = None,
        low_confidence_answers: list[str] | None = None,
        required_artifacts: list[ArtifactKind] | None = None,
        warnings: list[str] | None = None,
    ) -> SubmissionGateReport:
        missing_artifacts: list[ArtifactKind] = []
        required = list(required_artifacts or [ArtifactKind.REVIEW_PACKET])
        for expected in required:
            if expected not in artifact_kinds:
                missing_artifacts.append(expected)

        duplicate_risk = self.job_repository.duplicate_exists(job.duplicate_cluster_key, exclude_job_id=job.id)
        source_flow_valid = source_policy in {PolicyMode.HUMAN_IN_LOOP_SUBMIT, PolicyMode.REVIEW_ONLY}
        combined_missing = list(missing_required_fields or [])
        if artifact_validation_failures:
            combined_missing.extend(artifact_validation_failures)
        return SubmissionGateReport(
            application_mode=application.mode,
            duplicate_risk=duplicate_risk,
            missing_artifacts=missing_artifacts,
            missing_required_fields=self._unique_labels(combined_missing),
            ungrounded_answers=self._unique_labels(ungrounded_answers),
            low_confidence_answers=self._unique_labels(list(low_confidence_answers or [])),
            warnings=self._unique_labels(list(warnings or [])),
            source_policy=source_policy,
            source_flow_valid=source_flow_valid,
        )

    def promote_for_review(self, application: ApplicationRecord, gate: SubmissionGateReport) -> JobLifecycleStatus:
        if gate.is_ready:
            application.status = JobLifecycleStatus.READY_FOR_REVIEW
            return JobLifecycleStatus.READY_FOR_REVIEW
        application.status = JobLifecycleStatus.NEEDS_USER_INPUT
        return JobLifecycleStatus.NEEDS_USER_INPUT

    def review_packet_path(self, workspace: Path, job_id: str) -> Path:
        return workspace / ".fmj" / "artifacts" / f"review-{job_id}.json"

    def submission_output_dir(self, workspace: Path, application_id: str) -> Path:
        return workspace / ".fmj" / "snapshots" / application_id

    def artifact_path_map(self, artifacts: list) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for artifact in artifacts:
            mapping[artifact.kind.value if hasattr(artifact.kind, "value") else str(artifact.kind)] = artifact.path
        return mapping

    def artifact_validation_failures(self, artifacts: list, *, blocking_only: bool = True) -> list[str]:
        failures: list[str] = []
        for artifact in artifacts:
            validation = artifact.validation_results or {}
            if validation.get("valid", True):
                continue
            reason = str(validation.get("failure_reason") or "artifact_validation_failed")
            blocking = self._artifact_failure_is_blocking(artifact, reason)
            if blocking_only and not blocking:
                continue
            if not blocking_only and blocking:
                continue
            failures.append(f"{artifact.kind.value}:{reason}")
        return failures

    def artifact_validation_warnings(self, artifacts: list) -> list[str]:
        return self.artifact_validation_failures(artifacts, blocking_only=False)

    def missing_required_questions(self, question_answers: list[tuple[Any, Any]]) -> list[str]:
        missing = []
        for question, answer in question_answers:
            if question.question_type == QuestionType.FILE:
                continue
            if self._question_hidden_from_operator(question):
                continue
            if question.required and (answer is None or not str(answer.candidate_answer or '').strip()):
                missing.append(self._blocker_label(question))
        return self._unique_labels(missing)

    def ungrounded_question_prompts(self, question_answers: list[tuple[Any, Any]]) -> list[str]:
        prompts = []
        for question, answer in question_answers:
            if question.question_type == QuestionType.FILE or self._question_hidden_from_operator(question):
                continue
            if answer is None or answer.needs_user_input:
                prompts.append(self._blocker_label(question))
        return self._unique_labels(prompts)

    def low_confidence_answers(
        self,
        question_answers: list[tuple[Any, Any]],
        threshold: float = 0.5,
    ) -> list[str]:
        prompts = []
        for question, answer in question_answers:
            if question.question_type == QuestionType.FILE or self._question_hidden_from_operator(question):
                continue
            if answer is None:
                continue
            reason = getattr(answer, "confidence_reason", None)
            if reason is None:
                continue
            confidence = getattr(answer, "confidence", 1.0)
            if confidence is not None and confidence < threshold:
                prompts.append(self._blocker_label(question))
        return self._unique_labels(prompts)

    @classmethod
    def _question_hidden_from_operator(cls, question: Any) -> bool:
        field_config = getattr(question, "field_config", {}) or {}
        input_role = str(field_config.get("input_role") or getattr(question, "input_role", "data") or "data")
        visible_to_operator = field_config.get("visible_to_operator")
        if visible_to_operator is None:
            visible_to_operator = getattr(question, "visible_to_operator", True)
        widget_type = str(getattr(question, "widget_type", "") or field_config.get("widget_type") or "").strip().lower()
        if input_role == "helper" or widget_type in {"hidden", "input_hidden"} or not bool(visible_to_operator):
            return True
        names = [
            str(getattr(question, "source_field_name", "") or "").strip().lower(),
            str(getattr(question, "normalized_key", "") or "").strip().lower(),
            str(getattr(question, "prompt_text", "") or "").strip().lower(),
        ]
        return any(name in _HELPER_FIELD_TOKENS for name in names if name)

    @staticmethod
    def _artifact_failure_is_blocking(artifact: Any, reason: str) -> bool:
        artifact_kind = artifact.kind.value if hasattr(artifact.kind, "value") else str(artifact.kind)
        if reason in _NON_BLOCKING_ARTIFACT_FAILURE_REASONS:
            return False
        if artifact_kind in {ArtifactKind.RESUME_TEXT.value, ArtifactKind.COVER_LETTER_TEXT.value} and reason not in {
            "plain_text_empty",
            "cover_letter_empty",
            "plain_text_contains_placeholder",
            "cover_letter_contains_placeholder",
        }:
            return False
        return True

    @classmethod
    def _blocker_label(cls, question: Any) -> str:
        prompt = str(getattr(question, "prompt_text", "") or "").strip() or "Question"
        lowered = " ".join(
            value
            for value in [
                str(getattr(question, "source_field_name", "") or "").strip().lower(),
                str(getattr(question, "normalized_key", "") or "").strip().lower(),
                prompt.lower(),
            ]
            if value
        )
        if any(token in lowered for token in {"resume", "resume/cv", "curriculum vitae", " cv "}):
            return "Resume/CV"
        if "cover" in lowered and "letter" in lowered:
            return "Cover Letter"
        return prompt

    @staticmethod
    def _unique_labels(values: list[str]) -> list[str]:
        unique: list[str] = []
        for value in values:
            cleaned = str(value or '').strip()
            if cleaned and cleaned not in unique:
                unique.append(cleaned)
        return unique


