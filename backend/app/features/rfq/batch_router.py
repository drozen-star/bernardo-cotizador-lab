"""Router del batch de RFQs (laboratorio Bernardo, L2).

* ``POST /rfq-batches/import``  multipart: un Excel de formato fijo + datos de la
  obra + proveedores. Crea el batch, un RFQ por fila, una invitación por (RFQ,
  proveedor) y manda un mail por proveedor. Devuelve el batch con todo eso.
* ``GET /rfq-batches/{id}``     el batch con sus ítems e invitaciones.

Autenticación: ``CurrentUser``, como el resto de la API del comprador. El campo
``supplier_ids`` se repite en el multipart (``supplier_ids=3&supplier_ids=5``), que
es como lo manda Swagger UI y como lo lee FastAPI.
"""

from datetime import date

from fastapi import APIRouter
from fastapi import File
from fastapi import Form
from fastapi import UploadFile

from app.core.dependencies import CurrentUser
from app.core.dependencies import DBSession
from app.core.exceptions import BadRequestError
from app.features.rfq import batch_service
from app.features.rfq.batch_invite import invite_suppliers_to_batch
from app.features.rfq.batch_invite import resolve_suppliers
from app.features.rfq.batch_schema import RFQBatchResponse
from app.features.rfq.batch_schema import build_batch_response
from app.features.rfq.intake import parse_materials_xlsx

router = APIRouter(
    prefix="/rfq-batches",
    tags=["RFQ batches (lab)"],
)

#: Un Excel de materiales pesa decenas de KB; 5 MB es margen de sobra.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024


@router.post(
    "/import",
    response_model=RFQBatchResponse,
    status_code=201,
    summary="Importar un Excel de materiales como batch y avisar a los proveedores",
)
def import_batch(
    user: CurrentUser,
    db: DBSession,
    file: UploadFile = File(
        ...,
        description="Excel .xlsx: hoja 1, encabezados item, quantity, unit, "
        "specification, accepted_alternatives.",
    ),
    name: str = Form(..., min_length=1, max_length=255, description="Nombre del pedido."),
    delivery_expectation: date = Form(..., description="Fecha esperada de entrega."),
    site_name: str | None = Form(None, max_length=255),
    site_address: str | None = Form(None, max_length=1000),
    currency: str = Form("ARS", max_length=10),
    supplier_ids: list[int] = Form(
        [],
        description="Ids de proveedores a invitar. Repetir el campo por cada id.",
    ),
):
    filename = (file.filename or "").lower()

    if not filename.endswith(".xlsx"):
        raise BadRequestError("El archivo tiene que ser un Excel .xlsx.")

    payload = file.file.read(MAX_UPLOAD_BYTES + 1)

    if len(payload) > MAX_UPLOAD_BYTES:
        raise BadRequestError("El Excel supera los 5 MB.")

    # Primero lo que puede fallar sin efectos: el Excel y los proveedores. Así un
    # proveedor ajeno o una fila rota no dejan un batch a medias.
    rows = parse_materials_xlsx(payload)

    if supplier_ids:
        resolve_suppliers(db, user.id, supplier_ids)

    batch = batch_service.create_batch_from_rows(
        db=db,
        user=user,
        rows=rows,
        name=name,
        site_name=site_name,
        site_address=site_address,
        delivery_expectation=delivery_expectation,
        currency=currency,
    )

    invitations = []
    emails = []

    if supplier_ids:
        result = invite_suppliers_to_batch(db=db, batch=batch, supplier_ids=supplier_ids)
        invitations = result.invitations
        emails = result.emails

    db.refresh(batch)

    return build_batch_response(batch, invitations, emails)


@router.get(
    "/{batch_id}",
    response_model=RFQBatchResponse,
    summary="Un batch con sus ítems e invitaciones",
)
def get_batch(
    batch_id: int,
    user: CurrentUser,
    db: DBSession,
):
    batch = batch_service.get_batch(db, user, batch_id)

    return build_batch_response(
        batch,
        batch_service.list_batch_invitations(db, batch),
        [],
    )
