"""Schemas Pydantic del batch de RFQs (laboratorio Bernardo, L2).

Solo lectura: la entrada de ``POST /rfq-batches/import`` es multipart (archivo +
campos de formulario) y la declara el router. Acá viven la respuesta y el mapeo
ORM -> respuesta, para que el router quede en el ruteo.
"""

from dataclasses import asdict
from dataclasses import is_dataclass
from datetime import date
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from pydantic import Field

from app.core.config import settings
from app.features.invitation.model import Invitation
from app.features.rfq.batch_model import RFQBatch


class BatchItem(BaseModel):
    """Un RFQ del batch: una fila del Excel."""

    id: int
    rfq_number: str
    item_name: str
    quantity: int
    unit: str
    specification: str
    notes: str | None = None
    currency: str
    status: str


class BatchInvitation(BaseModel):
    id: int
    rfq_id: int
    supplier_id: int
    supplier_name: str
    form_link: str
    status: str
    sent_at: datetime | None = None


class BatchEmail(BaseModel):
    """Resultado del mail único por proveedor."""

    supplier_id: int
    supplier_name: str
    to_email: str
    subject: str
    #: ``sent`` | ``failed`` (el proveedor de mail rechazó o no respondió).
    status: str
    item_count: int
    whatsapp_link: str | None = None
    error: str | None = None


class RFQBatchResponse(BaseModel):
    id: int
    user_id: int | None
    name: str
    site_name: str | None
    site_address: str | None
    delivery_expectation: date | None
    deadline: datetime
    status: str
    notes: str | None
    created_at: datetime

    rfq_ids: list[int] = Field(default_factory=list)
    items: list[BatchItem] = Field(default_factory=list)
    invitations: list[BatchInvitation] = Field(default_factory=list)
    #: Solo en la respuesta del import; el GET devuelve la lista vacía porque los
    #: mails quedan registrados como ``FollowUp`` en la base, no acá.
    emails: list[BatchEmail] = Field(default_factory=list)


def _email_payload(email: Any) -> dict:
    if is_dataclass(email):
        return asdict(email)

    if isinstance(email, BaseModel):
        return email.model_dump()

    return dict(email)


def build_batch_response(
    batch: RFQBatch,
    invitations: list[Invitation],
    emails: list[Any],
) -> RFQBatchResponse:
    rfqs = sorted(batch.rfqs, key=lambda rfq: rfq.id)

    return RFQBatchResponse(
        id=batch.id,
        user_id=batch.user_id,
        name=batch.name,
        site_name=batch.site_name,
        site_address=batch.site_address,
        delivery_expectation=batch.delivery_expectation,
        deadline=batch.deadline,
        status=batch.status,
        notes=batch.notes,
        created_at=batch.created_at,
        rfq_ids=[rfq.id for rfq in rfqs],
        items=[
            BatchItem(
                id=rfq.id,
                rfq_number=rfq.rfq_number,
                item_name=rfq.item_name,
                quantity=rfq.quantity,
                unit=rfq.unit,
                specification=rfq.specification,
                notes=rfq.notes,
                currency=rfq.currency,
                status=rfq.status,
            )
            for rfq in rfqs
        ],
        invitations=[
            BatchInvitation(
                id=invitation.id,
                rfq_id=invitation.rfq_id,
                supplier_id=invitation.supplier_id,
                supplier_name=invitation.supplier.name if invitation.supplier else "",
                form_link=settings.public_form_link(invitation.rfq_id, invitation.token),
                status=invitation.status,
                sent_at=invitation.sent_at,
            )
            for invitation in sorted(invitations, key=lambda i: (i.supplier_id, i.rfq_id))
        ],
        emails=[BatchEmail(**_email_payload(email)) for email in emails],
    )
