"""Ejecutor de ``record_terms`` (L5f): las condiciones del proveedor para todo el pedido.

Régimen de facturación y flete viven en ``whatsapp_conversations`` (proveedor × pedido), no en
cada ``supplier_quotes``. Misma disciplina que ``record_quote``: evidencia literal y continua
(sin ``...``, hasta 300 caracteres) y "null conserva" en las correcciones. El porcentaje
facturado se registra solo si el proveedor lo dijo: nunca se pide (decisión D1).

``check_evidence_shape`` también la usa ``record_quote``: una evidencia pegada con puntos
suspensivos o más larga que una línea es la causa principal del gasto de salida del agente.
"""

from decimal import Decimal
from decimal import InvalidOperation

from app.features.whatsapp import quote_writer
from app.features.whatsapp.loop import ToolOutcome
from app.features.whatsapp.model import BILLING_REGIMES
from app.features.whatsapp.model import FREIGHT_BASES
from app.features.whatsapp.quote_completeness import LABELS
from app.features.whatsapp.tools import EVIDENCE_MAX_LENGTH

ELLIPSES = ("...", "…")


def check_evidence_shape(evidence: str) -> str | None:
    """Motivo de rechazo si la evidencia no es una línea literal y continua; None si está bien."""

    if len(evidence) > EVIDENCE_MAX_LENGTH:
        return (
            f"Rechazado: evidence tiene {len(evidence)} caracteres y el máximo es {EVIDENCE_MAX_LENGTH}. "
            "Citá solo la línea del precio de ese ítem, no el mensaje entero."
        )

    if any(mark in evidence for mark in ELLIPSES):
        return (
            "Rechazado: evidence une partes con '...'. Citá un solo fragmento continuo tal como lo "
            "escribió el proveedor; las condiciones comunes van con record_terms."
        )

    return None


def _decimal(value, *, minimum: Decimal | None = None, maximum: Decimal | None = None) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None

    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None

    if (minimum is not None and number < minimum) or (maximum is not None and number > maximum):
        return None

    return number


def _propagate_freight(executor, value: bool, *, only_missing: bool) -> None:
    """Refleja el flete del proveedor en sus supplier_quotes, para que la matriz sea coherente."""

    for quote in executor.conversation.quotes:
        if not only_missing or quote.freight_included is None:
            quote.freight_included = value


def plain_number(value: Decimal) -> str:
    """``45000.00`` -> ``45000``, ``12.50`` -> ``12.5``: sin ceros de más para el modelo."""

    return format(Decimal(value).normalize(), "f")


def terms_summary(conversation) -> str:
    """Qué quedó registrado de las condiciones y qué falta (para el modelo y el prompt)."""

    parts = []

    if conversation.billing_regime:
        regime = f"régimen {conversation.billing_regime}"

        if conversation.documented_pct is not None:
            regime += f" ({plain_number(conversation.documented_pct)}% facturado)"

        parts.append(regime)

    if conversation.freight_cost is not None:
        if conversation.freight_cost == 0 and conversation.freight_free_over is None:
            parts.append("flete incluido")
        else:
            parts.append(f"flete {plain_number(conversation.freight_cost)} por {conversation.freight_basis or 'pedido'}")

    if conversation.freight_free_over is not None:
        parts.append(f"sin cargo arriba de {plain_number(conversation.freight_free_over)}")

    missing = []

    if not conversation.billing_regime:
        missing.append(LABELS["billing_regime"])

    if conversation.freight_cost is None and conversation.freight_free_over is None:
        missing.append("flete del pedido")

    return f"Condiciones registradas: {', '.join(parts) or 'nada'}. Falta: {', '.join(missing) or 'nada'}."


def record_terms(executor, tool_input: dict) -> ToolOutcome:
    """``executor`` es el QuoteToolExecutor del turno: aporta db, conversación y mensajes."""

    evidence = str(tool_input.get("evidence") or "").strip()
    problem = check_evidence_shape(evidence)

    if problem:
        return ToolOutcome(content=problem, ok=False)

    if not quote_writer.evidence_is_literal(evidence, executor.inbound_bodies):
        return ToolOutcome(
            content=(
                "Rechazado: evidence no aparece literalmente en ningún mensaje del proveedor. "
                "Copiá el fragmento exacto que escribió, sin parafrasear."
            ),
            ok=False,
        )

    regime = tool_input.get("billing_regime")

    if regime is not None and regime not in BILLING_REGIMES:
        return ToolOutcome(content=f"billing_regime inválido. Usá uno de: {', '.join(BILLING_REGIMES)} o null.", ok=False)

    basis = tool_input.get("freight_basis")

    if basis is not None and basis not in FREIGHT_BASES:
        return ToolOutcome(content=f"freight_basis inválido. Usá uno de: {', '.join(FREIGHT_BASES)} o null.", ok=False)

    conversation = executor.conversation
    documented_pct = _decimal(tool_input.get("documented_pct"), minimum=Decimal(0), maximum=Decimal(100))
    freight_cost = _decimal(tool_input.get("freight_cost"), minimum=Decimal(0))
    freight_free_over = _decimal(tool_input.get("freight_free_over"), minimum=Decimal(0))
    freight_included = tool_input.get("freight_included")

    # Null conserva: solo se pisa lo que el proveedor dijo en este mensaje.
    if regime is not None:
        conversation.billing_regime = regime

    if documented_pct is not None:
        conversation.documented_pct = documented_pct

    if freight_cost is not None:
        conversation.freight_cost = freight_cost

    if basis is not None:
        conversation.freight_basis = basis

    if freight_free_over is not None:
        conversation.freight_free_over = freight_free_over

    if freight_included is True:
        conversation.freight_cost = Decimal(0)
        conversation.freight_basis = "pedido"
        _propagate_freight(executor, True, only_missing=False)
    elif freight_cost is not None or freight_free_over is not None:
        _propagate_freight(executor, False, only_missing=True)

    conversation.terms_evidence = evidence
    executor.db.flush()

    return ToolOutcome(content=terms_summary(conversation))
