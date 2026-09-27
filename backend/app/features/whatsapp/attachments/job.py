"""Job de background de un adjunto (L5e): descarga, convierte a texto y corre el agente.

Mismo patrón que ``inbound.run_agent_job``: sesión propia, nunca re-lanza, ``finally`` con
``db.close``, ``gate.release`` y ``_in_flight.discard``. El turno se toma **antes** de descargar
y se transcribe siempre, aunque haya jobs más nuevos encolados: así un texto que llega después
("ahí te mandé la lista") corre el agente viendo el adjunto ya leído.

Un adjunto que falla no se pierde en silencio: queda ``[adjunto no procesado: <nombre> — <motivo>]``
con ``attachment_failed`` y el agente corre igual, para pedir el reenvío.
"""

import logging
from collections.abc import Callable
from typing import Any

import httpx

from app.core.database import SessionLocal
from app.features.whatsapp import guardrails
from app.features.whatsapp import inbound as inbound_flow
from app.features.whatsapp.attachments import AttachmentError
from app.features.whatsapp.attachments import download
from app.features.whatsapp.attachments import pending
from app.features.whatsapp.attachments import spreadsheet
from app.features.whatsapp.attachments import transcribe
from app.features.whatsapp.attachments.marks import FLAG_FAILED
from app.features.whatsapp.attachments.marks import FLAG_IMAGE
from app.features.whatsapp.attachments.marks import FLAG_PDF
from app.features.whatsapp.attachments.marks import FLAG_PENDING
from app.features.whatsapp.attachments.marks import FLAG_TRANSCRIBED
from app.features.whatsapp.attachments.marks import FLAG_XLSX
from app.features.whatsapp.loop import default_client
from app.features.whatsapp.model import WhatsappConversation
from app.features.whatsapp.model import WhatsappMessage
from app.features.whatsapp.service import handle_inbound
from app.features.whatsapp.settings import whatsapp_settings
from app.features.whatsapp.turn_gate import gate

logger = logging.getLogger(__name__)

CODE_PROCESSING_FAILED = "processing_failed"

#: Fábricas inyectables: el cliente del modelo para transcribir y el HTTP para bajar de Meta.
attachment_client_factory: Callable[[], Any] = default_client
http_client_factory: Callable[[], httpx.Client | None] = lambda: None


# ------------------------------------------------------------------ extracción
def _extract(data: bytes, mime: str, name: str, rfqs) -> tuple[str, dict | None, list[str]]:
    """``(contenido, usage, flags)`` según el tipo. Otro tipo → ``unsupported_type``."""

    max_chars = whatsapp_settings.WHATSAPP_MAX_ATTACHMENT_CHARS

    if spreadsheet.is_xlsx(mime, name):
        content, usage = spreadsheet.extract(data, rfqs, client_factory=attachment_client_factory, max_chars=max_chars)
        return content, usage, [FLAG_XLSX]

    if mime == transcribe.PDF_MIME or mime in transcribe.IMAGE_MIMES:
        content, usage = transcribe.transcribe(data, mime, rfqs, client=attachment_client_factory())
        kind = FLAG_PDF if mime == transcribe.PDF_MIME else FLAG_IMAGE
        return content, usage, [kind, FLAG_TRANSCRIBED]

    raise AttachmentError(transcribe.CODE_UNSUPPORTED, mime or "sin mime")


def final_body(name: str, caption: str, content: str) -> str:
    if content.strip().upper() == transcribe.NO_MATCHES:
        return f"[adjunto: {name}] sin ítems del pedido" + (f"\n{caption}" if caption else "")

    parts = [f"[adjunto: {name}]"]

    if caption:
        parts.append(caption)

    parts.append(content.strip())

    return "\n".join(parts)


def failed_body(name: str, caption: str, code: str) -> str:
    return f"[adjunto no procesado: {name} — {code}]" + (f"\n{caption}" if caption else "")


def process(db, conversation: WhatsappConversation, message: WhatsappMessage) -> None:
    """Completa el inbound provisorio. Nunca levanta: una falla queda escrita en el mensaje."""

    settings = whatsapp_settings
    name, caption = pending.split_body(message.body)
    media_id = pending.media_id_of(message)
    rfqs = sorted(conversation.batch.rfqs, key=lambda rfq: rfq.id) if conversation.batch else []
    flags = [flag for flag in (message.guardrail_flags or []) if flag != FLAG_PENDING]
    record: dict[str, Any] = {"name": "attachment", "mime": None, "bytes": 0, "usage": None}

    try:
        if not media_id:
            raise AttachmentError(download.CODE_DOWNLOAD_FAILED, "sin media_id")

        data, mime = download.fetch_media(media_id, client=http_client_factory())
        record["mime"], record["bytes"] = mime, len(data)

        content, usage, kind_flags = _extract(data, mime, name, rfqs)
        record["usage"] = usage
        flags.extend(kind_flags)
        body = final_body(name, caption, content)
    except AttachmentError as exc:
        logger.warning(
            "whatsapp.attachment conversation_id=%s message_id=%s falló: %s (%s)",
            conversation.id, message.id, exc.code, exc.detail,
        )
        record["error"] = exc.code
        flags.append(FLAG_FAILED)
        body = failed_body(name, caption, exc.code)
    except Exception as exc:  # noqa: BLE001 - modelo, archivo roto: se registra y el agente pide reenvío
        logger.warning(
            "whatsapp.attachment conversation_id=%s message_id=%s falló: %s (%s)",
            conversation.id, message.id, CODE_PROCESSING_FAILED, type(exc).__name__,
        )
        record["error"] = f"{CODE_PROCESSING_FAILED}:{type(exc).__name__}"
        flags.append(FLAG_FAILED)
        body = failed_body(name, caption, CODE_PROCESSING_FAILED)

    sanitized = guardrails.sanitize_inbound(
        body, max_chars=settings.WHATSAPP_MAX_ATTACHMENT_CHARS, tag=settings.WHATSAPP_INPUT_TAG
    )
    message.body = sanitized.clean
    message.guardrail_flags = [*flags, *sanitized.flags]
    message.tool_calls = [record]

    usage = record.get("usage")

    if usage:
        conversation.input_tokens = (conversation.input_tokens or 0) + int(usage.get("input_tokens", 0))
        conversation.output_tokens = (conversation.output_tokens or 0) + int(usage.get("output_tokens", 0))
        conversation.model_calls = (conversation.model_calls or 0) + 1

    db.commit()
    db.refresh(message)


# ------------------------------------------------------------------------ job
def run_attachment_job(conversation_id: int, message_id: int, wa_message_id: str) -> None:
    remaining = gate.acquire(conversation_id)
    db = SessionLocal()

    try:
        conversation = db.get(WhatsappConversation, conversation_id)
        message = db.get(WhatsappMessage, message_id)

        if conversation is None or message is None:
            logger.warning("whatsapp.attachment conversation_id=%s message_id=%s no encontrado", conversation_id, message_id)
            return

        process(db, conversation, message)

        if remaining > 0:
            logger.info(
                "whatsapp.attachment conversation_id=%s message_id=%s wa_message_id=%s procesado, coalesced (quedan %s)",
                conversation_id, message_id, wa_message_id, remaining,
            )
        else:
            handle_inbound(
                db, conversation_id, "", client=inbound_flow.agent_client_factory(),
                wa_message_id=wa_message_id, existing_inbound=message,
            )
            logger.info(
                "whatsapp.attachment conversation_id=%s message_id=%s wa_message_id=%s procesado, agente corrió",
                conversation_id, message_id, wa_message_id,
            )
    except Exception:  # noqa: BLE001 - el bot ya recibió su 200; acá solo se registra
        logger.exception("whatsapp.attachment falló el job en conversation_id=%s", conversation_id)
    finally:
        db.close()
        gate.release(conversation_id)
        inbound_flow._in_flight.discard(wa_message_id)
