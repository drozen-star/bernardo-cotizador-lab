"""Aprobación humana de borradores y envío (L4).

Cada outbound nace como borrador (``approved_by`` y ``sent_at`` en NULL). Una persona lo
aprueba por ``POST /whatsapp/conversations/{id}/approve``; recién ahí se manda por Cloud
API y el mensaje queda con ``sent_at``, ``approved_by`` y el ``wa_message_id`` de Meta.

Reglas:
* Ventana de 24 h desde el último inbound de la conversación (Meta no deja escribir fuera
  de ella sin template). Fuera de la ventana: ``window_closed``, sin enviar.
* Ya enviado: ``already_sent``.
* Si Meta falla, el borrador sigue pendiente y el error queda en el log.
* Sin edición del texto en L4.
"""

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.mixins import utcnow
from app.features.whatsapp import drafts
from app.features.whatsapp import sender
from app.features.whatsapp.model import WhatsappConversation
from app.features.whatsapp.model import WhatsappMessage
from app.features.whatsapp.phone import normalize_phone
from app.features.whatsapp.settings import whatsapp_settings

logger = logging.getLogger(__name__)


class ApprovalError(Exception):
    """Errores de negocio de la aprobación; el router los mapea a HTTP."""

    def __init__(self, code: str, status_code: int = 409, detail: str = "") -> None:
        self.code = code
        self.status_code = status_code
        self.detail = detail or code
        super().__init__(self.detail)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None

    return value if value.tzinfo else value.replace(tzinfo=UTC)


# ---------------------------------------------------------------- consultas
def list_drafts(db: Session, conversation: WhatsappConversation) -> list[WhatsappMessage]:
    """Solo pendientes: sin enviar, sin aprobar y sin descartar (L5b)."""

    return drafts.pending_drafts(db, conversation)


def last_inbound_at(db: Session, conversation: WhatsappConversation) -> datetime | None:
    message = db.scalar(
        select(WhatsappMessage)
        .where(WhatsappMessage.conversation_id == conversation.id, WhatsappMessage.direction == "inbound")
        .order_by(WhatsappMessage.id.desc())
        .limit(1)
    )

    if message is None:
        return None

    return _aware(message.received_at or message.created_at)


def window_is_open(db: Session, conversation: WhatsappConversation, *, now: datetime | None = None) -> bool:
    last = last_inbound_at(db, conversation)

    if last is None:
        return False

    return (now or utcnow()) - last <= timedelta(hours=whatsapp_settings.WHATSAPP_WINDOW_HOURS)


def destination_for(conversation: WhatsappConversation) -> str:
    """El ``from`` crudo del último inbound; si no lo hay, el número cargado, normalizado."""

    if conversation.wa_from:
        return conversation.wa_from

    supplier = conversation.supplier

    return normalize_phone(supplier.whatsapp_phone if supplier else "")


# --------------------------------------------------------------- aprobación
def approve(
    db: Session,
    conversation: WhatsappConversation,
    message_id: int | None = None,
    *,
    approved_by_user_id: int | None = None,
    now: datetime | None = None,
) -> WhatsappMessage:
    """Aprueba y manda un borrador. Devuelve el mensaje ya enviado."""

    if message_id is not None:
        message = db.get(WhatsappMessage, message_id)

        if message is None or message.conversation_id != conversation.id or message.direction != "outbound":
            raise ApprovalError("not_found", status_code=404)

        if message.sent_at is not None:
            raise ApprovalError("already_sent")

        if message.discarded_at is not None:
            raise ApprovalError("draft_discarded")
    else:
        pending = list_drafts(db, conversation)

        if not pending:
            raise ApprovalError("no_draft", status_code=404)

        if len(pending) > 1:
            # Sin message_id no se adivina: Diego elige cuál.
            raise ApprovalError("ambiguous_draft")

        message = pending[0]

    if not window_is_open(db, conversation, now=now):
        raise ApprovalError("window_closed")

    destination = destination_for(conversation)

    if not destination:
        raise ApprovalError("no_destination", status_code=422)

    try:
        wa_message_id = sender.send_text(destination, message.body)
    except sender.SenderError as exc:
        # El borrador sigue pendiente; nada cambia en la base.
        logger.warning(
            "whatsapp.approve envío fallido conversation_id=%s message_id=%s code=%s",
            conversation.id, message.id, exc.code,
        )
        raise

    message.sent_at = now or utcnow()
    message.approved_by = approved_by_user_id
    message.wa_message_id = wa_message_id

    db.commit()
    db.refresh(message)

    logger.info(
        "whatsapp.approve enviado conversation_id=%s message_id=%s wa_message_id=%s",
        conversation.id, message.id, wa_message_id,
    )

    return message
