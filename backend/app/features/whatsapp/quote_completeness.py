"""Completitud de una cotización registrada por WhatsApp (L5a).

Una sola fuente de verdad para "qué le falta a este ítem": la usan ``record_quote`` (para
``completeness`` y ``missing_fields``), el resumen que vuelve al modelo y el rechazo de
``set_status complete`` cuando todavía hay huecos.

Reglas:
* ``iva_included`` y ``freight_included`` en ``False`` cuentan como respondidos: solo ``None`` falta.
* La validez está respondida si hay ``validity_date`` o una validez textual en ``remarks``
  con el prefijo ``Validez:`` ("48 horas", "hasta fin de mes").
* ``payment_terms`` vacío o solo espacios es falta.
"""

from app.features.quote.model import SupplierQuote

REQUIRED = ("unit_price", "iva_included", "freight_included", "lead_time", "payment_terms", "validity_date")

#: Etiquetas en castellano para el modelo y para el proveedor.
LABELS = {
    "unit_price": "precio",
    "iva_included": "IVA",
    "freight_included": "flete",
    "lead_time": "plazo",
    "payment_terms": "forma de pago",
    "validity_date": "validez",
}

VALIDITY_PREFIX = "Validez:"


def has_validity(quote: SupplierQuote) -> bool:
    if quote.validity_date is not None:
        return True

    return (quote.remarks or "").strip().startswith(VALIDITY_PREFIX)


def _answered(quote: SupplierQuote, field: str) -> bool:
    if field == "validity_date":
        return has_validity(quote)

    if field == "payment_terms":
        return bool((quote.payment_terms or "").strip())

    return getattr(quote, field) is not None


def missing_fields(quote: SupplierQuote) -> list[str]:
    return [field for field in REQUIRED if not _answered(quote, field)]


def assess(quote: SupplierQuote) -> tuple[str, list[str]]:
    """``("complete" | "incomplete", campos faltantes con las claves de REQUIRED)``."""

    missing = missing_fields(quote)

    return ("complete" if not missing else "incomplete"), missing


def labels_for(fields: list[str]) -> list[str]:
    return [LABELS.get(field, field) for field in fields]
