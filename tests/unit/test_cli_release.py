from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from findmyjob.cli.main import app
from findmyjob.core.types import GreenhouseBenchmarkSummary, LaunchCheckReport, ReleaseSnapshotReport, ValidationReport

runner = CliRunner()


def _snapshot(tmp_path: Path, report: LaunchCheckReport) -> ReleaseSnapshotReport:
    return ReleaseSnapshotReport(
        generated_at=datetime(2026, 4, 2, tzinfo=timezone.utc),
        workspace=str(tmp_path),
        workspace_name=tmp_path.name,
        config_path=str(tmp_path / '.fmj' / 'config.toml'),
        launch_check=report,
        config_validation=ValidationReport(context='config', workspace=str(tmp_path)),
        doctor=ValidationReport(context='doctor', workspace=str(tmp_path)),
    )


def test_launch_check_cli_json_reports_fail_state(monkeypatch, tmp_path: Path) -> None:
    report = LaunchCheckReport(workspace=str(tmp_path))
    report.add('fail', 'greenhouse.smoke_history', 'No successful controlled smoke result was recorded recently.')
    monkeypatch.setattr('findmyjob.cli.main.collect_release_snapshot', lambda *args, **kwargs: _snapshot(tmp_path, report))

    result = runner.invoke(app, ['launch-check', '--json', '--workspace', str(tmp_path)])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload['overall_status'] == 'fail'
    assert payload['fail_count'] == 1


def test_launch_check_export_writes_release_snapshot_json(monkeypatch, tmp_path: Path) -> None:
    report = LaunchCheckReport(workspace=str(tmp_path))
    report.add('pass', 'greenhouse.smoke_history', 'Confirmed smoke result recorded recently.')
    snapshot = _snapshot(tmp_path, report)
    snapshot.smoke_results = []
    snapshot.latest_benchmark = GreenhouseBenchmarkSummary(
        run_id='bench-1',
        status='completed',
        board_tokens=['acme'],
        boards_attempted=1,
        boards_succeeded=1,
        jobs_seen=12,
        jobs_enriched=12,
        inactive_jobs=0,
        request_count=5,
        rate_limited_count=0,
        failure_count=0,
        duration_seconds=3.5,
        jobs_per_minute=205.7,
    )
    snapshot.benchmark_summaries = [snapshot.latest_benchmark]
    monkeypatch.setattr('findmyjob.cli.main.collect_release_snapshot', lambda *args, **kwargs: snapshot)

    export_path = tmp_path / 'exports' / 'release-snapshot.json'
    result = runner.invoke(app, ['launch-check', '--export', str(export_path), '--workspace', str(tmp_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(export_path.read_text(encoding='utf-8'))
    assert payload['workspace'] == str(tmp_path)
    assert payload['launch_check']['overall_status'] == 'pass'
    assert payload['latest_benchmark']['run_id'] == 'bench-1'
    assert payload['benchmark_summaries'][0]['jobs_seen'] == 12


def test_greenhouse_benchmark_results_cli_json_lists_runs(monkeypatch, tmp_path: Path) -> None:
    summary = GreenhouseBenchmarkSummary(
        run_id='bench-1',
        status='completed',
        board_tokens=['acme'],
        boards_attempted=1,
        boards_succeeded=1,
        jobs_seen=12,
        jobs_enriched=12,
        inactive_jobs=0,
        request_count=5,
        rate_limited_count=0,
        failure_count=0,
        duration_seconds=3.5,
        jobs_per_minute=205.7,
    )

    class FakeRuntime:
        pass

    class FakeGreenhouseScaleOrchestrator:
        def __init__(self, runtime) -> None:
            self.runtime = runtime

        def list_benchmarks(self, limit: int = 10):
            assert limit == 10
            return [summary]

    monkeypatch.setattr('findmyjob.cli.main.runtime', lambda workspace=None: FakeRuntime())
    monkeypatch.setattr('findmyjob.cli.main.GreenhouseScaleOrchestrator', FakeGreenhouseScaleOrchestrator)

    result = runner.invoke(app, ['greenhouse', 'benchmark-results', '--json', '--workspace', str(tmp_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload[0]['run_id'] == 'bench-1'
    assert payload[0]['jobs_seen'] == 12
