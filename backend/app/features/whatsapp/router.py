"""Router WhatsApp (L4): inbound del bot, borradores y aprobación.

* ``POST /whatsapp/inbound``: lo llama el bot de Render con ``X-Bernardo-Lab-Secret``.
  Decide la propiedad en el request (rápido, sin modelo) y corre el agente en background
  con sesión propia. Contrato en docs/CONTRATO-BOT-LAB-L4.md.
* ``GET /whatsapp/conversations/{id}/drafts`` y ``POST .../approve``: para Diego, con
  ``X-Bernardo-Lab-Admin`` (secreto distinto del de inbound).

Los dos secretos se comparan con ``hmac.compare_digest``; si el configurado está vacío,
todo es 401. Las respuestas de autenticación no dan detalle.
"""

import hmac
from datetime import datetime

from fastapi import APIRouter
from fastapi import BackgroundTasks
from fastapi import Header
from fastapi import HTTPException
from pydantic import BaseModel

from app.core.dependencies import DBSession
from app.features.whatsapp import approval
from app.features.whatsapp import inbound as inbound_flow
from app.features.whatsapp.inbound import InboundPayload
from app.features.whatsapp.model import WhatsappConversation
from app.features.whatsapp.model import WhatsappMessage
from app.features.whatsapp.sender import SenderError
from app.features.whatsapp.settings import whatsapp_settings

router = APIRouter(
    prefix="/whatsapp",
    tags=["WhatsApp (lab)"],
)


# --------------------------------------------------------------- autenticación
def _check_secret(presented: str | None, expected: str) -> None:
    if not expected or not presented or not hmac.compare_digest(presented.encode(), expected.encode()):
        raise HTTPException(status_code=401)


def require_lab_secret(x_bernardo_lab_secret: str | None = Header(default=None)) -> None:
    _check_secret(x_bernardo_lab_secret, whatsapp_settings.LAB_SHARED_SECRET)


def require_admin(x_bernardo_lab_admin: str | None = Header(default=None)) -> None:
    _check_secret(x_bernardo_lab_admin, whatsapp_settings.LAB_ADMIN_TOKEN)


# ------------------------------------------------------------------- schemas
class InboundResponse(BaseModel):
    owned: bool
    reason: str


class ApproveRequest(BaseModel):
    message_id: int | None = None


class DraftOut(BaseModel):
    id: int
    conversation_id: int
    body: str
    guardrail_flags: list[str] = []
    created_at: datetime
    sent_at: datetime | None = None
    approved_by: int | None = None
    wa_message_id: str | None = None

    @classmethod
    def from_message(cls, message: WhatsappMessage) -> "DraftOut":
        return cls(
            id=message.id,
            conversation_id=message.conversation_id,
            body=message.body,
            guardrail_flags=list(message.guardrail_flags or []),
            created_at=message.created_at,
            sent_at=message.sent_at,
            approved_by=message.approved_by,
            wa_message_id=message.wa_message_id,
        )


class DraftsResponse(BaseModel):
    conversation_id: int
    status: str
    supplier_name: str
    destination: str
    window_open: bool
    drafts: list[DraftOut]


def _conversation_or_404(db, conversation_id: int) -> WhatsappConversation:
    conversation = db.get(WhatsappConversation, conversation_id)

    if conversation is None:
        raise HTTPException(status_code=404, detail="conversation_not_found")

    return conversation


# ------------------------------------------------------------------- inbound
@router.post(
    "/inbound",
    response_model=InboundResponse,
    summary="El bot pregunta si este mensaje es del lab",
)
def receive_inbound(
    payload: InboundPayload,
    background: BackgroundTasks,
    db: DBSession,
    x_bernardo_lab_secret: str | None = Header(default=None),
):
    require_lab_secret(x_bernardo_lab_secret)

    decision = inbound_flow.decide(db, payload)
    inbound_flow.log_decision(decision, payload)

    if decision.action == "agent" and decision.conversation_id is not None:
        # Sesión propia adentro del job: la del request se cierra al responder.
        background.add_task(
            inbound_flow.run_agent_job, decision.conversation_id, payload.text or "", payload.wa_message_id
        )

    return InboundResponse(**inbound_flow.decision_payload(decision))


# ------------------------------------------------------------------- drafts
@router.get(
    "/conversations/{conversation_id}/drafts",
    response_model=DraftsResponse,
    summary="Borradores pendientes de una conversación",
)
def list_drafts(
    conversation_id: int,
    db: DBSession,
    x_bernardo_lab_admin: str | None = Header(default=None),
):
    require_admin(x_bernardo_lab_admin)
    conversation = _conversation_or_404(db, conversation_id)

    return DraftsResponse(
        conversation_id=conversation.id,
        status=conversation.status,
        supplier_name=conversation.supplier.name if conversation.supplier else "",
        destination=approval.destination_for(conversation),
        window_open=approval.window_is_open(db, conversation),
        drafts=[DraftOut.from_message(message) for message in approval.list_drafts(db, conversation)],
    )


# ------------------------------------------------------------------- approve
@router.post(
    "/conversations/{conversation_id}/approve",
    response_model=DraftOut,
    summary="Aprobar un borrador y mandarlo por WhatsApp",
)
def approve_draft(
    conversation_id: int,
    db: DBSession,
    body: ApproveRequest | None = None,
    x_bernardo_lab_admin: str | None = Header(default=None),
):
    require_admin(x_bernardo_lab_admin)
    conversation = _conversation_or_404(db, conversation_id)

    # approved_by es FK a users: se registra el comprador dueño del batch (el admin actúa por él).
    approver = conversation.batch.user_id if conversation.batch else None

    try:
        message = approval.approve(
            db, conversation, body.message_id if body else None, approved_by_user_id=approver
        )
    except approval.ApprovalError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
    except SenderError as exc:
        raise HTTPException(status_code=502, detail=f"meta_send_failed:{exc.code}") from exc

    return DraftOut.from_message(message)
