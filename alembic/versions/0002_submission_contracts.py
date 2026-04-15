"""Add submission contract fields."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_submission_contracts"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    question_columns = {column["name"] for column in inspector.get_columns("application_questions")}
    answer_columns = {column["name"] for column in inspector.get_columns("application_answers")}

    with op.batch_alter_table("application_questions") as batch_op:
        if "widget_type" not in question_columns:
            batch_op.add_column(sa.Column("widget_type", sa.String(length=64), nullable=True))
        if "field_config" not in question_columns:
            batch_op.add_column(sa.Column("field_config", sa.JSON(), nullable=True))
    with op.batch_alter_table("application_answers") as batch_op:
        if "binding_payload" not in answer_columns:
            batch_op.add_column(sa.Column("binding_payload", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    question_columns = {column["name"] for column in inspector.get_columns("application_questions")}
    answer_columns = {column["name"] for column in inspector.get_columns("application_answers")}

    with op.batch_alter_table("application_answers") as batch_op:
        if "binding_payload" in answer_columns:
            batch_op.drop_column("binding_payload")
    with op.batch_alter_table("application_questions") as batch_op:
        if "field_config" in question_columns:
            batch_op.drop_column("field_config")
        if "widget_type" in question_columns:
            batch_op.drop_column("widget_type")