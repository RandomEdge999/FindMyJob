from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tomlkit import item, parse
from typer.testing import CliRunner

from findmyjob.cli.main import app
from findmyjob.core.assets import ensure_default_workspace_templates
from findmyjob.core.config import write_default_workspace_config
from findmyjob.core.enums import ApplicationMode, ArtifactKind, JobLifecycleStatus, PolicyMode, ReviewStatus
from findmyjob.core.paths import ensure_workspace, workspace_config_file
from findmyjob.core.runtime import AppRuntime, inspect_personal_rehearsal
from findmyjob.core.types import JobSearchQuery, LaunchCheckReport, ModelLaunchProfileReport, ModelLaunchRoleStatus, PersonalRehearsalReport, ProfileFact, ReleaseSnapshotReport, SavedSearch, ValidationReport
from findmyjob.db.repositories import ApplicationRepository, JobRepository, ProfileRepository, SavedSearchRepository, hash_content
from findmyjob.sources.normalizer import build_normalized_job

runner = CliRunner()


def _mock_runtime_checks(monkeypatch) -> None:
    monkeypatch.setattr('findmyjob.core.runtime.shutil.which', lambda value: 'C:/tools/typst.exe' if value == 'typst' else None)
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



def configure_workspace(
    tmp_path: Path,
    *,
    personal_enabled: bool = False,
    submit_enabled: bool = False,
    source_dir: Path | None = None,
    enabled_presets: list[str] | None = None,
) -> None:
    ensure_workspace(tmp_path)
    ensure_default_workspace_templates(tmp_path)
    config_path = workspace_config_file(tmp_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    write_default_workspace_config(config_path)
    doc = parse(config_path.read_text(encoding='utf-8'))
    greenhouse = doc['sources']['greenhouse']
    greenhouse['enabled'] = True
    greenhouse['boards'] = ['acme']
    greenhouse['submit_enabled'] = submit_enabled
    if submit_enabled:
        greenhouse['live_smoke_urls'] = item(['https://boards.greenhouse.io/acme/jobs/123'])
    personal = doc['personal']
    personal['enabled'] = personal_enabled
    if source_dir is not None:
        personal['source_dir'] = str(source_dir)
    if enabled_presets is not None:
        personal['enabled_saved_search_presets'] = item(enabled_presets)
    config_path.write_text(doc.as_string(), encoding='utf-8')



def seed_job(runtime: AppRuntime, *, source_job_id: str = '123') -> str:
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
        posted_at=datetime.now(timezone.utc),
    )
    with runtime.session_scope() as session:
        job = JobRepository(session).upsert_job(posting)
        return job.id



def seed_personal_facts(runtime: AppRuntime) -> None:
    with runtime.session_scope() as session:
        repo = ProfileRepository(session)
        repo.upsert_fact(
            ProfileFact(
                fact_id='test.contact.primary',
                kind='contact',
                payload={'name': 'Test User', 'email': 'test@example.com'},
            )
        )
        repo.upsert_fact(
            ProfileFact(
                fact_id='test.authorization.us',
                kind='authorization',
                payload={'is_authorized': True, 'country_code': 'US'},
            )
        )



def seed_saved_search(runtime: AppRuntime, name: str = 'local_backend') -> None:
    with runtime.session_scope() as session:
        SavedSearchRepository(session).save(
            SavedSearch(
                name=name,
                query_payload=JobSearchQuery(keyword='engineer', title_keywords=['engineer'], source_adapter='greenhouse', limit=25),
            )
        )



def seed_application_with_attempt(runtime: AppRuntime, tmp_path: Path) -> str:
    job_id = seed_job(runtime)
    with runtime.session_scope() as session:
        app_repo = ApplicationRepository(session)
        application = app_repo.ensure_application(job_id, ApplicationMode.AUTO_SUBMIT)
        application.status = JobLifecycleStatus.SUBMISSION_UNCERTAIN
        application.review_status = ReviewStatus.PENDING

        receipt = tmp_path / 'receipt.png'
        trace = tmp_path / 'trace.zip'
        receipt.write_text('receipt', encoding='utf-8')
        trace.write_text('trace', encoding='utf-8')
        app_repo.store_artifact(ArtifactKind.SUBMISSION_RECEIPT, str(receipt), hash_content(str(receipt)), {}, job_posting_id=job_id, application_id=application.id)
        app_repo.store_artifact(ArtifactKind.SUBMISSION_TRACE, str(trace), hash_content(str(trace)), {}, job_posting_id=job_id, application_id=application.id)
        app_repo.record_submit_attempt(
            application.id,
            JobLifecycleStatus.SUBMISSION_UNCERTAIN.value,
            PolicyMode.HUMAN_IN_LOOP_SUBMIT,
            {
                'status': JobLifecycleStatus.SUBMISSION_UNCERTAIN.value,
                'evidence': {
                    'failure_reason': 'confirmation_not_detected',
                    'final_url': 'https://boards.greenhouse.io/acme/jobs/123',
                    'field_audit': [{'field': 'resume', 'prompt': 'Resume/CV', 'status': 'bound', 'value_summary': 'resume.pdf'}],
                    'visible_validation_errors': ['Unknown outcome'],
                    'matched_confirmation_markers': [],
                    'missing_required_controls': [],
                    'submit_button_present': True,
                    'submit_button_enabled': True,
                    'pre_submit_snapshot_path': str(tmp_path / 'pre-submit.png'),
                    'final_snapshot_path': str(receipt),
                    'trace_path': str(trace),
                    'dom_snapshot_path': str(tmp_path / 'submit-dom-before.html'),
                    'post_submit_dom_snapshot_path': str(tmp_path / 'submit-dom-after.html'),
                },
            },
            snapshot_path=str(receipt),
        )
        return application.id



def _ready_launch_profile() -> ModelLaunchProfileReport:
    return ModelLaunchProfileReport(
        required_roles=['writer', 'classifier', 'question_answerer'],
        roles=[
            ModelLaunchRoleStatus(role='writer', profile_name='writer', transport='local', status='pass'),
            ModelLaunchRoleStatus(role='classifier', profile_name='classifier', transport='local', status='pass'),
            ModelLaunchRoleStatus(role='question_answerer', profile_name='question-answerer', transport='local', status='pass'),
        ],
        transport_mix='all_local',
        summary='3/3 launch-profile roles ready',
    )



def _snapshot(tmp_path: Path, launch_check: LaunchCheckReport | None = None) -> ReleaseSnapshotReport:
    return ReleaseSnapshotReport(
        generated_at=datetime(2026, 4, 2, tzinfo=timezone.utc),
        workspace=str(tmp_path),
        workspace_name=tmp_path.name,
        config_path=str(tmp_path / '.fmj' / 'config.toml'),
        launch_check=launch_check or LaunchCheckReport(workspace=str(tmp_path)),
        config_validation=ValidationReport(context='config', workspace=str(tmp_path)),
        doctor=ValidationReport(context='doctor', workspace=str(tmp_path)),
        launch_profile=_ready_launch_profile(),
    )



def test_support_bundle_cli_redacts_by_default_and_honors_artifact_flags(tmp_path: Path) -> None:
    external_source = tmp_path.parent / 'my_personal_information'
    external_source.mkdir(parents=True, exist_ok=True)
    configure_workspace(tmp_path, personal_enabled=True, submit_enabled=True, source_dir=external_source)
    runtime = AppRuntime.bootstrap(tmp_path)
    application_id = seed_application_with_attempt(runtime, tmp_path)

    result = runner.invoke(app, ['support', 'bundle', '--json', '--workspace', str(tmp_path), '--application-id', application_id])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    inspection = payload['application_inspections'][0]
    assert inspection['artifact_paths'] == {}
    assert inspection['sensitive_artifacts'] == []
    assert payload['redaction']['artifact_paths_included'] is False
    assert payload['redaction']['sensitive_artifacts_included'] is False
    assert 'T' in payload['generated_at']
    assert '[redacted-phone]' not in payload['generated_at']
    assert '[redacted-phone]' not in payload['version']['platform']
    assert str(external_source) not in result.output

    flagged = runner.invoke(
        app,
        [
            'support',
            'bundle',
            '--json',
            '--workspace',
            str(tmp_path),
            '--application-id',
            application_id,
            '--include-artifact-paths',
            '--include-sensitive-artifacts',
        ],
    )

    assert flagged.exit_code == 0, flagged.output
    flagged_payload = json.loads(flagged.output)
    inspection = flagged_payload['application_inspections'][0]
    assert inspection['artifact_paths']['trace_path'].endswith('trace.zip')
    assert any(item['kind'] == 'submission_trace' for item in inspection['sensitive_artifacts'])



def test_personal_rehearse_reports_warning_state_with_local_only_preview(monkeypatch, tmp_path: Path) -> None:
    source_dir = tmp_path / 'my_personal_information'
    source_dir.mkdir(parents=True, exist_ok=True)
    configure_workspace(tmp_path, personal_enabled=True, submit_enabled=False, source_dir=source_dir, enabled_presets=['local_backend'])
    _mock_runtime_checks(monkeypatch)
    monkeypatch.setattr('findmyjob.core.runtime.AppRuntime.inspect_model_launch_profile', lambda self: _ready_launch_profile())

    async def fake_resume(runtime, job_id=None, allow_synthetic: bool = False):
        return {
            'job_id': 'rehearsal-preview',
            'company': 'Local Preflight Company',
            'title': 'Workspace Preflight Role',
            'renderer': 'typst',
            'synthetic_job': True,
            'artifacts': [
                {'kind': 'context', 'path': str(tmp_path / '.fmj' / 'artifacts' / 'resume.context.json'), 'validation': {'valid': True}},
                {'kind': 'resume', 'path': str(tmp_path / '.fmj' / 'artifacts' / 'resume.txt'), 'validation': {'valid': True}},
            ],
        }

    async def fake_cover(runtime, job_id=None, allow_synthetic: bool = False):
        return {
            'job_id': 'rehearsal-preview',
            'company': 'Local Preflight Company',
            'title': 'Workspace Preflight Role',
            'renderer': 'typst',
            'synthetic_job': True,
            'artifacts': [
                {'kind': 'context', 'path': str(tmp_path / '.fmj' / 'artifacts' / 'cover.context.json'), 'validation': {'valid': True}},
                {'kind': 'cover_letter', 'path': str(tmp_path / '.fmj' / 'artifacts' / 'cover_letter.txt'), 'validation': {'valid': True}},
            ],
        }

    monkeypatch.setattr('findmyjob.personal.workflow.preview_personal_resume', fake_resume)
    monkeypatch.setattr('findmyjob.personal.workflow.preview_personal_cover_letter', fake_cover)

    runtime = AppRuntime.bootstrap(tmp_path)
    seed_personal_facts(runtime)
    seed_saved_search(runtime)
    seed_job(runtime)

    report = inspect_personal_rehearsal(runtime=runtime, include_daily_dry_run=True, daily_dry_run_limit=5)

    assert report.report.blocked_count == 0
    assert report.report.overall_status == 'warnings'
    assert report.daily_dry_run is not None
    assert report.daily_dry_run['visible_match_count'] == 1
    assert report.resume_preview is not None and report.resume_preview.synthetic_job is True
    assert any(finding.key == 'personal.daily_dry_run' and finding.status == 'ok' for finding in report.report.findings)



def test_personal_rehearse_cli_json_reports_blocked_state(monkeypatch, tmp_path: Path) -> None:
    configure_workspace(tmp_path)
    _mock_runtime_checks(monkeypatch)

    result = runner.invoke(app, ['personal', 'rehearse', '--json', '--workspace', str(tmp_path)])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload['report']['overall_status'] == 'blocked'
    assert any(finding['key'] == 'personal.onboarding' and finding['status'] == 'blocked' for finding in payload['report']['findings'])



def test_personal_rehearse_cli_json_reports_ready_state(monkeypatch, tmp_path: Path) -> None:
    report = ValidationReport(context='personal_rehearse', workspace=str(tmp_path))
    ready_report = PersonalRehearsalReport(
        generated_at=datetime(2026, 4, 2, tzinfo=timezone.utc),
        workspace=str(tmp_path),
        report=report,
        launch_snapshot=_snapshot(tmp_path),
        onboarding={'onboarding_enabled': True, 'contact_fact_count': 1, 'authorization_fact_count': 1, 'enabled_saved_search_presets': ['local_backend']},
        inbox_summary={'counts': {'new_matching': 1, 'ready_for_review': 0, 'needs_user_input': 0, 'approved_pending_submit': 0}},
        latest_daily_run={'matching_job_count': 1, 'new_job_count': 1, 'added_to_review_count': 0},
    )
    monkeypatch.setattr('findmyjob.cli.main.inspect_personal_rehearsal', lambda *args, **kwargs: ready_report)

    result = runner.invoke(app, ['personal', 'rehearse', '--json', '--workspace', str(tmp_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload['report']['overall_status'] == 'ready'
    assert payload['launch_snapshot']['launch_check']['overall_status'] == 'pass'



def test_supportability_help_lists_new_flags() -> None:
    support_help = runner.invoke(app, ['support', 'bundle', '--help'])
    rehearse_help = runner.invoke(app, ['personal', 'rehearse', '--help'])

    assert support_help.exit_code == 0, support_help.output
    assert '--include-artifact-paths' in support_help.output
    assert '--include-sensitive-artifacts' in support_help.output
    assert rehearse_help.exit_code == 0, rehearse_help.output
    assert '--daily-dry-run' in rehearse_help.output
    assert 'local daily-run' in rehearse_help.output
