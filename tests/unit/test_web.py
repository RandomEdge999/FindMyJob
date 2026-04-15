from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import yaml
from httpx import ASGITransport
from typer.testing import CliRunner

from findmyjob.cli.main import app
from findmyjob.core.lmstudio import (
    LMSTUDIO_DEFAULT_HOST,
    LMSTUDIO_DEFAULT_SCREENING_MODEL,
    LMSTUDIO_DEFAULT_WRITER_MODEL,
    ResolvedLocalBaseURL,
)
from findmyjob.filefirst.models import ApplicationEntry, EvaluationResult, FileFact, InboxJob, SubmissionQuestion, SubmissionRecord
from findmyjob.filefirst.workspace import FileWorkspace
from findmyjob.web.app import create_app

runner = CliRunner()


class _AsyncJsonResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


def _seed_workspace(tmp_path: Path) -> dict[str, str]:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    ws.save_cv('# Test User\n\nBackend engineer with local AI tooling experience.\n')
    ws.save_facts(
        [
            FileFact(
                fact_id='contact.primary',
                kind='contact',
                payload={
                    'name': 'Test User',
                    'email': 'user@example.com',
                    'phone': '555-111-2222',
                    'linkedin': 'linkedin.com/in/test-user',
                },
            ),
            FileFact(
                fact_id='work.primary',
                kind='work',
                payload={
                    'title': 'Backend Engineer',
                    'company': 'Acme',
                    'summary': 'Built backend automation and local inference tools.',
                    'bullets': ['Shipped local-first automation.', 'Built PDF generation pipelines.'],
                },
            ),
            FileFact(
                fact_id='location.primary',
                kind='location',
                payload={'display': 'Remote - United States', 'country_code': 'US'},
            ),
        ]
    )

    review_job = InboxJob(
        job_id='job-100',
        company='Acme',
        company_key='acme',
        title='Backend Platform Engineer',
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id='100',
        url='https://boards.greenhouse.io/acme/jobs/100',
        apply_url='https://boards.greenhouse.io/acme/jobs/100',
        location='Remote - United States',
        description='Build backend services and local AI workflows.',
        workflow_state='pdf_ready',
        board_family='greenhouse',
        automation_tier='auto_submit_supported',
        job_identity_key='job-100',
        duplicate_cluster_key='acme-backend-platform-engineer',
    )
    triage_job = InboxJob(
        job_id='job-101',
        company='Beta',
        company_key='beta',
        title='Operator Inbox Role',
        source='lever',
        source_kind='lever',
        source_job_id='101',
        url='https://jobs.lever.co/beta/101',
        apply_url='https://jobs.lever.co/beta/101',
        location='Remote',
        description='Operator workflow role.',
        workflow_state='pending',
        board_family='lever',
        automation_tier='auto_submit_supported',
        job_identity_key='job-101',
        duplicate_cluster_key='beta-operator-inbox-role',
    )
    ws.save_job(review_job)
    ws.save_job(triage_job)
    ws.upsert_inbox_jobs([review_job, triage_job])

    evaluation = EvaluationResult(
        job_id='job-100',
        company='Acme',
        role='Backend Platform Engineer',
        source='greenhouse',
        url=review_job.url,
        score=4.6,
        grade='A',
        summary='Strong fit for local-first backend automation.',
        keywords=['python', 'backend', 'local ai'],
        fit_reasons=['Strong Python background', 'Local tooling experience'],
        gaps=['Needs more explicit browser automation examples'],
        report_markdown='# Evaluation\n\nStrong fit.',
        resume_headline='Backend engineer for local AI systems',
        resume_summary_lines=['Built local-first automation.'],
    )
    ws.save_evaluation(evaluation)
    report_path = ws.report_path_for('001', 'Acme', '2026-04-05')
    report_path.write_text('# Evaluation\n\nStrong fit.\n', encoding='utf-8')
    pdf_path = ws.resume_pdf_path_for('001', 'Acme', '2026-04-05')
    pdf_path.write_bytes(b'%PDF-1.4\n%stub\n')

    application = ApplicationEntry(
        id='001',
        job_id='job-100',
        date='2026-04-05',
        company='Acme',
        role='Backend Platform Engineer',
        score=4.6,
        grade='A',
        status='Needs Input',
        pdf=True,
        report=ws.relative_path(report_path),
        url=review_job.url,
        source='greenhouse',
    )
    ws.upsert_application(application)
    ws.save_submission(
        SubmissionRecord(
            application_id='001',
            job_id='job-100',
            company='Acme',
            role='Backend Platform Engineer',
            source='greenhouse',
            apply_url=review_job.apply_url,
            status='needs_user_input',
            event_status='needs_user_input',
            submit_ready=False,
            questions=[
                SubmissionQuestion(
                    question_id='preferred-start-date',
                    prompt_text='What is your preferred start date?',
                    normalized_key='preferred-start-date',
                    question_type='date',
                    widget_type='date',
                    required=True,
                    needs_user_input=True,
                )
            ],
            missing_required_fields=['What is your preferred start date?'],
        )
    )

    return {
        'review_application_id': '001',
        'needs_input_application_id': '001',
        'needs_input_question_id': 'preferred-start-date',
        'triage_job_id': 'job-101',
    }


@pytest_asyncio.fixture()
async def seeded_client(tmp_path: Path):
    ids = _seed_workspace(tmp_path)
    transport = ASGITransport(app=create_app(tmp_path))
    async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as client:
        yield client, ids, tmp_path


def test_web_cli_launches_local_server(monkeypatch, tmp_path: Path) -> None:
    called: dict[str, object] = {}

    def fake_run_web_console(*, workspace, host, port, open_browser, open_path):
        called.update({'workspace': workspace, 'host': host, 'port': port, 'open_browser': open_browser, 'open_path': open_path})

    monkeypatch.setattr('findmyjob.cli.main.sync_frontend_bundle', lambda: None)
    monkeypatch.setattr('findmyjob.web.app.run_web_console', fake_run_web_console)

    result = runner.invoke(app, ['web', '--host', '127.0.0.1', '--port', '9000', '--no-open', '--workspace', str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert called['port'] == 9000
    assert called['open_browser'] is False


@pytest.mark.asyncio
async def test_pages_render_spa_shell(seeded_client) -> None:
    client, ids, _tmp_path = seeded_client
    pages = [
        '/',
        '/setup',
        '/settings',
        '/daily',
        f'/review?application_id={ids["review_application_id"]}',
        '/runs',
        '/training',
    ]
    for path in pages:
        response = await client.get(path)
        assert response.status_code == 200
        assert 'Find My Job Console' in response.text


@pytest.mark.asyncio
async def test_json_endpoints_return_operator_data(seeded_client) -> None:
    client, ids, _tmp_path = seeded_client

    dashboard = await client.get('/api/dashboard')
    assert dashboard.status_code == 200
    dashboard_payload = dashboard.json()
    assert 'jobs_table' in dashboard_payload
    assert 'live' in dashboard_payload

    jobs_table = await client.get('/api/jobs/table')
    assert jobs_table.status_code == 200
    assert any(item['company'] == 'Acme' for item in jobs_table.json()['items'])

    live_status = await client.get('/api/live/status')
    assert live_status.status_code == 200
    assert 'state' in live_status.json()
    assert 'events' in live_status.json()

    setup = await client.get('/api/setup/readiness')
    assert setup.status_code == 200
    setup_payload = setup.json()
    assert 'profile_surface' in setup_payload
    assert setup_payload['profile_surface']['mode'] in {'sample_mode', 'local_user_profile', 'advanced_local_overrides'}

    autonomous = await client.get('/api/autonomous/status')
    assert autonomous.status_code == 200
    autonomous_payload = autonomous.json()
    assert 'queue_depth' in autonomous_payload
    assert 'blocked_applications' in autonomous_payload
    assert 'default_submit_mode' in autonomous_payload
    assert 'ready_to_apply_threshold' in autonomous_payload
    assert set(autonomous_payload['source_metrics']) >= {'greenhouse', 'lever', 'ashby'}

    queue = await client.get('/api/review/queue')
    assert queue.status_code == 200
    review_item = next(item for item in queue.json()['items'] if item['application_id'] == ids['review_application_id'])
    assert review_item['classification']['board_family'] == 'greenhouse'
    assert review_item['gate']['missing_required_fields'] == ['What is your preferred start date?']

    detail = await client.get(f'/api/applications/{ids["review_application_id"]}')
    assert detail.status_code == 200
    detail_payload = detail.json()
    assert detail_payload['application']['application_id'] == ids['review_application_id']
    assert detail_payload['questions'][0]['prompt_text'] == 'What is your preferred start date?'
    assert detail_payload['blockers']


@pytest.mark.asyncio
async def test_settings_endpoint_returns_full_control_plane_state(seeded_client) -> None:
    client, _ids, _tmp_path = seeded_client

    response = await client.get('/api/settings')

    assert response.status_code == 200
    payload = response.json()
    assert payload['runtime_model']['provider'] == 'lmstudio'
    assert payload['runtime_model']['transport'] == 'local_http'
    assert payload['runtime_model']['base_url'] == LMSTUDIO_DEFAULT_HOST
    assert payload['runtime_model']['api_key_env'] is None
    assert payload['local_model'] == payload['runtime_model']
    assert set(payload['portals']['sources']) >= {'greenhouse', 'lever', 'ashby'}
    assert isinstance(payload['tracked_companies'], list)
    assert 'advanced_models' in payload
    assert 'last_model_checks' in payload
    assert payload['model_strategy']['mode'] == 'lm_studio_local'
    assert payload['autonomous']['captcha_strategy'] == 'skip'
    assert payload['autonomous']['captcha_strategy_effective'] == 'manual'
    assert 'findings' in payload['readiness']


@pytest.mark.asyncio
async def test_settings_models_available_probes_lmstudio_and_returns_canonical_url(monkeypatch, seeded_client) -> None:
    client, _ids, _tmp_path = seeded_client

    def fake_probe(base_url, *, timeout=15.0, api_key=None):
        _ = (timeout, api_key)
        assert base_url == LMSTUDIO_DEFAULT_HOST
        return ResolvedLocalBaseURL(
            requested_url=LMSTUDIO_DEFAULT_HOST,
            canonical_base_url=f'{LMSTUDIO_DEFAULT_HOST}/v1',
            candidates=(LMSTUDIO_DEFAULT_HOST, f'{LMSTUDIO_DEFAULT_HOST}/v1'),
            models_payload={'data': [{'id': LMSTUDIO_DEFAULT_WRITER_MODEL}]},
        )

    monkeypatch.setattr('findmyjob.web.routes.api.probe_lmstudio_base_url', fake_probe)

    response = await client.get(
        '/api/settings/models/available',
        params={'provider': 'lmstudio', 'transport': 'local_http', 'base_url': LMSTUDIO_DEFAULT_HOST},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload['count'] == 1
    assert payload['transport'] == 'local_http'
    assert payload['base_url'] == f'{LMSTUDIO_DEFAULT_HOST}/v1'
    assert payload['api_key_configured'] is False
    assert payload['key_scoped'] is False
    assert payload['models'][0]['id'] == LMSTUDIO_DEFAULT_WRITER_MODEL


@pytest.mark.asyncio
async def test_autonomous_run_endpoint_wiring(monkeypatch, seeded_client) -> None:
    client, _ids, _tmp_path = seeded_client

    monkeypatch.setattr(
        'findmyjob.filefirst.service.FileFirstOperatorService.launch_autonomous_run',
        lambda self: {'started': True, 'run_id': 'auto-web-1', 'submitted_application_ids': ['001'], 'failed_application_ids': []},
    )

    response = await client.post('/api/autonomous/run')

    assert response.status_code == 202
    payload = response.json()
    assert payload['started'] is True
    assert payload['run_id'] == 'auto-web-1'


@pytest.mark.asyncio
async def test_review_action_and_daily_triage_actions_work_against_backend(monkeypatch, seeded_client) -> None:
    client, ids, tmp_path = seeded_client
    ws = FileWorkspace(tmp_path)
    seeded_record = ws.load_submission(ids['review_application_id'])

    async def fake_prepare(self, application_id, run_id):
        _ = application_id
        _ = run_id
        return seeded_record

    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._prepare_submission_async', fake_prepare)

    async def fake_open_manual_handoff(self, application_id, run_id):
        _ = application_id
        _ = run_id
        return True

    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._open_manual_handoff_preview_async', fake_open_manual_handoff)

    review_response = await client.post('/api/review/action', json={'application_id': ids['review_application_id'], 'action': 'approve'})
    assert review_response.status_code == 200
    review_payload = review_response.json()
    assert review_payload['blocked'] is True
    assert review_payload['manual_handoff_opened'] is True
    assert any(item['category'] == 'missing_required_field' for item in review_payload['remaining_blockers'])

    triage_response = await client.post('/api/daily/triage', json={'job_id': ids['triage_job_id'], 'action': 'shortlist', 'scope': 'job'})
    assert triage_response.status_code == 200
    assert triage_response.json()['decision']['status'] == 'shortlisted'


@pytest.mark.asyncio
async def test_review_action_can_record_manual_submission(seeded_client) -> None:
    client, ids, tmp_path = seeded_client
    ws = FileWorkspace(tmp_path)

    response = await client.post('/api/review/action', json={'application_id': ids['review_application_id'], 'action': 'mark_submitted'})

    assert response.status_code == 200
    payload = response.json()
    assert payload['manual_submitted'] is True
    assert payload['status'] == 'submitted'

    submission = ws.load_submission(ids['review_application_id'])
    application = ws.find_application(ids['review_application_id'])
    queue = await client.get('/api/review/queue')

    assert submission is not None
    assert submission.status == 'submitted'
    assert submission.submitted_at is not None
    assert submission.result['manual_confirmation'] is True
    assert application is not None
    assert application.status == 'Applied'
    assert not any(item['application_id'] == ids['review_application_id'] for item in queue.json()['items'])


@pytest.mark.asyncio
async def test_question_answer_endpoint_updates_queue_item_and_live_state(seeded_client) -> None:
    client, ids, _tmp_path = seeded_client

    response = await client.post(
        '/api/questions/answer',
        json={
            'application_id': ids['needs_input_application_id'],
            'question_id': ids['needs_input_question_id'],
            'answer_text': '2026-05-01',
            'approve_memory': True,
            'auto_retry': False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload['question']['existing_answer'] == '2026-05-01'
    assert 'remaining_blockers' in payload
    assert isinstance(payload['remaining_blockers'], list)

    live_status = await client.get('/api/live/status')
    assert live_status.status_code == 200
    assert live_status.json()['events']


@pytest.mark.asyncio
async def test_settings_save_autonomous_and_submit_mode(seeded_client) -> None:
    client, _ids, tmp_path = seeded_client
    response = await client.post('/api/settings/autonomous', json={
        'enabled': True,
        'daily_submit_cap': 12,
        'per_company_daily_cap': 2,
        'ready_to_apply_threshold': 9,
        'browser_mode': 'headless',
        'max_open_tabs': 4,
        'submit_enabled': True,
        'browser_attach_enabled': False,
        'browser_cdp_url': 'http://127.0.0.1:9666',
        'default_submit_mode': 'preview_first',
        'captcha_strategy': 'manual',
        'captcha_provider': 'anti-captcha',
        'captcha_api_key_env': 'FMJ_CAPTCHA_KEY',
        'captcha_solve_timeout_seconds': 180,
    })
    assert response.status_code == 200
    assert response.json()['saved'] is True

    profile_doc = yaml.safe_load((tmp_path / '.fmj' / 'local-overrides' / 'filefirst' / 'config' / 'profile.yml').read_text(encoding='utf-8'))
    assert profile_doc['runtime']['automation']['enabled'] is True
    assert profile_doc['runtime']['automation']['daily_submit_cap'] == 12
    assert profile_doc['runtime']['automation']['ready_to_apply_threshold'] == 9
    assert profile_doc['runtime']['automation']['submit_enabled'] is True
    assert profile_doc['runtime']['automation']['default_submit_mode'] == 'preview_first'
    assert profile_doc['runtime']['automation']['browser_cdp_url'] == 'http://127.0.0.1:9666'
    assert response.json()['autonomous']['captcha_strategy'] == 'manual'
    assert response.json()['captcha']['captcha_provider'] == 'anti-captcha'

    workspace_config = (tmp_path / '.fmj' / 'config.toml').read_text(encoding='utf-8')
    assert 'default_application_mode = "dry_run"' in workspace_config
    assert 'require_human_review_for_submit = true' in workspace_config
    assert 'submit_enabled = false' in workspace_config
    assert 'browser_cdp_url = "http://127.0.0.1:9666"' in workspace_config
    assert 'strategy = "manual"' in workspace_config
    assert 'provider = "anti-captcha"' in workspace_config


@pytest.mark.asyncio
async def test_settings_payload_includes_chatgpt_drafting(seeded_client) -> None:
    client, _ids, _tmp_path = seeded_client

    response = await client.get('/api/settings')

    assert response.status_code == 200
    payload = response.json()
    assert payload['chatgpt_drafting']['renderer'] == 'chatgpt_download'
    assert payload['chatgpt_drafting']['gpt_url'] == 'https://chatgpt.com/g/your-custom-resume-cover-letter-writer'
    assert payload['chatgpt_drafting']['browser']['browser_cdp_url'] == 'http://127.0.0.1:9333'
    assert payload['chatgpt_drafting']['max_parallel_jobs'] == 10
    assert payload['chatgpt_drafting']['use_temporary_chat'] is False


@pytest.mark.asyncio
async def test_chatgpt_drafting_routes_save_launch_and_test(monkeypatch, seeded_client) -> None:
    client, _ids, tmp_path = seeded_client

    response = await client.post(
        '/api/settings/chatgpt-drafting',
        json={
            'enabled': True,
            'gpt_url': 'https://chatgpt.com/g/custom-test',
            'completion_start_marker': '[[PDF_OUTPUT_READY]]',
            'completion_end_marker': '[[PDF_OUTPUT_COMPLETE]]',
            'profile_dir': '.fmj/browser/chatgpt-profile',
            'downloads_dir': '.fmj/runtime/chatgpt-downloads',
            'browser_mode': 'attached',
            'browser_cdp_url': 'http://127.0.0.1:9555',
            'launch_if_missing': True,
            'use_temporary_chat': False,
            'timeout_seconds': 180,
            'prompt_submit_delay_ms': 250,
            'download_timeout_seconds': 90,
            'max_parallel_jobs': 8,
            'make_default': True,
        },
    )

    assert response.status_code == 200
    assert response.json()['saved'] is True
    workspace_config = (tmp_path / '.fmj' / 'config.toml').read_text(encoding='utf-8')
    assert 'resume_renderer = "chatgpt_download"' in workspace_config
    assert 'gpt_url = "https://chatgpt.com/g/custom-test"' in workspace_config
    assert 'browser_cdp_url = "http://127.0.0.1:9555"' in workspace_config
    assert 'use_temporary_chat = false' in workspace_config
    assert 'max_parallel_jobs = 8' in workspace_config

    monkeypatch.setattr(
        'findmyjob.filefirst.service.FileFirstOperatorService.launch_chatgpt_browser',
        lambda self, **kwargs: {'launched': True, 'browser': {'browser_cdp_url': 'http://127.0.0.1:9555'}, 'kwargs': kwargs},
    )
    monkeypatch.setattr(
        'findmyjob.filefirst.service.FileFirstOperatorService.test_chatgpt_drafting',
        lambda self, target=None: {'success': True, 'application_id': target or '001', 'renderer': 'chatgpt_download'},
    )

    status_response = await client.get('/api/chatgpt-drafting/status')
    assert status_response.status_code == 200
    assert status_response.json()['gpt_url'] == 'https://chatgpt.com/g/custom-test'

    launch_response = await client.post('/api/chatgpt-drafting/browser/launch')
    assert launch_response.status_code == 200
    assert launch_response.json()['launched'] is True

    test_response = await client.post('/api/chatgpt-drafting/test', json={'target': '001'})
    assert test_response.status_code == 200
    assert test_response.json()['success'] is True
    assert test_response.json()['renderer'] == 'chatgpt_download'


@pytest.mark.asyncio
async def test_settings_save_portals_updates_scope_and_targets(seeded_client) -> None:
    client, _ids, tmp_path = seeded_client

    response = await client.put(
        '/api/settings/portals',
        json={
            'sources': {
                'greenhouse': {
                    'enabled': True,
                    'boards': ['acme', 'acme-labs'],
                    'seed_urls': ['https://boards.greenhouse.io/acme'],
                    'seed_domains': ['GREENHOUSE.IO'],
                },
                'lever': {
                    'enabled': False,
                    'boards': ['beta'],
                    'seed_urls': ['https://jobs.lever.co/beta'],
                    'seed_domains': ['lever.co'],
                },
                'ashby': {
                    'enabled': True,
                    'boards': ['gamma'],
                    'seed_urls': ['https://jobs.ashbyhq.com/gamma'],
                    'seed_domains': ['jobs.ashbyhq.com'],
                },
            },
            'tracked_companies': [
                {
                    'name': 'Acme Robotics',
                    'source': 'ashby',
                    'careers_url': 'https://careers.acme.test',
                    'board': 'acme-robotics',
                    'notes': 'priority launch target',
                    'enabled': True,
                }
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload['saved'] is True
    assert payload['autonomous']['production_sources'] == ['greenhouse', 'ashby']

    ws = FileWorkspace(tmp_path)
    portals = ws.load_portals()
    profile = ws.load_profile()

    assert portals.sources['greenhouse'].boards == ['acme', 'acme-labs']
    assert portals.sources['greenhouse'].seed_urls == ['https://boards.greenhouse.io/acme']
    assert portals.sources['greenhouse'].seed_domains == ['greenhouse.io']
    assert portals.sources['lever'].enabled is False
    assert portals.sources['ashby'].seed_urls == ['https://jobs.ashbyhq.com/gamma']
    assert portals.sources['ashby'].seed_domains == ['jobs.ashbyhq.com']
    assert len(portals.tracked_companies) == 1
    assert portals.tracked_companies[0].name == 'Acme Robotics'
    assert portals.tracked_companies[0].source == 'ashby'
    assert profile.runtime.automation.production_sources == ['greenhouse', 'ashby']


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('payload', 'expected'),
    [
        pytest.param(
            {
                'provider': 'lmstudio',
                'transport': 'local_http',
                'model': LMSTUDIO_DEFAULT_WRITER_MODEL,
                'base_url': LMSTUDIO_DEFAULT_HOST,
                'api_key_env': 'IGNORED_LMSTUDIO_KEY',
                'temperature': 0.2,
                'max_tokens': 8192,
                'preferred_context_window': 131072,
                'local': True,
                'command': [],
                'working_dir': '',
            },
            {
                'provider': 'lmstudio',
                'transport': 'local_http',
                'base_url': f'{LMSTUDIO_DEFAULT_HOST}/v1',
                'api_key_env': None,
                'model': LMSTUDIO_DEFAULT_WRITER_MODEL,
                'command': [],
                'working_dir': None,
            },
            id='lmstudio-local-http',
        ),
        pytest.param(
            {
                'provider': 'llama.cpp',
                'transport': 'local_http',
                'model': 'gemma-4-E4B-it',
                'base_url': 'http://127.0.0.1:8080/v1',
                'api_key_env': None,
                'temperature': 0.1,
                'max_tokens': 4096,
                'preferred_context_window': 65536,
                'local': True,
                'command': [],
                'working_dir': '',
            },
            {
                'provider': 'lmstudio',
                'transport': 'local_http',
                'base_url': 'http://127.0.0.1:8080/v1',
                'api_key_env': None,
                'model': 'gemma-4-E4B-it',
                'command': [],
                'working_dir': None,
            },
            id='local-http',
        ),
        pytest.param(
            {
                'provider': 'custom',
                'transport': 'process',
                'model': 'runtime-process',
                'base_url': None,
                'api_key_env': None,
                'temperature': 0.0,
                'max_tokens': 2048,
                'preferred_context_window': 32768,
                'local': True,
                'command': ['python', '-m', 'findmyjob.local_runtime'],
                'working_dir': 'C:/runtime',
            },
            {
                'provider': 'lmstudio',
                'transport': 'local_http',
                'base_url': f'{LMSTUDIO_DEFAULT_HOST}/v1',
                'api_key_env': None,
                'model': 'runtime-process',
                'command': [],
                'working_dir': None,
            },
            id='process',
        ),
    ],
)
async def test_settings_save_runtime_model_normalizes_to_lmstudio_local_http(monkeypatch, seeded_client, payload, expected) -> None:
    client, _ids, tmp_path = seeded_client
    def fake_probe(base_url, *, timeout=15.0, api_key=None):
        _ = (timeout, api_key)
        requested = str(base_url or '').strip()
        trimmed = requested[:-3] if requested.endswith('/v1') else requested
        return ResolvedLocalBaseURL(
            requested_url=requested or LMSTUDIO_DEFAULT_HOST,
            canonical_base_url=f'{trimmed}/v1' if trimmed else f'{LMSTUDIO_DEFAULT_HOST}/v1',
            candidates=(trimmed or LMSTUDIO_DEFAULT_HOST, f'{trimmed or LMSTUDIO_DEFAULT_HOST}/v1'),
            models_payload={'data': [{'id': payload['model']}]},
        )

    monkeypatch.setattr('findmyjob.filefirst.service.probe_lmstudio_base_url', fake_probe)

    response = await client.put('/api/settings/runtime-model', json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body['saved'] is True
    assert body['runtime_model']['transport'] == expected['transport']
    assert body['runtime_model']['provider'] == expected['provider']
    assert body['local_model'] == body['runtime_model']
    assert body['runtime_model']['base_url'] == expected['base_url']
    assert body['runtime_model']['api_key_env'] == expected['api_key_env']

    runtime_model = FileWorkspace(tmp_path).load_profile().runtime.model
    assert runtime_model.provider == expected['provider']
    assert runtime_model.transport == expected['transport']
    assert runtime_model.base_url == expected['base_url']
    assert runtime_model.api_key_env == expected['api_key_env']
    assert runtime_model.model == expected['model']
    assert runtime_model.command == expected['command']
    assert runtime_model.working_dir == expected['working_dir']


@pytest.mark.asyncio
async def test_settings_save_local_model_context_window(monkeypatch, seeded_client) -> None:
    client, _ids, tmp_path = seeded_client

    def fake_probe(base_url, *, timeout=15.0, api_key=None):
        _ = (timeout, api_key)
        requested = str(base_url or '').strip() or LMSTUDIO_DEFAULT_HOST
        return ResolvedLocalBaseURL(
            requested_url=requested,
            canonical_base_url=f'{LMSTUDIO_DEFAULT_HOST}/v1',
            candidates=(LMSTUDIO_DEFAULT_HOST, f'{LMSTUDIO_DEFAULT_HOST}/v1'),
            models_payload={'data': [{'id': LMSTUDIO_DEFAULT_SCREENING_MODEL}]},
        )

    monkeypatch.setattr('findmyjob.filefirst.service.probe_lmstudio_base_url', fake_probe)

    response = await client.post(
        '/api/settings/models',
        json={
            'name': 'runtime-model',
            'provider': 'lmstudio',
            'model': LMSTUDIO_DEFAULT_SCREENING_MODEL,
            'base_url': LMSTUDIO_DEFAULT_HOST,
            'temperature': 0.2,
            'max_tokens': 4096,
            'preferred_context_window': 65536,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload['saved'] is True
    assert payload['local_model']['preferred_context_window'] == 65536

    profile_doc = yaml.safe_load((tmp_path / '.fmj' / 'local-overrides' / 'filefirst' / 'config' / 'profile.yml').read_text(encoding='utf-8'))
    assert profile_doc['runtime']['model']['preferred_context_window'] == 65536


@pytest.mark.asyncio
async def test_settings_save_profile_forces_lmstudio_and_regenerate_dossier(monkeypatch, seeded_client) -> None:
    client, _ids, tmp_path = seeded_client

    def fake_probe(base_url, *, timeout=15.0, api_key=None):
        _ = (timeout, api_key)
        requested = str(base_url or '').strip()
        trimmed = requested[:-3] if requested.endswith('/v1') else requested
        return ResolvedLocalBaseURL(
            requested_url=requested or LMSTUDIO_DEFAULT_HOST,
            canonical_base_url=f'{trimmed}/v1' if trimmed else f'{LMSTUDIO_DEFAULT_HOST}/v1',
            candidates=(trimmed or LMSTUDIO_DEFAULT_HOST, f'{trimmed or LMSTUDIO_DEFAULT_HOST}/v1'),
            models_payload={'data': [{'id': 'gemini-2.5-pro'}]},
        )

    monkeypatch.setattr('findmyjob.filefirst.advanced_models.probe_lmstudio_base_url', fake_probe)

    response = await client.post(
        '/api/settings/models',
        json={
            'name': 'gemini-writer',
            'role': 'writer',
            'provider': 'gemini',
            'model': 'gemini-2.5-pro',
            'base_url': 'https://example.invalid/v1',
            'api_key_env': 'GEMINI_API_KEY',
            'transport': 'remote_http',
            'supports_structured_output': True,
        },
    )
    assert response.status_code == 200
    assert response.json()['saved'] is True

    workspace_config = (tmp_path / '.fmj' / 'config.toml').read_text(encoding='utf-8')
    assert 'gemini-writer' in workspace_config
    assert 'role = "writer"' in workspace_config
    assert 'provider = "lmstudio"' in workspace_config
    assert 'transport = "local_http"' in workspace_config
    assert 'api_key_env' not in workspace_config

    dossier = await client.post('/api/profile/dossier/regenerate')
    assert dossier.status_code == 200
    payload = dossier.json()
    assert payload['saved'] is True
    assert (tmp_path / '.fmj' / 'local-overrides' / 'filefirst' / 'profile' / 'candidate-dossier.md').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('payload', 'expected_base_url'),
    [
        pytest.param(
            {
                'name': 'runtime-model',
                'provider': 'lmstudio',
                'transport': 'local_http',
                'model': LMSTUDIO_DEFAULT_SCREENING_MODEL,
                'base_url': LMSTUDIO_DEFAULT_HOST,
                'api_key_env': 'IGNORED_LMSTUDIO_KEY',
                'temperature': 0.2,
                'max_tokens': 1024,
                'preferred_context_window': 131072,
            },
            f'{LMSTUDIO_DEFAULT_HOST}/v1',
            id='lmstudio-local-http',
        ),
        pytest.param(
            {
                'name': 'runtime-model',
                'provider': 'llama.cpp',
                'transport': 'local_http',
                'model': 'gemma-4-E4B-it',
                'base_url': 'http://127.0.0.1:8080/v1',
                'temperature': 0.0,
                'max_tokens': 2048,
                'preferred_context_window': 65536,
                'local': True,
            },
            'http://127.0.0.1:8080/v1',
            id='local-http',
        ),
        pytest.param(
            {
                'name': 'runtime-model',
                'provider': 'custom',
                'transport': 'process',
                'model': 'runtime-process',
                'command': ['python', '-m', 'findmyjob.local_runtime'],
                'working_dir': 'C:/runtime',
                'local': True,
            },
            f'{LMSTUDIO_DEFAULT_HOST}/v1',
            id='process',
        ),
    ],
)
async def test_settings_model_ping_normalizes_to_lmstudio_local_http(monkeypatch, seeded_client, payload, expected_base_url) -> None:
    client, _ids, _tmp_path = seeded_client

    async def fake_chat_completion(self, profile, request_payload, *, mode):
        _ = self
        _ = profile
        _ = request_payload
        _ = mode
        return {'choices': [{'message': {'content': 'ready'}}]}

    async def fake_catalog_get(self, url, *args, **kwargs):
        _ = self
        _ = url
        _ = args
        _ = kwargs
        return _AsyncJsonResponse({'data': [{'id': payload['model']}]})

    def fake_probe(base_url, *, timeout=15.0, api_key=None):
        _ = (timeout, api_key)
        requested = str(base_url or '').strip() or LMSTUDIO_DEFAULT_HOST
        trimmed = requested[:-3] if requested.endswith('/v1') else requested
        return ResolvedLocalBaseURL(
            requested_url=requested,
            canonical_base_url=f'{trimmed}/v1',
            candidates=(trimmed, f'{trimmed}/v1'),
            models_payload={'data': [{'id': payload['model']}]},
        )

    monkeypatch.setattr('findmyjob.model_router.router.ModelRouter._chat_completion', fake_chat_completion)
    monkeypatch.setattr(httpx.AsyncClient, 'get', fake_catalog_get)
    monkeypatch.setattr('findmyjob.filefirst.service.probe_lmstudio_base_url', fake_probe)

    response = await client.post('/api/settings/models/ping', json=payload)

    assert response.status_code == 200
    result = response.json()
    assert result['ok'] is True
    assert result['classification'] == 'ok'
    assert result['transport'] == 'local_http'
    assert result['provider'] == 'lmstudio'
    assert result['model'] == payload['model']
    assert result['base_url'] == expected_base_url
    assert result['model_present'] is True
    assert result['latency_ms'] >= 0


@pytest.mark.asyncio
async def test_settings_model_ping_rejects_unknown_saved_profile(seeded_client) -> None:
    client, _ids, _tmp_path = seeded_client

    response = await client.post('/api/settings/models/ping', json={'profile_name': 'missing-profile'})

    assert response.status_code == 400
    assert 'Unknown model profile: missing-profile' in response.json()['detail']


@pytest.mark.asyncio
async def test_rehearsal_endpoints_route_to_filefirst_service(monkeypatch, seeded_client) -> None:
    client, _ids, _tmp_path = seeded_client

    monkeypatch.setattr(
        'findmyjob.filefirst.service.FileFirstOperatorService.start_launch_rehearsal',
        lambda self, limit=5: {'run_id': 'rehearsal-start-1', 'screened_jobs': [], 'suggested_job_id': None, 'limit': limit},
    )
    monkeypatch.setattr(
        'findmyjob.filefirst.service.FileFirstOperatorService.override_job_screening',
        lambda self, job_id, approved=True, note=None: {'job_id': job_id, 'approved': approved, 'note': note},
    )
    monkeypatch.setattr(
        'findmyjob.filefirst.service.FileFirstOperatorService.run_launch_rehearsal',
        lambda self, job_id, override_rejected=False: {'run_id': 'rehearsal-run-1', 'job_id': job_id, 'override_rejected': override_rejected, 'ready_to_review': True},
    )

    start = await client.post('/api/rehearsal/start', json={'limit': 3})
    assert start.status_code == 200
    assert start.json()['run_id'] == 'rehearsal-start-1'
    assert start.json()['limit'] == 3

    override = await client.post('/api/rehearsal/override', json={'job_id': 'job-100', 'approved': True, 'note': 'manual override'})
    assert override.status_code == 200
    assert override.json()['job_id'] == 'job-100'

    run = await client.post('/api/rehearsal/run', json={'job_id': 'job-100', 'override_rejected': True})
    assert run.status_code == 200
    assert run.json()['run_id'] == 'rehearsal-run-1'
    assert run.json()['ready_to_review'] is True



@pytest.mark.asyncio
async def test_live_trace_endpoint_returns_persisted_trace(seeded_client) -> None:
    client, _ids, tmp_path = seeded_client
    ws = FileWorkspace(tmp_path)
    trace_ref = ws.write_live_trace('run-web-1', category='model-calls', name='screen-step', payload={'prompt': 'hello', 'response': 'world'})

    response = await client.get(f'/api/live/traces?ref={trace_ref}')

    assert response.status_code == 200
    payload = response.json()
    assert payload['trace_ref'] == trace_ref
    assert payload['kind'] == 'summary'
    assert payload['payload']['prompt'] == '[REDACTED]'
    assert payload['payload']['response'] == '[REDACTED]'
