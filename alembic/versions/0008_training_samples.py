"""Add durable Greenhouse training samples."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0008_training_samples"
down_revision = "0007_personal_triage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "training_samples" not in tables:
        op.create_table(
            "training_samples",
            sa.Column("id", sa.String(length=32), primary_key=True, nullable=False),
            sa.Column("run_id", sa.String(length=32), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("jobs_page_url", sa.Text(), nullable=False),
            sa.Column("view_page_url", sa.Text(), nullable=False),
            sa.Column("company_page_url", sa.Text(), nullable=True),
            sa.Column("apply_page_url", sa.Text(), nullable=True),
            sa.Column("job_title", sa.String(length=255), nullable=True),
            sa.Column("company_name", sa.String(length=255), nullable=True),
            sa.Column("location", sa.String(length=255), nullable=True),
            sa.Column("posted_text", sa.String(length=255), nullable=True),
            sa.Column("description_excerpt", sa.Text(), nullable=True),
            sa.Column("extracted_form_fields", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("screenshot_paths", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("dom_snapshot_paths", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("page_captures", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("layout_notes", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("artifact_paths", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("draft_change_summary", sa.Text(), nullable=True),
            sa.Column("review_status", sa.String(length=32), nullable=False, server_default="pending"),
            sa.Column("review_reason_code", sa.String(length=64), nullable=True),
            sa.Column("review_note", sa.Text(), nullable=True),
            sa.Column("feedback_summary", sa.Text(), nullable=True),
            sa.Column("promoted_job_id", sa.String(length=32), sa.ForeignKey("job_postings.id", ondelete="SET NULL"), nullable=True),
            sa.Column("promoted_application_id", sa.String(length=32), sa.ForeignKey("applications.id", ondelete="SET NULL"), nullable=True),
            sa.Column("review_packet_path", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes("training_samples")} if "training_samples" in set(inspector.get_table_names()) else set()
    if "ix_training_samples_run_id" not in indexes:
        op.create_index("ix_training_samples_run_id", "training_samples", ["run_id"])
    if "ix_training_samples_review_status" not in indexes:
        op.create_index("ix_training_samples_review_status", "training_samples", ["review_status", "updated_at"])
    if "ix_training_samples_promoted_application" not in indexes:
        op.create_index("ix_training_samples_promoted_application", "training_samples", ["promoted_application_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "training_samples" not in tables:
        return

    indexes = {index["name"] for index in inspector.get_indexes("training_samples")}
    if "ix_training_samples_promoted_application" in indexes:
        op.drop_index("ix_training_samples_promoted_application", table_name="training_samples")
    if "ix_training_samples_review_status" in indexes:
        op.drop_index("ix_training_samples_review_status", table_name="training_samples")
    if "ix_training_samples_run_id" in indexes:
        op.drop_index("ix_training_samples_run_id", table_name="training_samples")
    op.drop_table("training_samples")
