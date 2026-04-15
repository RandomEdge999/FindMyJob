"""Add saved searches persistence."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006_saved_searches"
down_revision = "0005_advanced_job_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "saved_searches" not in tables:
        op.create_table(
            "saved_searches",
            sa.Column("id", sa.String(length=32), primary_key=True, nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("query_payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("source_adapter_hint", sa.String(length=64), nullable=True),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("name", name="uq_saved_search_name"),
        )

    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes("saved_searches")} if "saved_searches" in set(inspector.get_table_names()) else set()
    if "ix_saved_search_default" not in indexes:
        op.create_index("ix_saved_search_default", "saved_searches", ["is_default"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "saved_searches" not in tables:
        return

    indexes = {index["name"] for index in inspector.get_indexes("saved_searches")}
    if "ix_saved_search_default" in indexes:
        op.drop_index("ix_saved_search_default", table_name="saved_searches")
    op.drop_table("saved_searches")


