from __future__ import annotations

from findmyjob.core.config import AutonomousSettings, PolicySettings
from findmyjob.core.enums import PolicyMode, SourceKind

SOURCE_POLICY_DEFAULTS: dict[SourceKind, PolicyMode] = {
    SourceKind.GREENHOUSE: PolicyMode.HUMAN_IN_LOOP_SUBMIT,
    SourceKind.LEVER: PolicyMode.HUMAN_IN_LOOP_SUBMIT,
    SourceKind.ASHBY: PolicyMode.REVIEW_ONLY,
    SourceKind.COMPANY: PolicyMode.REVIEW_ONLY,
    SourceKind.AGGREGATOR: PolicyMode.PUBLIC_READ_ONLY,
}

SENSITIVE_QUESTION_KEYWORDS = {
    "visa",
    "sponsorship",
    "work authorization",
    "authorized",
    "salary",
    "compensation",
    "gender",
    "race",
    "ethnicity",
    "disability",
    "veteran",
}


def normalize_review_mode(review_mode: str | None) -> str:
    normalized = str(review_mode or '').strip().lower()
    if normalized in {'full_auto', 'review_exceptions', 'manual'}:
        return normalized
    return 'manual'


def legacy_policy_values_for_review_mode(review_mode: str | None) -> dict[str, PolicyMode | bool]:
    normalized = normalize_review_mode(review_mode)
    if normalized == 'manual':
        return {
            'require_human_review_for_submit': True,
            'default_source_policy': PolicyMode.REVIEW_ONLY,
        }
    return {
        'require_human_review_for_submit': False,
        'default_source_policy': PolicyMode.HUMAN_IN_LOOP_SUBMIT,
    }


def resolve_source_policy(
    source_kind: str,
    settings: PolicySettings | None = None,
    autonomous_settings: AutonomousSettings | None = None,
) -> PolicyMode:
    normalized = str(source_kind or '').strip().lower()
    if normalized == SourceKind.AGGREGATOR.value:
        return PolicyMode.PUBLIC_READ_ONLY
    if normalized == SourceKind.ASHBY.value:
        return PolicyMode.REVIEW_ONLY
    if autonomous_settings is not None:
        review_mode = normalize_review_mode(getattr(autonomous_settings, 'review_mode', None))
        if normalized in {SourceKind.GREENHOUSE.value, SourceKind.LEVER.value}:
            return PolicyMode.REVIEW_ONLY if review_mode == 'manual' else PolicyMode.HUMAN_IN_LOOP_SUBMIT
        if normalized == SourceKind.COMPANY.value:
            return PolicyMode.REVIEW_ONLY
    if settings is None:
        if normalized == SourceKind.GREENHOUSE.value:
            return SOURCE_POLICY_DEFAULTS[SourceKind.GREENHOUSE]
        if normalized == SourceKind.LEVER.value:
            return SOURCE_POLICY_DEFAULTS[SourceKind.LEVER]
        if normalized == SourceKind.COMPANY.value:
            return SOURCE_POLICY_DEFAULTS[SourceKind.COMPANY]
        return PolicyMode.PUBLIC_READ_ONLY
    if settings.require_human_review_for_submit:
        return PolicyMode.REVIEW_ONLY
    if normalized in {SourceKind.GREENHOUSE.value, SourceKind.LEVER.value, SourceKind.COMPANY.value}:
        return settings.default_source_policy
    return PolicyMode.PUBLIC_READ_ONLY
