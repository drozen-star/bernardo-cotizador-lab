"""Dos estrategias de compra sobre el costo real (β=1) más el flete por proveedor (L5d, L5f).

* **Menor costo total**: el subconjunto de proveedores que cubre los ítems cubribles con menor
  total (cada ítem al más barato dentro del subconjunto, más el flete de cada proveedor usado,
  una vez). Fuerza bruta sobre todos los subconjuntos hasta ``BRUTE_FORCE_MAX_SUPPLIERS``; con
  más, el más barato por ítem más el flete, con marca "resultado aproximado".
* **Menos proveedores**: el mínimo k que cubre los ítems cubribles y, dentro de k, el menor
  total (con flete). Con más de 12 proveedores, greedy y marca "resultado aproximado".

Empates: precio → menor plazo → nombre del proveedor. Ítem sin cotización comparable → "sin
cotización". Si el subconjunto mezcla regímenes (facturado con efectivo o parcial), la
estrategia lleva la marca de la regla de oro fiscal. El "porqué" vive en ``porque.py``.
"""

from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from itertools import combinations

from app.features.batch_comparison import freight as freights
from app.features.batch_comparison import porque
from app.features.batch_comparison.fiscal import HUNDRED
from app.features.batch_comparison.fiscal import ItemCost
from app.features.batch_comparison.regime import REGIME_INVOICED
from app.features.batch_comparison.regime import SupplierTerms

STRATEGY_LOWEST_COST = "menor_costo_total"
STRATEGY_FEWER_SUPPLIERS = "menos_proveedores"

TITLES = {
    STRATEGY_LOWEST_COST: "Menor costo total",
    STRATEGY_FEWER_SUPPLIERS: "Menos proveedores",
}

MARK_APPROXIMATE = "resultado aproximado"
MARK_NO_QUOTE = "sin cotización"
MARK_MIXED_REGIMES = "mezcla facturado y efectivo: comparar con cuidado"
BRUTE_FORCE_MAX_SUPPLIERS = 12

#: Plazo que ordena último a quien no informó plazo (solo para desempatar).
_NO_LEAD_TIME = 10**9

#: rfq_id → supplier_key → costo comparable.
CostMatrix = dict[int, dict[str, ItemCost]]
#: supplier_key → condiciones del proveedor (None si no vino de WhatsApp).
TermsMap = dict[str, SupplierTerms | None]


@dataclass
class Assignment:
    rfq_id: int
    item_name: str
    supplier_key: str | None
    supplier_name: str | None
    cost: ItemCost | None

    @property
    def covered(self) -> bool:
        return self.cost is not None


@dataclass
class StrategyResult:
    key: str
    title: str
    assignments: list[Assignment]
    total_costo_real: Decimal
    total_desembolso: Decimal
    supplier_names: list[str]
    max_lead_time: int | None
    marks: list[str]
    #: L5f: flete neto y desembolso del flete por proveedor (por nombre), una vez cada uno.
    freight_by_supplier: dict[str, Decimal] = field(default_factory=dict)
    freight_desembolso_by_supplier: dict[str, Decimal] = field(default_factory=dict)
    approximate: bool = False
    porque: str = ""

    @property
    def supplier_count(self) -> int:
        return len(self.supplier_names)

    @property
    def total_freight(self) -> Decimal:
        return sum(self.freight_by_supplier.values(), Decimal(0))


@dataclass
class ComparisonResult:
    lowest_cost: StrategyResult
    fewer_suppliers: StrategyResult
    difference_amount: Decimal
    difference_pct: Decimal | None
    strategies: list[StrategyResult] = field(default_factory=list)


# ---------------------------------------------------------------- utilidades
def build_matrix(costs: list[ItemCost]) -> CostMatrix:
    matrix: CostMatrix = {}

    for cost in costs:
        if cost.comparable and cost.costo_real_total is not None:
            matrix.setdefault(cost.rfq_id, {})[cost.supplier_key] = cost

    return matrix


def rank_key(cost: ItemCost, names: dict[str, str]) -> tuple:
    lead = cost.lead_time if cost.lead_time is not None else _NO_LEAD_TIME

    return (cost.costo_real_total, lead, names.get(cost.supplier_key, cost.supplier_key).lower())


def _cheapest(options: dict[str, ItemCost], allowed, names: dict[str, str]) -> ItemCost | None:
    candidates = [cost for key, cost in options.items() if key in allowed]

    if not candidates:
        return None

    return min(candidates, key=lambda cost: rank_key(cost, names))


def _freight(chosen: dict[int, ItemCost], names, terms_map: TermsMap) -> tuple[dict[str, Decimal], dict[str, Decimal], list[str]]:
    """Flete neto y desembolso por proveedor usado (por nombre), más las marcas "<Proveedor>: ...". """

    by_key: dict[str, list[ItemCost]] = {}

    for cost in chosen.values():
        by_key.setdefault(cost.supplier_key, []).append(cost)

    net: dict[str, Decimal] = {}
    disbursed: dict[str, Decimal] = {}
    marks: list[str] = []

    for key in sorted(by_key, key=lambda k: names.get(k, k).lower()):
        terms = terms_map.get(key)
        amount, freight_marks = freights.freight_for(key, by_key[key], terms)
        name = names.get(key, key)
        net[name] = amount
        disbursed[name] = freights.freight_disbursement(amount, terms)
        marks.extend(f"{name}: {mark}" for mark in freight_marks)

    return net, disbursed, marks


def _assemble(key: str, items, chosen: dict[int, ItemCost | None], names, terms_map: TermsMap, approximate=False) -> StrategyResult:
    assignments: list[Assignment] = []
    total_real = Decimal(0)
    total_desembolso = Decimal(0)
    supplier_keys: set[str] = set()
    leads: list[int] = []
    marks: list[str] = []

    for rfq in items:
        cost = chosen.get(rfq.id)

        if cost is None:
            assignments.append(Assignment(rfq.id, rfq.item_name, None, None, None))
            marks.append(f"{rfq.item_name}: {MARK_NO_QUOTE}")
            continue

        name = names.get(cost.supplier_key, cost.supplier_key)
        assignments.append(Assignment(rfq.id, rfq.item_name, cost.supplier_key, name, cost))
        total_real += cost.costo_real_total
        total_desembolso += cost.desembolso_total
        supplier_keys.add(cost.supplier_key)

        if cost.lead_time is not None:
            leads.append(cost.lead_time)

        marks.extend(f"{rfq.item_name}: {mark}" for mark in cost.marks)

    freight_net, freight_disbursed, freight_marks = _freight({k: v for k, v in chosen.items() if v is not None}, names, terms_map)
    total_real += sum(freight_net.values(), Decimal(0))
    total_desembolso += sum(freight_disbursed.values(), Decimal(0))
    marks.extend(freight_marks)

    regimes = {(terms_map.get(k).effective_regime if terms_map.get(k) else REGIME_INVOICED) for k in supplier_keys}

    if len(regimes) > 1:
        marks.append(MARK_MIXED_REGIMES)  # regla de oro fiscal: regímenes distintos no se comparan sin más

    if approximate:
        marks.append(MARK_APPROXIMATE)

    return StrategyResult(
        key=key,
        title=TITLES[key],
        assignments=assignments,
        total_costo_real=total_real,
        total_desembolso=total_desembolso,
        supplier_names=sorted((names.get(k, k) for k in supplier_keys), key=str.lower),
        max_lead_time=max(leads) if leads else None,
        marks=marks,
        freight_by_supplier=freight_net,
        freight_desembolso_by_supplier=freight_disbursed,
        approximate=approximate,
    )


# --------------------------------------------------------------- búsqueda
def _subset_total(coverable, matrix: CostMatrix, subset, names, terms_map: TermsMap) -> tuple[Decimal, dict[int, ItemCost]] | None:
    """Total (ítems + flete por proveedor usado) del subconjunto, o None si no cubre todo."""

    chosen: dict[int, ItemCost] = {}
    total = Decimal(0)

    for rfq in coverable:
        best = _cheapest(matrix[rfq.id], subset, names)

        if best is None:
            return None  # este subconjunto no cubre el ítem

        chosen[rfq.id] = best
        total += best.costo_real_total

    freight_net, _, _ = _freight(chosen, names, terms_map)

    return total + sum(freight_net.values(), Decimal(0)), chosen


def _subset_order(total: Decimal, chosen: dict[int, ItemCost], names) -> tuple:
    leads = [cost.lead_time for cost in chosen.values() if cost.lead_time is not None]
    max_lead = max(leads) if leads else _NO_LEAD_TIME
    supplier_names = tuple(sorted({names.get(c.supplier_key, c.supplier_key).lower() for c in chosen.values()}))

    return (total, len(supplier_names), max_lead, supplier_names)


def _best_subset(coverable, matrix, supplier_keys, names, terms_map, *, minimal_k: bool) -> dict[int, ItemCost]:
    """Fuerza bruta. ``minimal_k``: se queda en el primer tamaño que cubre (menos proveedores)."""

    best = None

    for size in range(1, len(supplier_keys) + 1):
        for subset in combinations(supplier_keys, size):
            result = _subset_total(coverable, matrix, set(subset), names, terms_map)

            if result is None:
                continue

            total, chosen = result
            order = _subset_order(total, chosen, names)

            if best is None or order < best[0]:
                best = (order, chosen)

        if minimal_k and best is not None:
            return best[1]

    return best[1] if best else {}


def _greedy(coverable, matrix, supplier_keys, names, terms_map) -> dict[int, ItemCost]:
    uncovered = {rfq.id for rfq in coverable}
    picked: set[str] = set()

    while uncovered:
        def score(key):
            covers = [matrix[rfq_id][key] for rfq_id in uncovered if key in matrix[rfq_id]]
            cost_sum = sum((c.costo_real_total for c in covers), Decimal(0))

            return (-len(covers), cost_sum, names.get(key, key).lower())

        candidates = [key for key in supplier_keys if key not in picked]
        chosen_key = min(candidates, key=score)
        picked.add(chosen_key)
        uncovered -= {rfq_id for rfq_id in uncovered if chosen_key in matrix[rfq_id]}

    result = _subset_total(coverable, matrix, picked, names, terms_map)

    return result[1] if result else {}


def _coverable(items, matrix: CostMatrix, names):
    coverable = [rfq for rfq in items if matrix.get(rfq.id)]
    supplier_keys = sorted({key for rfq in coverable for key in matrix[rfq.id]}, key=lambda k: (names.get(k, k).lower(), k))

    return coverable, supplier_keys


# --------------------------------------------------------------- estrategias
def lowest_cost_strategy(items, matrix: CostMatrix, names, terms_map: TermsMap) -> StrategyResult:
    coverable, supplier_keys = _coverable(items, matrix, names)

    if not coverable:
        return _assemble(STRATEGY_LOWEST_COST, items, {}, names, terms_map)

    if len(supplier_keys) > BRUTE_FORCE_MAX_SUPPLIERS:
        chosen = {rfq.id: _cheapest(matrix[rfq.id], matrix[rfq.id], names) for rfq in coverable}
        return _assemble(STRATEGY_LOWEST_COST, items, chosen, names, terms_map, approximate=True)

    chosen = _best_subset(coverable, matrix, supplier_keys, names, terms_map, minimal_k=False)

    return _assemble(STRATEGY_LOWEST_COST, items, chosen, names, terms_map)


def fewer_suppliers_strategy(items, matrix: CostMatrix, names, terms_map: TermsMap) -> StrategyResult:
    coverable, supplier_keys = _coverable(items, matrix, names)

    if not coverable:
        return _assemble(STRATEGY_FEWER_SUPPLIERS, items, {}, names, terms_map)

    approximate = len(supplier_keys) > BRUTE_FORCE_MAX_SUPPLIERS

    if approximate:
        chosen = _greedy(coverable, matrix, supplier_keys, names, terms_map)
    else:
        chosen = _best_subset(coverable, matrix, supplier_keys, names, terms_map, minimal_k=True)

    return _assemble(STRATEGY_FEWER_SUPPLIERS, items, chosen, names, terms_map, approximate=approximate)


# ---------------------------------------------------------------- comparar
def compare(items, costs: list[ItemCost], names: dict[str, str], terms_map: TermsMap | None = None) -> ComparisonResult:
    terms_map = terms_map or {}
    matrix = build_matrix(costs)
    lowest = lowest_cost_strategy(items, matrix, names, terms_map)
    fewer = fewer_suppliers_strategy(items, matrix, names, terms_map)

    diff = fewer.total_costo_real - lowest.total_costo_real
    pct = (diff / lowest.total_costo_real * HUNDRED) if lowest.total_costo_real > 0 else None

    lowest.porque = porque.with_caveats(porque.lowest_cost_porque(lowest), lowest)
    fewer.porque = porque.with_caveats(porque.fewer_suppliers_porque(fewer, lowest, diff, pct), fewer)

    return ComparisonResult(
        lowest_cost=lowest,
        fewer_suppliers=fewer,
        difference_amount=diff,
        difference_pct=pct,
        strategies=[lowest, fewer],
    )
