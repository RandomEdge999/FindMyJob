from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

textual = pytest.importorskip('textual')


@pytest.fixture()
def anyio_backend() -> str:
    return 'asyncio'

from findmyjob.core.enums import ApplicationMode, CompanySizeBucket, PersonalTriageStatus
from findmyjob.core.runtime import AppRuntime
from findmyjob.core.types import (
    GreenhouseBenchmarkSummary,
    JobSearchQuery,
    LaunchCheckReport,
    ModelLaunchProfileReport,
    ModelLaunchRoleStatus,
    ReleaseSnapshotReport,
    SavedSearch,
    SmokeTestResult,
    ValidationReport,
)
from findmyjob.db.board_repository import BoardRepository
from findmyjob.db.repositories import JobRepository, PersonalTriageRepository, RunRepository
from findmyjob.orchestrator.service import Orchestrator
from findmyjob.sources.normalizer import build_normalized_job
from findmyjob.tui.app import FindMyJobApp
from findmyjob.core.types import BoardRegistry
from findmyjob.personal.workflow import PersonalInboxItem, PersonalInboxSummary



def _seed_job(runtime: AppRuntime, *, title: str, source_job_id: str) -> None:
    posting = build_normalized_job(
        company_name='Acme',
        title=title,
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id=source_job_id,
        posting_url=f'https://boards.greenhouse.io/acme/jobs/{source_job_id}',
        apply_url=f'https://boards.greenhouse.io/acme/jobs/{source_job_id}',
        location_raw='Remote - United States',
        employment_type='full_time',
        compensation=[{'min_cents': 12000000, 'max_cents': 15000000, 'currency_type': 'USD', 'pay_input_type': 'yearly'}],
        description='Backend and platform engineering role.',
        posted_at=datetime.now(timezone.utc),
        company_size_bucket=CompanySizeBucket.MIDSIZE,
    )
    with runtime.session_scope() as session:
        JobRepository(session).upsert_job(posting)
        BoardRepository(session).upsert_board(BoardRegistry(source_adapter='greenhouse', board_token='acme', company_hint='Acme', validation_status='valid', active=True, live_job_count=1))
        RunRepository(session).create_run('sync', ApplicationMode.DRY_RUN, checkpoint_state={'jobs': 1})



def _release_snapshot(tmp_path: Path, *, smoke: SmokeTestResult | None = None, benchmark: GreenhouseBenchmarkSummary | None = None) -> ReleaseSnapshotReport:
    report = LaunchCheckReport(workspace=str(tmp_path))
    report.add('warning', 'greenhouse.smoke_history', 'Controlled smoke checks passed recently.')
    launch_profile = ModelLaunchProfileReport(
        required_roles=['writer', 'classifier', 'question_answerer'],
        optional_roles=['extractor', 'text_router'],
        roles=[
            ModelLaunchRoleStatus(role='writer', profile_name='writer', transport='remote', status='pass'),
            ModelLaunchRoleStatus(role='classifier', profile_name='classifier', transport='local', status='warning', issues=['classifier has no ready fallback']),
            ModelLaunchRoleStatus(role='question_answerer', profile_name='qa', transport='remote', status='pass'),
        ],
        missing_required_roles=[],
        transport_mix='mixed',
        risks=['classifier has no ready fallback.'],
        summary='3/3 required launch roles visible',
    )
    smoke_results = [smoke] if smoke is not None else []
    benchmark_summaries = [benchmark] if benchmark is not None else []
    return ReleaseSnapshotReport(
        generated_at=datetime(2026, 4, 2, tzinfo=timezone.utc),
        workspace=str(tmp_path),
        workspace_name=tmp_path.name,
        config_path=str(tmp_path / '.fmj' / 'config.toml'),
        launch_check=report,
        config_validation=ValidationReport(context='config', workspace=str(tmp_path)),
        doctor=ValidationReport(context='doctor', workspace=str(tmp_path)),
        launch_profile=launch_profile,
        latest_smoke_result=smoke,
        smoke_results=smoke_results,
        latest_benchmark=benchmark,
        benchmark_summaries=benchmark_summaries,
    )



def _patch_release_state(monkeypatch, tmp_path: Path, *, smoke_results: list[SmokeTestResult] | None = None, benchmark_summaries: list[GreenhouseBenchmarkSummary] | None = None) -> None:
    smoke_results = smoke_results or []
    benchmark_summaries = benchmark_summaries or []
    snapshot = _release_snapshot(
        tmp_path,
        smoke=smoke_results[0] if smoke_results else None,
        benchmark=benchmark_summaries[0] if benchmark_summaries else None,
    )
    monkeypatch.setattr(AppRuntime, 'collect_release_snapshot', lambda self, smoke_limit=20, benchmark_limit=10: snapshot)
    monkeypatch.setattr(AppRuntime, 'list_smoke_results', lambda self, limit=20: smoke_results[:limit])
    monkeypatch.setattr(AppRuntime, 'list_benchmark_summaries', lambda self, limit=10: benchmark_summaries[:limit])


@pytest.mark.anyio
async def test_tui_builder_results_and_operator_views(monkeypatch, tmp_path: Path) -> None:
    _patch_release_state(monkeypatch, tmp_path)
    runtime = AppRuntime.bootstrap(tmp_path)
    _seed_job(runtime, title='Backend Platform Engineer', source_job_id='1')

    app = FindMyJobApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press('2')
        assert app.current_view == 'search'

        builder = app.search_builder_view
        builder.name_input.value = 'remote-backend'
        builder.title_keywords_input.value = 'backend'
        builder.source_input.value = 'greenhouse'
        builder.countries_input.value = 'US'
        builder.remote_only.value = True
        builder.update_summary()

        await app._save_current_search()
        assert builder.saved_search_refs

        builder.reset_form()
        await app._load_selected_search()
        assert builder.title_keywords_input.value == 'backend'
        assert builder.source_input.value == 'greenhouse'

        app.current_query = builder.build_query()
        app.switch_view('results')
        app.refresh_results()
        assert app.current_view == 'results'
        assert app.results_view.job_ids

        await pilot.press('4')
        assert app.current_view == 'boards'
        assert app.boards_view.board_tokens == ['acme']

        await pilot.press('5')
        assert app.current_view == 'runs'
        assert app.runs_view.run_ids


@pytest.mark.anyio
async def test_tui_dashboard_shows_release_state_and_runs_expose_smoke_and_benchmarks(monkeypatch, tmp_path: Path) -> None:
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
    benchmark = GreenhouseBenchmarkSummary(
        run_id='bench-1',
        status='completed',
        board_tokens=['acme'],
        boards_attempted=1,
        boards_succeeded=1,
        jobs_seen=12,
        jobs_enriched=10,
        inactive_jobs=1,
        request_count=4,
        rate_limited_count=0,
        failure_count=0,
        duration_seconds=5.0,
        jobs_per_minute=144.0,
    )
    _patch_release_state(monkeypatch, tmp_path, smoke_results=[smoke], benchmark_summaries=[benchmark])
    runtime = AppRuntime.bootstrap(tmp_path)
    _seed_job(runtime, title='Staff Platform Engineer', source_job_id='2')

    app = FindMyJobApp(tmp_path)
    async with app.run_test() as pilot:
        assert 'Launch Check: pass_with_warnings' in app.dashboard_view.summary_text
        assert 'Latest Smoke: pass' in app.dashboard_view.summary_text
        assert 'Latest Benchmark: completed' in app.dashboard_view.summary_text
        assert 'transport=mixed' in app.dashboard_view.summary_text

        await pilot.press('s')
        assert app.current_view == 'runs'
        assert 'Smoke Result' in app.runs_view.ops_detail_text
        assert 'app-123' in app.runs_view.ops_detail_text

        await pilot.press('b')
        assert app.current_view == 'runs'
        assert 'Benchmark Run' in app.runs_view.ops_detail_text
        assert 'bench-1' in app.runs_view.ops_detail_text


@pytest.mark.anyio
async def test_tui_dashboard_shows_personal_inbox_summary(monkeypatch, tmp_path: Path) -> None:
    _patch_release_state(monkeypatch, tmp_path)
    runtime = AppRuntime.bootstrap(tmp_path)
    _seed_job(runtime, title='Backend Inbox Engineer', source_job_id='3')
    monkeypatch.setattr(
        'findmyjob.tui.dashboard.build_personal_inbox',
        lambda runtime, limit=10: PersonalInboxSummary(
            latest_daily_run_id='daily-1',
            enabled_presets=['backend-core'],
            new_matching_jobs=[
                PersonalInboxItem(bucket='new_matching', job_id='job-1', company='Acme', title='Backend Inbox Engineer', job_status='candidate', query_names=['backend-core'])
            ],
            ready_for_review=[
                PersonalInboxItem(bucket='ready_for_review', job_id='job-2', application_id='app-2', company='Acme', title='Prepared Backend Engineer', job_status='ready_for_review', review_status='pending', query_names=['backend-core'])
            ],
            needs_user_input=[
                PersonalInboxItem(bucket='needs_user_input', job_id='job-3', application_id='app-3', company='Acme', title='Needs Input Backend Engineer', job_status='needs_user_input', review_status='needs_user_input', query_names=['backend-core'])
            ],
            approved_pending_submit=[
                PersonalInboxItem(bucket='approved_pending_submit', job_id='job-4', application_id='app-4', company='Acme', title='Approved Backend Engineer', job_status='approved_for_submit', review_status='approved', query_names=['backend-core'])
            ],
        ),
    )

    app = FindMyJobApp(tmp_path)
    async with app.run_test() as pilot:
        assert 'Personal Inbox: shortlist=0 | watching=0 | new=1 | review=1 | needs_input=1 | approved=1' in app.dashboard_view.summary_text
        assert app.dashboard_view.personal_table.row_count == 4



@pytest.mark.anyio
async def test_tui_results_show_priority_and_allow_triage_actions(monkeypatch, tmp_path: Path) -> None:
    _patch_release_state(monkeypatch, tmp_path)
    runtime = AppRuntime.bootstrap(tmp_path)
    _seed_job(runtime, title='Backend Priority Engineer', source_job_id='4')

    app = FindMyJobApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press('3')
        job_id = app.results_view.current_job_id()
        assert job_id is not None
        assert app.results_view.explanations[job_id].priority_label in {'normal', 'medium', 'high'}

        await app._triage_selected_job('shortlist')
        with app.runtime.session_scope() as session:
            decision = PersonalTriageRepository(session).get_decision(job_id)
            assert decision is not None
            assert decision.status == PersonalTriageStatus.SHORTLISTED


@pytest.mark.anyio
async def test_tui_dashboard_shows_latest_training_summary(monkeypatch, tmp_path: Path) -> None:
    _patch_release_state(monkeypatch, tmp_path)
    runtime = AppRuntime.bootstrap(tmp_path)
    _seed_job(runtime, title='Training Summary Engineer', source_job_id='5')
    with runtime.session_scope() as session:
        run = RunRepository(session).create_run(
            'training',
            ApplicationMode.DRY_RUN,
            checkpoint_state={
                'sampled_jobs': [{'job_url': 'https://my.greenhouse.io/jobs/5'}],
                'approved_count': 1,
                'rejected_count': 1,
                'promoted_application_ids': ['app-5'],
            },
        )
        RunRepository(session).complete_run(run.id)

    app = FindMyJobApp(tmp_path)
    async with app.run_test():
        assert 'Latest Training: completed | sampled=1 | approved=1 | rejected=1 | promoted=1' in app.dashboard_view.summary_text
