"""lab_l2_rfq_batches_user_id

Laboratorio Bernardo, lote L2. Una sola cosa: ``rfq_batches.user_id``, FK a
``users.id`` con ``ON DELETE CASCADE`` (igual que ``rfqs.user_id`` en la base), con
índice.

Nullable **solo por la migración**: en L1 la tabla se creó sin comprador y podría
tener filas. Toda fila nueva lleva ``user_id`` (``batch_service.create_batch_from_rows``
lo exige), así que la columna pasa a ``NOT NULL`` en una migración posterior, cuando
haya que limpiar filas viejas: ``UPDATE rfq_batches SET user_id = ... WHERE user_id IS
NULL`` (o borrarlas) y después ``ALTER COLUMN user_id SET NOT NULL``.

Autogenerada contra un SQLite migrado a head (mismo método que ``alembic check`` en
CI). La FK ya sale con nombre porque el modelo lo declara.

Revision ID: 25a559685695
Revises: 80133b065ad3
Create Date: 2026-09-26 16:14:20.243840

"""
from typing import Sequence
from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = '25a559685695'
down_revision: Union[str, None] = '80133b065ad3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('rfq_batches', schema=None) as batch_op:
        batch_op.add_column(sa.Column('user_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_rfq_batches_user_id'), ['user_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_rfq_batches_user_id_users',
            'users',
            ['user_id'],
            ['id'],
            ondelete='CASCADE',
        )


def downgrade() -> None:
    with op.batch_alter_table('rfq_batches', schema=None) as batch_op:
        batch_op.drop_constraint('fk_rfq_batches_user_id_users', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_rfq_batches_user_id'))
        batch_op.drop_column('user_id')
