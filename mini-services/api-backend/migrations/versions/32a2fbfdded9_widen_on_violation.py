"""widen dataset_contracts / dataset_contract_revisions.on_violation to fit 'dead_letter'

Revision ID: 32a2fbfdded9
Revises: 04d93c4b75f0
Create Date: 2026-09-10 16:45:00.000000

The v67 dead-letter-queue feature added "dead_letter" (11 chars) as a
legal on_violation value (see contracts.py: validate_contract_def allows
"warn" | "error" | "dead_letter"), but the baseline schema created the
column as VARCHAR(10) - one character short. Any contract built with
on_violation="dead_letter" (e.g. via the AI System Builder's dead-letter
queue component) 500s on insert with StringDataRightTruncationError.
Widening to VARCHAR(20) leaves headroom for future values.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '32a2fbfdded9'
down_revision: Union[str, Sequence[str], None] = '04d93c4b75f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('dataset_contracts', 'on_violation',
                     existing_type=sa.String(length=10),
                     type_=sa.String(length=20),
                     existing_nullable=False)
    op.alter_column('dataset_contract_revisions', 'on_violation',
                     existing_type=sa.String(length=10),
                     type_=sa.String(length=20),
                     existing_nullable=False)


def downgrade() -> None:
    op.alter_column('dataset_contract_revisions', 'on_violation',
                     existing_type=sa.String(length=20),
                     type_=sa.String(length=10),
                     existing_nullable=False)
    op.alter_column('dataset_contracts', 'on_violation',
                     existing_type=sa.String(length=20),
                     type_=sa.String(length=10),
                     existing_nullable=False)
