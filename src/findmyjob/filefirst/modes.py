from __future__ import annotations

import json
from typing import Any

from anyio import run as run_async

from findmyjob.core.enums import ModelRole
from findmyjob.filefirst.advanced_models import load_model_router, mode_system_prompt
from findmyjob.filefirst.workspace import FileWorkspace

_MODE_ROLE_MAP: dict[str, ModelRole] = {
    "screen": ModelRole.CLASSIFIER,
    "eval": ModelRole.CLASSIFIER,
    "pdf": ModelRole.WRITER,
}

_MODE_ROLE_FALLBACKS: dict[str, tuple[ModelRole, ...]] = {
    "eval": (ModelRole.WRITER, ModelRole.CLASSIFIER),
}

_JSON_RETRYABLE_MARKERS = (
    "invalid json",
    "empty json response",
    "non-object json",
    "expecting value",
    "extra data",
)
_JSON_MAX_ATTEMPTS_PER_ROLE = 3


def _string_array_schema(*, min_items: int | None = None, max_items: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "array", "items": {"type": "string"}}
    if min_items is not None:
        schema["minItems"] = min_items
    if max_items is not None:
        schema["maxItems"] = max_items
    return schema


def _json_schema_for_mode(mode_name: str) -> dict[str, Any] | None:
    if mode_name == "screen":
        return {
            "type": "object",
            "properties": {
                "approved": {"type": "boolean"},
                "reasons": _string_array_schema(min_items=1),
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "internship_like": {"type": "boolean"},
                "seniority_too_high": {"type": "boolean"},
                "years_experience_signal": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "notes": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            },
            "required": [
                "approved",
                "reasons",
                "confidence",
                "internship_like",
                "seniority_too_high",
                "years_experience_signal",
                "notes",
            ],
            "additionalProperties": True,
        }
    if mode_name == "eval":
        return {
            "type": "object",
            "properties": {
                "company": {"type": "string"},
                "role": {"type": "string"},
                "archetype": {"type": "string"},
                "score": {"type": "number", "minimum": 0.0, "maximum": 5.0},
                "grade": {"type": "string"},
                "summary": {"type": "string"},
                "keywords": _string_array_schema(min_items=1),
                "fit_reasons": _string_array_schema(min_items=1),
                "gaps": _string_array_schema(),
                "report_markdown": {"type": "string"},
                "resume_headline": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "resume_summary_lines": _string_array_schema(max_items=4),
                "selected_work_fact_ids": _string_array_schema(),
                "selected_project_fact_ids": _string_array_schema(),
                "selected_skill_fact_ids": _string_array_schema(),
                "custom_bullets": _string_array_schema(max_items=4),
                "cover_letter_paragraphs": _string_array_schema(min_items=3, max_items=3),
            },
            "required": [
                "company",
                "role",
                "archetype",
                "score",
                "grade",
                "summary",
                "keywords",
                "fit_reasons",
                "gaps",
                "report_markdown",
                "resume_headline",
                "resume_summary_lines",
                "selected_work_fact_ids",
                "selected_project_fact_ids",
                "selected_skill_fact_ids",
                "custom_bullets",
                "cover_letter_paragraphs",
            ],
            "additionalProperties": True,
        }
    if mode_name == "pdf":
        return {
            "type": "object",
            "properties": {
                "headline": {"type": "string"},
                "summary_lines": _string_array_schema(max_items=4),
                "selected_work_fact_ids": _string_array_schema(),
                "selected_project_fact_ids": _string_array_schema(),
                "selected_skill_fact_ids": _string_array_schema(),
                "custom_bullets": _string_array_schema(max_items=4),
                "cover_letter_paragraphs": _string_array_schema(min_items=3, max_items=3),
            },
            "required": [
                "headline",
                "summary_lines",
                "selected_work_fact_ids",
                "selected_project_fact_ids",
                "selected_skill_fact_ids",
                "custom_bullets",
                "cover_letter_paragraphs",
            ],
            "additionalProperties": True,
        }
    return None


class ModeRunner:
    def __init__(self, workspace: FileWorkspace) -> None:
        self.workspace = workspace
        self.router = load_model_router(workspace)
        self.last_profile_name: str | None = None
        self.last_role: ModelRole | None = None

    @staticmethod
    def _payload_is_error(payload: dict[str, Any]) -> bool:
        keys = {str(key).strip().lower() for key in payload.keys()}
        if "error" not in keys:
            return False
        return keys <= {"error", "message", "detail", "code", "type"}

    def _role_for_mode(self, mode_name: str) -> ModelRole:
        return _MODE_ROLE_MAP.get(mode_name, ModelRole.WRITER)

    def _roles_for_mode(self, mode_name: str) -> tuple[ModelRole, ...]:
        return _MODE_ROLE_FALLBACKS.get(mode_name, (self._role_for_mode(mode_name),))

    @staticmethod
    def _should_retry_json_failure(exc: Exception) -> bool:
        text = str(exc or "").strip().casefold()
        return any(marker in text for marker in _JSON_RETRYABLE_MARKERS)

    def run_json(self, mode_name: str, context: dict[str, Any]) -> dict[str, Any]:
        prompt = "Context JSON:\n" + json.dumps(context, indent=2, default=str)
        json_schema = _json_schema_for_mode(mode_name)
        last_error: Exception | None = None
        for role in self._roles_for_mode(mode_name):
            for attempt in range(1, _JSON_MAX_ATTEMPTS_PER_ROLE + 1):
                try:
                    system_prompt = mode_system_prompt(self.workspace, mode_name)
                    try:
                        payload, profile_name = run_async(
                            lambda: self.router.generate_json_with_profile(
                                role,
                                prompt,
                                system_prompt=system_prompt,
                                json_schema=json_schema,
                            )
                        )
                    except TypeError as exc:
                        if "json_schema" not in str(exc):
                            raise
                        payload, profile_name = run_async(
                            lambda: self.router.generate_json_with_profile(
                                role,
                                prompt,
                                system_prompt=system_prompt,
                            )
                        )
                    if self._payload_is_error(payload):
                        raise RuntimeError(str(payload.get("error") or "Model returned an error payload."))
                    self.last_profile_name = profile_name
                    self.last_role = role
                    return payload
                except Exception as exc:
                    last_error = exc
                    if attempt < _JSON_MAX_ATTEMPTS_PER_ROLE and self._should_retry_json_failure(exc):
                        continue
                    break
        raise RuntimeError(str(last_error) if last_error is not None else f"No usable model role for mode `{mode_name}`.")

    def run_text(self, mode_name: str, context: dict[str, Any]) -> str:
        prompt = "Context JSON:\n" + json.dumps(context, indent=2, default=str)
        role = self._role_for_mode(mode_name)
        content, profile_name = run_async(
            lambda: self.router.generate_text_with_profile(
                role,
                prompt,
                system_prompt=mode_system_prompt(self.workspace, mode_name),
            )
        )
        self.last_profile_name = profile_name
        self.last_role = role
        return content
