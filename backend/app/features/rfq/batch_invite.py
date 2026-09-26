"""Invitación por batch: UN mail por proveedor con todos los ítems (lab L2).

La base manda un mail por (RFQ, proveedor). Para un pedido de obra de 5 ítems eso
son 5 mails al mismo corralón, cada uno con un link. Acá se crean las mismas
``Invitation`` (una por RFQ y proveedor, con el service de la base, así el formulario
público, el seguimiento y la comparación funcionan igual) pero se manda un solo mail
por proveedor con la lista completa y un link por ítem.

Cómo queda el registro: un ``FollowUp`` de tipo ``manual`` por invitación, con el
mismo asunto y cuerpo, igual que hace ``InvitationService.send``. Así el log de
comunicaciones del comprador y los contadores del dashboard no distinguen entre un
mail de batch y cinco mails sueltos, que es lo que corresponde.

El mail lo firma Bernardo, asistente de compras del comprador. Castellano
rioplatense, sin exclamaciones ni emojis. Si ``BERNARDO_WA_NUMBER`` está configurado
lleva un link ``wa.me`` con el texto ya armado; si no, sale sin él y se loguea un
warning.
"""

import logging
import re
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from urllib.parse import quote
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.email import EmailMessage
from app.core.exceptions import BadRequestError
from app.core.exceptions import ExternalServiceError
from app.core.exceptions import NotFoundError
from app.core.mixins import utcnow
from app.features.followup.model import FollowUp
from app.features.invitation.model import Invitation
from app.features.invitation.service import InvitationService
from app.features.invitation.service import send_email_message_sync
from app.features.rfq.batch_model import RFQBatch
from app.features.rfq.model import RFQ
from app.features.supplier.model import Supplier

logger = logging.getLogger(__name__)

BUENOS_AIRES = ZoneInfo("America/Argentina/Buenos_Aires")

WHATSAPP_TEXT = "Hola Bernardo, soy {supplier}. Mandame el pedido {batch}."


@dataclass
class BatchEmailOutcome:
    supplier_id: int
    supplier_name: str
    to_email: str
    subject: str
    status: str
    item_count: int
    whatsapp_link: str | None = None
    error: str | None = None


@dataclass
class BatchInviteResult:
    invitations: list[Invitation] = field(default_factory=list)
    emails: list[BatchEmailOutcome] = field(default_factory=list)


# ------------------------------------------------------------------ WhatsApp
def whatsapp_link(supplier_name: str, batch_name: str) -> str | None:
    """``https://wa.me/<número>?text=<texto>`` o ``None`` si no hay número."""

    number = re.sub(r"\D", "", settings.BERNARDO_WA_NUMBER or "")

    if not number:
        logger.warning(
            "BERNARDO_WA_NUMBER no está configurado: el mail del batch %r sale sin "
            "link de WhatsApp",
            batch_name,
        )
        return None

    text = WHATSAPP_TEXT.format(supplier=supplier_name, batch=batch_name)

    return f"https://wa.me/{number}?text={quote(text, safe='')}"


# ----------------------------------------------------------------- proveedores
def resolve_suppliers(db: Session, user_id: int, supplier_ids: list[int]) -> list[Supplier]:
    """Los proveedores del comprador, sin repetidos y en el orden pedido. Ajeno = 404."""

    unique_ids = list(dict.fromkeys(int(supplier_id) for supplier_id in supplier_ids))

    if not unique_ids:
        raise BadRequestError("Elegí al menos un proveedor.")

    suppliers: list[Supplier] = []

    for supplier_id in unique_ids:
        supplier = db.get(Supplier, supplier_id)

        if supplier is None or supplier.user_id != user_id:
            raise NotFoundError(f"Supplier {supplier_id} not found")

        suppliers.append(supplier)

    return suppliers


# -------------------------------------------------------------------- el mail
def _quantity_text(quantity: int) -> str:
    # Separador de miles argentino: 1.200, no 1,200.
    return f"{quantity:,}".replace(",", ".")


def build_batch_email(
    *,
    batch: RFQBatch,
    supplier: Supplier,
    buyer_company: str,
    entries: list[tuple[RFQ, str]],
    whatsapp: str | None,
) -> tuple[str, str]:
    """``(asunto, cuerpo)`` del mail único. ``entries`` es [(rfq, link_formulario)]."""

    first_name = (supplier.contact_name or "").split()
    greeting = f"Hola {first_name[0]}," if first_name else f"Hola, equipo de {supplier.name},"

    count = len(entries)
    subject = f"Pedido de cotización: {batch.name} ({count} {'ítem' if count == 1 else 'ítems'})"

    deadline = batch.deadline if batch.deadline.tzinfo else batch.deadline.replace(tzinfo=UTC)
    deadline_local = deadline.astimezone(BUENOS_AIRES)

    currency = entries[0][0].currency if entries else "ARS"

    lines = [
        greeting,
        "",
        f"Soy Bernardo, asistente de compras de {buyer_company}. Te escribo para pedirte "
        "cotización de los materiales de abajo. Cada ítem tiene su propio link al "
        "formulario; no hace falta cuenta ni registro.",
        "",
    ]

    if batch.site_name:
        lines.append(f"Obra:               {batch.site_name}")

    if batch.site_address:
        lines.append(f"Lugar de entrega:   {batch.site_address}")

    if batch.delivery_expectation:
        lines.append(f"Entrega esperada:   {batch.delivery_expectation:%d/%m/%Y}")

    lines.append(
        f"Plazo para cotizar: {deadline_local:%d/%m/%Y %H:%M} (hora de Buenos Aires)"
    )

    lines += ["", f"Materiales ({count}):"]

    for index, (rfq, link) in enumerate(entries, start=1):
        lines.append(f"{index}. {rfq.item_name}: {_quantity_text(rfq.quantity)} {rfq.unit}")

        if rfq.specification and rfq.specification.strip() != rfq.item_name.strip():
            lines.append(f"   Especificación: {rfq.specification}")

        if rfq.notes:
            lines.append(f"   {rfq.notes}")

        lines.append(f"   Cotizar: {link}")
        lines.append("")

    lines += [
        f"Para cada ítem indicá precio unitario en {currency}, si el precio incluye IVA y "
        "si incluye el flete hasta la obra, plazo de entrega, forma de pago y validez de "
        "la oferta. Podés cotizar solo los ítems que tengas.",
        "",
    ]

    if whatsapp:
        lines += [
            "Si te resulta más cómodo, escribime por WhatsApp y lo resolvemos por ahí:",
            whatsapp,
            "",
        ]

    lines += [
        "Cualquier duda, respondé este mail.",
        "",
        "Saludos,",
        "Bernardo",
        f"Asistente de compras de {buyer_company}",
    ]

    return subject, "\n".join(lines)


def _send_batch_email(
    db: Session,
    batch: RFQBatch,
    supplier: Supplier,
    invitations: list[Invitation],
    buyer_company: str,
    reply_to: str | None,
) -> BatchEmailOutcome:
    rfq_by_id = {rfq.id: rfq for rfq in batch.rfqs}
    invitations = sorted(invitations, key=lambda invitation: invitation.rfq_id)

    entries = [
        (rfq_by_id[invitation.rfq_id], settings.public_form_link(invitation.rfq_id, invitation.token))
        for invitation in invitations
    ]

    link = whatsapp_link(supplier.name, batch.name)

    subject, body = build_batch_email(
        batch=batch,
        supplier=supplier,
        buyer_company=buyer_company,
        entries=entries,
        whatsapp=link,
    )

    message = EmailMessage(
        to_email=supplier.contact_email,
        to_name=supplier.contact_name or supplier.name,
        subject=subject,
        body=body,
        reply_to=reply_to,
    )

    status = "sent"
    error = None
    provider_id = None

    try:
        result = send_email_message_sync(message)
        provider_id = result.message_id
    except ExternalServiceError as exc:
        # Como en la base: una caída del proveedor de mail no pierde las invitaciones.
        status = "failed"
        error = str(exc)
        logger.warning("Mail de batch %s a %s falló: %s", batch.id, supplier.contact_email, exc)

    now = utcnow()

    for invitation in invitations:
        db.add(
            FollowUp(
                rfq_id=invitation.rfq_id,
                invitation_id=invitation.id,
                supplier_id=supplier.id,
                kind="manual",
                status=status,
                sequence=0,
                to_email=message.to_email,
                subject=subject,
                body=body,
                llm_generated=False,
                requested_fields=None,
                provider_message_id=provider_id,
                error=error,
                sent_at=now if status == "sent" else None,
                triggered_by="buyer",
            )
        )

        if status == "sent":
            invitation.sent_at = invitation.sent_at or now
            invitation.last_sent_at = now

    if status == "sent":
        supplier.last_contacted_at = now

    db.commit()

    return BatchEmailOutcome(
        supplier_id=supplier.id,
        supplier_name=supplier.name,
        to_email=supplier.contact_email,
        subject=subject,
        status=status,
        item_count=len(entries),
        whatsapp_link=link,
        error=error,
    )


# ---------------------------------------------------------------- punto de entrada
def invite_suppliers_to_batch(
    db: Session,
    batch: RFQBatch,
    supplier_ids: list[int],
) -> BatchInviteResult:
    """Crea una ``Invitation`` por (RFQ, proveedor) y manda un mail por proveedor."""

    if batch.user_id is None:
        raise BadRequestError("El batch no tiene comprador asignado.")

    rfqs = sorted(batch.rfqs, key=lambda rfq: rfq.id)

    if not rfqs:
        raise BadRequestError("El batch no tiene ítems.")

    suppliers = resolve_suppliers(db, batch.user_id, supplier_ids)

    result = BatchInviteResult()
    by_supplier: dict[int, list[Invitation]] = {supplier.id: [] for supplier in suppliers}

    for rfq in rfqs:
        # bulk_create tolera un proveedor ya invitado a ese RFQ y devuelve la
        # invitación existente, así que reintentar un batch no rompe nada.
        created = InvitationService.bulk_create(
            db=db,
            user_id=batch.user_id,
            rfq_id=rfq.id,
            supplier_ids=[supplier.id for supplier in suppliers],
            send_now=False,
        )

        for invitation in created:
            by_supplier[invitation.supplier_id].append(invitation)
            result.invitations.append(invitation)

    owner = batch.owner
    buyer_company = (owner.company_name if owner else None) or "el comprador"
    reply_to = (owner.contact_email or owner.email) if owner else None

    for supplier in suppliers:
        result.emails.append(
            _send_batch_email(
                db=db,
                batch=batch,
                supplier=supplier,
                invitations=by_supplier[supplier.id],
                buyer_company=buyer_company,
                reply_to=reply_to,
            )
        )

    return result
