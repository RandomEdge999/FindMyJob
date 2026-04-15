from pathlib import Path

from findmyjob.filefirst.models import ApplicationEntry, InboxJob, SourceDiscoveryMetrics, SubmissionRecord
from findmyjob.filefirst.models import LiveRunState
from findmyjob.filefirst.operator_support import emit_live_event, jobs_table_payload, live_status_payload
from findmyjob.filefirst.workspace import FileWorkspace


def test_operator_support_jobs_table_and_live_snapshot(tmp_path: Path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    job = InboxJob(
        job_id='job-1',
        company='Acme',
        company_key='acme',
        title='Backend Engineer',
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id='1',
        url='https://example.com/jobs/1',
        apply_url='https://example.com/jobs/1',
        description='Build backend systems.',
        job_identity_key='job-1',
        duplicate_cluster_key='job-1',
    )
    ws.save_job(job)
    ws.upsert_inbox_jobs([job])
    ws.upsert_application(
        ApplicationEntry(
            id='001',
            job_id='job-1',
            date='2026-04-07',
            company='Acme',
            role='Backend Engineer',
            report='reports/001-acme.md',
            url='https://example.com/jobs/1',
            source='greenhouse',
        )
    )
    ws.save_submission(
        SubmissionRecord(
            application_id='001',
            job_id='job-1',
            company='Acme',
            role='Backend Engineer',
            source='greenhouse',
            status='needs_user_input',
            event_status='needs_user_input',
            missing_required_fields=['Start date'],
        )
    )

    rows = jobs_table_payload(ws)
    assert rows['count'] == 1
    assert rows['items'][0]['blocked'] is True

    emit_live_event(ws, run_id='run-1', run_type='autonomous', event_type='autonomous.started', message='Started run.', status='running')
    live = live_status_payload(ws)
    assert live['state']['event_count'] >= 1
    assert live['events']
    assert live['state']['stats']['blocked_by_questions'] == 1
    assert live['state']['stats']['ready_to_apply'] == 0


def test_operator_support_surfaces_source_metrics_and_zero_result_warnings(tmp_path: Path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()

    profile = ws.load_profile()
    profile.runtime.automation.production_sources = ['greenhouse', 'lever', 'ashby']
    ws.save_profile(profile)

    portals = ws.load_portals()
    for source_name in ('greenhouse', 'lever', 'ashby'):
        portals.sources[source_name].enabled = True
    ws.save_portals(portals)

    state = ws.load_board_discovery_state()
    state.sources['greenhouse'] = state.sources['greenhouse'].model_copy(
        update={
            'boards': ['acme', 'beta'],
            'domains': ['careers.acme.example'],
            'metrics': SourceDiscoveryMetrics(
                boards_scanned=5,
                boards_discovered=3,
                jobs_discovered=7,
                eligible_jobs=4,
                rejected_jobs=3,
            ),
        }
    )
    state.sources['lever'] = state.sources['lever'].model_copy(
        update={
            'boards': ['gamma'],
            'metrics': SourceDiscoveryMetrics(
                boards_scanned=4,
                boards_discovered=1,
                jobs_discovered=0,
                errors=1,
                zero_result=True,
                zero_result_reason='no_jobs_discovered',
                warning='lever scanned boards but discovered no jobs.',
            ),
        }
    )
    state.sources['ashby'] = state.sources['ashby'].model_copy(
        update={
            'metrics': SourceDiscoveryMetrics(
                zero_result=True,
                zero_result_reason='no_active_boards',
                warning='ashby is enabled but no active boards were available to scan.',
            ),
        }
    )
    ws.save_board_discovery_state(state)

    emit_live_event(ws, run_id='run-discovery', run_type='discover', event_type='discover.completed', message='Discovery complete.', status='completed')
    live = live_status_payload(ws)
    stats = live['state']['stats']

    assert stats['configured_sources'] == ['greenhouse', 'lever', 'ashby']
    assert set(stats['source_metrics']) == {'greenhouse', 'lever', 'ashby'}
    assert stats['source_metrics']['greenhouse']['jobs_discovered'] == 7
    assert stats['source_metrics']['lever']['boards_scanned'] == 4
    assert stats['source_metrics']['ashby']['zero_result_reason'] == 'no_active_boards'
    assert stats['zero_result_sources'] == ['lever', 'ashby']
    assert 'lever scanned boards but discovered no jobs.' in stats['source_warnings']
    assert 'ashby is enabled but no active boards were available to scan.' in stats['source_warnings']
    assert stats['persisted_board_counts'] == {'greenhouse': 2, 'lever': 1, 'ashby': 0}
    assert stats['persisted_domain_counts'] == {'greenhouse': 1, 'lever': 0, 'ashby': 0}


def test_emit_live_event_keeps_run_running_for_midstream_completed_events(tmp_path: Path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()

    emit_live_event(
        ws,
        run_id='auto-1',
        run_type='autonomous',
        event_type='autonomous.started',
        message='Started run.',
        status='running',
        stage='discovery',
    )
    emit_live_event(
        ws,
        run_id='auto-1',
        run_type='autonomous',
        event_type='autonomous.drafting.completed',
        message='Drafted one application.',
        status='completed',
        stage='drafting',
    )

    live = live_status_payload(ws)
    assert live['state']['status'] == 'running'
    assert live['state']['stage'] == 'drafting'
    assert live['events'][-1]['status'] == 'completed'


def test_emit_live_event_marks_submission_warning_completion_as_terminal(tmp_path: Path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()

    emit_live_event(
        ws,
        run_id='manual',
        run_type='submission',
        event_type='submission.prepare.started',
        message='Preparing application.',
        status='running',
        stage='prepare',
    )
    emit_live_event(
        ws,
        run_id='manual',
        run_type='submission',
        event_type='submission.submit.completed',
        message='Submission finished with uncertainty.',
        status='warning',
        stage='submit',
    )

    live = live_status_payload(ws)
    assert live['state']['status'] == 'completed_with_failures'
    assert live['state']['completed_at'] is not None
    assert live['state']['stage'] == 'submit'
    assert live['events'][-1]['status'] == 'warning'


def test_emit_live_event_resets_stale_manual_submission_on_new_prepare(tmp_path: Path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()

    emit_live_event(
        ws,
        run_id='manual',
        run_type='submission',
        event_type='submission.submit.completed',
        message='Previous submission finished with uncertainty.',
        status='warning',
        stage='submit',
    )

    completed_at = live_status_payload(ws)['state']['completed_at']
    assert completed_at is not None

    emit_live_event(
        ws,
        run_id='manual',
        run_type='submission',
        event_type='submission.prepare.started',
        message='Preparing a new application.',
        status='running',
        stage='prepare',
    )

    live = live_status_payload(ws)
    assert live['state']['status'] == 'running'
    assert live['state']['completed_at'] is None
    assert live['state']['stage'] == 'prepare'


def test_live_status_payload_normalizes_stale_running_submission_from_terminal_event(tmp_path: Path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()

    ws.save_live_state(
        LiveRunState(
            run_id='manual',
            run_type='submission',
            status='running',
            stage='submit',
            started_at='2026-04-13T19:24:35+00:00',
            run_started_at='2026-04-13T19:24:35+00:00',
        )
    )
    emit_live_event(
        ws,
        run_id='manual',
        run_type='submission',
        event_type='submission.submit.completed',
        message='Submission finished with uncertainty.',
        status='warning',
        stage='submit',
    )
    ws.save_live_state(
        ws.load_live_state().model_copy(
            update={
                'status': 'running',
                'completed_at': None,
            }
        )
    )

    live = live_status_payload(ws)
    assert live['state']['status'] == 'completed_with_failures'
    assert live['state']['completed_at'] is not None
