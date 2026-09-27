"""Completitud de una cotización registrada por WhatsApp (L5a, ampliada en L5f).

Una sola fuente de verdad para "qué le falta a este ítem": la usan ``record_quote`` (para
``completeness`` y ``missing_fields``), el resumen que vuelve al modelo y el rechazo de
``set_status complete`` cuando todavía hay huecos.

Reglas:
* ``iva_included`` y ``freight_included`` en ``False`` cuentan como respondidos: solo ``None`` falta.
* La validez está respondida si hay ``validity_date`` o una validez textual en ``remarks``
  con el prefijo ``Validez:`` ("48 horas", "hasta fin de mes").
* ``payment_terms`` vacío o solo espacios es falta.
* L5f: el flete también está respondido si la conversación tiene flete registrado
  (``freight_cost`` o ``freight_free_over``). El régimen de facturación es el séptimo campo y
  sale de la conversación; con ``efectivo`` o ``parcial`` el IVA no es obligatorio, y
  ``documented_pct`` nunca lo es. Sin conversación (cotizaciones que no vienen de WhatsApp) el
  régimen no se exige.
"""

from app.features.quote.model import SupplierQuote

REQUIRED = ("unit_price", "iva_included", "freight_included", "lead_time", "payment_terms", "validity_date")

#: L5f: campos de la conversación (condiciones del proveedor) que también hacen falta.
REQUIRED_TERMS = ("billing_regime",)

#: Con estos regímenes el precio no discrimina IVA: no se pide.
REGIMES_WITHOUT_IVA = ("efectivo", "parcial")

#: Etiquetas en castellano para el modelo y para el proveedor.
LABELS = {
    "unit_price": "precio",
    "iva_included": "IVA",
    "freight_included": "flete",
    "lead_time": "plazo",
    "payment_terms": "forma de pago",
    "validity_date": "validez",
    "billing_regime": "régimen de facturación (factura A o efectivo)",
}

VALIDITY_PREFIX = "Validez:"


def has_validity(quote: SupplierQuote) -> bool:
    if quote.validity_date is not None:
        return True

    return (quote.remarks or "").strip().startswith(VALIDITY_PREFIX)


def conversation_has_freight(conversation) -> bool:
    if conversation is None:
        return False

    return any(getattr(conversation, name, None) is not None for name in ("freight_cost", "freight_free_over"))


def _answered(quote: SupplierQuote, field: str, conversation) -> bool:
    if field == "validity_date":
        return has_validity(quote)

    if field == "payment_terms":
        return bool((quote.payment_terms or "").strip())

    if field == "freight_included":
        return quote.freight_included is not None or conversation_has_freight(conversation)

    if field == "iva_included" and getattr(conversation, "billing_regime", None) in REGIMES_WITHOUT_IVA:
        return True

    return getattr(quote, field) is not None


def missing_fields(quote: SupplierQuote, conversation=None) -> list[str]:
    missing = [field for field in REQUIRED if not _answered(quote, field, conversation)]

    if conversation is not None:
        missing += [field for field in REQUIRED_TERMS if not getattr(conversation, field, None)]

    return missing


def assess(quote: SupplierQuote, conversation=None) -> tuple[str, list[str]]:
    """``("complete" | "incomplete", campos faltantes con las claves de REQUIRED y REQUIRED_TERMS)``."""

    missing = missing_fields(quote, conversation)

    return ("complete" if not missing else "incomplete"), missing


def labels_for(fields: list[str]) -> list[str]:
    return [LABELS.get(field, field) for field in fields]
