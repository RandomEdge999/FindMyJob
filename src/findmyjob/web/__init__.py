from __future__ import annotations

from pathlib import Path


def create_app(workspace: Path | None = None):
	from findmyjob.web.app import create_app as _create_app

	return _create_app(workspace)


def run_web_console(
	*,
	workspace: Path | None = None,
	host: str = "127.0.0.1",
	port: int = 8765,
	open_browser: bool = True,
	open_path: str = "/",
) -> None:
	from findmyjob.web.app import run_web_console as _run_web_console

	_run_web_console(
		workspace=workspace,
		host=host,
		port=port,
		open_browser=open_browser,
		open_path=open_path,
	)

__all__ = ["create_app", "run_web_console"]
