from __future__ import annotations

from findmyjob.filefirst.readiness import inspect_filefirst_config
from findmyjob.filefirst.workspace import FileWorkspace


def test_filefirst_readiness_accepts_bootstrap_seed_scope_without_curated_portal_boards(tmp_path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()

    report = inspect_filefirst_config(ws)

    greenhouse_targets = next(finding for finding in report.findings if finding.key == 'sources.greenhouse.targets')
    lever_targets = next(finding for finding in report.findings if finding.key == 'sources.lever.targets')
    ashby_targets = next(finding for finding in report.findings if finding.key == 'sources.ashby.targets')

    assert greenhouse_targets.status == 'warning'
    assert lever_targets.status == 'warning'
    assert ashby_targets.status == 'warning'
    assert greenhouse_targets.summary == 'Greenhouse discovery scope relies only on bundled seeds (unverified).'
    assert 'configured=0' in greenhouse_targets.detail
    assert 'bootstrap=' in greenhouse_targets.detail
