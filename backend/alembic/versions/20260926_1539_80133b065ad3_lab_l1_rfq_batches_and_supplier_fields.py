"""lab_l1_rfq_batches_and_supplier_fields

Laboratorio Bernardo, lote L1. Tres cosas, y nada más:

* La tabla nueva ``rfq_batches``: la base modela un RFQ = un ítem; el lab agrupa N
  ítems en un batch que lleva obra, entrega esperada y plazo una sola vez.
* Columnas nullable en tablas existentes: ``rfqs.rfq_batch_id`` (FK al batch),
  cinco columnas de contacto e historial en ``suppliers`` y dos flags de alcance
  del precio en ``supplier_quotes`` (``iva_included``, ``freight_included``).
* La vista ``v_price_history``: una fila por cotización con el proveedor y el ítem
  ya resueltos, para consultar "qué cotizó cada uno, cuándo y en qué condiciones"
  sin repetir el join en cada consumidor.

El cuerpo de tablas y columnas fue autogenerado contra un SQLite migrado a head (el
mismo procedimiento que el paso ``alembic check`` del CI). Se editó a mano solo esto:

* La FK ``rfqs.rfq_batch_id`` tiene nombre (``fk_rfqs_rfq_batch_id_rfq_batches``),
  igual que en el modelo. Autogenerate la había dejado sin nombre y el
  ``drop_constraint`` del downgrade no puede ejecutarse así.
* La vista, que Alembic no autogenera. Es SQL plano válido en SQLite y PostgreSQL.
  ``suppliers`` va con LEFT JOIN porque ``supplier_quotes.supplier_id`` es nullable
  (cotizaciones manuales, o proveedor borrado con SET NULL); el nombre cae a
  ``supplier_quotes.supplier_name`` en ese caso. ``rfqs`` es INNER JOIN porque
  ``rfq_id`` no es nullable.

Revision ID: 80133b065ad3
Revises: f1de0d1828be
Create Date: 2026-09-26 15:39:26.354943

"""
from typing import Sequence
from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = '80133b065ad3'
down_revision: Union[str, None] = 'f1de0d1828be'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PRICE_HISTORY_VIEW = "v_price_history"

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


def upgrade() -> None:
    # ---------------------------------------------------------- rfq_batches
    op.create_table('rfq_batches',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('site_name', sa.String(length=255), nullable=True),
    sa.Column('site_address', sa.String(length=1000), nullable=True),
    sa.Column('delivery_expectation', sa.Date(), nullable=True),
    sa.Column('deadline', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='open', nullable=False),
    sa.Column('notes', sa.String(length=2000), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('rfq_batches', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_rfq_batches_deadline'), ['deadline'], unique=False)
        batch_op.create_index(batch_op.f('ix_rfq_batches_id'), ['id'], unique=False)
        batch_op.create_index(batch_op.f('ix_rfq_batches_status'), ['status'], unique=False)

    # ----------------------------------------------------- rfqs.rfq_batch_id
    with op.batch_alter_table('rfqs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('rfq_batch_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_rfqs_rfq_batch_id'), ['rfq_batch_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_rfqs_rfq_batch_id_rfq_batches',
            'rfq_batches',
            ['rfq_batch_id'],
            ['id'],
            ondelete='SET NULL',
        )

    # ------------------------------------------------- supplier_quotes flags
    with op.batch_alter_table('supplier_quotes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('iva_included', sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column('freight_included', sa.Boolean(), nullable=True))

    # ------------------------------------------------------------ suppliers
    with op.batch_alter_table('suppliers', schema=None) as batch_op:
        batch_op.add_column(sa.Column('whatsapp_phone', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('rubros', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('last_contacted_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('quoted_count', sa.Integer(), server_default='0', nullable=True))
        batch_op.add_column(sa.Column('awarded_count', sa.Integer(), server_default='0', nullable=True))

    # ------------------------------------------------------- v_price_history
    # Después de las columnas: la vista lee iva_included y freight_included.
    op.execute(DROP_PRICE_HISTORY_VIEW)
    op.execute(CREATE_PRICE_HISTORY_VIEW)


def downgrade() -> None:
    # La vista primero: depende de columnas que se borran más abajo.
    op.execute(DROP_PRICE_HISTORY_VIEW)

    with op.batch_alter_table('suppliers', schema=None) as batch_op:
        batch_op.drop_column('awarded_count')
        batch_op.drop_column('quoted_count')
        batch_op.drop_column('last_contacted_at')
        batch_op.drop_column('rubros')
        batch_op.drop_column('whatsapp_phone')

    with op.batch_alter_table('supplier_quotes', schema=None) as batch_op:
        batch_op.drop_column('freight_included')
        batch_op.drop_column('iva_included')

    with op.batch_alter_table('rfqs', schema=None) as batch_op:
        batch_op.drop_constraint('fk_rfqs_rfq_batch_id_rfq_batches', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_rfqs_rfq_batch_id'))
        batch_op.drop_column('rfq_batch_id')

    with op.batch_alter_table('rfq_batches', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_rfq_batches_status'))
        batch_op.drop_index(batch_op.f('ix_rfq_batches_id'))
        batch_op.drop_index(batch_op.f('ix_rfq_batches_deadline'))

    op.drop_table('rfq_batches')
