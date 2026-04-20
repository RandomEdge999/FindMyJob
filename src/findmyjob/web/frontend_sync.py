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


@dataclass(slots=True)
class FrontendBuildReadiness:
    status: str
    summary: str
    detail: str
    hint: str | None
    bundle_status: FrontendBundleStatus
    node_available: bool
    npm_available: bool


_REQUIRED_DIST_FILES = (
    "index.html",
)
_REQUIRED_DIST_GLOBS = (
    "assets/index*.js",
    "assets/index*.css",
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

    # Also check glob patterns (Vite outputs hashed filenames like index-AbCd1234.js)
    glob_outputs: list[Path] = []
    for pattern in _REQUIRED_DIST_GLOBS:
        matches = list(dist_dir.glob(pattern))
        if not matches:
            return FrontendBundleStatus(
                needs_build=True,
                reason="dist_missing",
                frontend_root=frontend_root,
                dist_dir=dist_dir,
            )
        glob_outputs.extend(matches)

    all_outputs = required_outputs + glob_outputs

    input_paths = _input_paths(frontend_root)
    if not input_paths:
        return FrontendBundleStatus(
            needs_build=False,
            reason="no_inputs",
            frontend_root=frontend_root,
            dist_dir=dist_dir,
        )

    latest_input_mtime = max(path.stat().st_mtime_ns for path in input_paths)
    newest_output_mtime = max(path.stat().st_mtime_ns for path in all_outputs)
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


def inspect_frontend_build_readiness(
    base_dir: Path | None = None,
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> FrontendBuildReadiness:
    bundle_status = frontend_bundle_status(base_dir)
    node_available = which("node") is not None
    npm_available = which("npm") is not None
    detail = (
        f"bundle={bundle_status.reason} :: "
        f"node={'ok' if node_available else 'missing'} :: "
        f"npm={'ok' if npm_available else 'missing'} :: "
        f"dist={bundle_status.dist_dir}"
    )

    if bundle_status.reason == "source_unavailable":
        return FrontendBuildReadiness(
            status="ok",
            summary="Frontend source tree is unavailable; using bundled frontend assets.",
            detail=detail,
            hint=None,
            bundle_status=bundle_status,
            node_available=node_available,
            npm_available=npm_available,
        )

    if not bundle_status.needs_build:
        return FrontendBuildReadiness(
            status="ok",
            summary="Frontend bundle is ready for launch.",
            detail=detail,
            hint=None,
            bundle_status=bundle_status,
            node_available=node_available,
            npm_available=npm_available,
        )

    if node_available and npm_available:
        return FrontendBuildReadiness(
            status="warning",
            summary="Frontend bundle is stale or missing, but the local toolchain can rebuild it.",
            detail=detail,
            hint="Run `fmj build` to refresh the local web frontend before launch.",
            bundle_status=bundle_status,
            node_available=node_available,
            npm_available=npm_available,
        )

    return FrontendBuildReadiness(
        status="blocked",
        summary="Frontend bundle is stale or missing, and Node.js/npm are unavailable.",
        detail=detail,
        hint="Install Node.js, then run `fmj build` or `npm --prefix frontend run build` before launching the web console.",
        bundle_status=bundle_status,
        node_available=node_available,
        npm_available=npm_available,
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
    "FrontendBuildReadiness",
    "FrontendBundleStatus",
    "FrontendSyncResult",
    "frontend_bundle_status",
    "inspect_frontend_build_readiness",
    "sync_frontend_bundle",
]
