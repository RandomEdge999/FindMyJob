from __future__ import annotations

from pathlib import Path

import anyio

from findmyjob.filefirst.live_market import SeedDiscovery, _fetch_html, _posting_to_inbox, discover_live_market
from findmyjob.filefirst.models import ApplicationEntry, FileFact, SourceDiscoveryMetrics, TrackedCompany
from findmyjob.filefirst.service import FileFirstOperatorService
from findmyjob.filefirst.source_targets import fallback_targets
from findmyjob.filefirst.workspace import FileWorkspace
from findmyjob.sources.normalizer import build_normalized_job


class _FakeResponse:
    def __init__(self, text: str, content_type: str = 'text/html') -> None:
        self.text = text
        self.headers = {'content-type': content_type}

    def raise_for_status(self) -> None:
        return None


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def get(self, url: str, timeout: float = 20.0):
        _ = url
        _ = timeout
        return self._response


def _workspace(tmp_path: Path) -> FileWorkspace:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    ws.save_cv('# Test User\n')
    ws.save_facts([
        FileFact(fact_id='contact.primary', kind='contact', payload={'name': 'Test User', 'email': 'user@example.com'}),
    ])
    return ws


def test_fetch_html_accepts_xml_sitemap_content_type() -> None:
    payload = anyio.run(_fetch_html, _FakeClient(_FakeResponse('<urlset></urlset>', 'application/xml')), 'https://example.com/sitemap.xml')

    assert payload == '<urlset></urlset>'


def test_discover_live_market_marks_supported_and_skipped_jobs(monkeypatch, tmp_path: Path) -> None:
    ws = _workspace(tmp_path)

    posting = build_normalized_job(
        company_name='Beta',
        title='New Grad Backend Engineer',
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id='200',
        posting_url='https://boards.greenhouse.io/beta/jobs/200',
        apply_url='https://boards.greenhouse.io/beta/jobs/200',
        location_raw='Remote - United States',
        employment_type='full_time',
        compensation=None,
        description='Entry-level backend role.',
        notes={'board': 'beta'},
    )

    class _FakeAdapter:
        async def discover(self, client, query):
            _ = client
            _ = query
            return [(posting, {'id': '200'})]

    async def fake_crawl(client, workspace, *, max_pages=20, crawl_depth=2, progress_callback=None):
        _ = client
        _ = workspace
        _ = max_pages
        _ = crawl_depth
        _ = progress_callback
        return SeedDiscovery(
            board_targets={'greenhouse': set(), 'lever': set(), 'ashby': set()},
            unsupported_urls=[{'url': 'https://acme.myworkdayjobs.com/job/123', 'ats_family': 'workday'}],
            crawled_pages=1,
            errors=[],
        )

    async def fake_fetch_html(client, url: str):
        _ = client
        if 'myworkdayjobs' in url:
            return '<html><title>Software Engineer</title><body>No sponsorship available for this role.</body></html>'
        return '<html><body><form><input name="name"><button type="submit">Apply</button></form></body></html>'

    monkeypatch.setattr('findmyjob.filefirst.live_market._builtin_targets', lambda: {'greenhouse': ['beta']})
    monkeypatch.setattr('findmyjob.filefirst.live_market._configured_targets', lambda ws: {})
    monkeypatch.setattr('findmyjob.filefirst.live_market._adapter_for', lambda source_name, boards: _FakeAdapter())
    monkeypatch.setattr('findmyjob.filefirst.live_market._crawl_seed_targets', fake_crawl)
    monkeypatch.setattr('findmyjob.filefirst.live_market._fetch_html', fake_fetch_html)

    result = anyio.run(lambda: discover_live_market(ws, limit=2))

    assert result['discovered'] == 2
    assert len(result['saved_job_ids']) == 2
    supported = ws.load_job(posting.job_identity_key)
    assert supported is not None
    assert supported.ats_family == 'greenhouse'
    assert supported.ats_preview_supported is True
    assert supported.rehearsal_eligible is True

    skipped_id = next(job_id for job_id in result['saved_job_ids'] if job_id != posting.job_identity_key)
    skipped = ws.load_job(skipped_id)
    assert skipped is not None
    assert skipped.ats_family == 'workday'
    assert skipped.rehearsal_eligible is False
    assert skipped.hard_reject_reason == 'unsupported_ats:workday'
    assert skipped.auth_reject_reason == 'no_sponsorship'
    assert skipped.job_id in result['skipped_job_ids']


def test_discover_live_market_skips_jobs_already_linked_to_applications(monkeypatch, tmp_path: Path) -> None:
    ws = _workspace(tmp_path)

    posting = build_normalized_job(
        company_name='Beta',
        title='New Grad Backend Engineer',
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id='200',
        posting_url='https://boards.greenhouse.io/beta/jobs/200',
        apply_url='https://boards.greenhouse.io/beta/jobs/200#application',
        location_raw='Remote - United States',
        employment_type='full_time',
        compensation=None,
        description='Entry-level backend role.',
        notes={'board': 'beta'},
    )
    ws.upsert_application(
        ApplicationEntry(
            id='001',
            job_id=posting.job_identity_key,
            date='2026-04-13',
            company='Beta',
            role='Previously Reviewed Role',
            score=4.0,
            grade='B',
            status='Applied',
            pdf=True,
            report='reports/001-beta.md',
            url='https://boards.greenhouse.io/beta/jobs/200#application',
            source='greenhouse',
        )
    )

    class _FakeAdapter:
        async def discover(self, client, query):
            _ = client
            _ = query
            return [(posting, {'id': '200'})]

    async def fake_crawl(client, workspace, *, max_pages=20, crawl_depth=2, progress_callback=None):
        _ = client
        _ = workspace
        _ = max_pages
        _ = crawl_depth
        _ = progress_callback
        return SeedDiscovery(
            board_targets={'greenhouse': set(), 'lever': set(), 'ashby': set()},
            unsupported_urls=[],
            crawled_pages=0,
            errors=[],
        )

    monkeypatch.setattr('findmyjob.filefirst.live_market._builtin_targets', lambda: {'greenhouse': ['beta']})
    monkeypatch.setattr('findmyjob.filefirst.live_market._configured_targets', lambda ws: {})
    monkeypatch.setattr('findmyjob.filefirst.live_market._adapter_for', lambda source_name, boards: _FakeAdapter())
    monkeypatch.setattr('findmyjob.filefirst.live_market._crawl_seed_targets', fake_crawl)

    async def _run():
        return await discover_live_market(ws, limit=1)

    result = anyio.run(_run)

    assert result['new_jobs'] == 0
    assert result['duplicates'] == 1
    assert result['saved_job_ids'] == []


def test_discover_live_market_skips_jobs_preserved_in_handled_memory_after_reset(monkeypatch, tmp_path: Path) -> None:
    ws = _workspace(tmp_path)

    posting = build_normalized_job(
        company_name='Beta',
        title='New Grad Backend Engineer',
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id='200',
        posting_url='https://boards.greenhouse.io/beta/jobs/200',
        apply_url='https://boards.greenhouse.io/beta/jobs/200#application',
        location_raw='Remote - United States',
        employment_type='full_time',
        compensation=None,
        description='Entry-level backend role.',
        notes={'board': 'beta'},
    )
    ws.upsert_application(
        ApplicationEntry(
            id='001',
            job_id=posting.job_identity_key,
            date='2026-04-13',
            company='Beta',
            role='New Grad Backend Engineer',
            score=4.0,
            grade='B',
            status='Applied',
            pdf=True,
            report='reports/001-beta.md',
            url='https://boards.greenhouse.io/beta/jobs/200#application',
            source='greenhouse',
        )
    )
    FileFirstOperatorService(tmp_path).reset_operational_state_payload()

    class _FakeAdapter:
        async def discover(self, client, query):
            _ = client
            _ = query
            return [(posting, {'id': '200'})]

    async def fake_crawl(client, workspace, *, max_pages=20, crawl_depth=2, progress_callback=None):
        _ = client
        _ = workspace
        _ = max_pages
        _ = crawl_depth
        _ = progress_callback
        return SeedDiscovery(
            board_targets={'greenhouse': set(), 'lever': set(), 'ashby': set()},
            unsupported_urls=[],
            crawled_pages=0,
            errors=[],
        )

    monkeypatch.setattr('findmyjob.filefirst.live_market._builtin_targets', lambda: {'greenhouse': ['beta']})
    monkeypatch.setattr('findmyjob.filefirst.live_market._configured_targets', lambda ws: {})
    monkeypatch.setattr('findmyjob.filefirst.live_market._adapter_for', lambda source_name, boards: _FakeAdapter())
    monkeypatch.setattr('findmyjob.filefirst.live_market._crawl_seed_targets', fake_crawl)

    result = anyio.run(lambda: discover_live_market(ws, limit=1))

    assert result['new_jobs'] == 0
    assert result['duplicates'] == 1
    assert result['saved_job_ids'] == []


def test_discover_live_market_respects_enabled_portal_scope(monkeypatch, tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    profile = ws.load_profile()
    profile.runtime.automation.production_sources = ['greenhouse', 'lever', 'ashby']
    ws.save_profile(profile)
    portals = ws.load_portals()
    portals.sources['greenhouse'].enabled = True
    portals.sources['lever'].enabled = False
    portals.sources['ashby'].enabled = False
    ws.save_portals(portals)

    seen_sources: list[str] = []

    class _FakeAdapter:
        def __init__(self, source_name: str) -> None:
            self._source_name = source_name

        async def discover(self, client, query):
            _ = client
            _ = query
            seen_sources.append(self._source_name)
            return []

    async def fake_crawl(client, workspace, *, max_pages=20, crawl_depth=2, progress_callback=None):
        _ = client
        _ = workspace
        _ = max_pages
        _ = crawl_depth
        _ = progress_callback
        return SeedDiscovery(
            board_targets={'greenhouse': set(), 'lever': set(), 'ashby': set()},
            unsupported_urls=[],
            crawled_pages=0,
            errors=[],
        )

    monkeypatch.setattr('findmyjob.filefirst.live_market._builtin_targets', lambda: {'greenhouse': ['beta'], 'lever': ['gamma'], 'ashby': ['delta']})
    monkeypatch.setattr('findmyjob.filefirst.live_market._configured_targets', lambda workspace: {})
    monkeypatch.setattr('findmyjob.filefirst.live_market._adapter_for', lambda source_name, boards: _FakeAdapter(source_name))
    monkeypatch.setattr('findmyjob.filefirst.live_market._crawl_seed_targets', fake_crawl)

    result = anyio.run(lambda: discover_live_market(ws, limit=1))

    assert result['targets'] == {'greenhouse': ['beta']}
    assert seen_sources == ['greenhouse']



def test_discover_live_market_round_robins_sources_before_one_source_can_fill_limit(monkeypatch, tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    profile = ws.load_profile()
    profile.runtime.automation.production_sources = ['greenhouse', 'lever', 'ashby']
    ws.save_profile(profile)
    portals = ws.load_portals()
    portals.sources['greenhouse'].enabled = True
    portals.sources['lever'].enabled = True
    portals.sources['ashby'].enabled = True
    ws.save_portals(portals)

    def make_posting(source: str, board: str, job_id: str):
        host = {
            'greenhouse': f'https://boards.greenhouse.io/{board}/jobs/{job_id}',
            'lever': f'https://jobs.lever.co/{board}/{job_id}',
            'ashby': f'https://jobs.ashbyhq.com/{board}/{job_id}',
        }[source]
        return build_normalized_job(
            company_name=board.title(),
            title=f'{source.title()} Backend Engineer {job_id}',
            source=source,
            source_kind=source,
            source_job_id=job_id,
            posting_url=host,
            apply_url=host,
            location_raw='Remote',
            employment_type='full_time',
            compensation=None,
            description='Entry-level backend role.',
            notes={'board': board},
        )

    class _FakeAdapter:
        def __init__(self, source_name: str, boards: list[str] | None = None) -> None:
            self._source_name = source_name
            self._boards = list(boards or [])

        async def discover(self, client, query):
            _ = client
            _ = query
            board = str(self._boards[0] if self._boards else '')
            if self._source_name == 'greenhouse':
                return [
                    (make_posting('greenhouse', board, 'gh-1'), {'id': 'gh-1'}),
                    (make_posting('greenhouse', board, 'gh-2'), {'id': 'gh-2'}),
                    (make_posting('greenhouse', board, 'gh-3'), {'id': 'gh-3'}),
                ]
            if self._source_name == 'lever':
                return [(make_posting('lever', board, 'lv-1'), {'id': 'lv-1'})]
            return [(make_posting('ashby', board, 'as-1'), {'id': 'as-1'})]

    async def fake_crawl(client, workspace, *, max_pages=20, crawl_depth=2, progress_callback=None):
        _ = client
        _ = workspace
        _ = max_pages
        _ = crawl_depth
        _ = progress_callback
        return SeedDiscovery(
            board_targets={'greenhouse': set(), 'lever': set(), 'ashby': set()},
            unsupported_urls=[],
            crawled_pages=0,
            errors=[],
        )

    async def fake_fetch_html(client, url: str):
        _ = client
        _ = url
        return '<html><body><form><input name="name"><button type="submit">Apply</button></form></body></html>'

    monkeypatch.setattr('findmyjob.filefirst.live_market._builtin_targets', lambda: {'greenhouse': ['acme'], 'lever': ['beta'], 'ashby': ['gamma']})
    monkeypatch.setattr('findmyjob.filefirst.live_market._configured_targets', lambda workspace: {})
    monkeypatch.setattr('findmyjob.filefirst.live_market._adapter_for', lambda source_name, boards: _FakeAdapter(source_name, boards))
    monkeypatch.setattr('findmyjob.filefirst.live_market._crawl_seed_targets', fake_crawl)
    monkeypatch.setattr('findmyjob.filefirst.live_market._fetch_html', fake_fetch_html)

    result = anyio.run(lambda: discover_live_market(ws, limit=2))

    saved_sources = [ws.load_job(job_id).source for job_id in result['saved_job_ids']]

    assert len(result['saved_job_ids']) == 2
    assert saved_sources == ['greenhouse', 'lever']
    assert result['source_counts']['greenhouse'] == 1
    assert result['source_counts']['lever'] == 1
    assert result['source_counts']['ashby'] == 0


def test_discover_live_market_scans_each_source_before_candidate_limit_stops_round(monkeypatch, tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    profile = ws.load_profile()
    profile.runtime.automation.production_sources = ['greenhouse', 'lever', 'ashby']
    ws.save_profile(profile)
    portals = ws.load_portals()
    portals.sources['greenhouse'].enabled = True
    portals.sources['lever'].enabled = True
    portals.sources['ashby'].enabled = True
    ws.save_portals(portals)

    def make_posting(source: str, board: str, job_id: str):
        host = {
            'greenhouse': f'https://boards.greenhouse.io/{board}/jobs/{job_id}',
            'lever': f'https://jobs.lever.co/{board}/{job_id}',
            'ashby': f'https://jobs.ashbyhq.com/{board}/{job_id}',
        }[source]
        return build_normalized_job(
            company_name=board.title(),
            title=f'{source.title()} Backend Engineer {job_id}',
            source=source,
            source_kind=source,
            source_job_id=job_id,
            posting_url=host,
            apply_url=host,
            location_raw='Remote',
            employment_type='full_time',
            compensation=None,
            description='Entry-level backend role.',
            notes={'board': board},
        )

    seen_sources: list[str] = []

    class _FakeAdapter:
        def __init__(self, source_name: str, boards: list[str] | None = None) -> None:
            self._source_name = source_name
            self._boards = list(boards or [])

        async def discover(self, client, query):
            _ = client
            _ = query
            seen_sources.append(self._source_name)
            board = str(self._boards[0] if self._boards else '')
            if self._source_name == 'greenhouse':
                return [
                    (make_posting('greenhouse', board, 'gh-1'), {'id': 'gh-1'}),
                    (make_posting('greenhouse', board, 'gh-2'), {'id': 'gh-2'}),
                    (make_posting('greenhouse', board, 'gh-3'), {'id': 'gh-3'}),
                ]
            if self._source_name == 'lever':
                return [(make_posting('lever', board, 'lv-1'), {'id': 'lv-1'})]
            return [(make_posting('ashby', board, 'as-1'), {'id': 'as-1'})]

    async def fake_crawl(client, workspace, *, max_pages=20, crawl_depth=2, progress_callback=None):
        _ = client
        _ = workspace
        _ = max_pages
        _ = crawl_depth
        _ = progress_callback
        return SeedDiscovery(
            board_targets={'greenhouse': set(), 'lever': set(), 'ashby': set()},
            unsupported_urls=[],
            crawled_pages=0,
            errors=[],
        )

    async def fake_fetch_html(client, url: str):
        _ = client
        _ = url
        return '<html><body><form><input name="name"><button type="submit">Apply</button></form></body></html>'

    monkeypatch.setattr('findmyjob.filefirst.live_market._builtin_targets', lambda: {'greenhouse': ['acme'], 'lever': ['beta'], 'ashby': ['gamma']})
    monkeypatch.setattr('findmyjob.filefirst.live_market._configured_targets', lambda workspace: {})
    monkeypatch.setattr('findmyjob.filefirst.live_market._adapter_for', lambda source_name, boards: _FakeAdapter(source_name, boards))
    monkeypatch.setattr('findmyjob.filefirst.live_market._crawl_seed_targets', fake_crawl)
    monkeypatch.setattr('findmyjob.filefirst.live_market._fetch_html', fake_fetch_html)

    result = anyio.run(lambda: discover_live_market(ws, limit=2, candidate_limit=3))

    assert seen_sources == ['greenhouse', 'lever', 'ashby']
    assert result['source_metrics']['greenhouse']['boards_scanned'] == 1
    assert result['source_metrics']['lever']['boards_scanned'] == 1
    assert result['source_metrics']['ashby']['boards_scanned'] == 1
    assert 'lever' not in result['zero_result_sources']
    assert 'ashby' not in result['zero_result_sources']


def test_discover_live_market_prefers_explicit_configured_scope_over_bootstrap_and_persisted_targets(monkeypatch, tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    profile = ws.load_profile()
    profile.runtime.automation.production_sources = ['greenhouse']
    ws.save_profile(profile)

    portals = ws.load_portals()
    portals.sources['greenhouse'].boards = ['beta']
    portals.sources['greenhouse'].seed_urls = [
        'https://boards.greenhouse.io/gamma',
        'https://boards.greenhouse.io/beta',
    ]
    portals.tracked_companies = [
        TrackedCompany(name='Acme Robotics', source='greenhouse', board='epsilon', enabled=True),
    ]
    ws.save_portals(portals)

    state = ws.load_board_discovery_state()
    state.sources['greenhouse'] = state.sources['greenhouse'].model_copy(
        update={
            'boards': ['alpha', 'gamma'],
            'metrics': SourceDiscoveryMetrics(boards_scanned=2, jobs_discovered=1),
        }
    )
    ws.save_board_discovery_state(state)

    seen_boards: list[str] = []

    class _FakeAdapter:
        def __init__(self, boards: list[str] | None = None) -> None:
            self._board = str((boards or [''])[0] or '')

        async def discover(self, client, query):
            _ = client
            _ = query
            seen_boards.append(self._board)
            return []

    async def fake_crawl(client, workspace, *, max_pages=20, crawl_depth=2, progress_callback=None):
        _ = client
        _ = workspace
        _ = max_pages
        _ = crawl_depth
        _ = progress_callback
        return SeedDiscovery(
            board_targets={'greenhouse': {'delta', 'beta'}, 'lever': set(), 'ashby': set()},
            unsupported_urls=[],
            crawled_pages=0,
            errors=[],
        )

    monkeypatch.setattr('findmyjob.filefirst.live_market._builtin_targets', lambda: {'greenhouse': ['alpha', 'beta']})
    monkeypatch.setattr('findmyjob.filefirst.live_market._adapter_for', lambda source_name, boards: _FakeAdapter(boards))
    monkeypatch.setattr('findmyjob.filefirst.live_market._crawl_seed_targets', fake_crawl)

    result = anyio.run(lambda: discover_live_market(ws, limit=1))

    assert result['targets'] == {'greenhouse': ['beta', 'gamma', 'epsilon']}
    assert seen_boards == ['beta', 'gamma', 'epsilon']
    assert result['source_metrics']['greenhouse']['boards_scanned'] == 3
    assert result['zero_result_sources'] == ['greenhouse']
    assert result['warnings'] == ['greenhouse scanned boards but discovered no jobs.']


def test_fallback_targets_keeps_bootstrap_targets_additive_when_manual_boards_exist() -> None:
    merged = fallback_targets(
        bootstrap={"greenhouse": ["bootstrap-board"]},
        configured={"greenhouse": ["manual-board"]},
        persisted={"greenhouse": ["persisted-board"]},
        extra={"greenhouse": ["crawl-board"]},
    )

    assert merged == {"greenhouse": ["manual-board", "persisted-board", "bootstrap-board", "crawl-board"]}


def test_discover_live_market_persists_discovered_boards_for_future_runs(monkeypatch, tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    profile = ws.load_profile()
    profile.runtime.automation.production_sources = ['greenhouse']
    ws.save_profile(profile)

    posting = build_normalized_job(
        company_name='Beta',
        title='New Grad Backend Engineer',
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id='200',
        posting_url='https://boards.greenhouse.io/fresh-board/jobs/200',
        apply_url='https://boards.greenhouse.io/fresh-board/jobs/200',
        location_raw='Remote - United States',
        employment_type='full_time',
        compensation=None,
        description='Entry-level backend role.',
        notes={'board': 'fresh-board'},
    )

    run_number = {'value': 1}
    seen_boards: list[str] = []

    class _FakeAdapter:
        def __init__(self, boards: list[str] | None = None) -> None:
            self._board = str((boards or [''])[0] or '')

        async def discover(self, client, query):
            _ = client
            _ = query
            seen_boards.append(self._board)
            if run_number['value'] == 1 and self._board == 'fresh-board':
                return [(posting, {'id': '200'})]
            return []

    async def fake_crawl(client, workspace, *, max_pages=20, crawl_depth=2, progress_callback=None):
        _ = client
        _ = workspace
        _ = max_pages
        _ = crawl_depth
        _ = progress_callback
        if run_number['value'] == 1:
            return SeedDiscovery(
                board_targets={'greenhouse': {'fresh-board'}, 'lever': set(), 'ashby': set()},
                source_domains={'greenhouse': {'careers.beta.example'}, 'lever': set(), 'ashby': set()},
                unsupported_urls=[],
                crawled_pages=1,
                errors=[],
            )
        return SeedDiscovery(
            board_targets={'greenhouse': set(), 'lever': set(), 'ashby': set()},
            unsupported_urls=[],
            crawled_pages=0,
            errors=[],
        )

    async def fake_fetch_html(client, url: str):
        _ = client
        _ = url
        return '<html><body><form><input name="name"><button type="submit">Apply</button></form></body></html>'

    monkeypatch.setattr('findmyjob.filefirst.live_market._builtin_targets', lambda: {})
    monkeypatch.setattr('findmyjob.filefirst.live_market._adapter_for', lambda source_name, boards: _FakeAdapter(boards))
    monkeypatch.setattr('findmyjob.filefirst.live_market._crawl_seed_targets', fake_crawl)
    monkeypatch.setattr('findmyjob.filefirst.live_market._fetch_html', fake_fetch_html)

    first = anyio.run(lambda: discover_live_market(ws, limit=1))
    persisted = ws.load_board_discovery_state()

    assert first['targets'] == {'greenhouse': ['fresh-board']}
    assert persisted.sources['greenhouse'].boards == ['fresh-board']
    assert persisted.sources['greenhouse'].domains == ['careers.beta.example']
    assert persisted.sources['greenhouse'].metrics.jobs_discovered == 1

    run_number['value'] = 2
    seen_boards.clear()

    second = anyio.run(lambda: discover_live_market(ws, limit=1))

    assert second['targets'] == {'greenhouse': ['fresh-board']}
    assert seen_boards == ['fresh-board']
    assert second['zero_result_sources'] == ['greenhouse']


def test_discover_live_market_stops_processing_large_board_once_limit_is_reached(monkeypatch, tmp_path: Path) -> None:
    ws = _workspace(tmp_path)

    def make_posting(job_id: str):
        return build_normalized_job(
            company_name='Acme',
            title=f'Backend Engineer {job_id}',
            source='greenhouse',
            source_kind='greenhouse',
            source_job_id=job_id,
            posting_url=f'https://boards.greenhouse.io/acme/jobs/{job_id}',
            apply_url=f'https://boards.greenhouse.io/acme/jobs/{job_id}',
            location_raw='Remote',
            employment_type='full_time',
            compensation=None,
            description='Entry-level backend role.',
            notes={'board': 'acme'},
        )

    class _FakeAdapter:
        async def discover(self, client, query):
            _ = client
            _ = query
            return [(make_posting(str(index)), {'id': str(index)}) for index in range(1, 8)]

    async def fake_crawl(client, workspace, *, max_pages=20, crawl_depth=2, progress_callback=None):
        _ = client
        _ = workspace
        _ = max_pages
        _ = crawl_depth
        _ = progress_callback
        return SeedDiscovery(
            board_targets={'greenhouse': set(), 'lever': set(), 'ashby': set()},
            unsupported_urls=[],
            crawled_pages=0,
            errors=[],
        )

    supported_calls: list[str] = []

    async def fake_supported_job(posting, *, source_name: str, board: str, client):
        _ = client
        supported_calls.append(str(posting.source_job_id))
        return (
            _posting_to_inbox(posting).model_copy(
                update={
                    'workflow_state': 'pending',
                    'ats_family': source_name,
                    'ats_preview_supported': True,
                    'rehearsal_eligible': True,
                    'rehearsal_rank': 100.0,
                    'discovery_method': f'live_market:{source_name}',
                    'notes': {'board': board},
                }
            ),
            None,
        )

    monkeypatch.setattr('findmyjob.filefirst.live_market._builtin_targets', lambda: {'greenhouse': ['acme']})
    monkeypatch.setattr('findmyjob.filefirst.live_market._configured_targets', lambda workspace: {})
    monkeypatch.setattr('findmyjob.filefirst.live_market._adapter_for', lambda source_name, boards: _FakeAdapter())
    monkeypatch.setattr('findmyjob.filefirst.live_market._crawl_seed_targets', fake_crawl)
    monkeypatch.setattr('findmyjob.filefirst.live_market._supported_job', fake_supported_job)

    result = anyio.run(lambda: discover_live_market(ws, limit=2))

    assert len(result['saved_job_ids']) == 2
    assert supported_calls == ['1', '2']


def test_discover_live_market_emits_incremental_progress_for_large_board(monkeypatch, tmp_path: Path) -> None:
    ws = _workspace(tmp_path)

    def make_posting(job_id: str):
        return build_normalized_job(
            company_name='Acme',
            title=f'Backend Engineer {job_id}',
            source='greenhouse',
            source_kind='greenhouse',
            source_job_id=job_id,
            posting_url=f'https://boards.greenhouse.io/acme/jobs/{job_id}',
            apply_url=f'https://boards.greenhouse.io/acme/jobs/{job_id}',
            location_raw='Remote',
            employment_type='full_time',
            compensation=None,
            description='Entry-level backend role.',
            notes={'board': 'acme'},
        )

    class _FakeAdapter:
        async def discover(self, client, query):
            _ = client
            _ = query
            return [(make_posting(str(index)), {'id': str(index)}) for index in range(1, 13)]

    async def fake_crawl(client, workspace, *, max_pages=20, crawl_depth=2, progress_callback=None):
        _ = client
        _ = workspace
        _ = max_pages
        _ = crawl_depth
        _ = progress_callback
        return SeedDiscovery(
            board_targets={'greenhouse': set(), 'lever': set(), 'ashby': set()},
            unsupported_urls=[],
            crawled_pages=0,
            errors=[],
        )

    async def fake_supported_job(posting, *, source_name: str, board: str, client):
        _ = client
        return (
            _posting_to_inbox(posting).model_copy(
                update={
                    'workflow_state': 'pending',
                    'ats_family': source_name,
                    'ats_preview_supported': True,
                    'rehearsal_eligible': True,
                    'rehearsal_rank': 100.0,
                    'discovery_method': f'live_market:{source_name}',
                    'notes': {'board': board},
                }
            ),
            None,
        )

    progress_events: list[dict[str, object]] = []

    monkeypatch.setattr('findmyjob.filefirst.live_market._builtin_targets', lambda: {'greenhouse': ['acme']})
    monkeypatch.setattr('findmyjob.filefirst.live_market._configured_targets', lambda workspace: {})
    monkeypatch.setattr('findmyjob.filefirst.live_market._adapter_for', lambda source_name, boards: _FakeAdapter())
    monkeypatch.setattr('findmyjob.filefirst.live_market._crawl_seed_targets', fake_crawl)
    monkeypatch.setattr('findmyjob.filefirst.live_market._supported_job', fake_supported_job)

    anyio.run(lambda: discover_live_market(ws, limit=12, progress_callback=lambda payload: progress_events.append(dict(payload))))

    assert any(
        event.get('phase') == 'source_board_progress' and int(event.get('discovered', 0) or 0) == 10
        for event in progress_events
    )
