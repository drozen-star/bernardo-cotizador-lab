"""lab_l3a_whatsapp_conversations

Laboratorio Bernardo, lote L3a. Tres cosas:

* Tablas nuevas ``whatsapp_conversations`` y ``whatsapp_messages`` (spec sección 5).
  ``tool_calls`` y ``guardrail_flags`` son ``JSON`` genérico, no ``jsonb`` ni ``ARRAY``,
  para que el mismo modelo corra en SQLite (tests) y en PostgreSQL.
* ``supplier_quotes.conversation_id`` (nullable, FK a ``whatsapp_conversations`` con
  ``SET NULL``, indexada) y la unicidad ``(conversation_id, rfq_id)``. Con
  ``conversation_id`` NULL la unicidad no aplica: los NULL son distintos entre sí en
  ambos motores, así que dos cotizaciones por formulario para el mismo RFQ siguen
  permitidas (hay test).
* **Solo en SQLite**, la vista ``v_price_history`` (L1) se suelta y se recrea alrededor
  del cambio en ``supplier_quotes``: el modo batch reconstruye la tabla y SQLite se niega
  a hacerlo mientras una vista la referencia ("error in view v_price_history: no such
  table"). En PostgreSQL ``ADD COLUMN`` no toca la tabla y la vista queda como está, así
  que no se ejecuta ningún ``DROP VIEW`` (revisión de Diego). El SQL de la vista es el
  mismo de L1; si cambia allá, cambia acá.

Autogenerada contra un SQLite migrado a head (mismo método que ``alembic check`` en CI) y
revisada a mano: todo lo que autogenerate detectó está, con nombres de constraint
explícitos porque los modelos los declaran. A mano solo se agregó el manejo de la vista.

Revision ID: 1690dd87a51a
Revises: 25a559685695
Create Date: 2026-09-26 20:02:16.133616

"""
from typing import Sequence
from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = '1690dd87a51a'
down_revision: Union[str, None] = '25a559685695'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PRICE_HISTORY_VIEW = "v_price_history"

#: Idéntico al de la migración L1 (80133b065ad3). Se recrea después de tocar supplier_quotes.
CREATE_PRICE_HISTORY_VIEW = f"""
CREATE VIEW {PRICE_HISTORY_VIEW} AS
SELECT
    sq.supplier_id                          AS supplier_id,
    COALESCE(s.name, sq.supplier_name)      AS supplier_name,
    sq.rfq_id                               AS rfq_id,
    r.item_name                             AS item_name,
    sq.unit_price                           AS unit_price,
    sq.currency                             AS currency,
    sq.iva_included                         AS iva_included,
    sq.freight_included                     AS freight_included,
    sq.payment_terms                        AS payment_terms,
    sq.lead_time                            AS lead_time,
    sq.validity_date                        AS validity_date,
    sq.source                               AS source,
    sq.submitted_at                         AS submitted_at
FROM supplier_quotes AS sq
JOIN rfqs AS r
    ON r.id = sq.rfq_id
LEFT JOIN suppliers AS s
    ON s.id = sq.supplier_id
"""

DROP_PRICE_HISTORY_VIEW = f"DROP VIEW IF EXISTS {PRICE_HISTORY_VIEW}"


def _is_sqlite() -> bool:
    # También funciona en modo offline (--sql): el bind simulado lleva el dialecto de la URL.
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    # ------------------------------------------------ whatsapp_conversations
    op.create_table('whatsapp_conversations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('rfq_batch_id', sa.Integer(), nullable=False),
    sa.Column('supplier_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=24), server_default='open', nullable=False),
    sa.Column('opened_by', sa.String(length=16), server_default='supplier', nullable=False),
    sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('closed_reason', sa.String(length=500), nullable=True),
    sa.Column('input_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('output_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('model_calls', sa.Integer(), server_default='0', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['rfq_batch_id'], ['rfq_batches.id'], name='fk_whatsapp_conversations_rfq_batch_id_rfq_batches', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id'], name='fk_whatsapp_conversations_supplier_id_suppliers', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('whatsapp_conversations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_whatsapp_conversations_id'), ['id'], unique=False)
        batch_op.create_index(batch_op.f('ix_whatsapp_conversations_rfq_batch_id'), ['rfq_batch_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_whatsapp_conversations_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_whatsapp_conversations_supplier_id'), ['supplier_id'], unique=False)

    # ----------------------------------------------------- whatsapp_messages
    op.create_table('whatsapp_messages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('direction', sa.String(length=8), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('media_url', sa.String(length=1000), nullable=True),
    sa.Column('media_type', sa.String(length=64), nullable=True),
    sa.Column('wa_message_id', sa.String(length=128), nullable=True),
    sa.Column('tool_calls', sa.JSON(), nullable=True),
    sa.Column('guardrail_flags', sa.JSON(), nullable=True),
    sa.Column('approved_by', sa.Integer(), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['approved_by'], ['users.id'], name='fk_whatsapp_messages_approved_by_users', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['conversation_id'], ['whatsapp_conversations.id'], name='fk_whatsapp_messages_conversation_id_whatsapp_conversations', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('whatsapp_messages', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_whatsapp_messages_conversation_id'), ['conversation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_whatsapp_messages_direction'), ['direction'], unique=False)
        batch_op.create_index(batch_op.f('ix_whatsapp_messages_id'), ['id'], unique=False)
        batch_op.create_index(batch_op.f('ix_whatsapp_messages_wa_message_id'), ['wa_message_id'], unique=False)

    # ------------------------------------------- supplier_quotes.conversation_id
    # Solo SQLite: la vista depende de supplier_quotes y el batch reconstruye la tabla.
    sqlite = _is_sqlite()

    if sqlite:
        op.execute(DROP_PRICE_HISTORY_VIEW)

    with op.batch_alter_table('supplier_quotes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('conversation_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_supplier_quotes_conversation_id'), ['conversation_id'], unique=False)
        batch_op.create_unique_constraint('uq_supplier_quote_conversation_rfq', ['conversation_id', 'rfq_id'])
        batch_op.create_foreign_key('fk_supplier_quotes_conversation_id_whatsapp_conversations', 'whatsapp_conversations', ['conversation_id'], ['id'], ondelete='SET NULL')

    if sqlite:
        op.execute(CREATE_PRICE_HISTORY_VIEW)


def downgrade() -> None:
    sqlite = _is_sqlite()

    if sqlite:
        op.execute(DROP_PRICE_HISTORY_VIEW)

    with op.batch_alter_table('supplier_quotes', schema=None) as batch_op:
        batch_op.drop_constraint('fk_supplier_quotes_conversation_id_whatsapp_conversations', type_='foreignkey')
        batch_op.drop_constraint('uq_supplier_quote_conversation_rfq', type_='unique')
        batch_op.drop_index(batch_op.f('ix_supplier_quotes_conversation_id'))
        batch_op.drop_column('conversation_id')

    if sqlite:
        op.execute(CREATE_PRICE_HISTORY_VIEW)

    with op.batch_alter_table('whatsapp_messages', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_whatsapp_messages_wa_message_id'))
        batch_op.drop_index(batch_op.f('ix_whatsapp_messages_id'))
        batch_op.drop_index(batch_op.f('ix_whatsapp_messages_direction'))
        batch_op.drop_index(batch_op.f('ix_whatsapp_messages_conversation_id'))

    op.drop_table('whatsapp_messages')

    with op.batch_alter_table('whatsapp_conversations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_whatsapp_conversations_supplier_id'))
        batch_op.drop_index(batch_op.f('ix_whatsapp_conversations_status'))
        batch_op.drop_index(batch_op.f('ix_whatsapp_conversations_rfq_batch_id'))
        batch_op.drop_index(batch_op.f('ix_whatsapp_conversations_id'))

    op.drop_table('whatsapp_conversations')
