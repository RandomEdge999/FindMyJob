from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from typer.testing import CliRunner

from findmyjob.cli.main import app
from findmyjob.core.enums import CompanySizeBucket
from findmyjob.core.runtime import AppRuntime
from findmyjob.db.repositories import JobRepository
from findmyjob.sources.normalizer import build_normalized_job

runner = CliRunner()


def _seed(runtime: AppRuntime, *, company_name: str, title: str, source_job_id: str, location_raw: str, posted_at: datetime, company_size_bucket: CompanySizeBucket) -> None:
    posting = build_normalized_job(
        company_name=company_name,
        title=title,
        source="greenhouse",
        source_kind="greenhouse",
        source_job_id=source_job_id,
        posting_url=f"https://boards.greenhouse.io/acme/jobs/{source_job_id}",
        apply_url=f"https://boards.greenhouse.io/acme/jobs/{source_job_id}",
        location_raw=location_raw,
        employment_type="full_time",
        compensation=[{"min_cents": 12000000, "max_cents": 15000000, "currency_type": "USD", "pay_input_type": "yearly"}],
        description="Build reliable backend systems.",
        posted_at=posted_at,
        company_size_bucket=company_size_bucket,
    )
    with runtime.session_scope() as session:
        JobRepository(session).upsert_job(posting)


def test_jobs_search_cli_accepts_structured_filters(tmp_path: Path) -> None:
    runtime = AppRuntime.bootstrap(tmp_path)
    now = datetime.now(timezone.utc)
    _seed(runtime, company_name="Acme", title="Entry Level Software Engineer", source_job_id="1", location_raw="Remote - United States", posted_at=now - timedelta(days=1), company_size_bucket=CompanySizeBucket.MIDSIZE)
    _seed(runtime, company_name="Beta", title="Senior Software Engineer", source_job_id="2", location_raw="Chicago, IL", posted_at=now - timedelta(days=40), company_size_bucket=CompanySizeBucket.ENTERPRISE)

    result = runner.invoke(
        app,
        [
            "jobs",
            "search",
            "--workspace",
            str(tmp_path),
            "--country",
            "US",
            "--remote-only",
            "--experience-level",
            "entry_level",
            "--posted-within-days",
            "7",
            "--compensation-min",
            "100000",
            "--company-size",
            "midsize",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Entry Level Software Engineer" in result.output
    assert "Senior Software Engineer" not in result.output
