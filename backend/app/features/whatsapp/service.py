"""Service del slice WhatsApp: abre conversaciones y procesa mensajes entrantes.

Sin envío y sin endpoint HTTP (eso es L4). Cada respuesta del agente queda como
**borrador** en ``whatsapp_messages`` (``approved_by`` y ``sent_at`` en NULL).

Flujo de ``handle_inbound``:

1. Se guarda el inbound (``received_at``), ya saneado.
2. Si la conversación no está ``open``, se termina ahí: el agente no corre.
3. Si es el primer mensaje del proveedor, la respuesta es la lista completa del pedido,
   determinística, sin llamar al modelo (decisión de L3a; ver ``prompt.build_opening_reply``).
4. Si no, se arma el system prompt (ficha + ya registrado), el historial de solo texto,
   y se corre el loop con el ejecutor de herramientas que escribe en la base.
5. La salida pasa por los frenos. Bloqueo, vueltas agotadas o set_status cierran la
   conversación según corresponda. Se guarda el outbound con ``tool_calls`` y
   ``guardrail_flags`` y se acumula el consumo.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import BadRequestError
from app.core.exceptions import NotFoundError
from app.core.mixins import utcnow
from app.features.quote.model import SupplierQuote
from app.features.rfq.batch_model import RFQBatch
from app.features.rfq.model import RFQ
from app.features.supplier.model import Supplier
from app.features.whatsapp import guardrails
from app.features.whatsapp import prompt as prompts
from app.features.whatsapp import tools
from app.features.whatsapp.loop import ModelClient
from app.features.whatsapp.loop import TurnResult
from app.features.whatsapp.loop import default_client
from app.features.whatsapp.loop import run_turn
from app.features.whatsapp.model import WhatsappConversation
from app.features.whatsapp.model import WhatsappMessage
from app.features.whatsapp.quote_writer import QuoteToolExecutor
from app.features.whatsapp.quote_writer import close_conversation
from app.features.whatsapp.settings import whatsapp_settings

logger = logging.getLogger(__name__)

#: Cierre fijo cuando el modelo termina con set_status y sin texto.
CLOSING_REPLY = "Gracias. Con esto tengo lo que necesito; {buyer_company} lo revisa y te escribimos."

FLAG_TOOL_ROUNDS_EXHAUSTED = "tool_rounds_exhausted"


@dataclass
class InboundResult:
    inbound: WhatsappMessage
    outbound: WhatsappMessage | None
    turn: TurnResult | None = None


# ------------------------------------------------------------------ apertura
def open_conversation(db: Session, rfq_batch_id: int, supplier_id: int) -> WhatsappConversation:
    """Crea la conversación en ``open``. Si ya hay una abierta para el par, la devuelve."""

    batch = db.get(RFQBatch, rfq_batch_id)

    if batch is None:
        raise NotFoundError("Batch not found")

    supplier = db.get(Supplier, supplier_id)

    if supplier is None or (batch.user_id is not None and supplier.user_id != batch.user_id):
        raise NotFoundError("Supplier not found")

    if not batch.rfqs:
        raise BadRequestError("El batch no tiene ítems.")

    existing = db.scalar(
        select(WhatsappConversation).where(
            WhatsappConversation.rfq_batch_id == rfq_batch_id,
            WhatsappConversation.supplier_id == supplier_id,
            WhatsappConversation.status == "open",
        )
    )

    if existing is not None:
        return existing

    conversation = WhatsappConversation(
        rfq_batch_id=rfq_batch_id,
        supplier_id=supplier_id,
        status="open",
        opened_by="supplier",
        opened_at=utcnow(),
    )

    db.add(conversation)
    db.commit()
    db.refresh(conversation)

    return conversation


# ------------------------------------------------------------------ helpers
def _buyer_company(batch: RFQBatch) -> str:
    owner = batch.owner

    return (owner.company_name if owner else None) or "el comprador"


def text_history(messages: list[WhatsappMessage], *, tag: str) -> list[dict]:
    """Historial para el modelo: solo texto. inbound -> user, outbound -> assistant.

    Dos mensajes seguidos del mismo lado se funden en uno, porque la API exige roles
    alternados. Los bloques de herramientas de turnos anteriores no se reconstruyen.
    """

    history: list[dict] = []

    for message in messages:
        role = "user" if message.direction == "inbound" else "assistant"
        text = f"<{tag}>\n{message.body}\n</{tag}>" if role == "user" else message.body

        if history and history[-1]["role"] == role:
            history[-1]["content"] += "\n\n" + text
        else:
            history.append({"role": role, "content": text})

    return history


def _conversation_quotes(db: Session, conversation_id: int) -> list[SupplierQuote]:
    return list(
        db.scalars(select(SupplierQuote).where(SupplierQuote.conversation_id == conversation_id)).all()
    )


def _save_outbound(
    db: Session,
    conversation: WhatsappConversation,
    body: str,
    *,
    tool_calls: list[dict] | None,
    flags: list[str],
) -> WhatsappMessage:
    outbound = WhatsappMessage(
        conversation_id=conversation.id,
        direction="outbound",
        body=body,
        tool_calls=tool_calls or None,
        guardrail_flags=list(flags),
        approved_by=None,  # borrador: lo aprueba un humano en L4
        sent_at=None,
    )

    db.add(outbound)

    return outbound


# -------------------------------------------------------------- handle_inbound
def handle_inbound(
    db: Session,
    conversation_id: int,
    text: str,
    *,
    client: ModelClient | None = None,
    wa_message_id: str | None = None,
) -> InboundResult:
    conversation = db.get(WhatsappConversation, conversation_id)

    if conversation is None:
        raise NotFoundError("Conversation not found")

    settings = whatsapp_settings
    sanitized = guardrails.sanitize_inbound(
        text, max_chars=settings.WHATSAPP_MAX_INPUT_CHARS, tag=settings.WHATSAPP_INPUT_TAG
    )

    inbound = WhatsappMessage(
        conversation_id=conversation.id,
        direction="inbound",
        body=sanitized.clean,
        wa_message_id=wa_message_id,
        guardrail_flags=list(sanitized.flags),
        received_at=utcnow(),
    )

    db.add(inbound)
    db.commit()
    db.refresh(inbound)

    if not conversation.is_open:
        logger.info("Conversación %s en %s: inbound guardado, el agente no corre", conversation.id, conversation.status)
        return InboundResult(inbound=inbound, outbound=None)

    batch = conversation.batch
    supplier = conversation.supplier
    rfqs = sorted(batch.rfqs, key=lambda rfq: rfq.id)
    buyer_company = _buyer_company(batch)

    # Primer mensaje del proveedor: la lista completa, sin modelo.
    has_outbound = any(message.direction == "outbound" for message in conversation.messages)

    if not has_outbound:
        opening = prompts.build_opening_reply(
            batch=batch, rfqs=rfqs, supplier=supplier, buyer_company=buyer_company
        )
        review = guardrails.review_outbound(
            opening, max_chars=max(settings.WHATSAPP_MAX_OUTPUT_CHARS, len(opening)), safe_reply=settings.WHATSAPP_SAFE_REPLY
        )
        outbound = _save_outbound(db, conversation, review.text, tool_calls=None, flags=review.flags)
        db.commit()
        db.refresh(outbound)

        return InboundResult(inbound=inbound, outbound=outbound)

    return _run_agent(
        db,
        conversation=conversation,
        batch=batch,
        rfqs=rfqs,
        supplier=supplier,
        buyer_company=buyer_company,
        inbound=inbound,
        client=client,
    )


def _run_agent(
    db: Session,
    *,
    conversation: WhatsappConversation,
    batch: RFQBatch,
    rfqs: list[RFQ],
    supplier: Supplier,
    buyer_company: str,
    inbound: WhatsappMessage,
    client: ModelClient | None,
) -> InboundResult:
    settings = whatsapp_settings
    messages = list(conversation.messages)

    system = prompts.build_system_prompt(
        batch=batch,
        rfqs=rfqs,
        supplier=supplier,
        buyer_company=buyer_company,
        quotes=_conversation_quotes(db, conversation.id),
        tag=settings.WHATSAPP_INPUT_TAG,
    )

    executor = QuoteToolExecutor(
        db=db,
        conversation=conversation,
        rfqs_by_id={rfq.id: rfq for rfq in rfqs},
        supplier=supplier,
        inbound_bodies=[message.body for message in messages if message.direction == "inbound"],
    )

    turn = run_turn(
        client=client or default_client(),
        system=system,
        history=text_history(messages, tag=settings.WHATSAPP_INPUT_TAG),
        tools=tools.TOOLS,
        execute_tool=executor,
        max_rounds=settings.WHATSAPP_MAX_TOOL_ROUNDS,
        max_tokens=settings.WHATSAPP_MAX_TOKENS,
    )

    conversation.input_tokens = (conversation.input_tokens or 0) + turn.input_tokens
    conversation.output_tokens = (conversation.output_tokens or 0) + turn.output_tokens
    conversation.model_calls = (conversation.model_calls or 0) + turn.model_calls

    flags: list[str] = []
    text = turn.text

    if turn.requested_status:
        close_conversation(conversation, turn.requested_status, turn.requested_reason or turn.requested_status)

        if not text and turn.requested_status in ("complete", "supplier_declined"):
            text = CLOSING_REPLY.format(buyer_company=buyer_company)

    if turn.exhausted:
        flags.append(FLAG_TOOL_ROUNDS_EXHAUSTED)
        close_conversation(conversation, "needs_human", "el agente agotó las vueltas de herramientas sin cerrar")
        text = settings.WHATSAPP_SAFE_REPLY

    review = guardrails.review_outbound(
        text, max_chars=settings.WHATSAPP_MAX_OUTPUT_CHARS, safe_reply=settings.WHATSAPP_SAFE_REPLY
    )
    flags += review.flags

    if review.blocked and not turn.exhausted:
        close_conversation(conversation, "needs_human", "freno de salida: " + ", ".join(review.blocking_flags))

    tool_calls = list(turn.tool_calls)

    if executor.buyer_questions:
        tool_calls.append({"name": "buyer_questions", "questions": list(executor.buyer_questions)})

    outbound = _save_outbound(db, conversation, review.text, tool_calls=tool_calls, flags=flags)

    db.commit()
    db.refresh(outbound)
    db.refresh(conversation)

    return InboundResult(inbound=inbound, outbound=outbound, turn=turn)
