from __future__ import annotations

from findmyjob.filefirst.readiness import inspect_filefirst_config, inspect_filefirst_readiness
from findmyjob.filefirst.workspace import FileWorkspace
from findmyjob.web.frontend_sync import FrontendBuildReadiness, FrontendBundleStatus


def test_filefirst_readiness_accepts_bootstrap_seed_scope_without_curated_portal_boards(tmp_path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()

    report = inspect_filefirst_config(ws)

    greenhouse_targets = next(finding for finding in report.findings if finding.key == 'sources.greenhouse.targets')
    lever_targets = [finding for finding in report.findings if finding.key == 'sources.lever.targets']
    ashby_targets = [finding for finding in report.findings if finding.key == 'sources.ashby.targets']

    assert greenhouse_targets.status == 'warning'
    assert lever_targets == []
    assert ashby_targets == []
    assert greenhouse_targets.summary == 'Greenhouse discovery scope relies only on bundled seeds (unverified).'
    assert 'configured=0' in greenhouse_targets.detail
    assert 'bootstrap=' in greenhouse_targets.detail


def test_filefirst_readiness_surfaces_frontend_bundle_blocker(monkeypatch, tmp_path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()

    monkeypatch.setattr(
        'findmyjob.filefirst.readiness.inspect_frontend_build_readiness',
        lambda *args, **kwargs: FrontendBuildReadiness(
            status='blocked',
            summary='Frontend bundle is stale or missing, and Node.js/npm are unavailable.',
            detail='bundle=stale_dist :: node=missing :: npm=missing',
            hint='Install Node.js, then run `fmj build` before launching the web console.',
            bundle_status=FrontendBundleStatus(
                needs_build=True,
                reason='stale_dist',
                frontend_root=tmp_path / 'frontend',
                dist_dir=tmp_path / 'src' / 'findmyjob' / 'web' / 'frontend_dist',
            ),
            node_available=False,
            npm_available=False,
        ),
    )

    report = inspect_filefirst_readiness(ws, check_models=False, check_browser=False, check_typst=False)

    finding = next(finding for finding in report.findings if finding.key == 'runtime.frontend.bundle')
    assert finding.status == 'blocked'
    assert finding.hint == 'Install Node.js, then run `fmj build` before launching the web console.'
