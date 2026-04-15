from __future__ import annotations

from pathlib import Path

from findmyjob.filefirst.models import FileFact
from findmyjob.filefirst.readiness import collect_filefirst_release_snapshot
from findmyjob.filefirst.workspace import FileWorkspace


def _mock_runtime_checks(monkeypatch) -> None:
    monkeypatch.setattr(
        "findmyjob.filefirst.readiness._inspect_playwright",
        lambda: {
            "package_ok": True,
            "browser_ok": True,
            "package_detail": "playwright import ok",
            "browser_detail": "C:/ms-playwright/chromium/chrome.exe",
        },
    )
    monkeypatch.setattr("findmyjob.filefirst.readiness.find_latex_engine", lambda: None)
    monkeypatch.setattr("findmyjob.filefirst.readiness.find_typst_executable", lambda: "C:/tools/typst.exe")


def _seed_launch_workspace(tmp_path: Path) -> FileWorkspace:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    ws.save_cv("# Test User\n\nBackend engineer with local LM Studio automation experience.\n")
    ws.save_facts(
        [
            FileFact(
                fact_id="contact.primary",
                kind="contact",
                payload={"name": "Test User", "email": "user@example.com", "phone": "555-0100"},
            ),
            FileFact(
                fact_id="authorization.primary",
                kind="authorization",
                payload={"is_authorized": True, "requires_future_sponsorship": False},
            ),
            FileFact(
                fact_id="work.primary",
                kind="work",
                payload={"summary": "Built Python services and local model workflows."},
            ),
        ]
    )

    profile = ws.load_profile()
    profile.runtime.model.base_url = "http://127.0.0.1:1234/v1"
    profile.runtime.model.model = "qwen3-8b"
    profile.runtime.automation.enabled = True
    profile.runtime.automation.submit_enabled = True
    profile.runtime.automation.default_submit_mode = "auto_submit"
    profile.runtime.automation.capture_traces = False
    profile.runtime.automation.capture_dom = False
    profile.runtime.automation.production_sources = ["greenhouse", "lever", "ashby"]
    ws.save_profile(profile)

    portals = ws.load_portals()
    portals.sources["greenhouse"].enabled = True
    portals.sources["greenhouse"].boards = ["datadog"]
    portals.sources["lever"].enabled = True
    portals.sources["lever"].boards = ["plaid"]
    portals.sources["ashby"].enabled = True
    portals.sources["ashby"].boards = ["openai"]
    ws.save_portals(portals)
    return ws


def test_launch_check_passes_for_configured_lmstudio_autonomous_workspace(monkeypatch, tmp_path: Path) -> None:
    _mock_runtime_checks(monkeypatch)
    ws = _seed_launch_workspace(tmp_path)

    snapshot = collect_filefirst_release_snapshot(ws)

    assert snapshot.launch_check.overall_status == "pass"
    assert snapshot.doctor.overall_status == "ready"
    assert snapshot.launch_profile.overall_status == "pass"
    assert any(finding.key == "source.greenhouse.targets" and finding.status == "pass" for finding in snapshot.launch_check.findings)
    assert any(finding.key == "source.lever.targets" and finding.status == "pass" for finding in snapshot.launch_check.findings)
    assert any(finding.key == "source.ashby.targets" and finding.status == "pass" for finding in snapshot.launch_check.findings)
    assert any(
        finding.key == "automation.submit_enabled" and "Autonomous submit mode is enabled" in finding.summary
        for finding in snapshot.launch_check.findings
    )


def test_launch_check_fails_when_launch_sources_only_have_bootstrap_scope(monkeypatch, tmp_path: Path) -> None:
    _mock_runtime_checks(monkeypatch)
    ws = _seed_launch_workspace(tmp_path)
    portals = ws.load_portals()
    portals.sources["greenhouse"].boards = []
    portals.sources["lever"].boards = []
    portals.sources["ashby"].boards = []
    ws.save_portals(portals)

    snapshot = collect_filefirst_release_snapshot(ws)

    assert snapshot.launch_check.overall_status == "fail"
    assert any(finding.key == "source.greenhouse.targets" and finding.status == "fail" for finding in snapshot.launch_check.findings)
    assert any(finding.key == "source.lever.targets" and finding.status == "fail" for finding in snapshot.launch_check.findings)
    assert any(finding.key == "source.ashby.targets" and finding.status == "fail" for finding in snapshot.launch_check.findings)


def test_launch_check_fails_when_renderer_is_non_launch_override(monkeypatch, tmp_path: Path) -> None:
    _mock_runtime_checks(monkeypatch)
    ws = _seed_launch_workspace(tmp_path)
    config_path = ws.root / ".fmj" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text('[personal]\nresume_renderer = "html_to_pdf"\n', encoding="utf-8")

    snapshot = collect_filefirst_release_snapshot(ws)

    assert snapshot.launch_check.overall_status == "fail"
    assert any(
        finding.key == "artifacts.renderer" and finding.status == "fail"
        for finding in snapshot.launch_check.findings
    )
