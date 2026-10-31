"""Progresso estruturado das execuções.

Revision ID: c8d31f9a72b0
Revises: b719d45a20ef
"""
from alembic import op
import sqlalchemy as sa

revision = "c8d31f9a72b0"
down_revision = "b719d45a20ef"
branch_labels = None
depends_on = None


def upgrade():
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("executions")}
    for name, column in (
        ("progress_percent", sa.Column("progress_percent", sa.Integer())),
        ("progress_message", sa.Column("progress_message", sa.String(255))),
        ("progress_updated_at", sa.Column("progress_updated_at", sa.DateTime())),
    ):
        if name not in columns:
            op.add_column("executions", column)


def downgrade():
    for name in ("progress_updated_at", "progress_message", "progress_percent"):
        op.drop_column("executions", name)
