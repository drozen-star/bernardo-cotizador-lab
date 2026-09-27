"""El "porqué" de cada estrategia (L5d): texto determinístico en castellano con números exactos.

Voz de Bernardo: sin signos de exclamación, sin emojis, sin "dashboard". Al final va una
oración por proveedor elegido cuya cotización tiene una marca que afecta el costo (flete a
cotizar, flete sin confirmar, IVA sin confirmar), hasta ``MAX_CAVEAT_SUPPLIERS``; el resto se
remite a la hoja Matriz. Las faltas de plazo, pago o validez no van acá: no cambian el costo.
"""

from decimal import Decimal

from app.features.batch_comparison.fiscal import MARK_FREIGHT_TO_QUOTE
from app.features.batch_comparison.fiscal import MARK_FREIGHT_UNCONFIRMED
from app.features.batch_comparison.fiscal import MARK_FROM_ATTACHMENT
from app.features.batch_comparison.fiscal import MARK_IVA_UNCONFIRMED
from app.features.batch_comparison.fiscal import format_money
from app.features.batch_comparison.fiscal import format_pct

#: Máximo de proveedores con salvedad en el porqué; el resto se remite a la hoja Matriz.
MAX_CAVEAT_SUPPLIERS = 3


# -------------------------------------------------------------------- texto
def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)

    return ", ".join(names[:-1]) + " y " + names[-1]


def _lead_sentence(result) -> str:
    if result.max_lead_time is None:
        return "Ningún proveedor elegido informó plazo."

    return f"El plazo más largo es de {_plural(result.max_lead_time, 'día', 'días')}."


def _freight_sentence(result) -> str:
    """L5f: el flete que pesa en el total, por proveedor, una vez cada uno."""

    paid = {name: amount for name, amount in result.freight_by_supplier.items() if amount > 0}

    if not paid:
        return ""

    detail = _join([f"{name} {format_money(amount)}" for name, amount in paid.items()])

    if len(paid) == 1:
        return f" El flete suma {format_money(result.total_freight)} ({detail})."

    return f" El flete suma {format_money(result.total_freight)}: {detail}."


def lowest_cost_porque(result) -> str:
    uncovered = [a.item_name for a in result.assignments if not a.covered]

    if result.supplier_count == 0:
        return "No hay cotizaciones comparables en pesos para este pedido, así que no puedo armar la compra."

    text = (
        f"Con la combinación de proveedores de menor costo total, el costo real es "
        f"{format_money(result.total_costo_real)} con {_plural(result.supplier_count, 'proveedor', 'proveedores')}: "
        f"{_join(result.supplier_names)}. El desembolso es {format_money(result.total_desembolso)}."
        f"{_freight_sentence(result)} {_lead_sentence(result)}"
    )

    if uncovered:
        text += f" Quedan sin cotización comparable: {_join(uncovered)}."

    return text


def fewer_suppliers_porque(result, lowest, diff: Decimal, pct: Decimal | None) -> str:
    if result.supplier_count == 0:
        return "No hay cotizaciones comparables en pesos para este pedido, así que no puedo armar la compra."

    totals = (
        f"El costo real total es {format_money(result.total_costo_real)} y el desembolso "
        f"{format_money(result.total_desembolso)}.{_freight_sentence(result)} {_lead_sentence(result)}"
    )

    if result.supplier_count >= lowest.supplier_count:
        text = (
            f"Comprar al menor costo total ya implica {_plural(result.supplier_count, 'proveedor', 'proveedores')}: "
            f"{_join(result.supplier_names)}. No hay una combinación con menos proveedores que cubra los mismos ítems. "
        )
    else:
        fewer = lowest.supplier_count - result.supplier_count
        pct_text = f" ({format_pct(pct)})" if pct is not None else ""
        cost_text = (
            "pagás lo mismo" if diff == 0 else f"pagás {format_money(diff)} más{pct_text}"
        )
        text = (
            f"Comprando a {_plural(result.supplier_count, 'proveedor', 'proveedores')} "
            f"({_join(result.supplier_names)}) en lugar de {lowest.supplier_count} {cost_text}. "
            f"A cambio coordinás {'una entrega menos' if fewer == 1 else f'{fewer} entregas menos'}. "
        )
        saved_freight = lowest.total_freight - result.total_freight

        if saved_freight > 0:
            text += f"Consolidar en {_join(result.supplier_names)} ahorra {format_money(saved_freight)} de flete neto. "

    if result.approximate:
        text += "Con más de 12 proveedores el resultado es aproximado. "

    return text + totals


# ------------------------------------------------------------- salvedades
_CAVEAT_KINDS = ("to_quote", "unconfirmed", "iva", "attachment")


def _caveat_sentence(name: str, entry: dict[str, list[str]]) -> str:
    """Una oración por proveedor sobre las marcas que afectan el costo (flete, IVA, adjunto)."""

    clauses = []

    for kind, verb in (("to_quote", "no incluyó"), ("unconfirmed", "no aclaró")):
        items = entry[kind]

        if items:
            scope = "en ningún ítem" if len(items) == len(entry["assigned"]) > 1 else f"en {_join(items)}"
            clauses.append(f"{verb} el flete {scope}")

    text = f"{name} {' y '.join(clauses)}: el total no lo contempla" if clauses else ""
    iva, attachment = entry["iva"], entry["attachment"]

    if iva:
        if text:
            text += f"; además, no aclaró el IVA en {_join(iva)}: se tomó sin IVA, el caso más caro"
        else:
            text = f"En {_join(iva)}, {name} no aclaró el IVA: se tomó sin IVA, el caso más caro"

    if attachment:
        if text:
            text += f"; además, el precio de {_join(attachment)} se leyó de un adjunto: revisar contra el archivo"
        else:
            text = f"{name}: el precio de {_join(attachment)} se leyó de un adjunto, revisar contra el archivo"

    return text + "."


def cost_caveats(result) -> str:
    """Salvedades de las cotizaciones elegidas cuyo costo puede cambiar: flete, IVA y precio
    leído de un adjunto (L5e). Las faltas de plazo, pago o validez no van acá porque no cambian
    el costo.
    """

    by_supplier: dict[str, dict[str, list[str]]] = {}

    for assignment in result.assignments:
        if assignment.cost is None:
            continue

        entry = by_supplier.setdefault(assignment.supplier_name, {"assigned": [], **{kind: [] for kind in _CAVEAT_KINDS}})
        entry["assigned"].append(assignment.item_name)
        marks = assignment.cost.marks

        if MARK_FREIGHT_TO_QUOTE in marks:
            entry["to_quote"].append(assignment.item_name)
        elif MARK_FREIGHT_UNCONFIRMED in marks:
            entry["unconfirmed"].append(assignment.item_name)

        if MARK_IVA_UNCONFIRMED in marks:
            entry["iva"].append(assignment.item_name)

        if MARK_FROM_ATTACHMENT in marks:
            entry["attachment"].append(assignment.item_name)

    flagged = [
        (name, entry)
        for name, entry in sorted(by_supplier.items(), key=lambda pair: pair[0].lower())
        if any(entry[kind] for kind in _CAVEAT_KINDS)
    ]
    sentences = [_caveat_sentence(name, entry) for name, entry in flagged[:MAX_CAVEAT_SUPPLIERS]]
    extra = len(flagged) - MAX_CAVEAT_SUPPLIERS

    if extra > 0:
        sentences[-1] = sentences[-1][:-1] + f", y {_plural(extra, 'caso', 'casos')} más en la hoja Matriz."

    return " ".join(sentences)


def with_caveats(text: str, result) -> str:
    caveats = cost_caveats(result)

    return f"{text} {caveats}" if caveats else text


