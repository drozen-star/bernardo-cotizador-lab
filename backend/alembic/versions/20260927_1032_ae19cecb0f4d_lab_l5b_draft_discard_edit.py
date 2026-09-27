"""lab_l5b_draft_discard_edit

Laboratorio Bernardo, lote L5b. Cuatro columnas nullable en ``whatsapp_messages`` para el
ciclo de vida de un borrador saliente:

* ``discarded_at`` / ``discard_reason`` (varchar 32): un borrador descartado no se manda ni
  entra al historial del modelo. Motivos: ``manual`` (Diego) y ``superseded`` (llegó un inbound
  nuevo y el agente redactó otra respuesta).
* ``original_body`` / ``edited_at``: cuando Diego edita el texto antes de aprobarlo, se guarda el
  original del agente (solo la primera vez) y cuándo se editó.

Autogenerada contra un SQLite migrado a head (mismo método que ``alembic check`` en CI) y
revisada: solo lo de arriba. NO aplicada a Supabase; SQL equivalente para aplicarla a mano::

    ALTER TABLE whatsapp_messages ADD COLUMN discarded_at TIMESTAMP WITH TIME ZONE;
    ALTER TABLE whatsapp_messages ADD COLUMN discard_reason VARCHAR(32);
    ALTER TABLE whatsapp_messages ADD COLUMN original_body TEXT;
    ALTER TABLE whatsapp_messages ADD COLUMN edited_at TIMESTAMP WITH TIME ZONE;
    UPDATE alembic_version SET version_num = 'ae19cecb0f4d' WHERE version_num = 'ce9f23959dc1';

Revision ID: ae19cecb0f4d
Revises: ce9f23959dc1
Create Date: 2026-09-27 10:32:47.259219

"""
from typing import Sequence
from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = 'ae19cecb0f4d'
down_revision: Union[str, None] = 'ce9f23959dc1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('whatsapp_messages', schema=None) as batch_op:
        batch_op.add_column(sa.Column('discarded_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('discard_reason', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('original_body', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('edited_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('whatsapp_messages', schema=None) as batch_op:
        batch_op.drop_column('edited_at')
        batch_op.drop_column('original_body')
        batch_op.drop_column('discard_reason')
        batch_op.drop_column('discarded_at')
