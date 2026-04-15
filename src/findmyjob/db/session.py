from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
import sqlite3
from typing import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from findmyjob.db.base import Base

sqlite3.register_adapter(datetime, lambda val: val.isoformat())
sqlite3.register_adapter(date, lambda val: val.isoformat())
sqlite3.register_converter("timestamp", lambda val: datetime.fromisoformat(val.decode()))


def create_sqlite_engine(database_path: Path) -> Engine:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite+pysqlite:///{database_path}",
        future=True,
        connect_args={"timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA busy_timeout=15000;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.close()

    return engine


FTS_CREATE_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS job_postings_fts USING fts5(
    job_posting_id UNINDEXED,
    company,
    title,
    normalized_description,
    location,
    source_adapter,
    board_token,
    lifecycle_status
)
"""

FTS_TRIGGER_INSERT_SQL = """
CREATE TRIGGER IF NOT EXISTS job_postings_ai AFTER INSERT ON job_postings BEGIN
    INSERT INTO job_postings_fts (
        rowid,
        job_posting_id,
        company,
        title,
        normalized_description,
        location,
        source_adapter,
        board_token,
        lifecycle_status
    ) VALUES (
        new.rowid,
        new.id,
        COALESCE((SELECT display_name FROM companies WHERE id = new.company_id), ''),
        COALESCE(new.title, ''),
        COALESCE(new.normalized_description, ''),
        TRIM(COALESCE(new.location_normalized, '') || ' ' || COALESCE(new.city, '') || ' ' || COALESCE(new.region_code, '') || ' ' || COALESCE(new.country_code, '') || ' ' || COALESCE(new.location_raw, '')),
        COALESCE(new.source_adapter, ''),
        COALESCE(new.board_token, ''),
        COALESCE(new.lifecycle_status, '')
    );
END
"""

FTS_TRIGGER_DELETE_SQL = """
CREATE TRIGGER IF NOT EXISTS job_postings_ad AFTER DELETE ON job_postings BEGIN
    DELETE FROM job_postings_fts WHERE rowid = old.rowid;
END
"""

FTS_TRIGGER_UPDATE_SQL = """
CREATE TRIGGER IF NOT EXISTS job_postings_au AFTER UPDATE ON job_postings BEGIN
    DELETE FROM job_postings_fts WHERE rowid = old.rowid;
    INSERT INTO job_postings_fts (
        rowid,
        job_posting_id,
        company,
        title,
        normalized_description,
        location,
        source_adapter,
        board_token,
        lifecycle_status
    ) VALUES (
        new.rowid,
        new.id,
        COALESCE((SELECT display_name FROM companies WHERE id = new.company_id), ''),
        COALESCE(new.title, ''),
        COALESCE(new.normalized_description, ''),
        TRIM(COALESCE(new.location_normalized, '') || ' ' || COALESCE(new.city, '') || ' ' || COALESCE(new.region_code, '') || ' ' || COALESCE(new.country_code, '') || ' ' || COALESCE(new.location_raw, '')),
        COALESCE(new.source_adapter, ''),
        COALESCE(new.board_token, ''),
        COALESCE(new.lifecycle_status, '')
    );
END
"""

FTS_REBUILD_SQL = """
INSERT INTO job_postings_fts (
    rowid,
    job_posting_id,
    company,
    title,
    normalized_description,
    location,
    source_adapter,
    board_token,
    lifecycle_status
)
SELECT
    job_postings.rowid,
    job_postings.id,
    COALESCE(companies.display_name, ''),
    COALESCE(job_postings.title, ''),
    COALESCE(job_postings.normalized_description, ''),
    TRIM(COALESCE(job_postings.location_normalized, '') || ' ' || COALESCE(job_postings.city, '') || ' ' || COALESCE(job_postings.region_code, '') || ' ' || COALESCE(job_postings.country_code, '') || ' ' || COALESCE(job_postings.location_raw, '')),
    COALESCE(job_postings.source_adapter, ''),
    COALESCE(job_postings.board_token, ''),
    COALESCE(job_postings.lifecycle_status, '')
FROM job_postings
JOIN companies ON companies.id = job_postings.company_id
"""


def ensure_sqlite_fts(connection: Connection) -> None:
    connection.execute(text(FTS_CREATE_SQL))
    connection.execute(text(FTS_TRIGGER_INSERT_SQL))
    connection.execute(text(FTS_TRIGGER_DELETE_SQL))
    connection.execute(text(FTS_TRIGGER_UPDATE_SQL))
    fts_count = connection.execute(text("SELECT count(*) FROM job_postings_fts")).scalar_one()
    job_count = connection.execute(text("SELECT count(*) FROM job_postings")).scalar_one()
    if fts_count != job_count:
        connection.execute(text("DELETE FROM job_postings_fts"))
        connection.execute(text(FTS_REBUILD_SQL))
    connection.execute(text("PRAGMA optimize;"))


def init_database(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        ensure_sqlite_fts(conn)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
