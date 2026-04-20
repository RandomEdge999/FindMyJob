from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from findmyjob.cli.main import app

runner = CliRunner()


def test_version_cli_json_reports_installed_version(monkeypatch) -> None:
    monkeypatch.setattr("findmyjob.cli.main._current_version", lambda: "9.9.9")

    result = runner.invoke(app, ["version", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["package"] == "findmyjob"
    assert payload["version"] == "9.9.9"
    assert payload["python"]
    assert payload["executable"]


def test_init_cli_seeds_default_templates(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "--workspace", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".fmj" / "config.toml").exists()
    assert (tmp_path / "templates" / "typst" / "resume.typ").exists()
    assert (tmp_path / "templates" / "typst" / "cover_letter.typ").exists()


def test_bootstrap_cli_json_reports_repo_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "findmyjob.cli.main.bootstrap_repo_environment",
        lambda **kwargs: {
            "status": "ready",
            "project_root": str(kwargs["project_root"]),
            "venv_python": str(tmp_path / ".venv312" / "Scripts" / "python.exe"),
            "summary": "Repo-local Python 3.12 environment ready.",
        },
    )

    result = runner.invoke(app, ["bootstrap", "--workspace", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "ready"
    assert payload["project_root"] == str(tmp_path)
