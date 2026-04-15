"""Add advanced job metadata and company size hooks."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from findmyjob.db.session import ensure_sqlite_fts

revision = "0005_advanced_job_metadata"
down_revision = "0004_sqlite_fts"
branch_labels = None
depends_on = None


JOB_COLUMNS = {
    "country_code": sa.Column("country_code", sa.String(length=8), nullable=True),
    "region_code": sa.Column("region_code", sa.String(length=16), nullable=True),
    "city": sa.Column("city", sa.String(length=255), nullable=True),
    "location_scope": sa.Column("location_scope", sa.String(length=32), nullable=False, server_default="unknown"),
    "experience_level": sa.Column("experience_level", sa.String(length=32), nullable=False, server_default="unknown"),
    "compensation_min": sa.Column("compensation_min", sa.Integer(), nullable=True),
    "compensation_max": sa.Column("compensation_max", sa.Integer(), nullable=True),
    "compensation_currency": sa.Column("compensation_currency", sa.String(length=16), nullable=True),
    "compensation_interval": sa.Column("compensation_interval", sa.String(length=32), nullable=True),
    "remote_country_codes": sa.Column("remote_country_codes", sa.JSON(), nullable=True),
    "metadata_quality": sa.Column("metadata_quality", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    "posted_at": sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
}

COMPANY_COLUMNS = {
    "employee_count_min": sa.Column("employee_count_min", sa.Integer(), nullable=True),
    "employee_count_max": sa.Column("employee_count_max", sa.Integer(), nullable=True),
    "company_size_bucket": sa.Column("company_size_bucket", sa.String(length=32), nullable=False, server_default="unknown"),
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    company_columns = {column["name"] for column in inspector.get_columns("companies")}
    with op.batch_alter_table("companies") as batch_op:
        for name, column in COMPANY_COLUMNS.items():
            if name not in company_columns:
                batch_op.add_column(column)

    job_columns = {column["name"] for column in inspector.get_columns("job_postings")}
    with op.batch_alter_table("job_postings") as batch_op:
        for name, column in JOB_COLUMNS.items():
            if name not in job_columns:
                batch_op.add_column(column)

    inspector = sa.inspect(bind)
    company_indexes = {index["name"] for index in inspector.get_indexes("companies")}
    if "ix_company_size_bucket" not in company_indexes:
        op.create_index("ix_company_size_bucket", "companies", ["company_size_bucket"])

    job_indexes = {index["name"] for index in inspector.get_indexes("job_postings")}
    if "ix_job_country_region_city" not in job_indexes:
        op.create_index("ix_job_country_region_city", "job_postings", ["country_code", "region_code", "city"])
    if "ix_job_location_scope" not in job_indexes:
        op.create_index("ix_job_location_scope", "job_postings", ["location_scope"])
    if "ix_job_experience_level" not in job_indexes:
        op.create_index("ix_job_experience_level", "job_postings", ["experience_level"])
    if "ix_job_posted_at" not in job_indexes:
        op.create_index("ix_job_posted_at", "job_postings", ["posted_at"])
    if "ix_job_compensation_floor" not in job_indexes:
        op.create_index("ix_job_compensation_floor", "job_postings", ["compensation_currency", "compensation_min"])

    ensure_sqlite_fts(bind)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    company_indexes = {index["name"] for index in inspector.get_indexes("companies")}
    if "ix_company_size_bucket" in company_indexes:
        op.drop_index("ix_company_size_bucket", table_name="companies")

    job_indexes = {index["name"] for index in inspector.get_indexes("job_postings")}
    for index_name in ("ix_job_compensation_floor", "ix_job_posted_at", "ix_job_experience_level", "ix_job_location_scope", "ix_job_country_region_city"):
        if index_name in job_indexes:
            op.drop_index(index_name, table_name="job_postings")

    company_columns = {column["name"] for column in inspector.get_columns("companies")}
    with op.batch_alter_table("companies") as batch_op:
        for name in reversed(list(COMPANY_COLUMNS)):
            if name in company_columns:
                batch_op.drop_column(name)

    job_columns = {column["name"] for column in inspector.get_columns("job_postings")}
    with op.batch_alter_table("job_postings") as batch_op:
        for name in reversed(list(JOB_COLUMNS)):
            if name in job_columns:
                batch_op.drop_column(name)

    ensure_sqlite_fts(bind)
