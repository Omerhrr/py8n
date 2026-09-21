"""v120: TOTP two-factor columns on users.

``totp_secret`` holds the base32 enrollment secret (it sits PENDING from
``POST /auth/2fa/setup`` until the first code verifies at
``POST /auth/2fa/enable``); ``totp_enabled`` flips the login door into
its challenge step. Fresh installs get both from create_all; this
migration carries every pre-v120 database.

Revision ID: 6c4f2a9d1e75
Revises: 32a2fbfdded9
Create Date: 2026-09-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '6c4f2a9d1e75'
down_revision: str | Sequence[str] | None = '32a2fbfdded9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('users', sa.Column('totp_secret', sa.String(length=64),
                                     nullable=False, server_default=''))
    op.add_column('users', sa.Column('totp_enabled', sa.Boolean(),
                                     nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column('users', 'totp_enabled')
    op.drop_column('users', 'totp_secret')
