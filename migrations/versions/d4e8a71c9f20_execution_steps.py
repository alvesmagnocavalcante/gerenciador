"""RunTrail execution steps.

Revision ID: d4e8a71c9f20
Revises: c8d31f9a72b0
"""
from alembic import op
import sqlalchemy as sa


revision = "d4e8a71c9f20"
down_revision = "c8d31f9a72b0"
branch_labels = None
depends_on = None


def upgrade():
    if "execution_steps" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "execution_steps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("execution_id", sa.Integer(), sa.ForeignKey("executions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("duration_seconds", sa.Float()),
        sa.Column("error_message", sa.Text()),
        sa.UniqueConstraint("execution_id", "event_id", name="uq_execution_step_event"),
    )
    op.create_index("ix_execution_steps_execution_id", "execution_steps", ["execution_id"])
    op.create_index("ix_execution_steps_execution_started", "execution_steps", ["execution_id", "started_at"])


def downgrade():
    op.drop_table("execution_steps")
