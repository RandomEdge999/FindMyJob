from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from findmyjob.cli.main import app
from findmyjob.core.enums import CompanySizeBucket
from findmyjob.core.runtime import AppRuntime
from findmyjob.db.repositories import JobRepository
from findmyjob.sources.normalizer import build_normalized_job

runner = CliRunner()



def _seed_job(runtime: AppRuntime, *, title: str, source_job_id: str) -> None:
    posting = build_normalized_job(
        company_name="Acme",
        title=title,
        source="greenhouse",
        source_kind="greenhouse",
        source_job_id=source_job_id,
        posting_url=f"https://boards.greenhouse.io/acme/jobs/{source_job_id}",
        apply_url=f"https://boards.greenhouse.io/acme/jobs/{source_job_id}",
        location_raw="Remote - United States",
        employment_type="full_time",
        compensation=[{"min_cents": 12000000, "max_cents": 15000000, "currency_type": "USD", "pay_input_type": "yearly"}],
        description="Backend and platform engineering role.",
        posted_at=datetime.now(timezone.utc),
        company_size_bucket=CompanySizeBucket.MIDSIZE,
    )
    with runtime.session_scope() as session:
        JobRepository(session).upsert_job(posting)



def test_searches_cli_save_list_show_run_delete(tmp_path: Path) -> None:
    runtime = AppRuntime.bootstrap(tmp_path)
    _seed_job(runtime, title="Backend Platform Engineer", source_job_id="1")

    save = runner.invoke(
        app,
        [
            "searches",
            "save",
            "remote-us-backend",
            "--workspace",
            str(tmp_path),
            "--title-keyword",
            "backend",
            "--country",
            "US",
            "--remote-only",
            "--default",
        ],
    )
    assert save.exit_code == 0, save.output
    assert "remote-us-backend" in save.output

    listed = runner.invoke(app, ["searches", "list", "--workspace", str(tmp_path)])
    assert listed.exit_code == 0, listed.output
    assert "remote-us-backend" in listed.output
    assert "yes" in listed.output

    shown = runner.invoke(app, ["searches", "show", "remote-us-backend", "--workspace", str(tmp_path)])
    assert shown.exit_code == 0, shown.output
    assert "title_keywords" in shown.output
    assert "remote-us-backend" in shown.output

    run_saved = runner.invoke(app, ["searches", "run", "remote-us-backend", "--workspace", str(tmp_path)])
    assert run_saved.exit_code == 0, run_saved.output
    assert "Backend Platform Engineer" in run_saved.output

    jobs_via_saved = runner.invoke(app, ["jobs", "search", "--workspace", str(tmp_path), "--saved-search", "remote-us-backend"])
    assert jobs_via_saved.exit_code == 0, jobs_via_saved.output
    assert "Backend Platform Engineer" in jobs_via_saved.output

    deleted = runner.invoke(app, ["searches", "delete", "remote-us-backend", "--workspace", str(tmp_path), "--yes"])
    assert deleted.exit_code == 0, deleted.output
    assert "Deleted saved search" in deleted.output
