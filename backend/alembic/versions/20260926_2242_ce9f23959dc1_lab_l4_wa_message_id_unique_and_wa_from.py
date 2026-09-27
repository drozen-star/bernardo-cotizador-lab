"""lab_l4_wa_message_id_unique_and_wa_from

Laboratorio Bernardo, lote L4. Dos cosas:

* ``whatsapp_messages.wa_message_id`` pasa a índice **único**: es la clave del dedupe de
  inbound (el bot puede reintentar) y del envío. Los borradores tienen NULL hasta que se
  mandan; los NULL no chocan entre sí en SQLite ni en PostgreSQL.
* ``whatsapp_conversations.wa_from`` (varchar 32, nullable): el ``from`` crudo de Meta del
  último inbound, tal cual llegó. El contrato bot -> lab exige guardarlo sin normalizar y
  usarlo como destino de las respuestas; no había columna donde ponerlo.

Autogenerada contra un SQLite migrado a head (mismo método que ``alembic check`` en CI) y
revisada: solo lo de arriba. NO aplicada a Supabase; SQL equivalente para aplicarla a mano::

    ALTER TABLE whatsapp_conversations ADD COLUMN wa_from VARCHAR(32);
    DROP INDEX ix_whatsapp_messages_wa_message_id;
    CREATE UNIQUE INDEX ix_whatsapp_messages_wa_message_id ON whatsapp_messages (wa_message_id);
    UPDATE alembic_version SET version_num = 'ce9f23959dc1' WHERE version_num = '1690dd87a51a';

Revision ID: ce9f23959dc1
Revises: 1690dd87a51a
Create Date: 2026-09-26 22:42:51.108894

"""
from typing import Sequence
from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = 'ce9f23959dc1'
down_revision: Union[str, None] = '1690dd87a51a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('whatsapp_conversations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('wa_from', sa.String(length=32), nullable=True))

    with op.batch_alter_table('whatsapp_messages', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_whatsapp_messages_wa_message_id'))
        batch_op.create_index(batch_op.f('ix_whatsapp_messages_wa_message_id'), ['wa_message_id'], unique=True)


def downgrade() -> None:
    with op.batch_alter_table('whatsapp_messages', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_whatsapp_messages_wa_message_id'))
        batch_op.create_index(batch_op.f('ix_whatsapp_messages_wa_message_id'), ['wa_message_id'], unique=False)

    with op.batch_alter_table('whatsapp_conversations', schema=None) as batch_op:
        batch_op.drop_column('wa_from')
