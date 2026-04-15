from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(slots=True)
class FrontendSyncResult:
    checked: bool
    built: bool
    reason: str
    frontend_root: Path
    dist_dir: Path


@dataclass(slots=True)
class FrontendBundleStatus:
    needs_build: bool
    reason: str
    frontend_root: Path
    dist_dir: Path


_REQUIRED_DIST_FILES = (
    "index.html",
    "assets/index.js",
    "assets/index.css",
    "assets/runtime-fixes.js",
)
_OPTIONAL_INPUT_FILES = (
    "build.mjs",
    "package.json",
    "package-lock.json",
    "vite.config.js",
)


def _repo_root(base_dir: Path | None = None) -> Path:
    if base_dir is not None:
        return Path(base_dir).resolve()
    return Path(__file__).resolve().parents[3]


def _frontend_root(base_dir: Path | None = None) -> Path:
    return _repo_root(base_dir) / "frontend"


def _frontend_dist(base_dir: Path | None = None) -> Path:
    return _repo_root(base_dir) / "src" / "findmyjob" / "web" / "frontend_dist"


def _input_paths(frontend_root: Path) -> list[Path]:
    src_dir = frontend_root / "src"
    inputs: list[Path] = []
    if src_dir.exists():
        inputs.extend(path for path in src_dir.rglob("*") if path.is_file())
    for relative_path in _OPTIONAL_INPUT_FILES:
        candidate = frontend_root / relative_path
        if candidate.exists() and candidate.is_file():
            inputs.append(candidate)
    return inputs


def frontend_bundle_status(base_dir: Path | None = None) -> FrontendBundleStatus:
    frontend_root = _frontend_root(base_dir)
    dist_dir = _frontend_dist(base_dir)
    src_dir = frontend_root / "src"
    if not frontend_root.exists() or not src_dir.exists():
        return FrontendBundleStatus(
            needs_build=False,
            reason="source_unavailable",
            frontend_root=frontend_root,
            dist_dir=dist_dir,
        )

    required_outputs = [dist_dir / relative_path for relative_path in _REQUIRED_DIST_FILES]
    if any(not path.exists() or not path.is_file() for path in required_outputs):
        return FrontendBundleStatus(
            needs_build=True,
            reason="dist_missing",
            frontend_root=frontend_root,
            dist_dir=dist_dir,
        )

    input_paths = _input_paths(frontend_root)
    if not input_paths:
        return FrontendBundleStatus(
            needs_build=False,
            reason="no_inputs",
            frontend_root=frontend_root,
            dist_dir=dist_dir,
        )

    latest_input_mtime = max(path.stat().st_mtime_ns for path in input_paths)
    newest_output_mtime = max(path.stat().st_mtime_ns for path in required_outputs)
    if latest_input_mtime > newest_output_mtime:
        return FrontendBundleStatus(
            needs_build=True,
            reason="stale_dist",
            frontend_root=frontend_root,
            dist_dir=dist_dir,
        )

    return FrontendBundleStatus(
        needs_build=False,
        reason="fresh",
        frontend_root=frontend_root,
        dist_dir=dist_dir,
    )


def sync_frontend_bundle(
    base_dir: Path | None = None,
    *,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
) -> FrontendSyncResult:
    status = frontend_bundle_status(base_dir)
    if not status.needs_build:
        return FrontendSyncResult(
            checked=status.reason != "source_unavailable",
            built=False,
            reason=status.reason,
            frontend_root=status.frontend_root,
            dist_dir=status.dist_dir,
        )

    node_executable = which("node")
    npm_executable = which("npm")
    if not node_executable or not npm_executable:
        raise RuntimeError(
            "Frontend bundle is stale or missing, but Node.js/npm are unavailable. "
            "Install Node.js and rerun `npm --prefix frontend run build` before launching the web console."
        )

    node_modules_dir = status.frontend_root / "node_modules"
    if not node_modules_dir.exists():
        install_result = run_command(
            [npm_executable, "install"],
            cwd=status.frontend_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if install_result.returncode != 0:
            stderr = (install_result.stderr or install_result.stdout or "").strip()
            raise RuntimeError(f"Frontend dependency install failed: {stderr or 'npm install exited non-zero.'}")

    build_result = run_command(
        [npm_executable, "run", "build"],
        cwd=status.frontend_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if build_result.returncode != 0:
        stderr = (build_result.stderr or build_result.stdout or "").strip()
        raise RuntimeError(f"Frontend build failed: {stderr or 'npm run build exited non-zero.'}")

    post_build_status = frontend_bundle_status(base_dir)
    if post_build_status.needs_build:
        raise RuntimeError("Frontend build finished, but the expected frontend_dist artifacts are still missing or stale.")

    return FrontendSyncResult(
        checked=True,
        built=True,
        reason=status.reason,
        frontend_root=status.frontend_root,
        dist_dir=status.dist_dir,
    )


__all__ = [
    "FrontendBundleStatus",
    "FrontendSyncResult",
    "frontend_bundle_status",
    "sync_frontend_bundle",
]
