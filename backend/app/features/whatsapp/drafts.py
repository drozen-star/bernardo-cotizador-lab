"""Borradores salientes: descartar, editar y reemplazo automático (L5b).

Un outbound nace como borrador (``is_draft``). Antes de mandarlo, Diego puede descartarlo o
editarlo. Lo que se aprueba es exactamente lo que se manda: una edición pasa por los mismos
frenos que un texto del agente y, si salta cualquier flag (bloqueante o de voz), no se
guarda nada.

Reemplazo automático: cuando llega un inbound nuevo y el agente va a redactar otra
respuesta, los borradores pendientes de la conversación se marcan ``superseded``. Eso pasa
adentro de la transacción del turno (sin commit acá): si el turno falla, el rollback deja
vivo el borrador anterior.
"""

from datetime import datetime

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.mixins import utcnow
from app.features.whatsapp import guardrails
from app.features.whatsapp.model import WhatsappConversation
from app.features.whatsapp.model import WhatsappMessage
from app.features.whatsapp.settings import whatsapp_settings

REASON_MANUAL = "manual"
REASON_SUPERSEDED = "superseded"
MAX_REASON_LENGTH = 32


class DraftError(Exception):
    """Errores de negocio sobre borradores; el router los mapea a HTTP."""

    def __init__(self, code: str, status_code: int = 409, detail: str = "", flags: list[str] | None = None) -> None:
        self.code = code
        self.status_code = status_code
        self.detail = detail or code
        self.flags = list(flags or [])
        super().__init__(self.detail)


# ----------------------------------------------------------------- consultas
def is_pending(message: WhatsappMessage) -> bool:
    return message.is_draft


def pending_drafts(db: Session, conversation: WhatsappConversation) -> list[WhatsappMessage]:
    return list(
        db.scalars(
            select(WhatsappMessage)
            .where(
                WhatsappMessage.conversation_id == conversation.id,
                WhatsappMessage.direction == "outbound",
                WhatsappMessage.approved_by.is_(None),
                WhatsappMessage.sent_at.is_(None),
                WhatsappMessage.discarded_at.is_(None),
            )
            .order_by(WhatsappMessage.id.asc())
        ).all()
    )


def _ensure_editable(message: WhatsappMessage) -> None:
    if message.direction != "outbound":
        raise DraftError("not_a_draft", status_code=404)

    if message.sent_at is not None:
        raise DraftError("already_sent")

    if message.discarded_at is not None:
        raise DraftError("already_discarded")


# ------------------------------------------------------------------ descartar
def discard(db: Session, message: WhatsappMessage, reason: str = REASON_MANUAL, *, now: datetime | None = None) -> WhatsappMessage:
    _ensure_editable(message)

    message.discarded_at = now or utcnow()
    message.discard_reason = (reason or REASON_MANUAL).strip()[:MAX_REASON_LENGTH] or REASON_MANUAL

    db.commit()
    db.refresh(message)

    return message


# --------------------------------------------------------------------- editar
def edit(db: Session, message: WhatsappMessage, new_body: str, *, now: datetime | None = None) -> WhatsappMessage:
    _ensure_editable(message)

    settings = whatsapp_settings
    new_body = (new_body or "").strip()

    if not new_body:
        raise DraftError("invalid_body", status_code=422, detail="el texto no puede estar vacío")

    if len(new_body) > settings.WHATSAPP_MAX_OUTPUT_CHARS:
        raise DraftError(
            "invalid_body", status_code=422,
            detail=f"el texto supera los {settings.WHATSAPP_MAX_OUTPUT_CHARS} caracteres",
        )

    # Mismos frenos y parámetros que un borrador del agente. Cualquier flag rechaza: lo que
    # se aprueba es exactamente lo que se manda, sin correcciones silenciosas.
    review = guardrails.review_outbound(
        new_body, max_chars=settings.WHATSAPP_MAX_OUTPUT_CHARS, safe_reply=settings.WHATSAPP_SAFE_REPLY
    )

    if review.flags:
        raise DraftError("guardrail", status_code=422, detail="el texto dispara frenos", flags=review.flags)

    if message.original_body is None:
        message.original_body = message.body  # solo la primera vez

    message.body = new_body
    message.edited_at = now or utcnow()

    db.commit()
    db.refresh(message)

    return message


# ---------------------------------------------------------- reemplazo automático
def opening_message_id(db: Session, conversation: WhatsappConversation) -> int | None:
    """El outbound de menor id: la apertura determinística con la lista del pedido."""

    return db.scalar(
        select(func.min(WhatsappMessage.id)).where(
            WhatsappMessage.conversation_id == conversation.id,
            WhatsappMessage.direction == "outbound",
        )
    )


def supersede_pending(db: Session, conversation: WhatsappConversation, *, now: datetime | None = None) -> list[int]:
    """Marca ``superseded`` los borradores pendientes. SIN commit: va en la transacción del turno.

    La apertura (el primer outbound, la lista del pedido) nunca se reemplaza: si sigue
    pendiente, queda pendiente y sigue en el historial del modelo.
    """

    stamp = now or utcnow()
    opening_id = opening_message_id(db, conversation)
    superseded: list[int] = []

    for message in pending_drafts(db, conversation):
        if message.id == opening_id:
            continue

        message.discarded_at = stamp
        message.discard_reason = REASON_SUPERSEDED
        superseded.append(message.id)

    return superseded
