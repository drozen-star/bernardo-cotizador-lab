"""Modelo fiscal del comparativo (L5d): de precio cotizado a neto, desembolso y costo real.

Sigue la sesión de diseño del comparador (secciones 2, 4, 5 y 9): alícuota de IVA por
ítem, nunca por documento; el comprador ve desembolso y costo real lado a lado; nada se
excluye por un criterio, todo se marca. Toda la aritmética es ``Decimal``; el redondeo a
dos decimales ocurre solo al presentar (``money`` / ``format_money``).

Reglas por cotización y por ítem:
* ``iva_included`` True  → neto_unit = precio / (1 + alícuota/100)
* ``iva_included`` False → neto_unit = precio
* ``iva_included`` None  → neto_unit = precio (se asume SIN IVA, el caso más caro) + marca
* desembolso_unit = neto_unit × (1 + alícuota/100); costo_real_unit (β=1) = neto_unit
* totales = unitario × cantidad del RFQ; el flete va por proveedor (``freight.py``, L5f), nunca acá
* régimen efectivo o parcial (``regime.py``, L5f): el precio es desembolso y el crédito de IVA
  solo alcanza a la parte facturada
* moneda distinta de ARS o sin precio → no comparable (aparece en la matriz, no compite)
"""

from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from decimal import InvalidOperation
from decimal import ROUND_HALF_UP

from app.features.batch_comparison import regime as regimes
from app.features.batch_comparison.regime import MARK_IVA_UNCONFIRMED  # noqa: F401 - la usan porque.py y los tests
from app.features.batch_comparison.regime import SupplierTerms
from app.features.whatsapp import quote_completeness
from app.features.whatsapp.attachments.marks import FLAG_FROM_ATTACHMENT

DEFAULT_ALICUOTA = Decimal("21")
BASE_CURRENCY = "ARS"

ONE = Decimal("1")
HUNDRED = Decimal("100")
CENT = Decimal("0.01")

MARK_FREIGHT_TO_QUOTE = "flete a cotizar"
MARK_FREIGHT_UNCONFIRMED = "flete sin confirmar"
MARK_NO_PRICE = "sin precio"
#: L5e: el precio salió de un PDF o una foto transcriptos (``risk_flags`` de la cotización).
MARK_FROM_ATTACHMENT = "precio leído de un adjunto"

#: Cómo se muestra ``iva_included`` en la matriz.
IVA_LABELS = {True: "incluido", False: "no incluido", None: "sin confirmar"}

#: Campos de completitud que se marcan como "falta <campo>" (IVA y flete tienen marca propia).
COMPLETENESS_MARKED = ("lead_time", "payment_terms", "validity_date")


@dataclass
class ItemCost:
    """Una cotización de un proveedor para un ítem, ya pasada por el modelo fiscal."""

    rfq_id: int
    supplier_key: str
    quote_id: int
    currency: str
    quantity: int
    alicuota: Decimal
    unit_price: Decimal | None
    iva_included: bool | None
    comparable: bool = False
    neto_unit: Decimal | None = None
    desembolso_unit: Decimal | None = None
    costo_real_unit: Decimal | None = None
    neto_total: Decimal | None = None
    desembolso_total: Decimal | None = None
    costo_real_total: Decimal | None = None
    shipping_cost: Decimal | None = None
    freight_included: bool | None = None
    lead_time: int | None = None
    payment_terms: str | None = None
    validity: str | None = None
    from_attachment: bool = False
    #: L5f: régimen del proveedor (de la conversación) y lo cotizado tal cual (para el umbral de flete).
    billing_regime: str | None = None
    documented_pct: Decimal | None = None
    quoted_total: Decimal | None = None
    marks: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ helpers
def to_decimal(value) -> Decimal:
    """Decimal exacto desde lo que venga de la base (Decimal, int o str). Nunca float directo."""

    if isinstance(value, Decimal):
        return value

    return Decimal(str(value))


def money(value: Decimal) -> Decimal:
    """Dos decimales, solo para presentar."""

    return to_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def format_money(value: Decimal) -> str:
    """``$ 41.200,00``: miles con punto, decimales con coma (uso argentino)."""

    quantized = money(value)
    sign = "-" if quantized < 0 else ""
    whole, fraction = f"{abs(quantized):.2f}".split(".")
    whole = f"{int(whole):,}".replace(",", ".")

    return f"{sign}$ {whole},{fraction}"


def format_pct(value: Decimal) -> str:
    """``2,1%`` con un decimal."""

    quantized = to_decimal(value).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)

    return f"{quantized}".replace(".", ",") + "%"


def validity_text(quote) -> str | None:
    """La validez como texto: la fecha, o lo que siga a ``Validez:`` en remarks."""

    if getattr(quote, "validity_date", None) is not None:
        return quote.validity_date.isoformat()

    remarks = (getattr(quote, "remarks", None) or "").strip()

    if remarks.startswith(quote_completeness.VALIDITY_PREFIX):
        return remarks[len(quote_completeness.VALIDITY_PREFIX):].strip() or None

    return None


# ---------------------------------------------------------------- alícuotas
def parse_alicuotas(raw: str | None) -> dict[int, Decimal]:
    """``"12:10.5,13:21"`` → ``{12: Decimal("10.5"), 13: Decimal("21")}``.

    El separador entre pares es la coma, así que el decimal va con punto. Cualquier
    forma inválida levanta ``ValueError`` con un mensaje en castellano.
    """

    overrides: dict[int, Decimal] = {}

    if raw is None or not raw.strip():
        return overrides

    for chunk in raw.split(","):
        pair = chunk.strip()

        if not pair:
            continue

        rfq_part, sep, pct_part = pair.partition(":")

        if not sep or not rfq_part.strip().isdigit():
            raise ValueError(f"alícuota inválida: '{pair}' (formato esperado <rfq_id>:<pct>)")

        try:
            pct = Decimal(pct_part.strip())
        except InvalidOperation:
            raise ValueError(f"alícuota inválida: '{pair}' (el porcentaje no es un número)") from None

        if not pct.is_finite() or pct < 0 or pct > HUNDRED:
            raise ValueError(f"alícuota inválida: '{pair}' (el porcentaje va de 0 a 100)")

        overrides[int(rfq_part)] = pct

    return overrides


def alicuota_for(rfq_id: int, overrides: dict[int, Decimal] | None) -> Decimal:
    if overrides and rfq_id in overrides:
        return overrides[rfq_id]

    return DEFAULT_ALICUOTA


# -------------------------------------------------------------------- costo
def compute_item_cost(
    quote, rfq, supplier_key: str, alicuota: Decimal = DEFAULT_ALICUOTA, terms: SupplierTerms | None = None
) -> ItemCost:
    """Aplica el modelo fiscal a una cotización de un ítem. No toca la base.

    L5f: el régimen del proveedor (``terms``) decide cómo se pasa de precio a neto, desembolso y
    costo real (``regime.py``). El flete ya no se suma acá: va por proveedor en ``freight.py``.
    """

    unit_price = to_decimal(quote.unit_price) if quote.unit_price is not None else None
    currency = (quote.currency or "").strip().upper()

    cost = ItemCost(
        rfq_id=rfq.id,
        supplier_key=supplier_key,
        quote_id=quote.id,
        currency=currency,
        quantity=int(rfq.quantity),
        alicuota=to_decimal(alicuota),
        unit_price=unit_price,
        iva_included=quote.iva_included,
        freight_included=quote.freight_included,
        shipping_cost=to_decimal(quote.shipping_cost) if quote.shipping_cost is not None else None,
        lead_time=quote.lead_time,
        payment_terms=(quote.payment_terms or "").strip() or None,
        validity=validity_text(quote),
        from_attachment=FLAG_FROM_ATTACHMENT in (getattr(quote, "risk_flags", None) or []),
        billing_regime=regimes.regime_of(terms),
        documented_pct=terms.documented_pct if terms is not None else None,
    )

    if currency != BASE_CURRENCY:
        cost.marks.append(f"moneda {currency or '?'}, no comparada")
        _append_completeness_marks(cost, quote, terms)

        return cost

    if unit_price is None or unit_price <= 0:
        cost.marks.append(MARK_NO_PRICE)
        _append_completeness_marks(cost, quote, terms)

        return cost

    quantity = Decimal(cost.quantity)
    result = regimes.apply_regime(unit_price, cost.alicuota, quote.iva_included, terms)
    cost.marks.extend(result.marks)

    # Sin dato de flete del proveedor para todo el pedido, vale lo que dijo por ítem.
    if terms is None or not terms.has_freight_info:
        if quote.freight_included is False:
            cost.marks.append(MARK_FREIGHT_TO_QUOTE)
        elif quote.freight_included is None:
            cost.marks.append(MARK_FREIGHT_UNCONFIRMED)

    cost.neto_unit, cost.desembolso_unit, cost.costo_real_unit = result.neto_unit, result.desembolso_unit, result.costo_real_unit
    cost.neto_total, cost.desembolso_total, cost.costo_real_total = result.totals(quantity)
    cost.quoted_total = unit_price * quantity
    cost.comparable = True

    _append_completeness_marks(cost, quote, terms)

    return cost


def _append_completeness_marks(cost: ItemCost, quote, terms: SupplierTerms | None = None) -> None:
    if cost.from_attachment:
        cost.marks.append(MARK_FROM_ATTACHMENT)

    missing = quote_completeness.missing_fields(quote, terms)

    for name in COMPLETENESS_MARKED:
        if name in missing:
            cost.marks.append(f"falta {quote_completeness.LABELS[name]}")
