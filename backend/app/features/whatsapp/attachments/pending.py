"""Inbound provisorio de un adjunto, guardado en el request sin descargar nada (L5e, C2).

El bot espera 5 s: acá solo se persiste ``[adjunto: <nombre>] (procesando)`` con el media_id
en ``media_url`` (``wa-media:<id>``) y el flag ``attachment_pending``. El job de background lo
completa después. El caption se sanea como cualquier texto del proveedor.
"""

import re

from sqlalchemy.orm import Session

from app.core.mixins import utcnow
from app.features.whatsapp import guardrails
from app.features.whatsapp.attachments.marks import FLAG_PENDING
from app.features.whatsapp.attachments.marks import MEDIA_URL_PREFIX
from app.features.whatsapp.model import WhatsappConversation
from app.features.whatsapp.model import WhatsappMessage
from app.features.whatsapp.settings import whatsapp_settings

ATTACHMENT_TYPES = ("document", "image")
PENDING_SUFFIX = " (procesando)"

_HEADER = re.compile(r"^\[adjunto: (?P<name>.*?)\]")


def is_attachment(payload) -> bool:
    """Documento o imagen con media_id: el bot nuevo. Sin media_id sigue como unsupported_media."""

    return payload.type in ATTACHMENT_TYPES and bool(payload.media_id)


def display_name(payload) -> str:
    """El nombre con el que el proveedor y el agente ven el adjunto: filename, o el tipo."""

    name = " ".join((payload.filename or "").split())

    return name[:255] or payload.type


def pending_body(name: str, caption: str | None) -> str:
    body = f"[adjunto: {name}]{PENDING_SUFFIX}"

    if caption:
        body += "\n" + caption

    return body


def split_body(body: str) -> tuple[str, str]:
    """``(nombre, caption)`` desde el cuerpo provisorio guardado."""

    first, _, rest = (body or "").partition("\n")
    match = _HEADER.match(first)
    name = match.group("name") if match else "adjunto"

    return name, rest.strip()


def media_id_of(message: WhatsappMessage) -> str | None:
    url = message.media_url or ""

    if url.startswith(MEDIA_URL_PREFIX):
        return url[len(MEDIA_URL_PREFIX):] or None

    return None


def persist_pending(db: Session, conversation: WhatsappConversation, payload) -> WhatsappMessage:
    settings = whatsapp_settings
    caption = guardrails.sanitize_inbound(
        payload.caption or "", max_chars=settings.WHATSAPP_MAX_INPUT_CHARS, tag=settings.WHATSAPP_INPUT_TAG
    )

    message = WhatsappMessage(
        conversation_id=conversation.id,
        direction="inbound",
        body=pending_body(display_name(payload), caption.clean),
        media_type=payload.type[:64],
        media_url=f"{MEDIA_URL_PREFIX}{payload.media_id}"[:1000],
        wa_message_id=payload.wa_message_id,
        guardrail_flags=[FLAG_PENDING, *caption.flags],
        received_at=utcnow(),
    )

    db.add(message)
    db.commit()
    db.refresh(message)

    return message
