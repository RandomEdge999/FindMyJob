import json
from pathlib import Path

from typer.testing import CliRunner

from findmyjob.cli.main import app
from findmyjob.core.assets import ensure_default_workspace_templates
from findmyjob.core.config import write_default_workspace_config
from findmyjob.core.paths import workspace_config_file
from findmyjob.core.runtime import AppRuntime

runner = CliRunner()


def test_start_cli_json_initializes_workspace(tmp_path: Path) -> None:
    result = runner.invoke(app, ["start", "--workspace", str(tmp_path), "--no-open", "--json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["entrypoint"] == "start"
    assert payload["initialization"]["created"] is True
    assert Path(payload["workspace"]["config_path"]).exists()
    assert (tmp_path / "templates" / "typst" / "resume.typ").exists()
    assert payload["web_console"]["requested"] is False


def test_start_cli_launches_console_and_chrome_debug(monkeypatch, tmp_path: Path) -> None:
    web_called: dict[str, object] = {}
    chrome_called: dict[str, object] = {}
    sync_called: list[bool] = []

    def fake_run_web_console(*, workspace, host, port, open_browser, open_path):
        web_called.update({
            "workspace": workspace,
            "host": host,
            "port": port,
            "open_browser": open_browser,
            "open_path": open_path,
        })

    def fake_launch_chrome_debug(*, port, start_url, profile_dir=None):
        _ = profile_dir
        chrome_called.update({"port": port, "start_url": start_url})

    monkeypatch.setattr("findmyjob.cli.main.sync_frontend_bundle", lambda: sync_called.append(True))
    monkeypatch.setattr("findmyjob.web.app.run_web_console", fake_run_web_console)
    monkeypatch.setattr("findmyjob.apply.cdp_session.launch_chrome_debug", fake_launch_chrome_debug)

    result = runner.invoke(app, ["start", "--workspace", str(tmp_path), "--page", "training", "--chrome-debug"])

    assert result.exit_code == 0, result.output
    assert sync_called == [True]
    assert web_called == {
        "workspace": tmp_path,
        "host": "127.0.0.1",
        "port": 8765,
        "open_browser": True,
        "open_path": "/training",
    }
    assert chrome_called == {
        "port": 9222,
        "start_url": "https://my.greenhouse.io/jobs",
    }


def test_day_cli_json_requires_existing_workspace(tmp_path: Path) -> None:
    result = runner.invoke(app, ["day", "--workspace", str(tmp_path), "--no-open", "--json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["entrypoint"] == "day"
    assert payload["issues"][0]["key"] == "workspace.config"
    assert payload["next_steps"][0].startswith("Run `fmj start`")


def test_day_cli_json_summarizes_existing_workspace(tmp_path: Path) -> None:
    config_path = workspace_config_file(tmp_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    write_default_workspace_config(config_path)
    ensure_default_workspace_templates(tmp_path)
    AppRuntime.bootstrap(tmp_path)

    result = runner.invoke(app, ["day", "--workspace", str(tmp_path), "--no-open", "--json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["entrypoint"] == "day"
    assert payload["web_console"]["page"] == "daily"
    assert payload["daily"] is not None
    assert payload["daily"]["counts"]["new_matching"] == 0
    assert payload["review"] is not None
    assert payload["review"]["counts"]["ready_for_review"] == 0


def test_web_cli_syncs_frontend_before_launch(monkeypatch, tmp_path: Path) -> None:
    called: dict[str, object] = {}
    sync_called: list[bool] = []

    def fake_run_web_console(*, workspace, host, port, open_browser, open_path):
        called.update({'workspace': workspace, 'host': host, 'port': port, 'open_browser': open_browser, 'open_path': open_path})

    monkeypatch.setattr('findmyjob.cli.main.sync_frontend_bundle', lambda: sync_called.append(True))
    monkeypatch.setattr('findmyjob.web.app.run_web_console', fake_run_web_console)

    result = runner.invoke(app, ['web', '--host', '127.0.0.1', '--port', '9000', '--no-open', '--workspace', str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert sync_called == [True]
    assert called['port'] == 9000
    assert called['open_browser'] is False
