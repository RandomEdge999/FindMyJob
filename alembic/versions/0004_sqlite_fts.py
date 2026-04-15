"""Create SQLite FTS objects for job search."""

from __future__ import annotations

from alembic import op

from findmyjob.db.session import ensure_sqlite_fts

revision = "0004_sqlite_fts"
down_revision = "0003_greenhouse_scale"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ensure_sqlite_fts(op.get_bind())


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP TRIGGER IF EXISTS job_postings_au")
    bind.exec_driver_sql("DROP TRIGGER IF EXISTS job_postings_ad")
    bind.exec_driver_sql("DROP TRIGGER IF EXISTS job_postings_ai")
    bind.exec_driver_sql("DROP TABLE IF EXISTS job_postings_fts")
