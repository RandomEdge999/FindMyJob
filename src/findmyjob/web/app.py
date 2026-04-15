from __future__ import annotations

import socket
import threading
import webbrowser
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from findmyjob.filefirst.workspace import FileWorkspace
from findmyjob.web.routes.api import router as api_router
from findmyjob.web.routes.pages import router as pages_router

_WEB_ROOT = Path(__file__).resolve().parent
_FRONTEND_DIST = _WEB_ROOT / "frontend_dist"


def _normalize_open_path(value: str | None) -> str:
    path = str(value or "/").strip() or "/"
    if not path.startswith("/"):
        path = "/" + path
    return path


def _browser_host(host: str) -> str:
    return "127.0.0.1" if host in {"0.0.0.0", "::"} else host


def _assert_port_available(host: str, port: int) -> None:
    family = socket.AF_INET6 if ":" in host and host != "0.0.0.0" else socket.AF_INET
    bind_target: Any = (host, port, 0, 0) if family == socket.AF_INET6 else (host, port)
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(bind_target)
        except OSError as exc:
            raise RuntimeError(f"Web console port {port} is already in use on {host}. Stop the previous backend before starting a new run.") from exc

def create_app(workspace: Path | None = None) -> FastAPI:
    ws = FileWorkspace(Path.cwd() if workspace is None else workspace)
    ws.ensure()
    app = FastAPI(title="Find My Job Operator Console", version="3.0", docs_url=None, redoc_url=None)
    app.state.workspace = ws.root
    app.state.file_workspace = ws
    app.state.frontend_dist = _FRONTEND_DIST
    assets_dir = _FRONTEND_DIST / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="frontend-assets")
    static_dir = _WEB_ROOT / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    app.include_router(api_router, prefix="/api")
    app.include_router(pages_router)
    return app


def run_web_console(
    *,
    workspace: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    open_path: str = "/",
) -> None:
    _assert_port_available(host, port)
    app = create_app(workspace)
    if open_browser:
        url = f"http://{_browser_host(host)}:{port}{_normalize_open_path(open_path)}"
        timer = threading.Timer(0.6, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    uvicorn.run(app, host=host, port=port, log_level="info")
