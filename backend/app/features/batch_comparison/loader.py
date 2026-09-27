"""Carga de un pedido (rfq_batch) con sus ítems y cotizaciones para el comparativo (L5d).

Solo lectura. Trae los RFQs del batch y todas las ``supplier_quotes`` de esos RFQs, de
cualquier fuente (form, manual, import, whatsapp). Si un proveedor tiene más de una para
el mismo ítem, se queda la más reciente por ``submitted_at`` (empate: mayor id). Si el
proveedor tiene conversación de WhatsApp en este batch, se anota su estado.

La identidad del proveedor es ``supplier_id`` cuando existe; si no, el nombre normalizado
(las cotizaciones importadas pueden venir sin proveedor del directorio).
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.features.batch_comparison.regime import SupplierTerms
from app.features.quote.model import SupplierQuote
from app.features.rfq.batch_model import RFQBatch
from app.features.rfq.model import RFQ
from app.features.whatsapp.model import WhatsappConversation  # solo lectura: estado y condiciones


@dataclass(frozen=True)
class SupplierRef:
    key: str
    supplier_id: int | None
    name: str
    conversation_status: str | None = None
    #: L5f: régimen y flete de la conversación de WhatsApp; None si la cotización no vino de ahí.
    terms: SupplierTerms | None = None


@dataclass
class LoadedBatch:
    batch: RFQBatch
    items: list[RFQ]
    suppliers: list[SupplierRef]
    #: (rfq_id, supplier_key) → la cotización vigente de ese proveedor para ese ítem.
    quotes: dict[tuple[int, str], SupplierQuote]

    @property
    def supplier_names(self) -> dict[str, str]:
        return {supplier.key: supplier.name for supplier in self.suppliers}

    @property
    def terms_by_key(self) -> dict[str, SupplierTerms | None]:
        return {supplier.key: supplier.terms for supplier in self.suppliers}

    def quote_for(self, rfq_id: int, supplier_key: str) -> SupplierQuote | None:
        return self.quotes.get((rfq_id, supplier_key))


def supplier_key(quote: SupplierQuote) -> str:
    if quote.supplier_id is not None:
        return f"id:{quote.supplier_id}"

    return f"name:{(quote.supplier_name or '').strip().lower()}"


def _recency(quote: SupplierQuote) -> tuple[datetime, int]:
    # SQLite devuelve datetimes naive y Postgres aware: se comparan sin tzinfo (ambos UTC).
    stamp = quote.submitted_at or quote.created_at or datetime.min

    return (stamp.replace(tzinfo=None), quote.id)


def load_batch(db: Session, batch_id: int) -> LoadedBatch | None:
    batch = db.get(RFQBatch, batch_id)

    if batch is None:
        return None

    items = sorted(batch.rfqs, key=lambda rfq: rfq.id)
    rfq_ids = [rfq.id for rfq in items]
    rows: list[SupplierQuote] = []

    if rfq_ids:
        rows = list(db.scalars(select(SupplierQuote).where(SupplierQuote.rfq_id.in_(rfq_ids))).all())

    latest: dict[tuple[int, str], SupplierQuote] = {}
    names: dict[str, str] = {}
    ids: dict[str, int | None] = {}

    for quote in sorted(rows, key=_recency):
        key = supplier_key(quote)
        names.setdefault(key, (quote.supplier_name or "").strip() or key)
        ids.setdefault(key, quote.supplier_id)
        latest[(quote.rfq_id, key)] = quote  # ordenadas por recencia: la última gana

    conversations = _conversations(db, batch.id, [sid for sid in ids.values() if sid is not None])

    suppliers = sorted(
        (
            SupplierRef(
                key=key,
                supplier_id=ids[key],
                name=names[key],
                conversation_status=conversations[ids[key]].status if ids[key] in conversations else None,
                terms=_terms_of(conversations[ids[key]]) if ids[key] in conversations else None,
            )
            for key in names
        ),
        key=lambda supplier: (supplier.name.lower(), supplier.key),
    )

    return LoadedBatch(batch=batch, items=items, suppliers=suppliers, quotes=latest)


def _terms_of(conversation: WhatsappConversation) -> SupplierTerms:
    return SupplierTerms(
        billing_regime=conversation.billing_regime,
        documented_pct=conversation.documented_pct,
        freight_cost=conversation.freight_cost,
        freight_basis=conversation.freight_basis,
        freight_free_over=conversation.freight_free_over,
    )


def _conversations(db: Session, batch_id: int, supplier_ids: list[int]) -> dict[int, WhatsappConversation]:
    """La conversación más nueva de cada proveedor en este batch (estado y condiciones)."""

    if not supplier_ids:
        return {}

    rows = db.scalars(
        select(WhatsappConversation)
        .where(
            WhatsappConversation.rfq_batch_id == batch_id,
            WhatsappConversation.supplier_id.in_(supplier_ids),
        )
        .order_by(WhatsappConversation.id.asc())
    ).all()

    return {conversation.supplier_id: conversation for conversation in rows}  # la más nueva gana
