from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tomlkit import parse, table

from findmyjob.core.config import write_default_workspace_config
from findmyjob.core.enums import ApplicationMode, ArtifactKind, JobLifecycleStatus, RunStatus
from findmyjob.core.paths import ensure_workspace, workspace_config_file
from findmyjob.core.runtime import AppRuntime, cleanup_workspace, inspect_readiness
from findmyjob.core.types import SmokeTestResult
from findmyjob.db.models import utcnow
from findmyjob.db.repositories import ApplicationRepository, AuditRepository, JobRepository, RunRepository, hash_content
from findmyjob.sources.normalizer import build_normalized_job


def configure_workspace(tmp_path: Path, *, retention_days: int = 30) -> None:
    ensure_workspace(tmp_path)
    config_path = workspace_config_file(tmp_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    write_default_workspace_config(config_path)
    doc = parse(config_path.read_text(encoding='utf-8'))
    greenhouse = doc['sources']['greenhouse']
    greenhouse['enabled'] = True
    greenhouse['boards'] = ['acme']
    doc['privacy']['artifact_retention_days'] = retention_days
    doc['personal']['enabled'] = False
    doc['autonomous']['use_personal_presets'] = False
    config_path.write_text(doc.as_string(), encoding='utf-8')


def seed_application_artifact(runtime: AppRuntime, *, source_job_id: str, status: JobLifecycleStatus, filename: str) -> tuple[str, Path]:
    posting = build_normalized_job(
        company_name='Acme',
        title='Software Engineer',
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id=source_job_id,
        posting_url=f'https://boards.greenhouse.io/acme/jobs/{source_job_id}',
        apply_url=f'https://boards.greenhouse.io/acme/jobs/{source_job_id}',
        location_raw='Remote - United States',
        employment_type='full_time',
        compensation=None,
        description='Build reliable systems.',
        posted_at=utcnow(),
    )
    with runtime.session_scope() as session:
        job = JobRepository(session).upsert_job(posting)
        repo = ApplicationRepository(session)
        application = repo.ensure_application(job.id, ApplicationMode.AUTO_SUBMIT)
        application.status = status
        artifact_path = runtime.config.snapshots_dir(runtime.workspace) / application.id / filename
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text('artifact', encoding='utf-8')
        old = (utcnow() - timedelta(days=5)).timestamp()
        os.utime(artifact_path, (old, old))
        artifact = repo.store_artifact(
            ArtifactKind.SUBMISSION_TRACE,
            str(artifact_path),
            hash_content(str(artifact_path)),
            {},
            job_posting_id=job.id,
            application_id=application.id,
        )
        artifact.created_at = utcnow() - timedelta(days=5)
        return application.id, artifact_path


def _mock_runtime_checks(monkeypatch) -> None:
    monkeypatch.setattr('findmyjob.core.runtime.find_typst_executable', lambda: 'C:/tools/typst.exe')
    monkeypatch.setattr(
        'findmyjob.core.runtime._inspect_playwright',
        lambda: {
            'package_ok': True,
            'browser_ok': True,
            'package_detail': 'playwright import ok',
            'browser_detail': 'C:/ms-playwright/chromium/chrome.exe',
        },
    )
    monkeypatch.setattr('findmyjob.core.runtime.keyring_status', lambda: {'available': True, 'backend': 'test.backend', 'detail': None})


def test_inspect_readiness_reports_ready_state_for_ready_workspace(monkeypatch, tmp_path: Path) -> None:
    configure_workspace(tmp_path)
    monkeypatch.setattr('findmyjob.core.runtime.find_typst_executable', lambda: 'C:/tools/typst.exe')
    monkeypatch.setattr(
        'findmyjob.core.runtime._inspect_playwright',
        lambda: {
            'package_ok': True,
            'browser_ok': True,
            'package_detail': 'playwright import ok',
            'browser_detail': 'C:/ms-playwright/chromium/chrome.exe',
        },
    )
    monkeypatch.setattr('findmyjob.core.runtime.keyring_status', lambda: {'available': True, 'backend': 'test.backend', 'detail': None})

    report = inspect_readiness(tmp_path, check_models=False, check_browser=True, check_typst=True)

    assert report.blocked_count == 0
    assert report.overall_status == 'ready'
    assert any(finding.key == 'sources.greenhouse.targets' and finding.status == 'ok' for finding in report.findings)
    assert any(finding.key == 'runtime.typst' and finding.status == 'ok' for finding in report.findings)
    assert not any(finding.status == 'warning' for finding in report.findings)


def test_inspect_readiness_blocks_when_greenhouse_launch_path_is_missing(tmp_path: Path) -> None:
    config_path = workspace_config_file(tmp_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    write_default_workspace_config(config_path)
    doc = parse(config_path.read_text(encoding='utf-8'))
    greenhouse = doc['sources']['greenhouse']
    greenhouse['browser_attach_enabled'] = False
    greenhouse['browser_jobs_url'] = ''
    greenhouse['boards'] = []
    greenhouse['seed_urls'] = []
    greenhouse['seed_domains'] = []
    greenhouse['use_builtin_board_universe'] = False
    config_path.write_text(doc.as_string(), encoding='utf-8')

    report = inspect_readiness(tmp_path, check_models=False, check_browser=False, check_typst=False)

    assert report.overall_status == 'blocked'
    assert report.blocked_count > 0
    assert any(finding.key == 'sources.greenhouse.submit_targets' and finding.status == 'blocked' for finding in report.findings)


def test_cleanup_workspace_dry_run_and_apply_respect_active_application_retention(tmp_path: Path) -> None:
    configure_workspace(tmp_path, retention_days=1)
    runtime = AppRuntime.bootstrap(tmp_path)
    old_application_id, old_path = seed_application_artifact(runtime, source_job_id='123', status=JobLifecycleStatus.SUBMITTED, filename='trace-old.zip')
    active_application_id, active_path = seed_application_artifact(runtime, source_job_id='124', status=JobLifecycleStatus.READY_FOR_REVIEW, filename='trace-active.zip')

    dry_run = cleanup_workspace(tmp_path, apply=False)

    assert any(item.action == 'delete' and item.application_id == old_application_id for item in dry_run.findings)
    assert any(item.action == 'skip-active' and item.application_id == active_application_id for item in dry_run.findings)
    assert old_path.exists()
    assert active_path.exists()

    cleanup_workspace(tmp_path, apply=True)

    assert not old_path.exists()
    assert active_path.exists()
    with runtime.session_scope() as session:
        repo = ApplicationRepository(session)
        assert repo.list_artifacts(application_id=old_application_id) == []
        assert len(repo.list_artifacts(application_id=active_application_id)) == 1


def test_collect_release_snapshot_includes_smoke_and_benchmark_history(monkeypatch, tmp_path: Path) -> None:
    configure_workspace(tmp_path)
    _mock_runtime_checks(monkeypatch)
    runtime = AppRuntime.bootstrap(tmp_path)

    smoke = SmokeTestResult(
        board_token='acme',
        source_job_id='123',
        apply_url='https://boards.greenhouse.io/acme/jobs/123',
        submit_confirmed=True,
        status='pass',
        application_id='app-123',
        checked_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        references={'review_packet': '.fmj/artifacts/review.packet.md'},
    )
    with runtime.session_scope() as session:
        AuditRepository(session).emit(
            'greenhouse.smoke_test.recorded',
            'greenhouse_smoke',
            entity_id='app-123',
            payload=smoke.model_dump(mode='json'),
        )
        run_repo = RunRepository(session)
        run = run_repo.create_run(
            'benchmark',
            ApplicationMode.DRY_RUN,
            checkpoint_state={
                'board_tokens': ['acme'],
                'boards_attempted': 1,
                'boards_succeeded': 1,
                'jobs_seen': 12,
                'enriched_jobs': 10,
                'inactive_jobs': 1,
                'request_count': 4,
                'rate_limited_count': 0,
                'failure_count': 0,
                'duration_seconds': 5.0,
                'jobs_per_minute': 144.0,
            },
        )
        run_repo.complete_run(run.id, RunStatus.COMPLETED, checkpoint_state=run.checkpoint_state)

    snapshot = runtime.collect_release_snapshot(smoke_limit=5, benchmark_limit=5)

    assert snapshot.workspace == str(tmp_path)
    assert snapshot.launch_profile is not None
    assert snapshot.latest_smoke_result is not None
    assert snapshot.latest_smoke_result.application_id == 'app-123'
    assert snapshot.latest_benchmark is not None
    assert snapshot.latest_benchmark.run_id == run.id
    assert snapshot.benchmark_summaries[0].jobs_seen == 12
    assert snapshot.config_path.endswith('.fmj\\config.toml') or snapshot.config_path.endswith('.fmj/config.toml')


def test_inspect_readiness_reports_process_profiles_without_ollama_dependency(monkeypatch, tmp_path: Path) -> None:
    configure_workspace(tmp_path)
    config_path = workspace_config_file(tmp_path)
    doc = parse(config_path.read_text(encoding='utf-8'))
    models = table()
    writer = table()
    writer['name'] = 'prism-writer'
    writer['role'] = 'writer'
    writer['provider'] = 'prism'
    writer['model'] = 'prism-local'
    writer['transport'] = 'process'
    writer['command'] = ['powershell', '-NoProfile']
    writer['working_dir'] = str(tmp_path)
    models['prism-writer'] = writer
    doc['models'] = models
    config_path.write_text(doc.as_string(), encoding='utf-8')

    report = inspect_readiness(tmp_path, check_models=True, check_browser=False, check_typst=False)

    assert any(finding.key == 'models.process' and finding.status == 'ok' for finding in report.findings)
    assert any(finding.key == 'models.ollama' and finding.status == 'ok' for finding in report.findings)


def test_inspect_readiness_separates_lmstudio_and_llamacpp_findings(monkeypatch, tmp_path: Path) -> None:
    configure_workspace(tmp_path)
    monkeypatch.setattr('findmyjob.core.runtime.keyring_status', lambda: {'available': False, 'backend': None, 'detail': 'disabled for test'})

    report = inspect_readiness(tmp_path, check_models=True, check_browser=False, check_typst=False)

    lmstudio = next(finding for finding in report.findings if finding.key == 'models.lmstudio')
    llamacpp = next(finding for finding in report.findings if finding.key == 'models.llamacpp')

    assert lmstudio.status == 'ok'
    assert lmstudio.summary == 'LM Studio model profiles are configured.'
    assert llamacpp.status == 'ok'
    assert llamacpp.summary == 'No llama.cpp local HTTP profiles are configured.'

