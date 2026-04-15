from __future__ import annotations

from pathlib import Path

import pytest

textual = pytest.importorskip('textual')

from findmyjob.filefirst.models import ApplicationEntry, EvaluationResult, InboxJob
from findmyjob.filefirst.tracker import FileFirstTrackerApp
from findmyjob.filefirst.workspace import FileWorkspace


@pytest.fixture()
def anyio_backend() -> str:
    return 'asyncio'



def _seed_tracker_workspace(tmp_path: Path) -> FileWorkspace:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    job = InboxJob(
        job_id='job-100',
        company='Acme',
        company_key='acme',
        title='Backend Engineer',
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id='100',
        url='https://boards.greenhouse.io/acme/jobs/100',
        apply_url='https://boards.greenhouse.io/acme/jobs/100',
        location='Remote - United States',
        description='Build backend services and internal tooling.',
        workflow_state='evaluated',
        board_family='greenhouse',
        automation_tier='auto_submit_supported',
        job_identity_key='job-100',
        duplicate_cluster_key='acme-backend-engineer',
    )
    ws.save_job(job)
    ws.upsert_inbox_jobs([job])
    ws.save_evaluation(
        EvaluationResult(
            job_id='job-100',
            company='Acme',
            role='Backend Engineer',
            source='greenhouse',
            url='https://boards.greenhouse.io/acme/jobs/100',
            score=4.2,
            grade='B',
            summary='Strong fit for backend automation.',
            fit_reasons=['Backend systems experience'],
            gaps=['Needs more multimodal work examples'],
            keywords=['python', 'backend'],
        )
    )
    report_path = ws.report_path_for('001', 'Acme')
    report_path.write_text('# Evaluation\n\nStrong fit.\n', encoding='utf-8')
    ws.upsert_application(
        ApplicationEntry(
            id='001',
            job_id='job-100',
            date='2026-04-05',
            company='Acme',
            role='Backend Engineer',
            score=4.2,
            grade='B',
            status='Evaluated',
            pdf=False,
            report=ws.relative_path(report_path),
            url='https://boards.greenhouse.io/acme/jobs/100',
            source='greenhouse',
        )
    )
    return ws


@pytest.mark.anyio
async def test_filefirst_tracker_filters_and_updates_status(tmp_path: Path) -> None:
    ws = _seed_tracker_workspace(tmp_path)

    app = FileFirstTrackerApp(tmp_path)
    async with app.run_test() as pilot:
        assert app.table.row_count == 1
        assert 'Acme' in str(app.preview.renderable)

        await pilot.press('s')
        assert app.sort_mode == 'score'

        await pilot.press('g')
        assert app.grouped is True
        assert app.table.row_count >= 2

        await pilot.press('a')

    applications = ws.load_applications()
    assert applications[0].status == 'Applied'
    assert ws.load_inbox()[0].workflow_state == 'applied'
