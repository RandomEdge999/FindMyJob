"""Add personal triage and suppression tables."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0007_personal_triage"
down_revision = "0006_saved_searches"
branch_labels = None
depends_on = None


TRIAGE_STATUS = sa.Enum(
    "new",
    "shortlisted",
    "dismissed",
    "archived",
    "watching",
    name="personaltriagestatus",
)
SUPPRESSION_SCOPE = sa.Enum(
    "job",
    "company_title",
    "company",
    name="personalsuppressionscope",
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "personal_job_triage" not in tables:
        op.create_table(
            "personal_job_triage",
            sa.Column("id", sa.String(length=32), primary_key=True, nullable=False),
            sa.Column("job_posting_id", sa.String(length=32), sa.ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False),
            sa.Column("status", TRIAGE_STATUS, nullable=False, server_default="new"),
            sa.Column("reason_code", sa.String(length=64), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_updated_by", sa.String(length=64), nullable=False, server_default="operator"),
            sa.UniqueConstraint("job_posting_id", name="uq_personal_job_triage_job"),
        )

    if "personal_suppression_rules" not in tables:
        op.create_table(
            "personal_suppression_rules",
            sa.Column("id", sa.String(length=32), primary_key=True, nullable=False),
            sa.Column("job_posting_id", sa.String(length=32), sa.ForeignKey("job_postings.id", ondelete="SET NULL"), nullable=True),
            sa.Column("scope", SUPPRESSION_SCOPE, nullable=False),
            sa.Column("company_normalized_name", sa.String(length=255), nullable=True),
            sa.Column("company_display_name", sa.String(length=255), nullable=True),
            sa.Column("title_key", sa.String(length=255), nullable=True),
            sa.Column("title_label", sa.String(length=255), nullable=True),
            sa.Column("reason_code", sa.String(length=64), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_by", sa.String(length=64), nullable=False, server_default="operator"),
            sa.Column("last_updated_by", sa.String(length=64), nullable=False, server_default="operator"),
        )

    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "personal_job_triage" in tables:
        indexes = {index["name"] for index in inspector.get_indexes("personal_job_triage")}
        if "ix_personal_job_triage_status" not in indexes:
            op.create_index("ix_personal_job_triage_status", "personal_job_triage", ["status"])
        if "ix_personal_job_triage_updated_at" not in indexes:
            op.create_index("ix_personal_job_triage_updated_at", "personal_job_triage", ["updated_at"])

    if "personal_suppression_rules" in tables:
        indexes = {index["name"] for index in inspector.get_indexes("personal_suppression_rules")}
        if "ix_personal_suppression_rules_active" not in indexes:
            op.create_index("ix_personal_suppression_rules_active", "personal_suppression_rules", ["active"])
        if "ix_personal_suppression_rules_scope_company_title" not in indexes:
            op.create_index(
                "ix_personal_suppression_rules_scope_company_title",
                "personal_suppression_rules",
                ["scope", "company_normalized_name", "title_key"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "personal_suppression_rules" in tables:
        indexes = {index["name"] for index in inspector.get_indexes("personal_suppression_rules")}
        if "ix_personal_suppression_rules_scope_company_title" in indexes:
            op.drop_index("ix_personal_suppression_rules_scope_company_title", table_name="personal_suppression_rules")
        if "ix_personal_suppression_rules_active" in indexes:
            op.drop_index("ix_personal_suppression_rules_active", table_name="personal_suppression_rules")
        op.drop_table("personal_suppression_rules")

    if "personal_job_triage" in tables:
        indexes = {index["name"] for index in inspector.get_indexes("personal_job_triage")}
        if "ix_personal_job_triage_updated_at" in indexes:
            op.drop_index("ix_personal_job_triage_updated_at", table_name="personal_job_triage")
        if "ix_personal_job_triage_status" in indexes:
            op.drop_index("ix_personal_job_triage_status", table_name="personal_job_triage")
        op.drop_table("personal_job_triage")
