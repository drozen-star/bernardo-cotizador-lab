"""Flete por proveedor dentro de una estrategia (L5f, decisiones D2, D3 y D4).

El flete se suma **una vez por proveedor usado**, nunca por ítem ni prorrateado. El umbral
"sin cargo arriba de $ X" se evalúa contra la suma de ``unit_price × quantity`` tal como cotizó
el proveedor, de lo que la estrategia le asigna. Con base "viaje" se supone un viaje. El flete
no lleva IVA en el costo real; en el desembolso se grava con la alícuota general si el régimen
es facturado y va tal cual si no.
"""

from decimal import Decimal

from app.features.batch_comparison.fiscal import MARK_FREIGHT_TO_QUOTE
from app.features.batch_comparison.fiscal import MARK_FREIGHT_UNCONFIRMED
from app.features.batch_comparison.fiscal import format_money
from app.features.batch_comparison.regime import HUNDRED
from app.features.batch_comparison.regime import ONE
from app.features.batch_comparison.regime import REGIME_INVOICED
from app.features.batch_comparison.regime import SupplierTerms

ZERO = Decimal(0)
GENERAL_ALICUOTA = Decimal("21")
BASIS_TRIP = "viaje"


def quoted_total(assigned_costs) -> Decimal:
    """Lo que cotizó el proveedor por lo asignado: unit_price × quantity, sin normalizar."""

    return sum((cost.quoted_total for cost in assigned_costs if cost.quoted_total is not None), ZERO)


def freight_for(supplier_key: str, assigned_costs, terms: SupplierTerms | None) -> tuple[Decimal, list[str]]:
    """``(flete neto, marcas)`` de un proveedor para los ítems que la estrategia le asigna."""

    if terms is None or not assigned_costs:
        return ZERO, []

    if terms.freight_included:
        return ZERO, []

    if not terms.has_freight_info:
        # Sin dato del proveedor: 0 y la marca que ya traen los ítems (a cotizar / sin confirmar).
        if any(MARK_FREIGHT_TO_QUOTE in cost.marks for cost in assigned_costs):
            return ZERO, [MARK_FREIGHT_TO_QUOTE]

        if any(MARK_FREIGHT_UNCONFIRMED in cost.marks for cost in assigned_costs):
            return ZERO, [MARK_FREIGHT_UNCONFIRMED]

        return ZERO, []

    cost = terms.freight_cost
    over = terms.freight_free_over
    marks: list[str] = []

    if over is not None:
        total = quoted_total(assigned_costs)

        if total >= over:
            return ZERO, [f"flete sin cargo: pedido de {format_money(total)} supera {format_money(over)}"]

        if cost is None:
            return ZERO, [f"flete a cotizar por debajo de {format_money(over)}"]

        marks.append(f"flete {format_money(cost)}: pedido de {format_money(total)} no llega a {format_money(over)}")

    if cost is None:
        return ZERO, marks

    if terms.freight_basis == BASIS_TRIP:
        marks.append(f"flete {format_money(cost)} por viaje: 1 viaje supuesto")

    return cost, marks


def freight_disbursement(freight: Decimal, terms: SupplierTerms | None, alicuota: Decimal = GENERAL_ALICUOTA) -> Decimal:
    """Lo que sale del bolsillo por el flete: con IVA si el régimen es facturado, tal cual si no."""

    if freight == 0:
        return ZERO

    regime = terms.effective_regime if terms is not None else REGIME_INVOICED

    return freight * (ONE + alicuota / HUNDRED) if regime == REGIME_INVOICED else freight
