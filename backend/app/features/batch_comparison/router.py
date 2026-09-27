"""Endpoints del comparativo por pedido (L5d), protegidos con ``X-Bernardo-Lab-Admin``.

* ``GET /rfq-batches/{id}/comparison``       → JSON con matriz y las dos estrategias.
* ``GET /rfq-batches/{id}/comparison.xlsx``  → Excel (Matriz, Estrategias, Supuestos).

Query opcional ``alicuotas="<rfq_id>:<pct>,<rfq_id>:<pct>"`` para pisar el 21 % por ítem
(``12:10.5,13:21``). Mal formada o con un ítem ajeno → 422. Batch inexistente → 404.
Reutiliza ``require_admin`` del router de WhatsApp (mismo token, misma comparación).
"""

from decimal import Decimal

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Response

from app.core.dependencies import DBSession
from app.features.batch_comparison import excel
from app.features.batch_comparison import service
from app.features.batch_comparison.fiscal import parse_alicuotas
from app.features.whatsapp.router import require_admin

router = APIRouter(
    prefix="/rfq-batches",
    tags=["Comparativo (lab)"],
    dependencies=[Depends(require_admin)],
)

ALICUOTAS_QUERY = Query(
    default=None,
    description="Alícuotas de IVA por ítem: `<rfq_id>:<pct>,<rfq_id>:<pct>` (decimal con punto). Default 21.",
)


def _build(db, batch_id: int, alicuotas: str | None) -> service.BatchComparison:
    try:
        overrides: dict[int, Decimal] = parse_alicuotas(alicuotas)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        result = service.build(db, batch_id, overrides)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if result is None:
        raise HTTPException(status_code=404, detail="batch_not_found")

    return result


@router.get("/{batch_id}/comparison", summary="Comparativo del pedido (JSON)")
def get_comparison(batch_id: int, db: DBSession, alicuotas: str | None = ALICUOTAS_QUERY) -> dict:
    return service.to_json(_build(db, batch_id, alicuotas))


@router.get("/{batch_id}/comparison.xlsx", summary="Comparativo del pedido (Excel)")
def get_comparison_xlsx(batch_id: int, db: DBSession, alicuotas: str | None = ALICUOTAS_QUERY) -> Response:
    result = _build(db, batch_id, alicuotas)
    content = excel.workbook_bytes(excel.build_workbook(result))
    filename = excel.filename_for(result.loaded.batch.name, result.generated_at)

    return Response(
        content=content,
        media_type=excel.XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
