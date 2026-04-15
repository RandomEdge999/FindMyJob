from __future__ import annotations

from collections import OrderedDict
from html.parser import HTMLParser
from typing import Any
import re

from findmyjob.core.policies import SENSITIVE_QUESTION_KEYWORDS
from findmyjob.sources.contracts import ExtractionResult, FormFieldContract, FormFieldSpec
from findmyjob.sources.normalizer import slugify

_HELPER_FIELD_NAMES = {
    "latitude",
    "longitude",
    "lat",
    "lng",
    "location_lat",
    "location_lng",
    "g-recaptcha-response",
    "h-captcha-response",
    "cf-turnstile-response",
}
_UUIDISH_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_GREENHOUSE_STANDARD_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "name": "first_name",
        "label": "First Name",
        "field_type": "text",
        "widget_type": "text",
        "section": "application",
        "required": True,
        "aliases": ("first_name", "first name", "given name"),
    },
    {
        "name": "last_name",
        "label": "Last Name",
        "field_type": "text",
        "widget_type": "text",
        "section": "application",
        "required": True,
        "aliases": ("last_name", "last name", "family name", "surname"),
    },
    {
        "name": "preferred_name",
        "label": "Preferred Name",
        "field_type": "text",
        "widget_type": "text",
        "section": "application",
        "required": False,
        "aliases": ("preferred_name", "preferred name", "nickname", "chosen name"),
    },
    {
        "name": "email",
        "label": "Email",
        "field_type": "text",
        "widget_type": "email",
        "section": "application",
        "required": True,
        "aliases": ("email", "email address", "e mail"),
    },
    {
        "name": "phone",
        "label": "Phone",
        "field_type": "text",
        "widget_type": "tel",
        "section": "application",
        "required": False,
        "aliases": ("phone", "phone number", "mobile", "telephone"),
    },
    {
        "name": "location",
        "label": "Location",
        "field_type": "text",
        "widget_type": "text",
        "section": "location",
        "required": False,
        "aliases": ("location", "current location", "city"),
    },
    {
        "name": "resume",
        "label": "Resume/CV",
        "field_type": "file",
        "widget_type": "file",
        "section": "application",
        "required": True,
        "aliases": ("resume", "resume/cv", "cv", "curriculum vitae"),
    },
    {
        "name": "cover_letter",
        "label": "Cover Letter",
        "field_type": "file",
        "widget_type": "file",
        "section": "application",
        "required": False,
        "aliases": ("cover_letter", "cover letter"),
    },
)


def _clean_field_label(value: str | None) -> str:
    text = " ".join(str(value or "").strip().split())
    if not text:
        return ""
    text = re.sub(r"\s*[✱*]+\s*$", "", text).strip()
    match = re.match(r"^(?P<label>[A-Za-z][A-Za-z /&()'-]{1,80}?)Select\b", text)
    if match:
        text = match.group("label").strip()
    lowered = text.lower()
    if lowered == "opportunitylocationid":
        return "Location"
    if lowered.startswith("resume/cv"):
        return "Resume/CV"
    if lowered.startswith("cover letter"):
        return "Cover Letter"
    if lowered.startswith("current location"):
        return "Current location"
    return text


def _question_identity_tokens(*values: Any) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        text = str(value or "").strip().lower()
        if not text:
            continue
        tokens.add(text)
        slug = slugify(text)
        if slug:
            tokens.add(slug)
            tokens.add(slug.replace("-", "_"))
    return tokens


def _append_greenhouse_standard_questions(questions: list[Any]) -> None:
    existing_tokens: set[str] = set()
    for question in questions:
        existing_tokens.update(
            _question_identity_tokens(
                getattr(question, "source_field_name", ""),
                getattr(question, "normalized_key", ""),
                getattr(question, "prompt_text", ""),
            )
        )

    for spec in _GREENHOUSE_STANDARD_FIELDS:
        aliases = _question_identity_tokens(spec["name"], spec["label"], *(spec.get("aliases") or ()))
        if existing_tokens.intersection(aliases):
            continue
        contract = _build_contract(
            name=spec["name"],
            label=spec["label"],
            field_type=spec["field_type"],
            widget_type=spec["widget_type"],
            section=spec["section"],
            required=bool(spec["required"]),
            prompt_text=spec["label"],
            normalized_key=slugify(spec["label"]),
            source_snapshot_ref=f"greenhouse:synthetic:{spec['name']}",
            submission_binding={"name": spec["name"], "id": spec["name"], "group": "synthetic", "raw_type": spec["field_type"]},
            source_confidence=0.35,
        )
        question = contract.to_question()
        questions.append(question)
        existing_tokens.update(aliases)
        existing_tokens.update(_question_identity_tokens(question.source_field_name, question.normalized_key, question.prompt_text))


def _looks_like_machine_identifier(value: str | None) -> bool:
    text = str(value or "").strip()
    return bool(text and _UUIDISH_PATTERN.fullmatch(text))


def _normalize_option_details(*sources: Any) -> tuple[list[str], list[dict[str, Any]]]:
    options: list[str] = []
    details: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source in sources:
        if not source:
            continue
        if isinstance(source, dict):
            source = source.get("options") or source.get("answer_options") or source.get("values") or []
        for item in source if isinstance(source, list) else []:
            if isinstance(item, dict):
                label = str(item.get("label") or item.get("text") or item.get("name") or item.get("title") or item.get("value") or item.get("id") or "").strip()
                value = str(item.get("value") or item.get("id") or item.get("key") or label).strip()
                free_form = bool(item.get("free_form"))
            else:
                label = str(item or "").strip()
                value = label
                free_form = False
            if not label and not value:
                continue
            normalized = (label.lower(), value.lower())
            if normalized in seen:
                continue
            seen.add(normalized)
            canonical_label = label or value
            canonical_value = value or label
            if canonical_label and canonical_label not in options:
                options.append(canonical_label)
            details.append({"label": canonical_label, "value": canonical_value, "free_form": free_form})
    return options, details


def _looks_like_boolean_question(label: str, option_details: list[dict[str, Any]]) -> bool:
    normalized = {str(item.get("label") or "").strip().lower() for item in option_details if str(item.get("label") or "").strip()}
    if normalized and normalized <= {"yes", "no"}:
        return True
    lowered = label.lower()
    return lowered.startswith(("do you", "are you", "will you", "have you", "can you"))


def _default_yes_no_options(label: str, widget_type: str, option_details: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    if widget_type not in {"select", "checkbox_group", "radio_group"} or option_details or not _looks_like_boolean_question(label, option_details):
        return [], option_details
    yes_no = [{"label": "Yes", "value": "yes", "free_form": False}, {"label": "No", "value": "no", "free_form": False}]
    return ["Yes", "No"], yes_no


def _is_helper_field(name: str, label: str, field_type: str) -> bool:
    lowered_name = str(name or "").strip().lower()
    lowered_label = str(label or "").strip().lower()
    lowered_type = str(field_type or "").strip().lower()
    if lowered_type in {"hidden", "input_hidden"}:
        return True
    if lowered_name in _HELPER_FIELD_NAMES:
        return True
    return lowered_label in {"latitude", "longitude"}


def _input_role(name: str, label: str, field_type: str) -> str:
    if _is_helper_field(name, label, field_type):
        return "helper"
    if str(field_type or "").strip().lower() in {"file", "input_file"}:
        return "file"
    return "data"


def _visible_to_operator(name: str, label: str, field_type: str) -> bool:
    return _input_role(name, label, field_type) != "helper"


def _build_contract(
    *,
    name: str,
    label: str,
    field_type: str,
    widget_type: str,
    required: bool,
    prompt_text: str,
    normalized_key: str | None,
    options: list[str] | None = None,
    option_details: list[dict[str, Any]] | None = None,
    section: str | None = None,
    accept: list[str] | None = None,
    sensitive: bool = False,
    source_snapshot_ref: str | None = None,
    submission_binding: dict[str, Any] | None = None,
    source_confidence: float = 1.0,
) -> FormFieldContract:
    resolved_options = list(options or [])
    resolved_details = list(option_details or [])
    default_options, resolved_details = _default_yes_no_options(prompt_text, widget_type, resolved_details)
    if default_options and not resolved_options:
        resolved_options = default_options
    role = _input_role(name, label, field_type)
    return FormFieldSpec(
        name=name,
        label=label or name,
        field_type=field_type,
        widget_type=widget_type,
        section=section,
        required=required and role != "helper",
        input_role=role,
        visible_to_operator=_visible_to_operator(name, label, field_type),
        prompt_text=prompt_text,
        normalized_key=normalized_key,
        options=resolved_options,
        option_details=resolved_details,
        accept=list(accept or []),
        sensitive=sensitive,
        source_snapshot_ref=source_snapshot_ref,
        submission_binding=dict(submission_binding or {}),
        source_confidence=source_confidence,
    )


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.labels: dict[str, str] = {}
        self.current_label_for: str | None = None
        self.current_label_parts: list[str] = []
        self.current_text_parts: list[str] = []
        self.fields: list[FormFieldContract] = []
        self.field_names: set[str] = set()
        self.select_stack: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "label":
            self.current_label_for = values.get("for")
            self.current_label_parts = []
            return
        if tag == "option" and self.select_stack is not None:
            self.current_text_parts = []
            self.select_stack["current_value"] = values.get("value") or ""
            return
        if tag == "select":
            binding_name = values.get("name") or values.get("id")
            if not binding_name:
                return
            label = self.labels.get(values.get("id") or "", values.get("aria-label") or binding_name)
            prompt = label or binding_name
            self.select_stack = {
                "name": binding_name,
                "html_name": values.get("name") or binding_name,
                "label": label or binding_name,
                "required": "required" in values,
                "prompt": prompt,
                "options": [],
                "option_details": [],
                "id": values.get("id"),
            }
            return
        if tag == "input":
            self._push_field(values, tag=tag)
            return
        if tag == "textarea":
            values = {**values, "type": "textarea"}
            self._push_field(values, tag=tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "label" and self.current_label_for is not None:
            self.labels[self.current_label_for] = " ".join(part for part in self.current_label_parts if part).strip()
            self.current_label_for = None
            self.current_label_parts = []
            return
        if tag == "option" and self.select_stack is not None:
            option_label = " ".join(part for part in self.current_text_parts if part).strip()
            if option_label:
                if option_label not in self.select_stack["options"]:
                    self.select_stack["options"].append(option_label)
                self.select_stack["option_details"].append({"label": option_label, "value": self.select_stack.get("current_value") or option_label, "free_form": False})
            self.current_text_parts = []
            return
        if tag == "select" and self.select_stack is not None:
            prompt = self.select_stack["prompt"]
            self.fields.append(
                _build_contract(
                    name=self.select_stack["name"],
                    label=self.select_stack["label"],
                    field_type="select",
                    widget_type="select",
                    required=self.select_stack["required"],
                    prompt_text=prompt,
                    normalized_key=slugify(prompt),
                    options=self.select_stack["options"],
                    option_details=self.select_stack["option_details"],
                    sensitive=any(keyword in prompt.lower() for keyword in SENSITIVE_QUESTION_KEYWORDS),
                    source_snapshot_ref=f"html:select:{self.select_stack['name']}",
                    submission_binding={"name": self.select_stack.get("html_name"), "id": self.select_stack.get("id"), "tag": "select"},
                )
            )
            self.field_names.add(self.select_stack["name"])
            self.select_stack = None

    def handle_data(self, data: str) -> None:
        if self.current_label_for is not None:
            self.current_label_parts.append(data.strip())
        if self.select_stack is not None:
            self.current_text_parts.append(data.strip())

    def _push_field(self, values: dict[str, str | None], *, tag: str) -> None:
        binding_name = values.get("name") or values.get("id")
        if not binding_name or binding_name in self.field_names:
            return
        field_type = values.get("type") or "text"
        if field_type in {"hidden", "submit"}:
            return
        label = self.labels.get(values.get("id") or "", values.get("aria-label") or binding_name)
        prompt = label or binding_name
        accept = [part.strip() for part in (values.get("accept") or "").split(",") if part.strip()]
        widget_type = "textarea" if tag == "textarea" else field_type
        self.fields.append(
            _build_contract(
                name=binding_name,
                label=label or binding_name,
                field_type="file" if field_type == "file" else field_type,
                widget_type=widget_type,
                required="required" in values,
                prompt_text=prompt,
                normalized_key=slugify(prompt),
                accept=accept,
                sensitive=any(keyword in prompt.lower() for keyword in SENSITIVE_QUESTION_KEYWORDS),
                source_snapshot_ref=f"html:{tag}:{binding_name}",
                submission_binding={"name": values.get("name") or binding_name, "id": values.get("id"), "tag": tag},
            )
        )
        self.field_names.add(binding_name)


def extract_questions_from_html(html: str, handoff_url: str | None = None) -> ExtractionResult:
    parser = _FormParser()
    parser.feed(html)
    return ExtractionResult(
        questions=[field.to_question() for field in parser.fields],
        raw_form={"field_count": len(parser.fields)},
        handoff_url=handoff_url,
    )


def extract_questions_from_greenhouse_payload(payload: Any, handoff_url: str | None = None) -> ExtractionResult:
    payload_dict = payload if isinstance(payload, dict) else {}
    questions: list = []

    def _coerce_group_items(raw_group: Any) -> list[dict[str, Any]]:
        if isinstance(raw_group, list):
            return [item for item in raw_group if isinstance(item, dict)]
        if not isinstance(raw_group, dict):
            return []
        if isinstance(raw_group.get("questions"), list):
            return [item for item in raw_group.get("questions", []) if isinstance(item, dict)]
        if isinstance(raw_group.get("fields"), list):
            synthetic_label = str(raw_group.get("label") or raw_group.get("question") or raw_group.get("name") or "Question").strip() or "Question"
            return [{
                "label": synthetic_label,
                "required": bool(raw_group.get("required")),
                "fields": [item for item in raw_group.get("fields", []) if isinstance(item, dict)],
                "answer_options": raw_group.get("answer_options"),
                "options": raw_group.get("options"),
                "values": raw_group.get("values"),
            }]
        return []

    def _append_greenhouse_question(
        *,
        group_name: str,
        section: str,
        label: str,
        item_required: bool,
        fields: list[dict[str, Any]],
        option_sources: list[Any] | None = None,
        sensitive: bool = False,
        source_confidence: float = 0.98,
    ) -> None:
        for field in fields:
            if not isinstance(field, dict):
                continue
            field_name = str(field.get("name") or field.get("id") or slugify(label))
            raw_field_type = str(field.get("type") or "input_text")
            widget_type = {
                "input_file": "file",
                "input_text": "text",
                "textarea": "textarea",
                "multi_value_single_select": "select",
                "multi_value_multi_select": "checkbox_group",
                "input_hidden": "hidden",
            }.get(raw_field_type, raw_field_type)
            normalized_field_type = {
                "input_file": "file",
                "multi_value_single_select": "select",
                "multi_value_multi_select": "checkbox",
                "input_hidden": "hidden",
            }.get(raw_field_type, raw_field_type)
            options, option_details = _normalize_option_details(
                *(option_sources or []),
                field.get("answer_options"),
                field.get("options"),
                field.get("values"),
            )
            contract = _build_contract(
                name=field_name,
                label=label,
                field_type=normalized_field_type,
                widget_type=widget_type,
                section=section,
                required=bool(item_required or field.get("required")),
                prompt_text=label,
                normalized_key=slugify(label),
                options=options,
                option_details=option_details,
                accept=[ext.strip() for ext in str(field.get("accepted_mime_types") or "").split(",") if ext.strip()],
                sensitive=sensitive or any(keyword in label.lower() for keyword in SENSITIVE_QUESTION_KEYWORDS),
                source_snapshot_ref=f"greenhouse:{group_name}:{field_name}",
                submission_binding={"name": field_name, "id": field.get("id"), "group": group_name, "raw_type": raw_field_type},
                source_confidence=source_confidence,
            )
            questions.append(contract.to_question())

    groups = (
        ("questions", "application"),
        ("location_questions", "location"),
        ("compliance", "compliance"),
        ("education", "education"),
    )
    for group_name, section in groups:
        group_items = _coerce_group_items(payload_dict.get(group_name) or [])
        for item in group_items:
            label = str(item.get("label") or item.get("question") or item.get("name") or "Question").strip() or "Question"
            fields = item.get("fields") or []
            if isinstance(fields, dict):
                fields = [fields]
            if not isinstance(fields, list):
                continue
            if not fields:
                synthetic_name = str(item.get("name") or item.get("id") or slugify(label))
                raw_type = str(item.get("type") or "input_text")
                fields = [{"name": synthetic_name, "id": item.get("id"), "type": raw_type}]
            _append_greenhouse_question(
                group_name=group_name,
                section=section,
                label=label,
                item_required=bool(item.get("required")),
                fields=fields,
                option_sources=[item.get("answer_options"), item.get("options"), item.get("values")],
                source_confidence=0.98,
            )
    demographic = payload_dict.get("demographic_questions") or {}
    demographic_items: list[dict[str, Any]] = []
    if isinstance(demographic, dict):
        raw_questions = demographic.get("questions") or []
        if isinstance(raw_questions, list):
            demographic_items = [item for item in raw_questions if isinstance(item, dict)]
    elif isinstance(demographic, list):
        demographic_items = [item for item in demographic if isinstance(item, dict)]
    for item in demographic_items:
        label = str(item.get("label") or item.get("question") or "Demographic question").strip() or "Demographic question"
        raw_field_type = str(item.get("type") or "multi_value_single_select")
        options, option_details = _normalize_option_details(item.get("answer_options"), item.get("options"), item.get("values"))
        contract = _build_contract(
            name=str(item.get("id") or slugify(label)),
            label=label,
            field_type="checkbox" if raw_field_type == "multi_value_multi_select" else "select",
            widget_type="checkbox_group" if raw_field_type == "multi_value_multi_select" else "select",
            section="demographic",
            required=bool(item.get("required")),
            prompt_text=label,
            normalized_key=slugify(label),
            options=options,
            option_details=option_details,
            sensitive=True,
            source_snapshot_ref=f"greenhouse:demographic:{item.get('id')}",
            submission_binding={"name": item.get("id") or slugify(label), "group": "demographic", "raw_type": raw_field_type},
            source_confidence=0.95,
        )
        questions.append(contract.to_question())

    eeoc_sections = payload_dict.get("eeoc_sections") or []
    if isinstance(eeoc_sections, list):
        for section_index, eeoc_section in enumerate(eeoc_sections):
            if not isinstance(eeoc_section, dict):
                continue
            raw_questions = eeoc_section.get("questions") or []
            if not isinstance(raw_questions, list):
                continue
            for item_index, item in enumerate(raw_questions):
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label") or item.get("question") or item.get("name") or f"EEOC question {item_index + 1}").strip() or f"EEOC question {item_index + 1}"
                fields = item.get("fields") or []
                if isinstance(fields, dict):
                    fields = [fields]
                if not isinstance(fields, list) or not fields:
                    synthetic_name = str(item.get("name") or item.get("id") or slugify(label))
                    fields = [{"name": synthetic_name, "id": item.get("id"), "type": item.get("type") or "multi_value_single_select"}]
                _append_greenhouse_question(
                    group_name="eeoc",
                    section="eeoc",
                    label=label,
                    item_required=bool(item.get("required")),
                    fields=[field for field in fields if isinstance(field, dict)],
                    option_sources=[item.get("answer_options"), item.get("options"), item.get("values")],
                    sensitive=True,
                    source_confidence=0.97,
                )
    _append_greenhouse_standard_questions(questions)
    return ExtractionResult(questions=questions, raw_form=payload_dict, handoff_url=handoff_url)

def extract_questions_from_lever_fields(field_rows: list[dict[str, Any]], handoff_url: str | None = None) -> ExtractionResult:
    grouped: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for row in field_rows:
        name = str(row.get("name") or row.get("id") or "")
        if not name:
            continue
        if row.get("type") == "hidden":
            continue
        label = _clean_field_label(row.get("group_label") or row.get("label") or row.get("placeholder") or name)
        if not label:
            continue
        widget_type = str(row.get("widget_type") or row.get("tag") or "text").strip().lower()
        group_label = _clean_field_label(row.get("group_label") or "")
        option_label = _clean_field_label(row.get("option_label") or "")
        identifier_values = [name, label, group_label, option_label]
        if widget_type in {"checkbox_group", "radio_group"} and all(
            not value or _looks_like_machine_identifier(value) for value in identifier_values
        ):
            continue
        if _looks_like_machine_identifier(label) and not group_label:
            if not option_label or _looks_like_machine_identifier(option_label):
                continue
        key = name
        if widget_type in {"checkbox_group", "radio_group"} and group_label:
            key = f"{widget_type}:{slugify(group_label)}"
        group = grouped.setdefault(
            key,
            {
                "name": name,
                "label": label,
                "field_type": row.get("field_type") or row.get("type") or row.get("tag") or "text",
                "widget_type": widget_type,
                "required": bool(row.get("required")),
                "options": [],
                "option_details": [],
                "accept": row.get("accept") or [],
                "sensitive": bool(row.get("sensitive")),
                "source_snapshot_ref": row.get("source_snapshot_ref"),
                "submission_binding": {"name": row.get("name") or name, "id": row.get("id"), "tag": row.get("tag"), "type": row.get("type")},
            },
        )
        preferred_label = group_label
        if preferred_label:
            group["label"] = preferred_label
        options, option_details = _normalize_option_details(
            row.get("options"),
            row.get("option_details"),
            [{"label": option_label, "value": row.get("option_value")}],
        )
        for option in options:
            if option not in group["options"]:
                group["options"].append(option)
        for detail in option_details:
            if detail not in group["option_details"]:
                group["option_details"].append(detail)

    questions = []
    for item in grouped.values():
        contract = _build_contract(
            name=item["name"],
            label=item["label"],
            field_type=item["field_type"],
            widget_type=item["widget_type"],
            required=item["required"],
            prompt_text=item["label"],
            normalized_key=slugify(item["label"]),
            options=item["options"],
            option_details=item["option_details"],
            accept=item["accept"],
            sensitive=item["sensitive"] or any(keyword in item["label"].lower() for keyword in SENSITIVE_QUESTION_KEYWORDS),
            source_snapshot_ref=item["source_snapshot_ref"],
            submission_binding=item["submission_binding"],
            source_confidence=0.85,
        )
        questions.append(contract.to_question())
    return ExtractionResult(questions=questions, raw_form={"field_count": len(questions)}, handoff_url=handoff_url)


def _question_index_keys(question: Any) -> list[tuple[str, str]]:
    field_name = str(question.source_field_name or "").strip().lower()
    normalized_key = str(question.normalized_key or "").strip().lower()
    prompt_key = str(slugify(question.prompt_text)).strip().lower()
    keys: list[tuple[str, str]] = []
    if field_name:
        keys.append((field_name, field_name))
    if field_name and normalized_key:
        keys.append((field_name, normalized_key))
    if field_name and prompt_key and prompt_key != normalized_key:
        keys.append((field_name, prompt_key))
    return keys


def _looks_like_generic_payload_location(question: Any) -> bool:
    field_name = str(question.source_field_name or "").strip().lower()
    normalized_key = str(question.normalized_key or "").strip().lower()
    section = str(question.section or "").strip().lower()
    binding = dict(getattr(question, "submission_binding", {}) or {})
    group = str(binding.get("group") or "").strip().lower()
    return field_name == "location" and normalized_key == "location" and section == "location" and group == "location_questions"


def _looks_like_rendered_location_field(question: Any) -> bool:
    descriptor = " ".join(
        str(part or "").strip().lower()
        for part in (
            getattr(question, "source_field_name", ""),
            getattr(question, "prompt_text", ""),
            getattr(question, "normalized_key", ""),
            getattr(question, "section", ""),
        )
    )
    source_snapshot_ref = str(getattr(question, "source_snapshot_ref", "") or "").strip().lower()
    if "location" not in descriptor:
        return False
    if str(getattr(question, "source_field_name", "") or "").strip().lower() == "location":
        return False
    return source_snapshot_ref.startswith(("html:", "input:")) or bool(getattr(question, "visible_to_operator", False))


def _is_synthetic_greenhouse_question(question: Any) -> bool:
    source_snapshot_ref = str(getattr(question, "source_snapshot_ref", "") or "").strip().lower()
    return source_snapshot_ref.startswith("greenhouse:synthetic:")


def merge_extraction_results(primary: ExtractionResult, fallback: ExtractionResult) -> ExtractionResult:
    merged = [question.model_copy(deep=True) for question in primary.questions]
    index: dict[tuple[str, str], int] = {}
    for idx, question in enumerate(merged):
        for key in _question_index_keys(question):
            index[key] = idx

    for candidate in fallback.questions:
        matched_index = None
        for key in _question_index_keys(candidate):
            matched_index = index.get(key)
            if matched_index is not None:
                break
        if matched_index is None and _looks_like_rendered_location_field(candidate):
            matched_index = next(
                (idx for idx, existing in enumerate(merged) if _looks_like_generic_payload_location(existing)),
                None,
            )
        if matched_index is None:
            if candidate.visible_to_operator:
                cloned = candidate.model_copy(deep=True)
                merged.append(cloned)
                for key in _question_index_keys(cloned):
                    index[key] = len(merged) - 1
            continue
        existing = merged[matched_index]
        if _is_synthetic_greenhouse_question(existing) and candidate.visible_to_operator and candidate.input_role != "helper":
            replacement = candidate.model_copy(deep=True)
            replacement.required = bool(replacement.required or existing.required)
            if not replacement.section:
                replacement.section = getattr(existing, "section", None)
            replacement.source_confidence = max(float(replacement.source_confidence or 0.0), float(existing.source_confidence or 0.0))
            merged[matched_index] = replacement
            for key in _question_index_keys(replacement):
                index[key] = matched_index
            continue
        if _looks_like_generic_payload_location(existing) and _looks_like_rendered_location_field(candidate):
            merged[matched_index] = candidate.model_copy(deep=True)
            for key in _question_index_keys(merged[matched_index]):
                index[key] = matched_index
            continue
        if not existing.options and candidate.options:
            existing.options = list(candidate.options)
        if (not existing.option_details or len(existing.option_details) < len(candidate.option_details)) and candidate.option_details:
            existing.option_details = list(candidate.option_details)
        if existing.widget_type in {"text", "unknown"} and candidate.widget_type not in {"text", "unknown"}:
            existing.widget_type = candidate.widget_type
        if existing.input_role == "helper" and candidate.input_role != "helper":
            existing.input_role = candidate.input_role
            existing.visible_to_operator = candidate.visible_to_operator
        if not existing.submission_binding and candidate.submission_binding:
            existing.submission_binding = dict(candidate.submission_binding)
        existing.source_confidence = max(float(existing.source_confidence or 0.0), float(candidate.source_confidence or 0.0))
    raw_form = dict(primary.raw_form or {})
    raw_form["fallback_question_count"] = len(fallback.questions)
    return ExtractionResult(questions=merged, raw_form=raw_form, handoff_url=primary.handoff_url or fallback.handoff_url)

