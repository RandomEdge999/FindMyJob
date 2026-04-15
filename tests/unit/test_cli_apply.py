from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tomlkit import parse
from typer.testing import CliRunner

from findmyjob.cli.main import app
from findmyjob.core.config import write_default_workspace_config
from findmyjob.core.enums import ApplicationMode, ArtifactKind, JobLifecycleStatus, PolicyMode, ReviewStatus
from findmyjob.core.paths import workspace_config_file
from findmyjob.core.runtime import AppRuntime
from findmyjob.db.repositories import ApplicationRepository, JobRepository, hash_content
from findmyjob.sources.greenhouse_scale import GreenhouseScaleClient
from findmyjob.sources.normalizer import build_normalized_job

runner = CliRunner()


def configure_greenhouse_workspace(tmp_path: Path, live_smoke_urls: list[str]) -> None:
    config_path = workspace_config_file(tmp_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    write_default_workspace_config(config_path)
    doc = parse(config_path.read_text(encoding="utf-8"))
    greenhouse = doc["sources"]["greenhouse"]
    greenhouse["enabled"] = True
    greenhouse["submit_enabled"] = True
    greenhouse["boards"] = ["acme"]
    greenhouse["live_smoke_urls"] = live_smoke_urls
    config_path.write_text(doc.as_string(), encoding="utf-8")


def seed_application_with_attempt(runtime: AppRuntime, tmp_path: Path) -> str:
    posting = build_normalized_job(
        company_name="Acme",
        title="Software Engineer",
        source="greenhouse",
        source_kind="greenhouse",
        source_job_id="123",
        posting_url="https://boards.greenhouse.io/acme/jobs/123",
        apply_url="https://boards.greenhouse.io/acme/jobs/123",
        location_raw="Remote - United States",
        employment_type="full_time",
        compensation=None,
        description="Build reliable systems.",
        posted_at=datetime.now(timezone.utc),
    )
    with runtime.session_scope() as session:
        job = JobRepository(session).upsert_job(posting)
        app_repo = ApplicationRepository(session)
        application = app_repo.ensure_application(job.id, ApplicationMode.AUTO_SUBMIT)
        application.status = JobLifecycleStatus.SUBMISSION_UNCERTAIN
        application.review_status = ReviewStatus.PENDING

        receipt = tmp_path / "receipt.png"
        trace = tmp_path / "trace.zip"
        receipt.write_text("receipt", encoding="utf-8")
        trace.write_text("trace", encoding="utf-8")
        app_repo.store_artifact(ArtifactKind.SUBMISSION_RECEIPT, str(receipt), hash_content(str(receipt)), {}, job_posting_id=job.id, application_id=application.id)
        app_repo.store_artifact(ArtifactKind.SUBMISSION_TRACE, str(trace), hash_content(str(trace)), {}, job_posting_id=job.id, application_id=application.id)
        app_repo.record_submit_attempt(
            application.id,
            JobLifecycleStatus.SUBMISSION_UNCERTAIN.value,
            PolicyMode.HUMAN_IN_LOOP_SUBMIT,
            {
                "status": JobLifecycleStatus.SUBMISSION_UNCERTAIN.value,
                "evidence": {
                    "failure_reason": "confirmation_not_detected",
                    "confirmation_strategy": None,
                    "confirmation_text": None,
                    "final_url": "https://boards.greenhouse.io/acme/jobs/123",
                    "field_audit": [{"field": "resume", "prompt": "Resume/CV", "status": "bound", "value_summary": "resume.pdf"}],
                    "visible_validation_errors": ["Unknown outcome"],
                    "matched_confirmation_markers": [],
                    "missing_required_controls": [],
                    "submit_button_present": True,
                    "submit_button_enabled": True,
                    "pre_submit_snapshot_path": str(tmp_path / "pre-submit.png"),
                    "final_snapshot_path": str(receipt),
                    "trace_path": str(trace),
                    "dom_snapshot_path": str(tmp_path / "submit-dom-before.html"),
                    "post_submit_dom_snapshot_path": str(tmp_path / "submit-dom-after.html"),
                },
            },
            snapshot_path=str(receipt),
        )
        return application.id


def test_apply_inspect_result_cli_shows_submit_evidence(tmp_path: Path) -> None:
    configure_greenhouse_workspace(tmp_path, ["https://boards.greenhouse.io/acme/jobs/123"])
    runtime = AppRuntime.bootstrap(tmp_path)
    application_id = seed_application_with_attempt(runtime, tmp_path)

    result = runner.invoke(app, ["apply", "inspect-result", application_id, "--workspace", str(tmp_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.find("{"):])
    assert payload["failure_reason"] == "confirmation_not_detected"
    assert payload["submission_status"] == JobLifecycleStatus.SUBMISSION_UNCERTAIN.value
    assert payload["receipt_path"].endswith("receipt.png")
    assert payload["trace_path"].endswith("trace.zip")
    assert payload["field_audit_summary"][0]["field"] == "resume"
    assert payload["validation_errors"] == ["Unknown outcome"]


def test_greenhouse_smoke_test_cli_requires_explicit_allowlist(tmp_path: Path) -> None:
    configure_greenhouse_workspace(tmp_path, [])

    result = runner.invoke(app, ["greenhouse", "smoke-test", "acme", "--job-id", "123", "--workspace", str(tmp_path)])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output[result.output.find("{"):])
    assert payload["status"] == "fail"
    assert "live_smoke_urls is empty" in payload["failure_reason"]

    history = runner.invoke(app, ["greenhouse", "smoke-results", "--json", "--workspace", str(tmp_path)])

    assert history.exit_code == 0, history.output
    results = json.loads(history.output[history.output.rfind("[\n"):])
    assert results[0]["status"] == "fail"
    assert "live_smoke_urls is empty" in results[0]["failure_reason"]


def test_greenhouse_smoke_test_cli_rejects_non_allowlisted_posting(tmp_path: Path, monkeypatch) -> None:
    configure_greenhouse_workspace(tmp_path, ["https://boards.greenhouse.io/acme/jobs/999"])

    async def fake_validate_board(self, client, board_token: str):
        return {"name": "Acme"}

    async def fake_fetch_board_jobs(self, client, board_token: str):
        return {"jobs": [{"id": 123, "absolute_url": "https://boards.greenhouse.io/acme/jobs/123", "title": "Engineer", "offices": [], "metadata": {}}]}

    async def fake_fetch_job_detail(self, client, board_token: str, job_id: str):
        return {"questions": []}

    monkeypatch.setattr(GreenhouseScaleClient, "validate_board", fake_validate_board)
    monkeypatch.setattr(GreenhouseScaleClient, "fetch_board_jobs", fake_fetch_board_jobs)
    monkeypatch.setattr(GreenhouseScaleClient, "fetch_job_detail", fake_fetch_job_detail)

    result = runner.invoke(app, ["greenhouse", "smoke-test", "acme", "--job-id", "123", "--workspace", str(tmp_path)])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output[result.output.find("{"):])
    assert payload["status"] == "fail"
    assert "not explicitly allowlisted" in payload["failure_reason"]

    history = runner.invoke(app, ["greenhouse", "smoke-results", "--json", "--workspace", str(tmp_path)])

    assert history.exit_code == 0, history.output
    results = json.loads(history.output[history.output.rfind("[\n"):])
    assert results[0]["board_token"] == "acme"
    assert results[0]["source_job_id"] == "123"
    assert "not explicitly allowlisted" in results[0]["failure_reason"]







