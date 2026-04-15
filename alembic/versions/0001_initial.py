"""Initial schema baseline."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("id", sa.String(length=32), primary_key=True, nullable=False),
        sa.Column("normalized_name", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("domains", sa.JSON(), nullable=False),
        sa.Column("ats_hosts", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("normalized_name", name="uq_companies_normalized_name"),
    )

    op.create_table(
        "job_postings",
        sa.Column("id", sa.String(length=32), primary_key=True, nullable=False),
        sa.Column("company_id", sa.String(length=32), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("source_adapter", sa.String(length=64), nullable=False),
        sa.Column("source_kind", sa.String(length=64), nullable=False),
        sa.Column("source_job_id", sa.String(length=255), nullable=False),
        sa.Column("posting_url", sa.Text(), nullable=False),
        sa.Column("apply_url", sa.Text(), nullable=True),
        sa.Column("location_raw", sa.String(length=255), nullable=True),
        sa.Column("location_normalized", sa.String(length=255), nullable=True),
        sa.Column("workplace_type", sa.String(length=32), nullable=False),
        sa.Column("employment_type", sa.String(length=64), nullable=True),
        sa.Column("compensation", sa.JSON(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("normalized_description", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("job_identity_key", sa.String(length=64), nullable=False),
        sa.Column("duplicate_cluster_key", sa.String(length=64), nullable=False),
        sa.Column("lifecycle_status", sa.String(length=32), nullable=False),
        sa.Column("notes", sa.JSON(), nullable=False),
        sa.UniqueConstraint("source_adapter", "source_job_id", name="uq_job_source_id"),
    )
    op.create_index("ix_job_identity_key", "job_postings", ["job_identity_key"])
    op.create_index("ix_duplicate_cluster_key", "job_postings", ["duplicate_cluster_key"])

    op.create_table(
        "job_raw_records",
        sa.Column("id", sa.String(length=32), primary_key=True, nullable=False),
        sa.Column("job_posting_id", sa.String(length=32), sa.ForeignKey("job_postings.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_adapter", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("html_snapshot", sa.Text(), nullable=True),
    )

    op.create_table(
        "qualification_results",
        sa.Column("id", sa.String(length=32), primary_key=True, nullable=False),
        sa.Column("job_posting_id", sa.String(length=32), sa.ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("rule_version", sa.String(length=64), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("sponsorship_current", sa.String(length=32), nullable=False),
        sa.Column("sponsorship_future", sa.String(length=32), nullable=False),
        sa.Column("cpt_support", sa.String(length=32), nullable=False),
        sa.Column("opt_support", sa.String(length=32), nullable=False),
        sa.Column("fit", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("job_posting_id", name="uq_qualification_results_job_posting_id"),
    )

    op.create_table(
        "profile_facts",
        sa.Column("id", sa.String(length=32), primary_key=True, nullable=False),
        sa.Column("fact_id", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("sensitivity", sa.String(length=32), nullable=False),
        sa.Column("allowed_for_generation", sa.Boolean(), nullable=False),
        sa.Column("disallowed", sa.Boolean(), nullable=False),
        sa.Column("provenance", sa.String(length=64), nullable=False),
        sa.Column("confirmed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("fact_id", name="uq_profile_fact_id"),
    )

    op.create_table(
        "templates",
        sa.Column("id", sa.String(length=32), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("renderer", sa.String(length=64), nullable=False),
        sa.Column("validation_settings", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", "version", name="uq_template_version"),
    )

    op.create_table(
        "applications",
        sa.Column("id", sa.String(length=32), primary_key=True, nullable=False),
        sa.Column("job_posting_id", sa.String(length=32), sa.ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("review_status", sa.String(length=32), nullable=False),
        sa.Column("review_flags", sa.JSON(), nullable=False),
        sa.Column("handoff_reason", sa.Text(), nullable=True),
        sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("job_posting_id", name="uq_application_job"),
    )

    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(length=32), primary_key=True, nullable=False),
        sa.Column("job_posting_id", sa.String(length=32), sa.ForeignKey("job_postings.id", ondelete="SET NULL"), nullable=True),
        sa.Column("application_id", sa.String(length=32), sa.ForeignKey("applications.id", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("template_version", sa.String(length=64), nullable=True),
        sa.Column("fact_set_hash", sa.String(length=64), nullable=True),
        sa.Column("validation_results", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "application_questions",
        sa.Column("id", sa.String(length=32), primary_key=True, nullable=False),
        sa.Column("application_id", sa.String(length=32), sa.ForeignKey("applications.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_field_name", sa.String(length=255), nullable=True),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        sa.Column("normalized_key", sa.String(length=255), nullable=True),
        sa.Column("question_type", sa.String(length=32), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("source_snapshot_ref", sa.Text(), nullable=True),
    )

    op.create_table(
        "application_answers",
        sa.Column("id", sa.String(length=32), primary_key=True, nullable=False),
        sa.Column("question_id", sa.String(length=32), sa.ForeignKey("application_questions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("candidate_answer", sa.Text(), nullable=True),
        sa.Column("provenance", sa.String(length=64), nullable=False),
        sa.Column("grounded_fact_ids", sa.JSON(), nullable=False),
        sa.Column("answer_source", sa.String(length=64), nullable=False),
        sa.Column("verification_status", sa.String(length=32), nullable=False),
        sa.Column("needs_user_input", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "answer_memory",
        sa.Column("id", sa.String(length=32), primary_key=True, nullable=False),
        sa.Column("canonical_question", sa.Text(), nullable=False),
        sa.Column("context_constraints", sa.JSON(), nullable=False),
        sa.Column("answer_text", sa.Text(), nullable=False),
        sa.Column("grounded_fact_ids", sa.JSON(), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "runs",
        sa.Column("id", sa.String(length=32), primary_key=True, nullable=False),
        sa.Column("run_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("checkpoint_state", sa.JSON(), nullable=False),
        sa.Column("resume_token", sa.String(length=128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=32), primary_key=True, nullable=False),
        sa.Column("run_id", sa.String(length=32), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checkpoint_state", sa.JSON(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_task_status", "tasks", ["status"])

    op.create_table(
        "source_cursors",
        sa.Column("id", sa.String(length=32), primary_key=True, nullable=False),
        sa.Column("source_adapter", sa.String(length=64), nullable=False),
        sa.Column("cursor_key", sa.String(length=255), nullable=False),
        sa.Column("cursor_value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_adapter", "cursor_key", name="uq_source_cursor"),
    )

    op.create_table(
        "submit_attempts",
        sa.Column("id", sa.String(length=32), primary_key=True, nullable=False),
        sa.Column("application_id", sa.String(length=32), sa.ForeignKey("applications.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("source_policy", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("snapshot_path", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=32), primary_key=True, nullable=False),
        sa.Column("run_id", sa.String(length=32), sa.ForeignKey("runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("task_id", sa.String(length=32), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=True),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("submit_attempts")
    op.drop_table("source_cursors")
    op.drop_index("ix_task_status", table_name="tasks")
    op.drop_table("tasks")
    op.drop_table("runs")
    op.drop_table("answer_memory")
    op.drop_table("application_answers")
    op.drop_table("application_questions")
    op.drop_table("artifacts")
    op.drop_table("applications")
    op.drop_table("templates")
    op.drop_table("profile_facts")
    op.drop_table("qualification_results")
    op.drop_table("job_raw_records")
    op.drop_index("ix_duplicate_cluster_key", table_name="job_postings")
    op.drop_index("ix_job_identity_key", table_name="job_postings")
    op.drop_table("job_postings")
    op.drop_table("companies")
