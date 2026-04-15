import json
from pathlib import Path

from typer.testing import CliRunner

from findmyjob.cli.main import app
from findmyjob.core.config import write_default_workspace_config
from findmyjob.core.paths import workspace_config_file
from findmyjob.core.types import CleanupReport, ValidationReport

runner = CliRunner()


def test_doctor_cli_json_reports_blocked_state(monkeypatch, tmp_path: Path) -> None:
    report = ValidationReport(context="doctor", workspace=str(tmp_path))
    report.add("blocked", "runtime.typst", "Typst is not installed.")
    monkeypatch.setattr("findmyjob.cli.main.inspect_readiness", lambda *args, **kwargs: report)

    result = runner.invoke(app, ["doctor", "--browser", "--typst", "--json", "--workspace", str(tmp_path)])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["report"]["overall_status"] == "blocked"
    assert payload["report"]["blocked_count"] == 1


def test_config_validate_cli_json_reports_warnings(tmp_path: Path) -> None:
    config_path = workspace_config_file(tmp_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    write_default_workspace_config(config_path)

    result = runner.invoke(app, ["config", "validate", "--json", "--workspace", str(tmp_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["overall_status"] == "warnings"
    assert payload["warning_count"] >= 1


def test_cleanup_cli_json_reports_delete_candidates(monkeypatch, tmp_path: Path) -> None:
    report = CleanupReport(dry_run=True, workspace=str(tmp_path), retention_days=30, preserve_active_applications=True)
    report.add(path=str(tmp_path / ".fmj" / "snapshots" / "old.zip"), action="delete", reason="expired artifact")
    monkeypatch.setattr("findmyjob.cli.main.cleanup_workspace", lambda *args, **kwargs: report)

    result = runner.invoke(app, ["cleanup", "--json", "--workspace", str(tmp_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["delete_count"] == 1
    assert payload["findings"][0]["action"] == "delete"


def test_greenhouse_training_history_cli_json(monkeypatch, tmp_path: Path) -> None:
    history = [
        {
            "run_id": "run-123",
            "job_title": "Backend Engineer",
            "company_name": "Acme",
            "approved": False,
            "rejection_reason_code": "missing_evidence",
            "linked_application_id": "app-123",
        }
    ]
    monkeypatch.setattr("findmyjob.cli.main.runtime", lambda workspace: object())
    monkeypatch.setattr("findmyjob.personal.training.list_training_review_history", lambda runtime, limit=25, run_id=None: history)

    result = runner.invoke(app, ["greenhouse", "training-history", "--json", "--workspace", str(tmp_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload[0]["run_id"] == "run-123"
    assert payload[0]["rejection_reason_code"] == "missing_evidence"



def test_greenhouse_training_report_cli_json(monkeypatch, tmp_path: Path) -> None:
    report = {
        "run_id": "run-123",
        "status": "completed",
        "sampled_count": 5,
        "approved_count": 3,
        "rejected_count": 2,
        "promoted_application_ids": ["app-1", "app-2", "app-3"],
        "reviews": [],
    }
    monkeypatch.setattr("findmyjob.cli.main.runtime", lambda workspace: object())
    monkeypatch.setattr("findmyjob.personal.training.build_training_report", lambda runtime, run_id=None: report)

    result = runner.invoke(app, ["greenhouse", "training-report", "--json", "--workspace", str(tmp_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["run_id"] == "run-123"
    assert payload["approved_count"] == 3
