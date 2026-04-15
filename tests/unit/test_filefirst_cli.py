from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from findmyjob.cli.main import app
from findmyjob.filefirst.models import FileFact, InboxJob, ScreeningDecision
from findmyjob.filefirst.workspace import FileWorkspace

runner = CliRunner()


def test_filefirst_cli_eval_command_is_registered(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        'findmyjob.cli.filefirst.evaluate_target',
        lambda workspace, target: {
            'application_id': '001',
            'job_id': target,
            'company': 'Acme',
            'role': 'Backend Engineer',
            'score': 4.8,
            'grade': 'A',
            'report_path': 'reports/001-acme-2026-04-05.md',
        },
    )

    result = runner.invoke(app, ['eval', 'job-100', '--workspace', str(tmp_path), '--json'])

    assert result.exit_code == 0, result.output
    assert '"job_id": "job-100"' in result.stdout
    assert '"application_id": "001"' in result.stdout


def test_filefirst_cli_local_check_command_is_registered(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        'findmyjob.cli.filefirst.LocalGemmaClient.verify',
        lambda self: {
            'base_url': 'http://127.0.0.1:8080/v1',
            'model': 'gemma-4-E4B-it',
            'available_models': ['gemma-4-E4B-it'],
            'model_present': True,
            'text_ok': True,
            'json_ok': True,
        },
    )

    result = runner.invoke(app, ['models', 'local-check', '--workspace', str(tmp_path), '--json'])

    assert result.exit_code == 0, result.output
    assert '"model_present": true' in result.stdout
    assert '"json_ok": true' in result.stdout


def test_filefirst_cli_screen_reset_all_command_is_registered(tmp_path: Path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    ws.save_cv('# Test User\n')
    ws.save_facts([FileFact(fact_id='contact.primary', kind='contact', payload={'name': 'Test User', 'email': 'user@example.com'})])
    job = InboxJob(
        job_id='job-screen-1',
        company='Acme',
        company_key='acme',
        title='Software Engineering Intern',
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id='100',
        url='https://boards.greenhouse.io/acme/jobs/100',
        apply_url='https://boards.greenhouse.io/acme/jobs/100',
        location='Remote',
        description='Internship role for summer 2026.',
        workflow_state='screened_out',
        board_family='greenhouse',
        automation_tier='auto_submit_supported',
        job_identity_key='job-screen-1',
        duplicate_cluster_key='acme-intern',
        screening=ScreeningDecision(approved=False, reasons=['Rejected'], confidence=0.9),
    )
    ws.save_job(job)
    ws.upsert_inbox_jobs([job])

    result = runner.invoke(app, ['screen', 'reset', '--all', '--workspace', str(tmp_path), '--json'])

    assert result.exit_code == 0, result.output
    assert '"reset": 1' in result.stdout
    reloaded = ws.load_job('job-screen-1')
    assert reloaded is not None
    assert reloaded.workflow_state == 'pending'
    assert reloaded.screening is None
