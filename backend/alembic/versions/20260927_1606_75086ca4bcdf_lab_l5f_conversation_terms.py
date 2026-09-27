"""lab_l5f_conversation_terms

Laboratorio Bernardo, lote L5f. Seis columnas nullable en ``whatsapp_conversations`` con las
condiciones del proveedor para todo el pedido (decisión D5: viven por proveedor × pedido, no en
cada ``supplier_quotes``):

* ``billing_regime`` (facturado | efectivo | parcial) y ``documented_pct`` (0 a 100, solo si el
  proveedor dijo qué porcentaje factura; nunca se pide).
* ``freight_cost`` (>= 0; 0 = incluido), ``freight_basis`` (pedido | viaje) y
  ``freight_free_over`` (>= 0; umbral "sin cargo arriba de $ X").
* ``terms_evidence``: último fragmento literal que respaldó las condiciones.

Autogenerada contra un SQLite migrado a head (mismo método que ``alembic check`` en CI) y
completada a mano con los CHECK nombrados que declara el modelo. NO aplicada a Supabase; SQL
equivalente para aplicarla a mano::

    ALTER TABLE whatsapp_conversations ADD COLUMN billing_regime VARCHAR(16);
    ALTER TABLE whatsapp_conversations ADD COLUMN documented_pct NUMERIC(5, 2);
    ALTER TABLE whatsapp_conversations ADD COLUMN freight_cost NUMERIC(14, 2);
    ALTER TABLE whatsapp_conversations ADD COLUMN freight_basis VARCHAR(16);
    ALTER TABLE whatsapp_conversations ADD COLUMN freight_free_over NUMERIC(14, 2);
    ALTER TABLE whatsapp_conversations ADD COLUMN terms_evidence TEXT;
    ALTER TABLE whatsapp_conversations ADD CONSTRAINT ck_whatsapp_conversations_billing_regime
        CHECK (billing_regime IS NULL OR billing_regime IN ('facturado', 'efectivo', 'parcial'));
    ALTER TABLE whatsapp_conversations ADD CONSTRAINT ck_whatsapp_conversations_documented_pct
        CHECK (documented_pct IS NULL OR (documented_pct >= 0 AND documented_pct <= 100));
    ALTER TABLE whatsapp_conversations ADD CONSTRAINT ck_whatsapp_conversations_freight_cost
        CHECK (freight_cost IS NULL OR freight_cost >= 0);
    ALTER TABLE whatsapp_conversations ADD CONSTRAINT ck_whatsapp_conversations_freight_basis
        CHECK (freight_basis IS NULL OR freight_basis IN ('pedido', 'viaje'));
    ALTER TABLE whatsapp_conversations ADD CONSTRAINT ck_whatsapp_conversations_freight_free_over
        CHECK (freight_free_over IS NULL OR freight_free_over >= 0);
    UPDATE alembic_version SET version_num = '75086ca4bcdf' WHERE version_num = 'ae19cecb0f4d';

Revision ID: 75086ca4bcdf
Revises: ae19cecb0f4d
Create Date: 2026-09-27 16:06:00

"""
from typing import Sequence
from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = '75086ca4bcdf'
down_revision: Union[str, None] = 'ae19cecb0f4d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CHECKS = (
    ("ck_whatsapp_conversations_billing_regime", "billing_regime IS NULL OR billing_regime IN ('facturado', 'efectivo', 'parcial')"),
    ("ck_whatsapp_conversations_documented_pct", "documented_pct IS NULL OR (documented_pct >= 0 AND documented_pct <= 100)"),
    ("ck_whatsapp_conversations_freight_cost", "freight_cost IS NULL OR freight_cost >= 0"),
    ("ck_whatsapp_conversations_freight_basis", "freight_basis IS NULL OR freight_basis IN ('pedido', 'viaje')"),
    ("ck_whatsapp_conversations_freight_free_over", "freight_free_over IS NULL OR freight_free_over >= 0"),
)


def upgrade() -> None:
    with op.batch_alter_table('whatsapp_conversations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('billing_regime', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('documented_pct', sa.Numeric(precision=5, scale=2), nullable=True))
        batch_op.add_column(sa.Column('freight_cost', sa.Numeric(precision=14, scale=2), nullable=True))
        batch_op.add_column(sa.Column('freight_basis', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('freight_free_over', sa.Numeric(precision=14, scale=2), nullable=True))
        batch_op.add_column(sa.Column('terms_evidence', sa.Text(), nullable=True))

        for name, condition in CHECKS:
            batch_op.create_check_constraint(name, condition)


def downgrade() -> None:
    with op.batch_alter_table('whatsapp_conversations', schema=None) as batch_op:
        for name, _ in reversed(CHECKS):
            batch_op.drop_constraint(name, type_='check')

        batch_op.drop_column('terms_evidence')
        batch_op.drop_column('freight_free_over')
        batch_op.drop_column('freight_basis')
        batch_op.drop_column('freight_cost')
        batch_op.drop_column('documented_pct')
        batch_op.drop_column('billing_regime')
