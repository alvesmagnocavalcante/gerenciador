"""add_cleanup_mode

Revision ID: 228d4f9c5b3c
Revises: 3d73692998de
Create Date: 2026-10-27 13:39:26.575527

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '228d4f9c5b3c'
down_revision: Union[str, Sequence[str], None] = '3d73692998de'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('automations', sa.Column('cleanup_mode', sa.String(length=30), nullable=False, server_default='none'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('automations', 'cleanup_mode')
