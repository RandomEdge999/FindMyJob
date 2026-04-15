from __future__ import annotations

from pathlib import Path

from platformdirs import user_config_dir

APP_NAME = "findmyjob"


def global_config_dir() -> Path:
    return Path(user_config_dir(APP_NAME))


def global_config_file() -> Path:
    return global_config_dir() / "config.toml"


def workspace_root(start: Path | None = None) -> Path:
    return (start or Path.cwd()).resolve()


def workspace_dir(start: Path | None = None) -> Path:
    return workspace_root(start) / ".fmj"


def workspace_config_file(start: Path | None = None) -> Path:
    return workspace_dir(start) / "config.toml"


def workspace_database_file(start: Path | None = None) -> Path:
    return workspace_dir(start) / "findmyjob.db"


def workspace_artifacts_dir(start: Path | None = None) -> Path:
    return workspace_dir(start) / "artifacts"


def workspace_exports_dir(start: Path | None = None) -> Path:
    return workspace_dir(start) / "exports"


def workspace_snapshots_dir(start: Path | None = None) -> Path:
    return workspace_dir(start) / "snapshots"


def ensure_workspace(start: Path | None = None) -> Path:
    base = workspace_dir(start)
    for path in (
        base,
        workspace_artifacts_dir(start),
        workspace_exports_dir(start),
        workspace_snapshots_dir(start),
    ):
        path.mkdir(parents=True, exist_ok=True)
    return base
