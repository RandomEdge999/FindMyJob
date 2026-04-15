from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Connection

from findmyjob.core.assets import materialize_alembic_assets


@contextmanager
def _quiet_alembic_logging():
    managed = [logging.getLogger('alembic'), logging.getLogger('alembic.runtime.migration')]
    previous_levels = [logger.level for logger in managed]
    for logger in managed:
        logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        for logger, level in zip(managed, previous_levels):
            logger.setLevel(level)



def database_url(database_path: Path) -> str:
    return f"sqlite:///{database_path.resolve().as_posix()}"



def alembic_config(database_path: Path, *, ini_path: Path, script_path: Path) -> Config:
    config = Config(str(ini_path))
    config.set_main_option('script_location', str(script_path))
    config.set_main_option('sqlalchemy.url', database_url(database_path))
    config.attributes['configure_logger'] = False
    return config



def _infer_legacy_revision(connection: Connection) -> str | None:
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    if not tables:
        return None
    if not {'companies', 'job_postings'}.issubset(tables):
        return None
    job_columns = {column['name'] for column in inspector.get_columns('job_postings')}
    question_columns = {column['name'] for column in inspector.get_columns('application_questions')} if 'application_questions' in tables else set()
    answer_columns = {column['name'] for column in inspector.get_columns('application_answers')} if 'application_answers' in tables else set()

    if {'board_registry', 'board_discovery_evidence'}.issubset(tables) and {'board_token', 'source_updated_at'}.issubset(job_columns):
        if 'saved_searches' in tables:
            return '0006_saved_searches'
        if 'job_postings_fts' in tables:
            return '0004_sqlite_fts'
        return '0003_greenhouse_scale'
    if {'widget_type', 'field_config'}.issubset(question_columns) and {'binding_payload'}.issubset(answer_columns):
        return '0002_submission_contracts'
    return '0001_initial'



def upgrade_database(database_path: Path, revision: str = 'head') -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with materialize_alembic_assets() as (ini_path, script_path):
        config = alembic_config(database_path, ini_path=ini_path, script_path=script_path)
        engine = create_engine(database_url(database_path), future=True)
        try:
            with _quiet_alembic_logging():
                inferred: str | None = None
                with engine.connect() as connection:
                    context = MigrationContext.configure(connection)
                    current = context.get_current_revision()
                    if current is None:
                        inferred = _infer_legacy_revision(connection)
                if inferred is not None:
                    command.stamp(config, inferred)
                command.upgrade(config, revision)
        finally:
            engine.dispose()



def current_revision(database_path: Path) -> str | None:
    if not database_path.exists():
        return None
    engine = create_engine(database_url(database_path), future=True)
    try:
        with _quiet_alembic_logging():
            with engine.connect() as connection:
                context = MigrationContext.configure(connection)
                return context.get_current_revision()
    finally:
        engine.dispose()
