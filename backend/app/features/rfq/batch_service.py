"""Batch de RFQs: N filas del Excel -> un ``RFQBatch`` con un ``RFQ`` por fila (lab L2).

La base modela un RFQ = un ítem y todo lo demás (invitaciones, formulario público,
seguimiento, comparación) cuelga de ese RFQ. Acá no se toca nada de eso: se crean
RFQs tal como los crearía la base, y se los agrupa con ``rfq_batch_id``.

Lo que se decide en este módulo y no en el Excel:

* ``procurement_type="goods"``: son materiales. El default de la base es "service" y
  le pediría al corralón un SLA de atención y matrículas en vez de plazo de entrega.
* ``deadline``: ahora + 72 h, el mismo del batch, para que los links del formulario
  expiren juntos (``InvitationService.default_expiry`` parte de ``rfq.deadline``).
* ``specification`` vacía cae al nombre del ítem, porque la columna es NOT NULL.
* ``accepted_alternatives`` va a ``notes`` con el prefijo "Alternativas aceptadas:".
"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import BadRequestError
from app.core.exceptions import NotFoundError
from app.core.mixins import generate_rfq_number
from app.features.invitation.model import Invitation
from app.features.rfq import taxonomy
from app.features.rfq.batch_model import RFQBatch
from app.features.rfq.batch_model import default_batch_deadline
from app.features.rfq.model import RFQ

PROCUREMENT_TYPE = "goods"
CATEGORY = "materiales"
MAX_ITEMS_PER_BATCH = 200


def _notes_for(row: dict) -> str | None:
    alternatives = (row.get("accepted_alternatives") or "").strip()

    if not alternatives:
        return None

    return f"Alternativas aceptadas: {alternatives}"[:2000]


def create_batch_from_rows(
    db: Session,
    user,
    rows: list[dict],
    name: str,
    site_name: str | None,
    site_address: str | None,
    delivery_expectation: date,
    currency: str = "ARS",
) -> RFQBatch:
    """Crea el batch y sus RFQs en una sola transacción y devuelve el batch."""

    if user is None or getattr(user, "id", None) is None:
        raise BadRequestError("El batch necesita un comprador autenticado.")

    name = (name or "").strip()

    if not name:
        raise BadRequestError("El batch necesita un nombre.")

    if not rows:
        raise BadRequestError("El batch necesita al menos un ítem.")

    if len(rows) > MAX_ITEMS_PER_BATCH:
        raise BadRequestError(f"Un batch admite hasta {MAX_ITEMS_PER_BATCH} ítems.")

    currency = (currency or "ARS").strip().upper()[:10]
    deadline = default_batch_deadline()

    batch = RFQBatch(
        user_id=user.id,
        name=name[:255],
        site_name=(site_name or "").strip()[:255] or None,
        site_address=(site_address or "").strip()[:1000] or None,
        delivery_expectation=delivery_expectation,
        deadline=deadline,
        status="open",
    )

    db.add(batch)
    db.flush()  # necesita el id para los RFQs

    required_fields = list(taxonomy.default_required_fields(PROCUREMENT_TYPE))

    for row in rows:
        item = str(row["item"]).strip()
        specification = (row.get("specification") or "").strip() or item

        rfq = RFQ(
            user_id=user.id,
            rfq_batch_id=batch.id,
            # Mismo generador que usa la base como default de columna: RFQ-<año>-<8 hex>.
            rfq_number=generate_rfq_number(),
            item_name=item[:255],
            specification=specification[:1000],
            quantity=int(row["quantity"]),
            unit=str(row["unit"]).strip()[:32],
            delivery_expectation=delivery_expectation,
            currency=currency,
            status="open",
            buyer_company=user.company_name,
            site_name=batch.site_name,
            site_address=batch.site_address,
            deadline=deadline,
            procurement_type=PROCUREMENT_TYPE,
            required_fields=required_fields,
            category=CATEGORY,
            notes=_notes_for(row),
        )

        db.add(rfq)

    db.commit()
    db.refresh(batch)

    return batch


def get_batch(db: Session, user, batch_id: int) -> RFQBatch:
    """El batch del comprador, o 404. Un batch ajeno también es 404, como en la base."""

    batch = db.get(RFQBatch, batch_id)

    if batch is None or (batch.user_id is not None and batch.user_id != user.id):
        raise NotFoundError("Batch not found")

    return batch


def list_batch_invitations(db: Session, batch: RFQBatch) -> list[Invitation]:
    rfq_ids = [rfq.id for rfq in batch.rfqs]

    if not rfq_ids:
        return []

    stmt = (
        select(Invitation)
        .where(Invitation.rfq_id.in_(rfq_ids))
        .order_by(Invitation.supplier_id.asc(), Invitation.rfq_id.asc())
    )

    return list(db.scalars(stmt).all())
