"""Decisión de propiedad de un mensaje entrante y su procesamiento en background (L4).

El bot de Render pregunta "¿este mensaje es tuyo?" y espera 5 s. Acá se decide rápido y
sin llamar al modelo; el agente corre después, en una tarea de background con su propia
sesión de base. ``service.py`` no se toca: lo que faltaba (resolver el batch desde el primer
mensaje del link ``wa.me``) vive en este módulo.

Motivos (contrato docs/CONTRATO-BOT-LAB-L4.md):
  open_conversation      había una conversación open: el inbound va al agente
  opened_now             primer mensaje: se abrió la conversación con el batch resuelto
  needs_human_hold       la conversación espera a una persona: se guarda, sin agente
  not_a_supplier         el número no está en suppliers.whatsapp_phone
  no_active_conversation proveedor conocido sin conversación abrible
  duplicate              ya se procesó ese wa_message_id
"""

import logging
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from typing import Any

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.mixins import utcnow
from app.features.invitation.model import Invitation
from app.features.rfq.batch_model import RFQBatch
from app.features.rfq.model import RFQ
from app.features.supplier.model import Supplier
from app.features.whatsapp import guardrails
from app.features.whatsapp.attachments import pending as attachments_pending
from app.features.whatsapp.loop import default_client
from app.features.whatsapp.model import WhatsappConversation
from app.features.whatsapp.model import WhatsappMessage
from app.features.whatsapp.phone import last_digits
from app.features.whatsapp.phone import normalize_phone
from app.features.whatsapp.service import handle_inbound
from app.features.whatsapp.service import open_conversation
from app.features.whatsapp.settings import whatsapp_settings
from app.features.whatsapp.turn_gate import gate

logger = logging.getLogger(__name__)

FLAG_UNSUPPORTED_MEDIA = "unsupported_media"

REASON_OPEN = "open_conversation"
REASON_OPENED_NOW = "opened_now"
REASON_HOLD = "needs_human_hold"
REASON_NOT_SUPPLIER = "not_a_supplier"
REASON_NO_ACTIVE = "no_active_conversation"
REASON_DUPLICATE = "duplicate"

#: Fábrica del cliente del modelo para el job de background. Los tests la reemplazan.
agent_client_factory = default_client

#: wa_message_id en proceso en este worker: cierra la ventana entre "decidí" y "persistí".
_in_flight: set[str] = set()


class InboundPayload(BaseModel):
    """Lo que manda el bot. ``from`` es palabra reservada: se expone como ``from_``."""

    model_config = ConfigDict(populate_by_name=True)

    wa_message_id: str = Field(min_length=1, max_length=128)
    from_: str = Field(alias="from", min_length=3, max_length=32)
    timestamp: str = Field(max_length=64)
    type: str = Field(min_length=1, max_length=32)
    text: str | None = Field(default=None, max_length=20_000)
    # L5e: adjuntos (document, image). El lab descarga el archivo con el media_id.
    media_id: str | None = Field(default=None, max_length=256)
    mime_type: str | None = Field(default=None, max_length=128)
    filename: str | None = Field(default=None, max_length=255)
    caption: str | None = Field(default=None, max_length=3000)

    @property
    def is_text(self) -> bool:
        return self.type == "text"


@dataclass
class Decision:
    owned: bool
    reason: str
    conversation_id: int | None = None
    supplier_id: int | None = None
    #: "agent": encolar handle_inbound · "attachment": encolar el job del adjunto ·
    #: "persisted": ya se guardó sin agente · None: nada
    action: str | None = None
    flags: list[str] = field(default_factory=list)
    #: El inbound provisorio del adjunto (L5e), para que el job lo complete.
    message_id: int | None = None


# ------------------------------------------------------------------ lookups
def find_supplier_by_phone(db: Session, normalized: str) -> Supplier | None:
    """Compara normalizado contra normalizado: el comprador puede haber cargado "+54 9 ..."."""

    if not normalized:
        return None

    candidates = db.scalars(select(Supplier).where(Supplier.whatsapp_phone.is_not(None))).all()

    for supplier in candidates:
        if normalize_phone(supplier.whatsapp_phone) == normalized:
            return supplier

    return None


def is_duplicate(db: Session, wa_message_id: str) -> bool:
    if wa_message_id in _in_flight:
        return True

    return db.scalar(select(WhatsappMessage.id).where(WhatsappMessage.wa_message_id == wa_message_id)) is not None


def latest_conversation(db: Session, supplier_id: int) -> WhatsappConversation | None:
    return db.scalar(
        select(WhatsappConversation)
        .where(WhatsappConversation.supplier_id == supplier_id)
        .order_by(WhatsappConversation.id.desc())
        .limit(1)
    )


def resolve_batch(db: Session, supplier: Supplier, text: str | None) -> RFQBatch | None:
    """El batch que abre la conversación: invitado, open, vigente y sin conversación previa.

    Con un solo candidato no hay duda. Con varios, gana el que aparece por nombre en el
    texto (el link wa.me de L2 dice "Mandame el pedido <nombre>"); si ninguno aparece, el
    más reciente, con warning: el borrador que sale igual lo aprueba una persona.
    """

    now = utcnow()

    already = set(
        db.scalars(
            select(WhatsappConversation.rfq_batch_id).where(WhatsappConversation.supplier_id == supplier.id)
        ).all()
    )

    def still_valid(batch: RFQBatch) -> bool:
        deadline = batch.deadline

        if deadline is None:
            return True

        # SQLite devuelve el datetime sin zona; la base lo trata igual en InvitationService.
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)

        return deadline > now

    candidates = [
        batch
        for batch in db.scalars(
            select(RFQBatch)
            .join(RFQ, RFQ.rfq_batch_id == RFQBatch.id)
            .join(Invitation, Invitation.rfq_id == RFQ.id)
            .where(Invitation.supplier_id == supplier.id, RFQBatch.status == "open")
            .distinct()
            .order_by(RFQBatch.id.desc())
        ).all()
        if batch.id not in already and still_valid(batch)
    ]

    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0]

    haystack = " ".join((text or "").lower().split())
    named = [batch for batch in candidates if " ".join(batch.name.lower().split()) in haystack]

    if len(named) == 1:
        return named[0]

    logger.warning(
        "whatsapp.inbound batch ambiguo para supplier=%s: %s candidatos, se toma el más reciente (%s)",
        supplier.id, len(candidates), candidates[0].id,
    )

    return candidates[0]


# ------------------------------------------------------------- persistencia
def persist_inbound_text(
    db: Session,
    conversation: WhatsappConversation,
    body: str,
    wa_message_id: str | None,
    *,
    media_type: str | None = None,
    flags: list[str] | None = None,
) -> WhatsappMessage:
    """Guarda un inbound saneado sin correr el agente. Base de los demás persist_*."""

    settings = whatsapp_settings
    sanitized = guardrails.sanitize_inbound(body, max_chars=settings.WHATSAPP_MAX_INPUT_CHARS, tag=settings.WHATSAPP_INPUT_TAG)

    message = WhatsappMessage(
        conversation_id=conversation.id,
        direction="inbound",
        body=sanitized.clean or (f"[{media_type}]" if media_type else ""),
        media_type=media_type,
        wa_message_id=wa_message_id,
        guardrail_flags=[*sanitized.flags, *(flags or [])],
        received_at=utcnow(),
    )

    db.add(message)
    db.commit()
    db.refresh(message)

    return message


def persist_without_agent(
    db: Session, conversation: WhatsappConversation, payload: InboundPayload, flags: list[str]
) -> WhatsappMessage:
    """Guarda el inbound sin correr el agente (hold, media, conversación cerrada)."""

    body = payload.text if payload.is_text and payload.text else f"[{payload.type}]"

    return persist_inbound_text(
        db, conversation, body, payload.wa_message_id,
        media_type=None if payload.is_text else payload.type[:64], flags=flags,
    )


def _touch_destination(db: Session, conversation: WhatsappConversation, raw_from: str) -> None:
    conversation.wa_from = raw_from[:32]
    db.commit()


# ---------------------------------------------------------------- decisión
def decide(db: Session, payload: InboundPayload) -> Decision:
    """Decide la propiedad y deja la base lista. No llama al modelo."""

    normalized = normalize_phone(payload.from_)
    supplier = find_supplier_by_phone(db, normalized)

    if supplier is None:
        return Decision(owned=False, reason=REASON_NOT_SUPPLIER)

    if is_duplicate(db, payload.wa_message_id):
        return Decision(owned=True, reason=REASON_DUPLICATE, supplier_id=supplier.id)

    conversation = latest_conversation(db, supplier.id)
    reason = REASON_OPEN

    if conversation is not None and conversation.status == "needs_human":
        _touch_destination(db, conversation, payload.from_)
        persist_without_agent(db, conversation, payload, flags=[])
        return Decision(owned=True, reason=REASON_HOLD, conversation_id=conversation.id, supplier_id=supplier.id, action="persisted")

    if conversation is None or not conversation.is_open:
        batch = resolve_batch(db, supplier, payload.text or payload.caption)

        if batch is None:
            return Decision(owned=False, reason=REASON_NO_ACTIVE, supplier_id=supplier.id)

        conversation = open_conversation(db, batch.id, supplier.id)
        reason = REASON_OPENED_NOW

    _touch_destination(db, conversation, payload.from_)

    if attachments_pending.is_attachment(payload):
        # L5e: provisorio en el request; el job de background descarga y transcribe.
        message = attachments_pending.persist_pending(db, conversation, payload)
        return Decision(
            owned=True, reason=reason, conversation_id=conversation.id, supplier_id=supplier.id,
            action="attachment", flags=[attachments_pending.FLAG_PENDING], message_id=message.id,
        )

    if not payload.is_text:
        persist_without_agent(db, conversation, payload, flags=[FLAG_UNSUPPORTED_MEDIA])
        return Decision(
            owned=True, reason=reason, conversation_id=conversation.id, supplier_id=supplier.id,
            action="persisted", flags=[FLAG_UNSUPPORTED_MEDIA],
        )

    _in_flight.add(payload.wa_message_id)
    gate.enqueue(conversation.id)  # L5b: un turno a la vez; la ráfaga se funde en el último job

    return Decision(owned=True, reason=reason, conversation_id=conversation.id, supplier_id=supplier.id, action="agent")


# -------------------------------------------------------------- background
def run_agent_job(conversation_id: int, text: str, wa_message_id: str) -> None:
    """Corre el turno con sesión propia y un solo turno a la vez por conversación.

    Toma el turno (bloqueante). Si quedan jobs más nuevos encolados para la misma
    conversación, este solo guarda su inbound: el último de la ráfaga corre el agente viendo
    todos los mensajes. Nunca re-lanza: loguea con el conversation_id.
    """

    remaining = gate.acquire(conversation_id)
    db = SessionLocal()

    try:
        if remaining > 0:
            conversation = db.get(WhatsappConversation, conversation_id)

            if conversation is not None:
                persist_inbound_text(db, conversation, text or "", wa_message_id)

            logger.info(
                "whatsapp.turn conversation_id=%s wa_message_id=%s coalesced (quedan %s más nuevos)",
                conversation_id, wa_message_id, remaining,
            )
        else:
            handle_inbound(db, conversation_id, text or "", client=agent_client_factory(), wa_message_id=wa_message_id)
            logger.info("whatsapp.turn conversation_id=%s wa_message_id=%s ran", conversation_id, wa_message_id)
    except Exception:  # noqa: BLE001 - el bot ya recibió su 200; acá solo se registra
        logger.exception("whatsapp.inbound falló el agente en conversation_id=%s", conversation_id)
    finally:
        db.close()
        gate.release(conversation_id)
        _in_flight.discard(wa_message_id)


def log_decision(decision: Decision, payload: InboundPayload) -> None:
    """Log estructurado: owned, motivo, últimos 4 dígitos y wa_message_id. Nunca el texto."""

    logger.info(
        "whatsapp.inbound owned=%s reason=%s phone=***%s wa_message_id=%s conversation_id=%s action=%s flags=%s",
        decision.owned, decision.reason, last_digits(payload.from_), payload.wa_message_id,
        decision.conversation_id, decision.action, ",".join(decision.flags) or "-",
    )


def decision_payload(decision: Decision) -> dict[str, Any]:
    return {"owned": decision.owned, "reason": decision.reason}
