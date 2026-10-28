"""add_target_worker

Revision ID: 8a9b0c1d2e3f
Revises: 228d4f9c5b3c
Create Date: 2026-10-28 00:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8a9b0c1d2e3f'
down_revision: Union[str, Sequence[str], None] = '228d4f9c5b3c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('automations', sa.Column('target_worker', sa.String(length=100), nullable=False, server_default='local'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('automations', 'target_worker')
