"""Add Greenhouse scale schema objects."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003_greenhouse_scale"
down_revision = "0002_submission_contracts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "board_registry" not in tables:
        op.create_table(
            "board_registry",
            sa.Column("id", sa.String(length=32), primary_key=True, nullable=False),
            sa.Column("source_adapter", sa.String(length=64), nullable=False),
            sa.Column("board_token", sa.String(length=255), nullable=False),
            sa.Column("company_hint", sa.String(length=255), nullable=True),
            sa.Column("source_url", sa.Text(), nullable=True),
            sa.Column("board_url", sa.Text(), nullable=True),
            sa.Column("source_domain", sa.String(length=255), nullable=True),
            sa.Column("discovery_method", sa.String(length=64), nullable=False, server_default="manual"),
            sa.Column("validation_status", sa.String(length=32), nullable=False, server_default="unknown"),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("last_sync_status", sa.String(length=32), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("live_job_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("notes", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("source_adapter", "board_token", name="uq_board_registry_source_token"),
        )
        op.create_index("ix_board_registry_active", "board_registry", ["source_adapter", "active"])

    if "board_discovery_evidence" not in tables:
        op.create_table(
            "board_discovery_evidence",
            sa.Column("id", sa.String(length=32), primary_key=True, nullable=False),
            sa.Column("board_registry_id", sa.String(length=32), sa.ForeignKey("board_registry.id", ondelete="CASCADE"), nullable=False),
            sa.Column("source_adapter", sa.String(length=64), nullable=False),
            sa.Column("source_url", sa.Text(), nullable=False),
            sa.Column("discovery_method", sa.String(length=64), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_board_discovery_board_id", "board_discovery_evidence", ["board_registry_id"])

    job_columns = {column["name"] for column in inspector.get_columns("job_postings")}
    with op.batch_alter_table("job_postings") as batch_op:
        if "board_token" not in job_columns:
            batch_op.add_column(sa.Column("board_token", sa.String(length=255), nullable=True))
        if "source_updated_at" not in job_columns:
            batch_op.add_column(sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True))

    existing_indexes = {index["name"] for index in inspector.get_indexes("job_postings")}
    if "ix_job_board_token" not in existing_indexes:
        op.create_index("ix_job_board_token", "job_postings", ["source_adapter", "board_token"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    job_columns = {column["name"] for column in inspector.get_columns("job_postings")}
    existing_indexes = {index["name"] for index in inspector.get_indexes("job_postings")}

    if "ix_job_board_token" in existing_indexes:
        op.drop_index("ix_job_board_token", table_name="job_postings")

    with op.batch_alter_table("job_postings") as batch_op:
        if "source_updated_at" in job_columns:
            batch_op.drop_column("source_updated_at")
        if "board_token" in job_columns:
            batch_op.drop_column("board_token")

    if "board_discovery_evidence" in tables:
        op.drop_index("ix_board_discovery_board_id", table_name="board_discovery_evidence")
        op.drop_table("board_discovery_evidence")
    if "board_registry" in tables:
        op.drop_index("ix_board_registry_active", table_name="board_registry")
        op.drop_table("board_registry")
