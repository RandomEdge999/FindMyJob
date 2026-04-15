from __future__ import annotations

from collections.abc import Iterable
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from findmyjob.core.enums import CompanySizeBucket, ExperienceLevel, WorkplaceType

COUNTRY_ALIASES: dict[str, set[str]] = {
    "US": {"us", "u.s.", "u.s.a.", "usa", "united states", "united states of america"},
    "CA": {"canada"},
    "GB": {"gb", "uk", "u.k.", "great britain", "united kingdom"},
    "IE": {"ireland"},
    "DE": {"germany"},
    "FR": {"france"},
    "ES": {"spain"},
    "IT": {"italy"},
    "NL": {"netherlands", "the netherlands"},
    "PL": {"poland"},
    "PT": {"portugal"},
    "SE": {"sweden"},
    "NO": {"norway"},
    "DK": {"denmark"},
    "FI": {"finland"},
    "CH": {"switzerland"},
    "AT": {"austria"},
    "BE": {"belgium"},
    "AU": {"australia"},
    "NZ": {"new zealand"},
    "SG": {"singapore"},
    "IN": {"india"},
    "JP": {"japan"},
    "MX": {"mexico"},
    "BR": {"brazil"},
    "AR": {"argentina"},
}

US_STATE_CODES: dict[str, str] = {
    "ALABAMA": "AL",
    "ALASKA": "AK",
    "ARIZONA": "AZ",
    "ARKANSAS": "AR",
    "CALIFORNIA": "CA",
    "COLORADO": "CO",
    "CONNECTICUT": "CT",
    "DELAWARE": "DE",
    "DISTRICT OF COLUMBIA": "DC",
    "FLORIDA": "FL",
    "GEORGIA": "GA",
    "HAWAII": "HI",
    "IDAHO": "ID",
    "ILLINOIS": "IL",
    "INDIANA": "IN",
    "IOWA": "IA",
    "KANSAS": "KS",
    "KENTUCKY": "KY",
    "LOUISIANA": "LA",
    "MAINE": "ME",
    "MARYLAND": "MD",
    "MASSACHUSETTS": "MA",
    "MICHIGAN": "MI",
    "MINNESOTA": "MN",
    "MISSISSIPPI": "MS",
    "MISSOURI": "MO",
    "MONTANA": "MT",
    "NEBRASKA": "NE",
    "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH",
    "NEW JERSEY": "NJ",
    "NEW MEXICO": "NM",
    "NEW YORK": "NY",
    "NORTH CAROLINA": "NC",
    "NORTH DAKOTA": "ND",
    "OHIO": "OH",
    "OKLAHOMA": "OK",
    "OREGON": "OR",
    "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN",
    "TEXAS": "TX",
    "UTAH": "UT",
    "VERMONT": "VT",
    "VIRGINIA": "VA",
    "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI",
    "WYOMING": "WY",
}

CANADA_PROVINCE_CODES: dict[str, str] = {
    "ALBERTA": "AB",
    "BRITISH COLUMBIA": "BC",
    "MANITOBA": "MB",
    "NEW BRUNSWICK": "NB",
    "NEWFOUNDLAND AND LABRADOR": "NL",
    "NOVA SCOTIA": "NS",
    "ONTARIO": "ON",
    "PRINCE EDWARD ISLAND": "PE",
    "QUEBEC": "QC",
    "SASKATCHEWAN": "SK",
    "NORTHWEST TERRITORIES": "NT",
    "NUNAVUT": "NU",
    "YUKON": "YT",
}

USER_COUNTRY_LOOKUP: dict[str, str] = {}
for code, aliases in COUNTRY_ALIASES.items():
    USER_COUNTRY_LOOKUP[code] = code
    for alias in aliases:
        USER_COUNTRY_LOOKUP[alias.upper()] = code

USER_REGION_LOOKUP: dict[str, str] = {}
for name, code in US_STATE_CODES.items():
    USER_REGION_LOOKUP[name] = code
    USER_REGION_LOOKUP[code] = code
for name, code in CANADA_PROVINCE_CODES.items():
    USER_REGION_LOOKUP[name] = code
    USER_REGION_LOOKUP[code] = code


@dataclass(slots=True)
class FilterEvaluation:
    matched: bool = True
    score_delta: int = 0
    reasons: list[str] = field(default_factory=list)


def normalize_spaces(value: str | None) -> str:
    return " ".join((value or "").strip().split())


def normalize_country_code(value: str | None) -> str | None:
    cleaned = normalize_spaces(value)
    if not cleaned:
        return None
    upper = cleaned.upper()
    if len(upper) == 2 and upper.isalpha():
        return upper if upper in USER_COUNTRY_LOOKUP.values() else None
    return USER_COUNTRY_LOOKUP.get(upper)


def normalize_region_code(value: str | None) -> str | None:
    cleaned = normalize_spaces(value)
    if not cleaned:
        return None
    upper = cleaned.upper()
    if "-" in upper:
        upper = upper.rsplit("-", 1)[-1]
    return USER_REGION_LOOKUP.get(upper)


def normalize_city(value: str | None) -> str | None:
    cleaned = normalize_spaces(value)
    if not cleaned:
        return None
    return cleaned.casefold()


def enum_values(values: Iterable[Any] | None) -> set[str]:
    normalized: set[str] = set()
    for value in values or []:
        if value is None:
            continue
        normalized.add(str(getattr(value, "value", value)).strip().lower())
    return normalized


def value_or_unknown(value: Any, fallback: str = "unknown") -> str:
    if value is None:
        return fallback
    raw = str(getattr(value, "value", value)).strip().lower()
    return raw or fallback


def is_remote_job(job: Any) -> bool:
    workplace = value_or_unknown(getattr(job, "workplace_type", None))
    location_scope = value_or_unknown(getattr(job, "location_scope", None))
    return workplace == WorkplaceType.REMOTE.value or location_scope.startswith("remote_")


def explicit_country_codes(text: str) -> list[str]:
    lowered = normalize_spaces(text).casefold()
    matches: list[tuple[int, str]] = []
    for code, aliases in COUNTRY_ALIASES.items():
        matched_aliases = [alias for alias in aliases if re.search(r"(?<![a-z])" + re.escape(alias.casefold()) + r"(?![a-z])", lowered)]
        if matched_aliases:
            matches.append((max(len(alias) for alias in matched_aliases), code))
    matches.sort(reverse=True)
    ordered: list[str] = []
    for _size, code in matches:
        if code not in ordered:
            ordered.append(code)
    return ordered


def job_location_haystack(job: Any) -> str:
    parts = [
        getattr(job, "location_raw", None),
        getattr(job, "location_normalized", None),
        getattr(job, "city", None),
        getattr(job, "region_code", None),
        getattr(job, "country_code", None),
    ]
    return " ".join(normalize_spaces(part).casefold() for part in parts if part)


def _record_match(evaluation: FilterEvaluation, matched: bool, ok_reason: str, miss_reason: str) -> None:
    if matched:
        evaluation.score_delta += 5
        evaluation.reasons.append(ok_reason)
        return
    evaluation.matched = False
    evaluation.score_delta -= 20
    evaluation.reasons.append(miss_reason)


def evaluate_job_against_query(job: Any, query: Any) -> FilterEvaluation:
    evaluation = FilterEvaluation()
    title_keywords = [normalize_spaces(value).casefold() for value in getattr(query, "title_keywords", []) if normalize_spaces(value)]
    if title_keywords:
        haystack = normalize_spaces(getattr(job, 'title', None)).casefold()
        matched = any(keyword in haystack for keyword in title_keywords)
        _record_match(evaluation, matched, "Matched requested title keywords", "Title did not match requested keywords")

    locations = [normalize_spaces(value).casefold() for value in getattr(query, "locations", []) if normalize_spaces(value)]
    if locations:
        haystack = job_location_haystack(job)
        matched = any(token in haystack or (token == "remote" and is_remote_job(job)) for token in locations)
        _record_match(evaluation, matched, "Matched requested free-text location", "Location did not match requested free-text filters")

    workplace_values = enum_values(getattr(query, "workplace_types", []))
    if workplace_values:
        matched = value_or_unknown(getattr(job, "workplace_type", None)) in workplace_values
        _record_match(evaluation, matched, "Matched requested workplace type", "Workplace type did not match requested filters")

    employment_values = {normalize_spaces(value).strip().lower() for value in getattr(query, "employment_types", []) if normalize_spaces(value)}
    if employment_values:
        matched = normalize_spaces(getattr(job, "employment_type", None)).lower() in employment_values
        _record_match(evaluation, matched, "Matched requested employment type", "Employment type did not match requested filters")

    country_values = {code for code in (normalize_country_code(value) for value in getattr(query, "countries", [])) if code}
    if country_values:
        job_country_codes = {normalize_country_code(getattr(job, "country_code", None))}
        job_country_codes.update(normalize_country_code(code) for code in getattr(job, "remote_country_codes", []) or [])
        known_codes = {code for code in job_country_codes if code}
        # If the job has no country info at all, let it through (don't reject unknown locations)
        matched = not known_codes or bool(country_values & known_codes)
        _record_match(evaluation, matched, "Matched requested country", "Country did not match requested filters")

    region_values = {code for code in (normalize_region_code(value) for value in getattr(query, "regions", [])) if code}
    if region_values:
        matched = normalize_region_code(getattr(job, "region_code", None)) in region_values
        _record_match(evaluation, matched, "Matched requested region", "Region did not match requested filters")

    city_values = {city for city in (normalize_city(value) for value in getattr(query, "cities", [])) if city}
    if city_values:
        matched = normalize_city(getattr(job, "city", None)) in city_values
        _record_match(evaluation, matched, "Matched requested city", "City did not match requested filters")

    location_scope_values = enum_values(getattr(query, "location_scopes", []))
    if location_scope_values:
        matched = value_or_unknown(getattr(job, "location_scope", None)) in location_scope_values
        _record_match(evaluation, matched, "Matched requested location scope", "Location scope did not match requested filters")

    if bool(getattr(query, "remote_only", False)):
        _record_match(evaluation, is_remote_job(job), "Matched remote-only preference", "Role is not remote")

    experience_values = enum_values(getattr(query, "experience_levels", []))
    if experience_values:
        experience_level = value_or_unknown(getattr(job, "experience_level", None))
        matched = experience_level in experience_values
        if experience_level == ExperienceLevel.UNKNOWN.value and bool(getattr(query, "allow_unknown_experience_level", False)):
            matched = True
        _record_match(evaluation, matched, "Matched requested experience level", "Experience level did not match requested filters")

    company_size_values = enum_values(getattr(query, "company_size_buckets", []))
    if company_size_values:
        matched = value_or_unknown(getattr(job, "company_size_bucket", None), CompanySizeBucket.UNKNOWN.value) in company_size_values
        _record_match(evaluation, matched, "Matched requested company size", "Company size did not match requested filters")

    posted_within_days = getattr(query, "posted_within_days", None)
    if posted_within_days is not None:
        posted_at = getattr(job, "posted_at", None)
        if isinstance(posted_at, datetime):
            now = datetime.now(timezone.utc)
            if posted_at.tzinfo is None:
                posted_at = posted_at.replace(tzinfo=timezone.utc)
            matched = posted_at >= now - timedelta(days=int(posted_within_days))
            _record_match(evaluation, matched, "Matched requested recency window", "Posting age did not match requested filters")
        # If no posted_at date, don't reject — let job through

    compensation_currency = normalize_spaces(getattr(query, "compensation_currency", None)).upper()
    if compensation_currency:
        job_currency = normalize_spaces(getattr(job, "compensation_currency", None)).upper()
        matched = job_currency == compensation_currency
        if not job_currency and bool(getattr(query, "allow_unknown_compensation", False)):
            matched = True
        _record_match(evaluation, matched, "Matched requested compensation currency", "Compensation currency did not match requested filters")

    compensation_floor = getattr(query, "compensation_min", None)
    if compensation_floor is not None:
        min_value = getattr(job, "compensation_min", None)
        max_value = getattr(job, "compensation_max", None)
        matched = False
        if min_value is None and max_value is None:
            matched = bool(getattr(query, "allow_unknown_compensation", False))
        elif max_value is not None and max_value >= compensation_floor:
            matched = True
        elif min_value is not None and min_value >= compensation_floor:
            matched = True
        _record_match(evaluation, matched, "Matched requested compensation floor", "Compensation floor did not match requested filters")

    return evaluation
