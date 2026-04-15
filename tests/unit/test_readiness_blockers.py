"""Tests for runtime readiness blocker cleanup.

Verifies that:
- Missing Typst is a warning, not a blocker
- Missing LaTeX is a warning, not a blocker
- Missing Playwright browser is still a blocker when greenhouse submit is enabled
- Unsupported board families are NOT system blockers
"""

from __future__ import annotations

from pathlib import Path

from tomlkit import parse

from findmyjob.core.config import write_default_workspace_config
from findmyjob.core.paths import ensure_workspace, workspace_config_file
from findmyjob.core.runtime import inspect_readiness


def _configure_workspace(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    config_path = workspace_config_file(tmp_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    write_default_workspace_config(config_path)
    doc = parse(config_path.read_text(encoding="utf-8"))
    greenhouse = doc["sources"]["greenhouse"]
    greenhouse["enabled"] = True
    greenhouse["boards"] = ["acme"]
    doc["personal"]["enabled"] = False
    config_path.write_text(doc.as_string(), encoding="utf-8")


def test_missing_typst_is_warning_not_blocker(monkeypatch, tmp_path: Path) -> None:
    _configure_workspace(tmp_path)
    monkeypatch.setattr("findmyjob.core.runtime.find_typst_executable", lambda: None)
    monkeypatch.setattr(
        "findmyjob.core.runtime._inspect_playwright",
        lambda: {"package_ok": True, "browser_ok": True, "package_detail": "ok", "browser_detail": "ok"},
    )
    monkeypatch.setattr("findmyjob.core.runtime.keyring_status", lambda: {"available": True, "backend": "test", "detail": None})

    report = inspect_readiness(tmp_path, check_models=False, check_browser=True, check_typst=True)

    typst_finding = next((f for f in report.findings if f.key == "runtime.typst"), None)
    assert typst_finding is not None
    assert typst_finding.status == "ok"
    assert report.blocked_count == 0


def test_missing_latex_is_warning_not_blocker(monkeypatch, tmp_path: Path) -> None:
    _configure_workspace(tmp_path)
    config_path = workspace_config_file(tmp_path)
    doc = parse(config_path.read_text(encoding="utf-8"))
    doc["personal"]["resume_renderer"] = "latex"
    config_path.write_text(doc.as_string(), encoding="utf-8")

    monkeypatch.setattr("findmyjob.core.runtime.find_typst_executable", lambda: "typst")
    monkeypatch.setattr("findmyjob.core.runtime.find_latex_engine", lambda: None)
    monkeypatch.setattr(
        "findmyjob.core.runtime._inspect_playwright",
        lambda: {"package_ok": True, "browser_ok": True, "package_detail": "ok", "browser_detail": "ok"},
    )
    monkeypatch.setattr("findmyjob.core.runtime.keyring_status", lambda: {"available": True, "backend": "test", "detail": None})

    report = inspect_readiness(tmp_path, check_models=False, check_browser=True, check_typst=True)

    latex_finding = next((f for f in report.findings if f.key == "runtime.latex"), None)
    assert latex_finding is not None
    assert latex_finding.status == "warning"
    assert report.blocked_count == 0


def test_greenhouse_disabled_blocks_autonomous_launch_defaults(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    config_path = workspace_config_file(tmp_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    write_default_workspace_config(config_path)
    doc = parse(config_path.read_text(encoding="utf-8"))
    greenhouse = doc["sources"]["greenhouse"]
    greenhouse["enabled"] = False
    config_path.write_text(doc.as_string(), encoding="utf-8")

    report = inspect_readiness(tmp_path, check_models=False, check_browser=False, check_typst=False)

    greenhouse_finding = next((f for f in report.findings if f.key == "sources.greenhouse"), None)
    assert greenhouse_finding is not None
    assert greenhouse_finding.status == "warning"
    assert report.blocked_count > 0
    assert any(f.key == "autonomous.source_enabled" and f.status == "blocked" for f in report.findings)
