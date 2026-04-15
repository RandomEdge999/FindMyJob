from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

router = APIRouter()


_SPA_ROUTES = ["/", "/setup", "/settings", "/autopilot", "/daily", "/review", "/runs", "/training"]


def _frontend_index(request: Request) -> Path:
    frontend_dist = Path(getattr(request.app.state, "frontend_dist", Path(".")))
    return frontend_dist / "index.html"


@router.get("/", response_class=HTMLResponse)
@router.get("/setup", response_class=HTMLResponse)
@router.get("/autopilot", response_class=HTMLResponse)
@router.get("/daily", response_class=HTMLResponse)
@router.get("/review", response_class=HTMLResponse)
@router.get("/runs", response_class=HTMLResponse)
@router.get("/settings", response_class=HTMLResponse)
@router.get("/training", response_class=HTMLResponse)
def spa_shell(request: Request):
    index_path = _frontend_index(request)
    if index_path.exists():
        return FileResponse(index_path)
    return HTMLResponse("<html><body><h1>Find My Job Console</h1><p>Frontend build not found.</p></body></html>")


@router.get("/files/{relative_path:path}")
def workspace_file(request: Request, relative_path: str):
    workspace = Path(request.app.state.workspace)
    relative = Path(relative_path)
    resolved = (workspace / relative).resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="File is outside the workspace.") from exc
    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(resolved, filename=quote(resolved.name))


@router.get("/{full_path:path}", response_class=HTMLResponse)
def spa_catch_all(request: Request, full_path: str):
    """Catch-all route for unknown paths — serves the SPA shell."""
    _ = full_path
    index_path = _frontend_index(request)
    if index_path.exists():
        return FileResponse(index_path)
    return HTMLResponse("<html><body><h1>Find My Job Console</h1><p>Frontend build not found.</p></body></html>")
