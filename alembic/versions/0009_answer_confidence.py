"""Add confidence column to application_answers for submit/review gating."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0009_answer_confidence"
down_revision = "0008_training_samples"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("application_answers")}
    if "confidence" not in columns:
        op.add_column("application_answers", sa.Column("confidence", sa.Float, server_default="0.0", nullable=False))
    if "confidence_reason" not in columns:
        op.add_column("application_answers", sa.Column("confidence_reason", sa.Text, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("application_answers")}
    if "confidence_reason" in columns:
        op.drop_column("application_answers", "confidence_reason")
    if "confidence" in columns:
        op.drop_column("application_answers", "confidence")
