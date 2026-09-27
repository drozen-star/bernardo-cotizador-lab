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
* totales = unitario × cantidad del RFQ; ``shipping_cost`` no nulo se suma al neto total
* moneda distinta de ARS o sin precio → no comparable (aparece en la matriz, no compite)
"""

from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from decimal import InvalidOperation
from decimal import ROUND_HALF_UP

from app.features.whatsapp import quote_completeness
from app.features.whatsapp.attachments.marks import FLAG_FROM_ATTACHMENT

DEFAULT_ALICUOTA = Decimal("21")
BASE_CURRENCY = "ARS"

ONE = Decimal("1")
HUNDRED = Decimal("100")
CENT = Decimal("0.01")

MARK_IVA_UNCONFIRMED = "IVA sin confirmar"
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
def compute_item_cost(quote, rfq, supplier_key: str, alicuota: Decimal = DEFAULT_ALICUOTA) -> ItemCost:
    """Aplica el modelo fiscal a una cotización de un ítem. No toca la base."""

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
    )

    if currency != BASE_CURRENCY:
        cost.marks.append(f"moneda {currency or '?'}, no comparada")
        _append_completeness_marks(cost, quote)

        return cost

    if unit_price is None or unit_price <= 0:
        cost.marks.append(MARK_NO_PRICE)
        _append_completeness_marks(cost, quote)

        return cost

    factor = ONE + cost.alicuota / HUNDRED
    quantity = Decimal(cost.quantity)

    if quote.iva_included is True:
        neto_unit = unit_price / factor
    else:
        # False o None: el precio se toma como neto. Con None se marca, porque es una
        # suposición (la más cara para el comprador), no un dato del proveedor.
        neto_unit = unit_price

        if quote.iva_included is None:
            cost.marks.append(MARK_IVA_UNCONFIRMED)

    neto_total = neto_unit * quantity

    if cost.shipping_cost is not None:
        neto_total += cost.shipping_cost
        cost.marks.append(f"flete {format_money(cost.shipping_cost)} aparte, sumado")
    elif quote.freight_included is False:
        cost.marks.append(MARK_FREIGHT_TO_QUOTE)
    elif quote.freight_included is None:
        cost.marks.append(MARK_FREIGHT_UNCONFIRMED)

    cost.neto_unit = neto_unit
    cost.desembolso_unit = neto_unit * factor
    cost.costo_real_unit = neto_unit  # β = 1: el IVA vuelve como crédito fiscal
    cost.neto_total = neto_total
    cost.desembolso_total = neto_total * factor
    cost.costo_real_total = neto_total
    cost.comparable = True

    _append_completeness_marks(cost, quote)

    return cost


def _append_completeness_marks(cost: ItemCost, quote) -> None:
    if cost.from_attachment:
        cost.marks.append(MARK_FROM_ATTACHMENT)

    missing = quote_completeness.missing_fields(quote)

    for name in COMPLETENESS_MARKED:
        if name in missing:
            cost.marks.append(f"falta {quote_completeness.LABELS[name]}")
