"""Reservas exclusivas, heartbeat e solicitação de cancelamento.

Revision ID: b719d45a20ef
Revises: 8a9b0c1d2e3f
"""
from alembic import op
from datetime import datetime, timezone
import sqlalchemy as sa

revision = "b719d45a20ef"
down_revision = "8a9b0c1d2e3f"
branch_labels = None
depends_on = None


def upgrade():
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("executions")}
    for name, column in (
        ("owner_id", sa.Column("owner_id", sa.String(150))),
        ("heartbeat_at", sa.Column("heartbeat_at", sa.DateTime())),
        ("cancel_requested", sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false())),
    ):
        if name not in columns:
            op.add_column("executions", column)
    if "heartbeat_at" not in columns:
        op.get_bind().execute(sa.text("UPDATE executions SET heartbeat_at=:now WHERE status IN ('PENDING','RUNNING')"),
                              {"now": datetime.now(timezone.utc)})
        op.execute(sa.text("""UPDATE executions SET owner_id='remote:' ||
            (SELECT target_worker FROM automations WHERE automations.id=executions.automation_id)
            WHERE status IN ('PENDING','RUNNING') AND automation_id IN
            (SELECT id FROM automations WHERE lower(target_worker) != 'local')"""))
    indexes = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("executions")}
    if "ix_executions_owner_id" not in indexes:
        op.create_index("ix_executions_owner_id", "executions", ["owner_id"])
    if "uq_execution_active_automation" not in indexes:
        op.create_index("uq_execution_active_automation", "executions", ["automation_id"],
                        unique=True, sqlite_where=sa.text("status IN ('PENDING', 'RUNNING')"))


def downgrade():
    op.drop_index("uq_execution_active_automation", table_name="executions")
    op.drop_index("ix_executions_owner_id", table_name="executions")
    for name in ("cancel_requested", "heartbeat_at", "owner_id"):
        op.drop_column("executions", name)
