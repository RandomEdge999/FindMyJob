from pathlib import Path

from sqlalchemy import select

from findmyjob.core.runtime import AppRuntime
from findmyjob.db.models import Company


def test_runtime_bootstrap_creates_database(tmp_path: Path) -> None:
    runtime = AppRuntime.bootstrap(tmp_path)
    assert runtime.config.database_path(tmp_path).exists()
    with runtime.session_scope() as session:
        rows = session.scalars(select(Company)).all()
    assert rows == []
