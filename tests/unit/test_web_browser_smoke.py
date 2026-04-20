from __future__ import annotations

import contextlib
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from findmyjob.filefirst.models import ApplicationEntry, EvaluationResult, FileFact, InboxJob, SubmissionQuestion, SubmissionRecord
from findmyjob.filefirst.workspace import FileWorkspace
from findmyjob.web.app import create_app


def _seed_workspace(tmp_path: Path) -> str:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    ws.save_cv('# Test User\n\nBackend engineer with local AI tooling experience.\n')
    ws.save_facts(
        [
            FileFact(
                fact_id='contact.primary',
                kind='contact',
                payload={'name': 'Test User', 'email': 'user@example.com', 'phone': '555-111-2222'},
            ),
            FileFact(
                fact_id='work.primary',
                kind='work',
                payload={'title': 'Backend Engineer', 'company': 'Acme', 'summary': 'Built local-first automation.'},
            ),
        ]
    )

    job = InboxJob(
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
    ws.save_job(job)
    ws.upsert_inbox_jobs([job])
    ws.save_evaluation(
        EvaluationResult(
            job_id='job-100',
            company='Acme',
            role='Backend Platform Engineer',
            source='greenhouse',
            url=job.url,
            score=4.6,
            grade='A',
            summary='Strong fit for local-first backend automation.',
            report_markdown='# Evaluation\n\nStrong fit.',
        )
    )
    report_path = ws.report_path_for('001', 'Acme', '2026-04-05')
    report_path.write_text('# Evaluation\n\nStrong fit.\n', encoding='utf-8')
    ws.resume_pdf_path_for('001', 'Acme', '2026-04-05').write_bytes(b'%PDF-1.4\n%stub\n')
    ws.upsert_application(
        ApplicationEntry(
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
            url=job.url,
            source='greenhouse',
        )
    )
    ws.save_submission(
        SubmissionRecord(
            application_id='001',
            job_id='job-100',
            company='Acme',
            role='Backend Platform Engineer',
            source='greenhouse',
            apply_url=job.apply_url,
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
    return '001'


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', 0))
        return int(probe.getsockname()[1])


@contextlib.contextmanager
def _serve_app(workspace: Path):
    port = _free_port()
    app = create_app(workspace)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host='127.0.0.1',
            port=port,
            log_level='error',
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f'http://127.0.0.1:{port}'
    deadline = time.time() + 15
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = httpx.get(base_url, timeout=1.0)
            if response.status_code == 200:
                break
        except Exception as exc:  # pragma: no cover - startup race
            last_error = exc
        time.sleep(0.1)
    else:  # pragma: no cover - environment dependent
        server.should_exit = True
        thread.join(timeout=10)
        raise RuntimeError(f'Web console test server did not start: {last_error}')
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.mark.timeout(60)
def test_served_spa_routes_render_in_a_real_browser(tmp_path: Path) -> None:
    frontend_index = Path('src/findmyjob/web/frontend_dist/index.html')
    if not frontend_index.exists():
        pytest.skip('frontend_dist is missing; browser smoke test requires a built SPA bundle.')

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - dependency optional in some envs
        pytest.skip(f'Playwright is unavailable: {exc}')

    review_application_id = _seed_workspace(tmp_path)
    with _serve_app(tmp_path) as base_url:
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page()

                def _goto_and_assert_heading(route: str, text: str) -> None:
                    page.goto(f'{base_url}{route}', wait_until='domcontentloaded')
                    page.get_by_role('heading', name=text).first.wait_for(timeout=15000)
                    assert page.get_by_role('heading', name=text).first.is_visible()

                _goto_and_assert_heading('/', 'Dashboard')
                page.get_by_text('FindMyJob').first.wait_for(timeout=15000)
                assert page.get_by_text('Run Status').first.is_visible()

                for route, heading, text in (
                    ('/setup', 'Setup', 'Readiness'),
                    ('/settings', 'Settings', 'Readiness & Workspace Health'),
                    ('/autopilot', 'Autopilot', 'Job queue'),
                    ('/runs', 'Run History', 'No runs yet'),
                ):
                    _goto_and_assert_heading(route, heading)
                    page.get_by_text(text).first.wait_for(timeout=15000)
                    assert page.get_by_text(text).first.is_visible()

                _goto_and_assert_heading(f'/review?id={review_application_id}', 'Review')
                page.get_by_text('Backend Platform Engineer').first.wait_for(timeout=15000)
                assert page.get_by_text('Acme').first.is_visible()
                page.reload(wait_until='domcontentloaded')
                page.get_by_text('Backend Platform Engineer').first.wait_for(timeout=15000)
                page.get_by_text('Acme').first.wait_for(timeout=15000)
                assert page.get_by_text('Acme').first.is_visible()

                browser.close()
        except Exception as exc:  # pragma: no cover - depends on local browser install
            message = str(exc)
            if 'Executable doesn' in message or 'Please run the following command' in message:
                pytest.skip(f'Playwright browser binaries are unavailable: {exc}')
            raise
