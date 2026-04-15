from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import anyio
import pytest

from findmyjob.filefirst.live_market import SeedDiscovery, discover_live_market
from findmyjob.filefirst.models import FileFact
from findmyjob.filefirst.workspace import FileWorkspace
from findmyjob.sources.normalizer import build_normalized_job


def test_discover_live_market_rejects_dead_public_apply_pages(monkeypatch) -> None:
    root = Path('.test-live-market-dead-page-workspace').resolve()
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    try:
        ws = FileWorkspace(root)
        ws.ensure()
        ws.save_cv('# Test User\n')
        ws.save_facts([
            FileFact(fact_id='contact.primary', kind='contact', payload={'name': 'Test User', 'email': 'user@example.com'}),
        ])

        posting = build_normalized_job(
            company_name='Beta',
            title='Software Engineer, Server',
            source='greenhouse',
            source_kind='greenhouse',
            source_job_id='8492315002',
            posting_url='https://app.careerpuck.com/job-board/beta/job/8492315002?gh_jid=8492315002',
            apply_url='https://app.careerpuck.com/job-board/beta/job/8492315002?gh_jid=8492315002',
            location_raw='San Francisco, CA',
            employment_type='full_time',
            compensation=None,
            description='Build the core rider experience.',
            notes={'board': 'beta'},
        )

        class _FakeAdapter:
            async def discover(self, client, query):
                _ = client
                _ = query
                return [(posting, {'id': '8492315002'})]

        async def fake_crawl(client, workspace, *, max_pages=20, crawl_depth=2, progress_callback=None):
            _ = client
            _ = workspace
            _ = max_pages
            _ = crawl_depth
            _ = progress_callback
            return SeedDiscovery(board_targets={'greenhouse': set(), 'lever': set(), 'ashby': set()}, unsupported_urls=[], crawled_pages=0, errors=[])

        async def fake_fetch_html(client, url: str):
            _ = client
            _ = url
            return '<html><title>Beta open jobs</title><body>Browse all open jobs.</body></html>'

        monkeypatch.setattr('findmyjob.filefirst.live_market._builtin_targets', lambda: {'greenhouse': ['beta']})
        monkeypatch.setattr('findmyjob.filefirst.live_market._configured_targets', lambda ws: {})
        monkeypatch.setattr('findmyjob.filefirst.live_market._adapter_for', lambda source_name, boards: _FakeAdapter())
        monkeypatch.setattr('findmyjob.filefirst.live_market._crawl_seed_targets', fake_crawl)
        monkeypatch.setattr('findmyjob.filefirst.live_market._fetch_html', fake_fetch_html)

        result = anyio.run(lambda: discover_live_market(ws, limit=1))

        assert result['saved_job_ids'] == [posting.job_identity_key]
        saved = ws.load_job(posting.job_identity_key)
        assert saved is not None
        assert saved.hard_reject_reason == 'apply_page_unavailable'
        assert saved.rehearsal_eligible is False
    finally:
        shutil.rmtree(root, ignore_errors=True)

