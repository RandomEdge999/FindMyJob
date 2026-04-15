"""Source automation classification.

Classifies each discovered job/application by board family and automation
tier so the autonomous loop can decide whether to auto-submit, prepare-only,
or skip entirely.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class AutomationTier(StrEnum):
    AUTO_SUBMIT_SUPPORTED = "auto_submit_supported"
    PREPARE_ONLY = "prepare_only"
    UNSUPPORTED_HIGH_FRICTION = "unsupported_high_friction"


class BoardFamily(StrEnum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    WORKDAY = "workday"
    ICIMS = "icims"
    TALEO = "taleo"
    SUCCESSFACTORS = "successfactors"
    SMARTRECRUITERS = "smartrecruiters"
    JOBVITE = "jobvite"
    BAMBOOHR = "bamboohr"
    UNKNOWN = "unknown"


class BoardClassification(BaseModel):
    """Classification result for a single job or application URL."""

    board_family: BoardFamily = BoardFamily.UNKNOWN
    automation_tier: AutomationTier = AutomationTier.UNSUPPORTED_HIGH_FRICTION
    supports_auto_submit: bool = False
    automation_skip_reason: str | None = None
    detection_method: str = "unknown"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Domain / URL pattern tables
# ---------------------------------------------------------------------------

_GREENHOUSE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"boards\.greenhouse\.io", re.IGNORECASE),
    re.compile(r"boards-api\.greenhouse\.io", re.IGNORECASE),
    re.compile(r"job-boards\.greenhouse\.io", re.IGNORECASE),
    re.compile(r"my\.greenhouse\.io", re.IGNORECASE),
]

_LEVER_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"jobs\.lever\.co", re.IGNORECASE),
    re.compile(r"api\.lever\.co", re.IGNORECASE),
]

_ASHBY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"jobs\.ashbyhq\.com", re.IGNORECASE),
    re.compile(r"api\.ashbyhq\.com", re.IGNORECASE),
]

_WORKDAY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\.myworkdayjobs\.com", re.IGNORECASE),
    re.compile(r"\.wd\d+\.myworkdaysite\.com", re.IGNORECASE),
    re.compile(r"workday\.com/.*?/job/", re.IGNORECASE),
]

_ICIMS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\.icims\.com", re.IGNORECASE),
    re.compile(r"careers-.*\.icims\.com", re.IGNORECASE),
]

_TALEO_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\.taleo\.net", re.IGNORECASE),
    re.compile(r"\.oraclecloud\.com/hcmUI/CandidateExperience", re.IGNORECASE),
]

_SUCCESSFACTORS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\.successfactors\.com", re.IGNORECASE),
    re.compile(r"\.successfactors\.eu", re.IGNORECASE),
]

_SMARTRECRUITERS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"jobs\.smartrecruiters\.com", re.IGNORECASE),
    re.compile(r"api\.smartrecruiters\.com", re.IGNORECASE),
]

_JOBVITE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"jobs\.jobvite\.com", re.IGNORECASE),
    re.compile(r"app\.jobvite\.com", re.IGNORECASE),
]

_BAMBOOHR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\.bamboohr\.com/careers", re.IGNORECASE),
    re.compile(r"\.bamboohr\.com/jobs", re.IGNORECASE),
]

_FAMILY_PATTERN_MAP: list[tuple[BoardFamily, list[re.Pattern[str]]]] = [
    (BoardFamily.GREENHOUSE, _GREENHOUSE_PATTERNS),
    (BoardFamily.LEVER, _LEVER_PATTERNS),
    (BoardFamily.ASHBY, _ASHBY_PATTERNS),
    (BoardFamily.WORKDAY, _WORKDAY_PATTERNS),
    (BoardFamily.ICIMS, _ICIMS_PATTERNS),
    (BoardFamily.TALEO, _TALEO_PATTERNS),
    (BoardFamily.SUCCESSFACTORS, _SUCCESSFACTORS_PATTERNS),
    (BoardFamily.SMARTRECRUITERS, _SMARTRECRUITERS_PATTERNS),
    (BoardFamily.JOBVITE, _JOBVITE_PATTERNS),
    (BoardFamily.BAMBOOHR, _BAMBOOHR_PATTERNS),
]

# Tier policy: which families support which tier
_FAMILY_TIER_MAP: dict[BoardFamily, AutomationTier] = {
    BoardFamily.GREENHOUSE: AutomationTier.AUTO_SUBMIT_SUPPORTED,
    BoardFamily.LEVER: AutomationTier.AUTO_SUBMIT_SUPPORTED,
    BoardFamily.ASHBY: AutomationTier.AUTO_SUBMIT_SUPPORTED,
    BoardFamily.WORKDAY: AutomationTier.UNSUPPORTED_HIGH_FRICTION,
    BoardFamily.ICIMS: AutomationTier.UNSUPPORTED_HIGH_FRICTION,
    BoardFamily.TALEO: AutomationTier.UNSUPPORTED_HIGH_FRICTION,
    BoardFamily.SUCCESSFACTORS: AutomationTier.UNSUPPORTED_HIGH_FRICTION,
    BoardFamily.SMARTRECRUITERS: AutomationTier.PREPARE_ONLY,
    BoardFamily.JOBVITE: AutomationTier.PREPARE_ONLY,
    BoardFamily.BAMBOOHR: AutomationTier.PREPARE_ONLY,
    BoardFamily.UNKNOWN: AutomationTier.UNSUPPORTED_HIGH_FRICTION,
}

_SKIP_REASONS: dict[AutomationTier, str | None] = {
    AutomationTier.AUTO_SUBMIT_SUPPORTED: None,
    AutomationTier.PREPARE_ONLY: "board_family_prepare_only",
    AutomationTier.UNSUPPORTED_HIGH_FRICTION: "board_family_unsupported_high_friction",
}


def detect_board_family(url: str) -> tuple[BoardFamily, str]:
    """Detect board family from a URL using domain/path heuristics.

    Returns (board_family, detection_method).
    """
    if not url:
        return BoardFamily.UNKNOWN, "empty_url"
    for family, patterns in _FAMILY_PATTERN_MAP:
        for pattern in patterns:
            if pattern.search(url):
                return family, f"url_pattern:{pattern.pattern}"
    return BoardFamily.UNKNOWN, "no_match"


# ---------------------------------------------------------------------------
# HTML content-based heuristics for custom-domain boards
# ---------------------------------------------------------------------------

_HTML_CONTENT_SIGNALS: list[tuple[BoardFamily, list[str], str]] = [
    # Greenhouse
    (BoardFamily.GREENHOUSE, [
        "greenhouse.io",
        "data-greenhouse",
        "grnhse_app",
        "greenhouse-job-board",
        "boards.greenhouse.io",
        "grnhse_iframe",
    ], "html_content:greenhouse"),
    # Lever
    (BoardFamily.LEVER, [
        "lever.co",
        "data-lever",
        "lever-jobs-container",
        "lever-application",
        "jobs.lever.co",
    ], "html_content:lever"),
    # Ashby
    (BoardFamily.ASHBY, [
        "ashbyhq.com",
        "ashby-job-posting",
        "data-ashby",
    ], "html_content:ashby"),
    # Workday
    (BoardFamily.WORKDAY, [
        "workday.com",
        "myworkdayjobs.com",
        "wd-uiautomation",
        "workdayCustom",
        "WORKDAY_",
    ], "html_content:workday"),
    # iCIMS
    (BoardFamily.ICIMS, [
        "icims.com",
        "iCIMS",
        "data-icims",
        "icims_content",
    ], "html_content:icims"),
    # Taleo
    (BoardFamily.TALEO, [
        "taleo.net",
        "oraclecloud.com/hcmUI",
        "taleologin",
        "data-taleo",
    ], "html_content:taleo"),
    # SuccessFactors
    (BoardFamily.SUCCESSFACTORS, [
        "successfactors.com",
        "successfactors.eu",
        "sap-successfactors",
    ], "html_content:successfactors"),
    # SmartRecruiters
    (BoardFamily.SMARTRECRUITERS, [
        "smartrecruiters.com",
        "smrtr.io",
        "smartrecruiters",
    ], "html_content:smartrecruiters"),
    # Jobvite
    (BoardFamily.JOBVITE, [
        "jobvite.com",
        "data-jobvite",
        "jv-careersite",
    ], "html_content:jobvite"),
    # BambooHR
    (BoardFamily.BAMBOOHR, [
        "bamboohr.com",
        "BambooHR",
        "data-bamboohr",
    ], "html_content:bamboohr"),
]


def detect_board_family_from_html(html: str) -> tuple[BoardFamily, str]:
    """Detect board family from page HTML content.

    Uses script sources, meta tags, class names, and other DOM signals
    to identify boards behind custom domains.

    Returns (board_family, detection_method).
    """
    if not html:
        return BoardFamily.UNKNOWN, "empty_html"
    lowered = html.lower()
    for family, signals, method in _HTML_CONTENT_SIGNALS:
        matches = sum(1 for signal in signals if signal.lower() in lowered)
        if matches >= 2:
            return family, method
    return BoardFamily.UNKNOWN, "no_html_match"


def detect_board_family_from_source_kind(source_kind: str) -> tuple[BoardFamily, str]:
    """Map a known source_kind string to a board family."""
    mapping = {
        "greenhouse": BoardFamily.GREENHOUSE,
        "lever": BoardFamily.LEVER,
        "ashby": BoardFamily.ASHBY,
    }
    family = mapping.get(source_kind.lower())
    if family is not None:
        return family, f"source_kind:{source_kind}"
    return BoardFamily.UNKNOWN, f"source_kind_unknown:{source_kind}"


def classify_job(
    *,
    source_kind: str | None = None,
    apply_url: str | None = None,
    posting_url: str | None = None,
    source_adapter: str | None = None,
    notes: dict[str, Any] | None = None,
    page_html: str | None = None,
) -> BoardClassification:
    """Classify a job for automation tier.

    Uses source_kind first (cheapest), then URL heuristics, then page content.
    """
    family = BoardFamily.UNKNOWN
    method = "unknown"

    # 1. Try source_kind (from adapter)
    if source_kind:
        family, method = detect_board_family_from_source_kind(source_kind)

    # 2. Try source_adapter as fallback for source_kind
    if family == BoardFamily.UNKNOWN and source_adapter:
        family, method = detect_board_family_from_source_kind(source_adapter)

    # 3. Try apply_url
    if family == BoardFamily.UNKNOWN and apply_url:
        family, method = detect_board_family(apply_url)

    # 4. Try posting_url
    if family == BoardFamily.UNKNOWN and posting_url:
        family, method = detect_board_family(posting_url)

    # 5. Try page HTML content (for custom-domain boards)
    if family == BoardFamily.UNKNOWN and page_html:
        family, method = detect_board_family_from_html(page_html)

    tier = _FAMILY_TIER_MAP.get(family, AutomationTier.UNSUPPORTED_HIGH_FRICTION)
    skip_reason = _SKIP_REASONS.get(tier)

    confidence = 1.0 if family != BoardFamily.UNKNOWN else 0.0
    if method.startswith("url_pattern"):
        confidence = 0.9

    return BoardClassification(
        board_family=family,
        automation_tier=tier,
        supports_auto_submit=(tier == AutomationTier.AUTO_SUBMIT_SUPPORTED),
        automation_skip_reason=skip_reason,
        detection_method=method,
        confidence=confidence,
    )


def classify_url(url: str) -> BoardClassification:
    """Convenience: classify a single URL."""
    return classify_job(apply_url=url)


def is_auto_submittable(classification: BoardClassification) -> bool:
    """Check if a classification allows autonomous submission."""
    return classification.automation_tier == AutomationTier.AUTO_SUBMIT_SUPPORTED


def skip_reason_for_tier(tier: AutomationTier) -> str | None:
    """Return the standard skip reason for a tier, or None if submittable."""
    return _SKIP_REASONS.get(tier)
