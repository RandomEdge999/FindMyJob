from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from findmyjob.core.enums import JobLifecycleStatus
from findmyjob.core.filtering import enum_values, normalize_city, normalize_country_code, normalize_region_code
from findmyjob.core.types import JobSearchQuery
from findmyjob.db.models import JobPosting


def _append_in_condition(sql: list[str], params: dict[str, Any], column: str, values: list[str], prefix: str) -> None:
    if not values:
        return
    placeholders: list[str] = []
    for index, value in enumerate(values):
        key = f"{prefix}_{index}"
        placeholders.append(f":{key}")
        params[key] = value
    sql.append(f"AND {column} IN ({', '.join(placeholders)})")


def _append_like_condition(sql: list[str], params: dict[str, Any], location_tokens: list[str]) -> None:
    if not location_tokens:
        return
    clauses: list[str] = []
    for index, token in enumerate(location_tokens):
        key = f"location_{index}"
        params[key] = f"%{token}%"
        clauses.append(
            "(" +
            "LOWER(COALESCE(job_postings.location_raw, '')) LIKE :{key} OR "
            "LOWER(COALESCE(job_postings.location_normalized, '')) LIKE :{key} OR "
            "LOWER(COALESCE(job_postings.city, '')) LIKE :{key} OR "
            "LOWER(COALESCE(job_postings.region_code, '')) LIKE :{key} OR "
            "LOWER(COALESCE(job_postings.country_code, '')) LIKE :{key}".format(key=key)
            + ")"
        )
    sql.append(f"AND ({' OR '.join(clauses)})")


def _base_sql(query: JobSearchQuery) -> tuple[list[str], dict[str, Any]]:
    sql = [
        "SELECT job_postings.id",
        "FROM job_postings",
        "JOIN companies ON companies.id = job_postings.company_id",
        "LEFT JOIN qualification_results ON qualification_results.job_posting_id = job_postings.id",
        "WHERE 1 = 1",
    ]
    params: dict[str, Any] = {"limit": query.limit}
    if query.source_adapter:
        sql.append("AND job_postings.source_adapter = :source_adapter")
        params["source_adapter"] = query.source_adapter
    if query.board_token:
        sql.append("AND job_postings.board_token = :board_token")
        params["board_token"] = query.board_token
    if query.active_only:
        sql.append("AND LOWER(COALESCE(job_postings.lifecycle_status, '')) != :inactive_status")
        params["inactive_status"] = JobLifecycleStatus.INACTIVE.value

    title_keywords = [value.strip().lower() for value in getattr(query, "title_keywords", []) if value and value.strip()]
    if title_keywords:
        clauses: list[str] = []
        for index, token in enumerate(title_keywords):
            key = f"title_keyword_{index}"
            params[key] = f"%{token}%"
            clauses.append(
                "LOWER(COALESCE(job_postings.title, '')) LIKE :{key}".format(key=key)
            )
        sql.append(f"AND ({' OR '.join(clauses)})")
    if query.compensation_present is True:
        sql.append("AND job_postings.compensation IS NOT NULL")
    elif query.compensation_present is False:
        sql.append("AND job_postings.compensation IS NULL")
    if query.sponsorship_fit:
        sql.append("AND qualification_results.fit = :sponsorship_fit")
        params["sponsorship_fit"] = query.sponsorship_fit

    location_tokens = [value.strip().lower() for value in query.locations if value and value.strip()]
    _append_like_condition(sql, params, location_tokens)

    workplace_values = sorted(enum_values(query.workplace_types))
    _append_in_condition(sql, params, "job_postings.workplace_type", workplace_values, "workplace")

    employment_values = sorted({value.strip().lower() for value in query.employment_types if value and value.strip()})
    if employment_values:
        placeholders: list[str] = []
        for index, value in enumerate(employment_values):
            key = f"employment_{index}"
            placeholders.append(f":{key}")
            params[key] = value
        sql.append(f"AND LOWER(COALESCE(job_postings.employment_type, '')) IN ({', '.join(placeholders)})")

    country_values = sorted({code for code in (normalize_country_code(value) for value in query.countries) if code})
    _append_in_condition(sql, params, "job_postings.country_code", country_values, "country")

    region_values = sorted({code for code in (normalize_region_code(value) for value in query.regions) if code})
    _append_in_condition(sql, params, "job_postings.region_code", region_values, "region")

    city_values = sorted({city for city in (normalize_city(value) for value in query.cities) if city})
    if city_values:
        placeholders: list[str] = []
        for index, value in enumerate(city_values):
            key = f"city_{index}"
            placeholders.append(f":{key}")
            params[key] = value
        sql.append(f"AND LOWER(COALESCE(job_postings.city, '')) IN ({', '.join(placeholders)})")

    location_scope_values = sorted(enum_values(query.location_scopes))
    _append_in_condition(sql, params, "job_postings.location_scope", location_scope_values, "location_scope")

    experience_values = sorted(enum_values(query.experience_levels))
    if experience_values:
        placeholders: list[str] = []
        for index, value in enumerate(experience_values):
            key = f"experience_{index}"
            placeholders.append(f":{key}")
            params[key] = value
        clause = f"job_postings.experience_level IN ({', '.join(placeholders)})"
        if query.allow_unknown_experience_level:
            clause = f"({clause} OR job_postings.experience_level = 'unknown')"
        sql.append(f"AND {clause}")

    if query.posted_within_days is not None:
        params["posted_cutoff"] = datetime.now(timezone.utc) - timedelta(days=int(query.posted_within_days))
        sql.append("AND job_postings.posted_at IS NOT NULL AND job_postings.posted_at >= :posted_cutoff")

    if query.compensation_min is not None:
        params["compensation_min"] = query.compensation_min
        clause = "COALESCE(job_postings.compensation_max, job_postings.compensation_min) >= :compensation_min"
        if query.allow_unknown_compensation:
            clause = f"(({clause}) OR (job_postings.compensation_min IS NULL AND job_postings.compensation_max IS NULL))"
        else:
            clause = f"({clause})"
        sql.append(f"AND {clause}")

    if query.compensation_currency:
        params["compensation_currency"] = query.compensation_currency.upper()
        clause = "UPPER(COALESCE(job_postings.compensation_currency, '')) = :compensation_currency"
        if query.allow_unknown_compensation:
            clause = f"({clause} OR job_postings.compensation_currency IS NULL)"
        sql.append(f"AND {clause}")

    company_size_values = sorted(enum_values(query.company_size_buckets))
    _append_in_condition(sql, params, "companies.company_size_bucket", company_size_values, "company_size")

    if query.remote_only:
        sql.append("AND (job_postings.workplace_type = 'remote' OR job_postings.location_scope LIKE 'remote_%')")

    return sql, params


def search_jobs(session: Session, query: JobSearchQuery) -> Sequence[JobPosting]:
    sql, params = _base_sql(query)
    if query.keyword:
        sql.insert(2, "JOIN job_postings_fts ON job_postings_fts.rowid = job_postings.rowid")
        sql.append("AND job_postings_fts MATCH :keyword")
        params["keyword"] = query.keyword
        sql.append("ORDER BY bm25(job_postings_fts), job_postings.discovered_at DESC LIMIT :limit")
    else:
        sql.append("ORDER BY job_postings.discovered_at DESC LIMIT :limit")

    ids = [row[0] for row in session.execute(text("\n".join(sql)), params).all()]
    if not ids:
        return []
    jobs = session.scalars(select(JobPosting).where(JobPosting.id.in_(ids))).all()
    by_id = {job.id: job for job in jobs}
    return [by_id[job_id] for job_id in ids if job_id in by_id]

