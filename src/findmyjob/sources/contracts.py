from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from findmyjob.core.enums import CompanySizeBucket, ExperienceLevel, LocationScope, QuestionType, WorkplaceType
from findmyjob.core.types import ApplicationQuestion


class DiscoveryQuery(BaseModel):
    title_keywords: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    workplace_types: list[WorkplaceType] = Field(default_factory=list)
    employment_types: list[str] = Field(default_factory=list)
    location_scopes: list[LocationScope] = Field(default_factory=list)
    experience_levels: list[ExperienceLevel] = Field(default_factory=list)
    company_size_buckets: list[CompanySizeBucket] = Field(default_factory=list)
    posted_within_days: int | None = Field(default=None, ge=1, le=3650)
    compensation_min: int | None = Field(default=None, ge=0)
    compensation_currency: str | None = None
    requires_future_sponsorship: bool = False
    remote_only: bool = False
    allow_unknown_compensation: bool = False
    allow_unknown_experience_level: bool = False


class FormFieldContract(BaseModel):
    name: str
    label: str | None = None
    field_type: str = "text"
    widget_type: str = "text"
    section: str | None = None
    step_id: str | None = None
    required: bool = False
    input_role: str = "data"
    visible_to_operator: bool = True
    options: list[str] = Field(default_factory=list)
    option_details: list[dict[str, Any]] = Field(default_factory=list)
    accept: list[str] = Field(default_factory=list)
    sensitive: bool = False
    prompt_text: str
    normalized_key: str | None = None
    source_snapshot_ref: str | None = None
    submission_binding: dict[str, Any] = Field(default_factory=dict)
    source_confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @property
    def field_key(self) -> str:
        return self.name

    def to_question(self) -> ApplicationQuestion:
        question_type = QuestionType.UNKNOWN
        lowered = (self.label or self.prompt_text).lower()
        normalized_options = {option.lower() for option in self.options if option}
        if self.field_type in {"select", "dropdown", "radio", "multi_value_single_select"}:
            if normalized_options and normalized_options <= {"yes", "no"}:
                question_type = QuestionType.BOOLEAN
            else:
                question_type = QuestionType.SELECT
        elif self.field_type in {"checkbox", "multi_value_multi_select"}:
            if normalized_options and normalized_options <= {"yes", "no"}:
                question_type = QuestionType.BOOLEAN
            elif self.options:
                question_type = QuestionType.SELECT
            else:
                question_type = QuestionType.BOOLEAN
        elif self.field_type == "file":
            question_type = QuestionType.FILE
        elif self.widget_type == "textarea":
            question_type = QuestionType.NARRATIVE
        elif any(token in lowered for token in ("salary", "gpa", "years")):
            question_type = QuestionType.NUMERIC
        elif any(token in lowered for token in ("date", "when")):
            question_type = QuestionType.DATE
        elif self.sensitive:
            question_type = QuestionType.SENSITIVE
        return ApplicationQuestion(
            source_field_name=self.name,
            prompt_text=self.prompt_text,
            normalized_key=self.normalized_key,
            question_type=question_type,
            widget_type=self.widget_type,
            section=self.section,
            step_id=self.step_id,
            required=self.required,
            input_role=self.input_role,
            visible_to_operator=self.visible_to_operator,
            options=self.options,
            option_details=self.option_details,
            sensitive=self.sensitive,
            file_constraints={"accept": self.accept},
            submission_binding=self.submission_binding,
            source_confidence=self.source_confidence,
            source_snapshot_ref=self.source_snapshot_ref,
        )


FormFieldSpec = FormFieldContract


@dataclass(slots=True)
class DiscoveryResult:
    jobs: list[tuple[object, dict]] = field(default_factory=list)
    cursor: dict | None = None


@dataclass(slots=True)
class ExtractionResult:
    questions: list[ApplicationQuestion] = field(default_factory=list)
    raw_form: dict[str, Any] | None = None
    handoff_url: str | None = None
