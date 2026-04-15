from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from findmyjob.core.enums import CompanySizeBucket, ExperienceLevel
from findmyjob.core.runtime import AppRuntime
from findmyjob.core.types import JobSearchQuery
from findmyjob.db.repositories import JobRepository
from findmyjob.db.search import search_jobs
from findmyjob.sources.normalizer import build_normalized_job


def _seed_job(
    runtime: AppRuntime,
    *,
    company: str,
    title: str,
    source_job_id: str,
    location_raw: str,
    compensation,
    posted_at: datetime,
    company_size_bucket: CompanySizeBucket,
) -> str:
    posting = build_normalized_job(
        company_name=company,
        title=title,
        source="greenhouse",
        source_kind="greenhouse",
        source_job_id=source_job_id,
        posting_url=f"https://boards.greenhouse.io/{company.lower()}/jobs/{source_job_id}",
        apply_url=f"https://boards.greenhouse.io/{company.lower()}/jobs/{source_job_id}",
        location_raw=location_raw,
        employment_type="full_time",
        compensation=compensation,
        description="Build reliable backend systems.",
        posted_at=posted_at,
        company_size_bucket=company_size_bucket,
    )
    with runtime.session_scope() as session:
        return JobRepository(session).upsert_job(posting).id


def test_search_jobs_supports_structured_filters_without_keyword(tmp_path: Path) -> None:
    runtime = AppRuntime.bootstrap(tmp_path)
    now = datetime.now(timezone.utc)
    _seed_job(
        runtime,
        company="Acme",
        title="Entry Level Software Engineer",
        source_job_id="1",
        location_raw="Remote - United States",
        compensation=[{"min_cents": 12000000, "max_cents": 15000000, "currency_type": "USD", "pay_input_type": "yearly"}],
        posted_at=now - timedelta(days=2),
        company_size_bucket=CompanySizeBucket.MIDSIZE,
    )
    _seed_job(
        runtime,
        company="Beta",
        title="Senior Software Engineer",
        source_job_id="2",
        location_raw="Toronto, ON, Canada",
        compensation=None,
        posted_at=now - timedelta(days=1),
        company_size_bucket=CompanySizeBucket.ENTERPRISE,
    )
    _seed_job(
        runtime,
        company="Gamma",
        title="Software Engineer",
        source_job_id="3",
        location_raw="Remote - United States",
        compensation=[{"min_cents": 8000000, "max_cents": 9000000, "currency_type": "USD", "pay_input_type": "yearly"}],
        posted_at=now - timedelta(days=30),
        company_size_bucket=CompanySizeBucket.STARTUP,
    )

    with runtime.session_scope() as session:
        jobs = search_jobs(
            session,
            JobSearchQuery(
                countries=["US"],
                remote_only=True,
                experience_levels=[ExperienceLevel.ENTRY_LEVEL],
                posted_within_days=7,
                compensation_min=100000,
                compensation_currency="USD",
                company_size_buckets=[CompanySizeBucket.MIDSIZE],
            ),
        )

    assert [job.title for job in jobs] == ["Entry Level Software Engineer"]


def test_search_jobs_supports_keyword_and_unknown_experience_toggle(tmp_path: Path) -> None:
    runtime = AppRuntime.bootstrap(tmp_path)
    now = datetime.now(timezone.utc)
    _seed_job(
        runtime,
        company="Acme",
        title="Entry Level Backend Engineer",
        source_job_id="10",
        location_raw="Remote - United States",
        compensation=None,
        posted_at=now - timedelta(days=2),
        company_size_bucket=CompanySizeBucket.MIDSIZE,
    )
    _seed_job(
        runtime,
        company="Delta",
        title="Backend Engineer",
        source_job_id="11",
        location_raw="Remote - United States",
        compensation=None,
        posted_at=now - timedelta(days=2),
        company_size_bucket=CompanySizeBucket.MIDSIZE,
    )

    with runtime.session_scope() as session:
        strict = search_jobs(session, JobSearchQuery(keyword="engineer", experience_levels=[ExperienceLevel.ENTRY_LEVEL]))
        relaxed = search_jobs(
            session,
            JobSearchQuery(keyword="engineer", experience_levels=[ExperienceLevel.ENTRY_LEVEL], allow_unknown_experience_level=True),
        )

    assert [job.title for job in strict] == ["Entry Level Backend Engineer"]
    assert {job.title for job in relaxed} == {"Entry Level Backend Engineer", "Backend Engineer"}


def test_search_jobs_title_keywords_match_title_not_description(tmp_path: Path) -> None:
    runtime = AppRuntime.bootstrap(tmp_path)
    now = datetime.now(timezone.utc)
    _seed_job(
        runtime,
        company="Acme",
        title="Technical Recruiter",
        source_job_id="20",
        location_raw="Remote - United States",
        compensation=None,
        posted_at=now - timedelta(days=1),
        company_size_bucket=CompanySizeBucket.MIDSIZE,
    )
    with runtime.session_scope() as session:
        job = search_jobs(session, JobSearchQuery(title_keywords=["software engineer"]))

    assert [item.title for item in job] == []
