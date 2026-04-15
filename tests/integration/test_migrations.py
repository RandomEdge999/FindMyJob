from pathlib import Path

from sqlalchemy import inspect, text

from findmyjob.db.migrations import current_revision, upgrade_database
from findmyjob.db.session import create_sqlite_engine


HEAD_REVISION = "0009_answer_confidence"



def test_upgrade_database_builds_head_schema_and_fts(tmp_path: Path) -> None:
    database_path = tmp_path / "migrated.db"
    upgrade_database(database_path)

    assert current_revision(database_path) == HEAD_REVISION

    engine = create_sqlite_engine(database_path)
    with engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        assert {"companies", "job_postings", "board_registry", "board_discovery_evidence", "saved_searches", "personal_job_triage", "personal_suppression_rules"}.issubset(tables)
        job_columns = {column["name"] for column in inspector.get_columns("job_postings")}
        company_columns = {column["name"] for column in inspector.get_columns("companies")}
        saved_search_columns = {column["name"] for column in inspector.get_columns("saved_searches")}
        triage_columns = {column["name"] for column in inspector.get_columns("personal_job_triage")}
        suppression_columns = {column["name"] for column in inspector.get_columns("personal_suppression_rules")}
        assert {
            "country_code",
            "region_code",
            "city",
            "location_scope",
            "experience_level",
            "posted_at",
            "compensation_min",
            "compensation_max",
            "compensation_currency",
            "compensation_interval",
            "remote_country_codes",
            "metadata_quality",
        }.issubset(job_columns)
        assert {"employee_count_min", "employee_count_max", "company_size_bucket"}.issubset(company_columns)
        assert {"name", "query_payload", "source_adapter_hint", "is_default", "last_used_at"}.issubset(saved_search_columns)
        assert {"job_posting_id", "status", "reason_code", "note", "last_updated_by"}.issubset(triage_columns)
        assert {"scope", "company_normalized_name", "title_key", "active", "last_updated_by"}.issubset(suppression_columns)
        trigger_names = {row[0] for row in connection.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))}
        assert {"job_postings_ai", "job_postings_au", "job_postings_ad"}.issubset(trigger_names)
        fts_tables = {row[0] for row in connection.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        assert "job_postings_fts" in fts_tables



def test_upgrade_database_preserves_revision_chain_from_initial(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    upgrade_database(database_path, "0001_initial")
    assert current_revision(database_path) == "0001_initial"

    engine = create_sqlite_engine(database_path)
    with engine.connect() as connection:
        inspector = inspect(connection)
        assert "board_registry" not in set(inspector.get_table_names())
        job_columns = {column["name"] for column in inspector.get_columns("job_postings")}
        assert "board_token" not in job_columns

    upgrade_database(database_path)
    assert current_revision(database_path) == HEAD_REVISION

    engine = create_sqlite_engine(database_path)
    with engine.connect() as connection:
        inspector = inspect(connection)
        job_columns = {column["name"] for column in inspector.get_columns("job_postings")}
        assert {"board_token", "source_updated_at", "experience_level", "posted_at", "country_code"}.issubset(job_columns)
        assert "saved_searches" in set(inspector.get_table_names())



def test_upgrade_database_stamps_legacy_unversioned_runtime_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy_runtime.db"
    upgrade_database(database_path, "0004_sqlite_fts")

    engine = create_sqlite_engine(database_path)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE alembic_version"))

    upgrade_database(database_path)
    assert current_revision(database_path) == HEAD_REVISION

