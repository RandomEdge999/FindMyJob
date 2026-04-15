from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import as_file, files
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEMPLATE_FILES = ("resume.typ", "cover_letter.typ")


def ensure_default_workspace_templates(workspace: Path) -> list[Path]:
    root = workspace.resolve()
    target_dir = root / "templates" / "typst"
    target_dir.mkdir(parents=True, exist_ok=True)

    created: list[Path] = []
    with materialize_default_template_dir() as source_dir:
        for filename in _TEMPLATE_FILES:
            source = source_dir / filename
            if not source.is_file():
                raise FileNotFoundError(f"Missing bundled template: {source}")
            destination = target_dir / filename
            if destination.exists():
                continue
            destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            created.append(destination)
    return created


@contextmanager
def materialize_default_template_dir() -> Iterator[Path]:
    resource_dir = files("findmyjob").joinpath("_bundled", "templates", "typst")
    if resource_dir.is_dir():
        with as_file(resource_dir) as path:
            yield Path(path)
            return

    fallback = _REPO_ROOT / "templates" / "typst"
    if fallback.is_dir():
        yield fallback
        return
    raise FileNotFoundError("Bundled template directory is not available.")


@contextmanager
def materialize_alembic_assets() -> Iterator[tuple[Path, Path]]:
    resource_ini = files("findmyjob").joinpath("_bundled", "alembic.ini")
    resource_dir = files("findmyjob").joinpath("_bundled", "alembic")
    if resource_ini.is_file() and resource_dir.is_dir():
        with as_file(resource_ini) as ini_path, as_file(resource_dir) as script_dir:
            yield Path(ini_path), Path(script_dir)
            return

    fallback_ini = _REPO_ROOT / "alembic.ini"
    fallback_dir = _REPO_ROOT / "alembic"
    if fallback_ini.is_file() and fallback_dir.is_dir():
        yield fallback_ini, fallback_dir
        return
    raise FileNotFoundError("Bundled Alembic assets are not available.")