"""Dos estrategias de compra sobre el costo real (β=1) de cada ítem (L5d).

* **Menor costo por ítem**: cada ítem al proveedor con menor costo real total. Empate por
  precio → menor plazo → nombre del proveedor. Ítem sin cotización comparable → "sin
  cotización".
* **Menos proveedores**: el mínimo k de proveedores que cubre todos los ítems cubribles y,
  entre los subconjuntos de tamaño k, el de menor total (cada ítem al más barato dentro del
  subconjunto). Fuerza bruta hasta ``BRUTE_FORCE_MAX_SUPPLIERS``; con más, greedy y marca
  "resultado aproximado".

El "porqué" de cada estrategia vive en ``porque.py``: determinístico, en castellano y con los
números exactos del cálculo (voz de Bernardo: sin signos de exclamación, sin emojis, sin "dashboard").
"""

from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from itertools import combinations

from app.features.batch_comparison import porque
from app.features.batch_comparison.fiscal import HUNDRED
from app.features.batch_comparison.fiscal import ItemCost

STRATEGY_LOWEST_COST = "menor_costo_por_item"
STRATEGY_FEWER_SUPPLIERS = "menos_proveedores"

TITLES = {
    STRATEGY_LOWEST_COST: "Menor costo por ítem",
    STRATEGY_FEWER_SUPPLIERS: "Menos proveedores",
}

MARK_APPROXIMATE = "resultado aproximado"
MARK_NO_QUOTE = "sin cotización"
BRUTE_FORCE_MAX_SUPPLIERS = 12


#: Plazo que ordena último a quien no informó plazo (solo para desempatar).
_NO_LEAD_TIME = 10**9

#: rfq_id → supplier_key → costo comparable.
CostMatrix = dict[int, dict[str, ItemCost]]


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
    approximate: bool = False
    porque: str = ""

    @property
    def supplier_count(self) -> int:
        return len(self.supplier_names)


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


def _assemble(key: str, items, chosen: dict[int, ItemCost | None], names: dict[str, str], approximate=False) -> StrategyResult:
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
        approximate=approximate,
    )


# --------------------------------------------------------------- estrategias
def lowest_cost_strategy(items, matrix: CostMatrix, names: dict[str, str]) -> StrategyResult:
    chosen = {rfq.id: _cheapest(matrix.get(rfq.id, {}), matrix.get(rfq.id, {}), names) for rfq in items}

    return _assemble(STRATEGY_LOWEST_COST, items, chosen, names)


def _subset_total(coverable, matrix: CostMatrix, subset, names) -> tuple[Decimal, dict[int, ItemCost]] | None:
    chosen: dict[int, ItemCost] = {}
    total = Decimal(0)

    for rfq in coverable:
        best = _cheapest(matrix[rfq.id], subset, names)

        if best is None:
            return None  # este subconjunto no cubre el ítem

        chosen[rfq.id] = best
        total += best.costo_real_total

    return total, chosen


def _subset_order(total: Decimal, chosen: dict[int, ItemCost], names) -> tuple:
    leads = [cost.lead_time for cost in chosen.values() if cost.lead_time is not None]
    max_lead = max(leads) if leads else _NO_LEAD_TIME
    supplier_names = tuple(sorted(names.get(c.supplier_key, c.supplier_key).lower() for c in chosen.values()))

    return (total, max_lead, supplier_names)


def _brute_force(coverable, matrix, supplier_keys, names) -> dict[int, ItemCost]:
    for size in range(1, len(supplier_keys) + 1):
        best = None

        for subset in combinations(supplier_keys, size):
            result = _subset_total(coverable, matrix, set(subset), names)

            if result is None:
                continue

            total, chosen = result
            order = _subset_order(total, chosen, names)

            if best is None or order < best[0]:
                best = (order, chosen)

        if best is not None:
            return best[1]

    return {}


def _greedy(coverable, matrix, supplier_keys, names) -> dict[int, ItemCost]:
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

    result = _subset_total(coverable, matrix, picked, names)

    return result[1] if result else {}


def fewer_suppliers_strategy(items, matrix: CostMatrix, names: dict[str, str]) -> StrategyResult:
    coverable = [rfq for rfq in items if matrix.get(rfq.id)]
    supplier_keys = sorted(
        {key for rfq in coverable for key in matrix[rfq.id]}, key=lambda k: (names.get(k, k).lower(), k)
    )

    if not coverable:
        return _assemble(STRATEGY_FEWER_SUPPLIERS, items, {}, names)

    approximate = len(supplier_keys) > BRUTE_FORCE_MAX_SUPPLIERS
    search = _greedy if approximate else _brute_force
    chosen = search(coverable, matrix, supplier_keys, names)

    return _assemble(STRATEGY_FEWER_SUPPLIERS, items, chosen, names, approximate=approximate)


# ---------------------------------------------------------------- comparar
def compare(items, costs: list[ItemCost], names: dict[str, str]) -> ComparisonResult:
    matrix = build_matrix(costs)
    lowest = lowest_cost_strategy(items, matrix, names)
    fewer = fewer_suppliers_strategy(items, matrix, names)

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
