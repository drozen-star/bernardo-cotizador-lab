"""Orquestación del comparativo (L5d): cargar, costear, comparar y serializar a JSON.

La usan el router (JSON y Excel) y el script de demo. Los montos salen como strings con
dos decimales (``"41200.00"``): nunca float en el JSON.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.mixins import utcnow
from app.features.batch_comparison import fiscal
from app.features.batch_comparison import strategies
from app.features.batch_comparison.fiscal import ItemCost
from app.features.batch_comparison.loader import LoadedBatch
from app.features.batch_comparison.loader import load_batch
from app.features.batch_comparison.strategies import ComparisonResult
from app.features.batch_comparison.strategies import StrategyResult

BUENOS_AIRES = ZoneInfo("America/Argentina/Buenos_Aires")


@dataclass
class BatchComparison:
    loaded: LoadedBatch
    alicuotas: dict[int, Decimal]
    costs: list[ItemCost]
    comparison: ComparisonResult
    generated_at: datetime

    def cost_for(self, rfq_id: int, supplier_key: str) -> ItemCost | None:
        for cost in self.costs:
            if cost.rfq_id == rfq_id and cost.supplier_key == supplier_key:
                return cost

        return None

    def best_supplier_key(self, rfq_id: int) -> str | None:
        """El proveedor con menor costo real para el ítem (para resaltar en la matriz)."""

        for assignment in self.comparison.lowest_cost.assignments:
            if assignment.rfq_id == rfq_id:
                return assignment.supplier_key

        return None


def build(db: Session, batch_id: int, alicuotas: dict[int, Decimal] | None = None, *, now: datetime | None = None) -> BatchComparison | None:
    """``None`` si el batch no existe. ``ValueError`` si una alícuota refiere a un ítem ajeno."""

    loaded = load_batch(db, batch_id)

    if loaded is None:
        return None

    alicuotas = dict(alicuotas or {})
    item_ids = {rfq.id for rfq in loaded.items}
    unknown = sorted(set(alicuotas) - item_ids)

    if unknown:
        raise ValueError(f"alícuota para ítems que no son de este pedido: {', '.join(map(str, unknown))}")

    resolved = {rfq.id: fiscal.alicuota_for(rfq.id, alicuotas) for rfq in loaded.items}
    costs: list[ItemCost] = []

    for rfq in loaded.items:
        for supplier in loaded.suppliers:
            quote = loaded.quote_for(rfq.id, supplier.key)

            if quote is not None:
                costs.append(fiscal.compute_item_cost(quote, rfq, supplier.key, resolved[rfq.id]))

    comparison = strategies.compare(loaded.items, costs, loaded.supplier_names)

    return BatchComparison(
        loaded=loaded, alicuotas=resolved, costs=costs, comparison=comparison, generated_at=now or utcnow()
    )


# ---------------------------------------------------------------------- JSON
def _amount(value: Decimal | None) -> str | None:
    return None if value is None else str(fiscal.money(value))


def _cost_json(cost: ItemCost) -> dict:
    return {
        "quote_id": cost.quote_id,
        "currency": cost.currency,
        "comparable": cost.comparable,
        "unit_price": _amount(cost.unit_price),
        "iva_included": cost.iva_included,
        "iva": fiscal.IVA_LABELS[cost.iva_included],
        "alicuota": str(cost.alicuota),
        "neto_unit": _amount(cost.neto_unit),
        "desembolso_unit": _amount(cost.desembolso_unit),
        "costo_real_unit": _amount(cost.costo_real_unit),
        "neto_total": _amount(cost.neto_total),
        "desembolso_total": _amount(cost.desembolso_total),
        "costo_real_total": _amount(cost.costo_real_total),
        "shipping_cost": _amount(cost.shipping_cost),
        "freight_included": cost.freight_included,
        "lead_time": cost.lead_time,
        "payment_terms": cost.payment_terms,
        "validity": cost.validity,
        "marks": list(cost.marks),
    }


def _strategy_json(result: StrategyResult) -> dict:
    return {
        "key": result.key,
        "title": result.title,
        "assignments": [
            {
                "rfq_id": a.rfq_id,
                "item_name": a.item_name,
                "supplier_key": a.supplier_key,
                "supplier_name": a.supplier_name,
                "costo_real_total": _amount(a.cost.costo_real_total) if a.cost else None,
                "desembolso_total": _amount(a.cost.desembolso_total) if a.cost else None,
            }
            for a in result.assignments
        ],
        "total_costo_real": _amount(result.total_costo_real),
        "total_desembolso": _amount(result.total_desembolso),
        "supplier_count": result.supplier_count,
        "supplier_names": list(result.supplier_names),
        "max_lead_time": result.max_lead_time,
        "marks": list(result.marks),
        "approximate": result.approximate,
        "porque": result.porque,
    }


def to_json(result: BatchComparison) -> dict:
    loaded = result.loaded
    batch = loaded.batch
    pct = result.comparison.difference_pct

    return {
        "batch": {
            "id": batch.id,
            "name": batch.name,
            "site_name": batch.site_name,
            "status": batch.status,
            "generated_at": result.generated_at.astimezone(BUENOS_AIRES).isoformat(),
        },
        "currency": fiscal.BASE_CURRENCY,
        "alicuotas": {str(rfq_id): str(pct_value) for rfq_id, pct_value in result.alicuotas.items()},
        "suppliers": [
            {
                "key": s.key,
                "supplier_id": s.supplier_id,
                "name": s.name,
                "conversation_status": s.conversation_status,
            }
            for s in loaded.suppliers
        ],
        "items": [
            {
                "rfq_id": rfq.id,
                "item_name": rfq.item_name,
                "quantity": rfq.quantity,
                "unit": rfq.unit,
                "alicuota": str(result.alicuotas[rfq.id]),
                "best_supplier_key": result.best_supplier_key(rfq.id),
                "quotes": {
                    s.key: _cost_json(cost)
                    for s in loaded.suppliers
                    if (cost := result.cost_for(rfq.id, s.key)) is not None
                },
            }
            for rfq in loaded.items
        ],
        "strategies": [_strategy_json(s) for s in result.comparison.strategies],
        "difference": {
            "amount": _amount(result.comparison.difference_amount),
            "pct": None if pct is None else str(pct.quantize(Decimal("0.1"))),
        },
    }
