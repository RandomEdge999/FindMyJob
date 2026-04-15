from __future__ import annotations

import anyio

from findmyjob.filefirst.models import ApplicationEntry
from findmyjob.filefirst.discovery import scan_workspace
from findmyjob.filefirst.workspace import FileWorkspace
from findmyjob.sources.normalizer import build_normalized_job


class _FakeAdapter:
    async def discover(self, client, query):
        posting = build_normalized_job(
            company_name='Acme',
            title='Backend Engineer',
            source='greenhouse',
            source_kind='greenhouse',
            source_job_id='100',
            posting_url='https://boards.greenhouse.io/acme/jobs/100',
            apply_url='https://boards.greenhouse.io/acme/jobs/100',
            location_raw='Remote - United States',
            employment_type='full_time',
            compensation=None,
            description='Build backend services and internal tooling.',
            notes={'board': 'acme'},
        )
        return [(posting, {'id': '100'})]



def test_scan_workspace_writes_inbox_and_deduplicates(monkeypatch, tmp_path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    profile = ws.load_profile()
    profile.runtime.automation.production_sources = ['greenhouse']
    ws.save_profile(profile)
    portals = ws.load_portals()
    portals.sources['greenhouse'].boards = ['acme']
    portals.sources['lever'].enabled = False
    portals.sources['ashby'].enabled = False
    ws.save_portals(portals)

    monkeypatch.setattr('findmyjob.filefirst.discovery._adapter_for', lambda source_name, boards: _FakeAdapter())
    monkeypatch.setattr('findmyjob.filefirst.discovery.resolve_targets', lambda ws, source=None, board=None: {'greenhouse': ['acme']})

    async def _run_scan():
        return await scan_workspace(tmp_path)

    first = anyio.run(_run_scan)
    second = anyio.run(_run_scan)

    assert first['new_jobs'] == 1
    assert first['duplicates'] == 0
    assert second['new_jobs'] == 0
    assert second['duplicates'] == 1
    assert len(ws.load_inbox()) == 1
    assert len(ws.load_scan_history()) == 1


def test_scan_workspace_skips_jobs_already_tied_to_existing_applications(monkeypatch, tmp_path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    profile = ws.load_profile()
    profile.runtime.automation.production_sources = ['greenhouse']
    ws.save_profile(profile)
    portals = ws.load_portals()
    portals.sources['greenhouse'].boards = ['acme']
    portals.sources['lever'].enabled = False
    portals.sources['ashby'].enabled = False
    ws.save_portals(portals)

    monkeypatch.setattr('findmyjob.filefirst.discovery._adapter_for', lambda source_name, boards: _FakeAdapter())
    monkeypatch.setattr('findmyjob.filefirst.discovery.resolve_targets', lambda ws, source=None, board=None: {'greenhouse': ['acme']})

    seeded_posting = _FakeAdapter()

    async def _load_posting():
        return await seeded_posting.discover(None, None)

    posting = anyio.run(_load_posting)[0][0]
    ws.upsert_application(
        ApplicationEntry(
            id='001',
            job_id=posting.job_identity_key,
            date='2026-04-13',
            company='Acme',
            role='Previously Reviewed Role',
            score=4.2,
            grade='B',
            status='Applied',
            pdf=True,
            report='reports/001-acme.md',
            url='https://boards.greenhouse.io/acme/jobs/100#application',
            source='greenhouse',
        )
    )

    async def _run_scan():
        return await scan_workspace(tmp_path)

    result = anyio.run(_run_scan)

    assert result['new_jobs'] == 0
    assert result['duplicates'] == 1
    assert len(ws.load_inbox()) == 0
