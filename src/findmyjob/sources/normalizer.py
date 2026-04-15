from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

from findmyjob.core.enums import CompanySizeBucket, CompensationInterval, ExperienceLevel, LocationScope, WorkplaceType
from findmyjob.core.filtering import explicit_country_codes, normalize_country_code, normalize_region_code, normalize_spaces
from findmyjob.core.types import NormalizedJobPosting

WHITESPACE_RE = re.compile(r"\s+")
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
REMOTE_RE = re.compile(r"\bremote\b|\bwork from home\b", re.IGNORECASE)
HYBRID_RE = re.compile(r"\bhybrid\b", re.IGNORECASE)
CITY_STATE_RE = re.compile(r"^(?P<city>[^,|]+?),\s*(?P<region>[A-Za-z]{2})\s*$")
CITY_REGION_COUNTRY_RE = re.compile(r"^(?P<city>[^,|]+?),\s*(?P<region>[^,|]+?),\s*(?P<country>[^,|]+)$")

ENTRY_PATTERNS = (
    re.compile(r"\bintern(ship)?\b", re.IGNORECASE),
    re.compile(r"\bnew grad\b", re.IGNORECASE),
    re.compile(r"\bgraduate\b", re.IGNORECASE),
    re.compile(r"\bentry[ -]?level\b", re.IGNORECASE),
    re.compile(r"\bjunior\b", re.IGNORECASE),
    re.compile(r"\bjr\.?\b", re.IGNORECASE),
    re.compile(r"\bapprentice\b", re.IGNORECASE),
)
ASSOCIATE_PATTERNS = (re.compile(r"\bassociate\b", re.IGNORECASE),)
MID_PATTERNS = (
    re.compile(r"\bmid[ -]?level\b", re.IGNORECASE),
    re.compile(r"\bintermediate\b", re.IGNORECASE),
)
SENIOR_PATTERNS = (
    re.compile(r"\bsenior\b", re.IGNORECASE),
    re.compile(r"\bsr\.?\b", re.IGNORECASE),
)
STAFF_PATTERNS = (re.compile(r"\bstaff\b", re.IGNORECASE),)
PRINCIPAL_PATTERNS = (
    re.compile(r"\bprincipal\b", re.IGNORECASE),
    re.compile(r"\bdistinguished\b", re.IGNORECASE),
)
MANAGER_PATTERNS = (re.compile(r"\bmanager\b", re.IGNORECASE),)
DIRECTOR_PATTERNS = (
    re.compile(r"\bdirector\b", re.IGNORECASE),
    re.compile(r"\bhead of\b", re.IGNORECASE),
    re.compile(r"\bvice president\b", re.IGNORECASE),
    re.compile(r"\bvp\b", re.IGNORECASE),
)

COMPENSATION_INTERVAL_ALIASES = {
    "hour": CompensationInterval.HOURLY,
    "hourly": CompensationInterval.HOURLY,
    "day": CompensationInterval.DAILY,
    "daily": CompensationInterval.DAILY,
    "week": CompensationInterval.WEEKLY,
    "weekly": CompensationInterval.WEEKLY,
    "month": CompensationInterval.MONTHLY,
    "monthly": CompensationInterval.MONTHLY,
    "salary": CompensationInterval.YEARLY,
    "annual": CompensationInterval.YEARLY,
    "annually": CompensationInterval.YEARLY,
    "year": CompensationInterval.YEARLY,
    "yearly": CompensationInterval.YEARLY,
}


@dataclass(slots=True)
class StructuredLocation:
    country_code: str | None = None
    region_code: str | None = None
    city: str | None = None
    location_scope: LocationScope = LocationScope.UNKNOWN
    workplace_type: WorkplaceType = WorkplaceType.UNKNOWN
    remote_country_codes: list[str] = field(default_factory=list)
    metadata_quality: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExperienceInference:
    level: ExperienceLevel = ExperienceLevel.UNKNOWN
    metadata_quality: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CompensationSummary:
    minimum: int | None = None
    maximum: int | None = None
    currency: str | None = None
    interval: CompensationInterval | None = None
    metadata_quality: dict[str, Any] = field(default_factory=dict)


def normalize_text(value: str) -> str:
    return WHITESPACE_RE.sub(" ", value).strip()


def slugify(value: str) -> str:
    normalized = normalize_text(value).lower()
    normalized = NON_ALNUM_RE.sub("-", normalized).strip("-")
    return normalized or "unknown"


def normalize_company_name(company_name: str) -> str:
    return slugify(company_name.replace("inc.", "inc").replace("llc", "").replace(",", " "))


def normalize_description(description: str) -> str:
    return normalize_text(re.sub(r"<[^>]+>", " ", description))


def detect_workplace_type(location: str | None, description: str) -> WorkplaceType:
    haystack = f"{location or ''} {description}".lower()
    if HYBRID_RE.search(haystack):
        return WorkplaceType.HYBRID
    if REMOTE_RE.search(haystack):
        return WorkplaceType.REMOTE
    if location:
        return WorkplaceType.ONSITE
    return WorkplaceType.UNKNOWN


def location_bucket(location: str | None) -> str:
    if not location:
        return "unknown"
    return slugify(location)


def _conservative_country_code(value: str | None) -> str | None:
    cleaned = normalize_spaces(value)
    if not cleaned:
        return None
    upper = cleaned.upper()
    if len(upper) <= 2 and upper not in {"US", "UK", "GB"}:
        return None
    return normalize_country_code(cleaned)


def _region_from_fragment(value: str | None) -> tuple[str | None, str | None]:
    normalized = normalize_spaces(value)
    if not normalized:
        return None, None
    region_code = normalize_region_code(normalized)
    if region_code is None:
        return None, None
    if normalize_region_code(normalized) == region_code and len(normalized) == 2 and normalized.upper() in {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}:
        return region_code, "CA"
    if normalize_region_code(normalized) == region_code and len(normalized) == 2 and normalized.upper() in {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DC", "DE", "FL", "GA", "HI", "IA", "ID", "IL", "IN", "KS", "KY", "LA", "MA", "MD", "ME", "MI", "MN", "MO", "MS", "MT", "NC", "ND", "NE", "NH", "NJ", "NM", "NV", "NY", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VA", "VT", "WA", "WI", "WV", "WY"}:
        return region_code, "US"
    upper = normalized.upper()
    if upper in {"ONTARIO", "QUEBEC", "ALBERTA", "BRITISH COLUMBIA", "MANITOBA", "NEW BRUNSWICK", "NEWFOUNDLAND AND LABRADOR", "NOVA SCOTIA", "PRINCE EDWARD ISLAND", "SASKATCHEWAN", "NORTHWEST TERRITORIES", "NUNAVUT", "YUKON"}:
        return region_code, "CA"
    return region_code, "US"


def _clean_city(value: str | None) -> str | None:
    cleaned = normalize_spaces(value)
    if not cleaned:
        return None
    return cleaned


def parse_structured_location(location: str | None, description: str = "") -> StructuredLocation:
    workplace_type = detect_workplace_type(location, description)
    structured = StructuredLocation(workplace_type=workplace_type)
    if not location:
        structured.metadata_quality["location"] = "missing"
        return structured

    cleaned = normalize_spaces(location)
    if "|" in cleaned or ";" in cleaned:
        segments = [segment.strip() for segment in re.split(r"[|;]", cleaned) if segment.strip()]
    else:
        segments = [cleaned]

    explicit_countries = explicit_country_codes(cleaned)
    if workplace_type == WorkplaceType.REMOTE:
        if explicit_countries:
            structured.country_code = explicit_countries[0]
            structured.remote_country_codes = [explicit_countries[0]]
            structured.location_scope = LocationScope.REMOTE_US if explicit_countries[0] == "US" else LocationScope.REMOTE_COUNTRY
            structured.metadata_quality["location"] = "remote_explicit_country"
            return structured
        if re.search(r"\b(us|u\.s\.?|usa|united states)\b", cleaned, re.IGNORECASE):
            structured.country_code = "US"
            structured.remote_country_codes = ["US"]
            structured.location_scope = LocationScope.REMOTE_US
            structured.metadata_quality["location"] = "remote_us"
            return structured
        structured.location_scope = LocationScope.REMOTE_GLOBAL
        structured.metadata_quality["location"] = "remote_without_geography"
        return structured

    segment = segments[0]
    city: str | None = None
    region_code: str | None = None
    country_code: str | None = explicit_countries[0] if explicit_countries else None

    match = CITY_REGION_COUNTRY_RE.match(segment)
    if match:
        city = _clean_city(match.group("city"))
        region_code, inferred_country = _region_from_fragment(match.group("region"))
        country_code = _conservative_country_code(match.group("country")) or country_code or inferred_country
    else:
        match = CITY_STATE_RE.match(segment)
        if match:
            city = _clean_city(match.group("city"))
            region_code, inferred_country = _region_from_fragment(match.group("region"))
            country_code = country_code or inferred_country
        else:
            parts = [part.strip() for part in segment.split(",") if part.strip()]
            if len(parts) >= 2:
                region_code, inferred_country = _region_from_fragment(parts[-1])
                if region_code:
                    country_code = country_code or inferred_country
                    city = _clean_city(parts[0]) if len(parts) >= 2 else None
                else:
                    maybe_country = _conservative_country_code(parts[-1])
                    if maybe_country:
                        country_code = maybe_country
                        if len(parts) == 2:
                            city = _clean_city(parts[0])
            elif len(parts) == 1:
                region_code, inferred_country = _region_from_fragment(parts[0])
                if region_code:
                    country_code = country_code or inferred_country
                else:
                    maybe_country = _conservative_country_code(parts[0])
                    if maybe_country:
                        country_code = maybe_country

    structured.country_code = country_code
    structured.region_code = region_code
    structured.city = city
    if city:
        structured.location_scope = LocationScope.CITY_SPECIFIC
        structured.metadata_quality["location"] = "city_region_country"
    elif region_code:
        structured.location_scope = LocationScope.STATE_WIDE
        structured.metadata_quality["location"] = "region_only"
    elif country_code:
        structured.location_scope = LocationScope.COUNTRY_WIDE
        structured.metadata_quality["location"] = "country_only"
    else:
        structured.metadata_quality["location"] = "unparsed"
    return structured


def infer_experience_level(title: str, description: str = "") -> ExperienceInference:
    haystacks = [(normalize_spaces(title), "title"), (normalize_spaces(description), "description")]
    patterns = [
        (ExperienceLevel.DIRECTOR, DIRECTOR_PATTERNS),
        (ExperienceLevel.MANAGER, MANAGER_PATTERNS),
        (ExperienceLevel.PRINCIPAL, PRINCIPAL_PATTERNS),
        (ExperienceLevel.STAFF, STAFF_PATTERNS),
        (ExperienceLevel.SENIOR, SENIOR_PATTERNS),
        (ExperienceLevel.MID_LEVEL, MID_PATTERNS),
        (ExperienceLevel.ASSOCIATE, ASSOCIATE_PATTERNS),
        (ExperienceLevel.ENTRY_LEVEL, ENTRY_PATTERNS),
    ]
    for haystack, source in haystacks:
        if not haystack:
            continue
        for level, compiled_patterns in patterns:
            if any(pattern.search(haystack) for pattern in compiled_patterns):
                return ExperienceInference(level=level, metadata_quality={"experience_level": f"inferred_from_{source}"})
    return ExperienceInference(metadata_quality={"experience_level": "unknown"})


def parse_datetime_value(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned.isdigit():
            return datetime.fromtimestamp(int(cleaned), tz=timezone.utc)
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(cleaned)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def extract_posted_at(*payloads: Any) -> datetime | None:
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in ("posted_at", "published_at", "first_published", "created_at", "listed_at"):
            parsed = parse_datetime_value(payload.get(key))
            if parsed is not None:
                return parsed
    return None


def extract_source_updated_at(*payloads: Any) -> datetime | None:
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in ("updated_at", "updatedAt", "last_updated", "modified_at"):
            parsed = parse_datetime_value(payload.get(key))
            if parsed is not None:
                return parsed
    return None


def _normalize_currency(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = normalize_spaces(str(value)).upper()
    return cleaned or None


def _normalize_interval(value: Any) -> CompensationInterval | None:
    if value is None:
        return None
    cleaned = normalize_spaces(str(value)).lower()
    return COMPENSATION_INTERVAL_ALIASES.get(cleaned)


def _parse_amount(key: str, value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if "cent" in key.lower():
        amount = amount / Decimal("100")
    try:
        return int(amount)
    except (ValueError, OverflowError):
        return None


def _range_candidates(payload: dict[str, Any]) -> tuple[int | None, int | None]:
    for minimum_key, maximum_key in (
        ("min", "max"),
        ("min_amount", "max_amount"),
        ("minimum", "maximum"),
        ("min_cents", "max_cents"),
        ("minimum_cents", "maximum_cents"),
        ("min_value", "max_value"),
        ("lower_bound", "upper_bound"),
    ):
        if minimum_key in payload or maximum_key in payload:
            return _parse_amount(minimum_key, payload.get(minimum_key)), _parse_amount(maximum_key, payload.get(maximum_key))
    return None, None


def normalize_compensation(payload: dict[str, Any] | list[dict[str, Any]] | None) -> CompensationSummary:
    summary = CompensationSummary(metadata_quality={"compensation": "unknown"})
    if payload is None:
        return summary

    candidates = payload if isinstance(payload, list) else [payload]
    normalized_rows: list[tuple[int | None, int | None, str | None, CompensationInterval | None]] = []
    for row in candidates:
        if not isinstance(row, dict):
            continue
        minimum, maximum = _range_candidates(row)
        currency = None
        for currency_key in ("currency", "currency_type", "currency_code", "currencyCode"):
            currency = _normalize_currency(row.get(currency_key)) or currency
        interval = None
        for interval_key in ("interval", "pay_interval", "compensation_interval", "pay_input_type", "salary_type"):
            interval = _normalize_interval(row.get(interval_key)) or interval
        if minimum is None and maximum is None:
            continue
        normalized_rows.append((minimum, maximum, currency, interval))

    if not normalized_rows:
        summary.metadata_quality["compensation"] = "present_but_unparsed"
        return summary

    mins = [value for value, _max, _currency, _interval in normalized_rows if value is not None]
    maxes = [value for _min, value, _currency, _interval in normalized_rows if value is not None]
    currencies = [currency for _min, _max, currency, _interval in normalized_rows if currency]
    intervals = [interval for _min, _max, _currency, interval in normalized_rows if interval is not None]
    summary.minimum = min(mins) if mins else None
    summary.maximum = max(maxes) if maxes else None
    summary.currency = currencies[0] if currencies else None
    summary.interval = intervals[0] if intervals else None
    summary.metadata_quality["compensation"] = "normalized"
    return summary


def shingle_hash(text: str, size: int = 3) -> str:
    tokens = [token for token in slugify(text).split("-") if token]
    if len(tokens) < size:
        shingles = tokens
    else:
        shingles = [" ".join(tokens[index : index + size]) for index in range(len(tokens) - size + 1)]
    top = [item for item, _count in Counter(shingles).most_common(24)]
    return hashlib.sha256("|".join(top).encode("utf-8")).hexdigest()


def identity_key(source_kind: str, company_key: str, source_job_id: str) -> str:
    return hashlib.sha256(f"{source_kind}|{company_key}|{source_job_id}".encode("utf-8")).hexdigest()


def duplicate_cluster_key(company_key: str, title: str, location: str | None, workplace_type: WorkplaceType, description: str) -> str:
    payload = "|".join(
        [
            company_key,
            slugify(title),
            location_bucket(location),
            workplace_type.value,
            shingle_hash(description),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def domain_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    return parsed.netloc or None


def build_normalized_job(
    *,
    company_name: str,
    title: str,
    source: str,
    source_kind: str,
    source_job_id: str,
    posting_url: str,
    apply_url: str | None,
    location_raw: str | None,
    employment_type: str | None,
    compensation: dict[str, Any] | list[dict[str, Any]] | None,
    description: str,
    posted_at: Any = None,
    source_updated_at: Any = None,
    company_size_bucket: CompanySizeBucket = CompanySizeBucket.UNKNOWN,
    company_employee_count_min: int | None = None,
    company_employee_count_max: int | None = None,
    metadata_quality: dict[str, Any] | None = None,
    notes: dict[str, Any] | None = None,
) -> NormalizedJobPosting:
    normalized_company = normalize_company_name(company_name)
    normalized_description = normalize_description(description)
    location_details = parse_structured_location(location_raw, normalized_description)
    experience = infer_experience_level(title, normalized_description)
    compensation_summary = normalize_compensation(compensation)
    quality = {
        **location_details.metadata_quality,
        **experience.metadata_quality,
        **compensation_summary.metadata_quality,
        **(metadata_quality or {}),
    }
    return NormalizedJobPosting(
        company_name=company_name,
        company_key=normalized_company,
        title=normalize_text(title),
        source=source,
        source_kind=source_kind,
        source_job_id=str(source_job_id),
        posting_url=posting_url,
        apply_url=apply_url,
        location_raw=location_raw,
        location_normalized=location_bucket(location_raw),
        country_code=location_details.country_code,
        region_code=location_details.region_code,
        city=location_details.city,
        location_scope=location_details.location_scope,
        workplace_type=location_details.workplace_type,
        employment_type=employment_type,
        experience_level=experience.level,
        posted_at=parse_datetime_value(posted_at),
        source_updated_at=parse_datetime_value(source_updated_at),
        compensation=compensation,
        compensation_min=compensation_summary.minimum,
        compensation_max=compensation_summary.maximum,
        compensation_currency=compensation_summary.currency,
        compensation_interval=compensation_summary.interval,
        remote_country_codes=location_details.remote_country_codes,
        company_employee_count_min=company_employee_count_min,
        company_employee_count_max=company_employee_count_max,
        company_size_bucket=company_size_bucket,
        metadata_quality=quality,
        description=description,
        normalized_description=normalized_description,
        discovered_at=datetime.now(timezone.utc),
        job_identity_key=identity_key(source_kind, normalized_company, str(source_job_id)),
        duplicate_cluster_key=duplicate_cluster_key(normalized_company, title, location_raw, location_details.workplace_type, normalized_description),
        notes={"source_domain": domain_from_url(posting_url), **(notes or {})},
    )
