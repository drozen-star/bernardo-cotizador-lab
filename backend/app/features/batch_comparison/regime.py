"""Régimen de facturación del proveedor → base documentada, desembolso y costo real (L5f).

Fórmula del diseño del comparador (sección 4): ``costo_real = desembolso − base_documentada ×
alícuota × β``, con β = 1 (el comprador computa el IVA). Por régimen del proveedor:

| régimen             | desembolso                     | costo real                                      |
| facturado (o null)  | neto × (1 + a)                 | neto                                            |
| efectivo            | precio × cantidad, tal cual    | = desembolso (sin crédito de IVA)               |
| parcial con p %     | precio × cantidad              | desembolso − (desembolso × p/100) / (1 + a) × a |
| parcial sin %       | precio × cantidad              | = desembolso, con marca                         |

Con efectivo o parcial ``iva_included`` se ignora: el precio es lo que sale del bolsillo.
Toda la aritmética es ``Decimal``.
"""

from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal

ONE = Decimal("1")
HUNDRED = Decimal("100")

REGIME_INVOICED = "facturado"
REGIME_CASH = "efectivo"
REGIME_PARTIAL = "parcial"

MARK_REGIME_UNCONFIRMED = "régimen sin confirmar, se asume facturado"
MARK_CASH = "en efectivo, sin factura"
MARK_PARTIAL_UNKNOWN = "factura una parte, % sin dato: costo real sin crédito de IVA"
MARK_IVA_UNCONFIRMED = "IVA sin confirmar"


@dataclass(frozen=True)
class SupplierTerms:
    """Condiciones del proveedor para todo el pedido, leídas de ``whatsapp_conversations``."""

    billing_regime: str | None = None
    documented_pct: Decimal | None = None
    freight_cost: Decimal | None = None
    freight_basis: str | None = None
    freight_free_over: Decimal | None = None

    @property
    def freight_included(self) -> bool:
        return self.freight_cost is not None and self.freight_cost == 0 and self.freight_free_over is None

    @property
    def has_freight_info(self) -> bool:
        return self.freight_cost is not None or self.freight_free_over is not None

    @property
    def effective_regime(self) -> str:
        return self.billing_regime or REGIME_INVOICED


@dataclass
class RegimeResult:
    neto_unit: Decimal
    desembolso_unit: Decimal
    costo_real_unit: Decimal
    marks: list[str] = field(default_factory=list)

    def totals(self, quantity: Decimal) -> tuple[Decimal, Decimal, Decimal]:
        """``(neto_total, desembolso_total, costo_real_total)``."""

        return self.neto_unit * quantity, self.desembolso_unit * quantity, self.costo_real_unit * quantity


def regime_of(terms: SupplierTerms | None) -> str | None:
    return terms.billing_regime if terms is not None else None


def apply_regime(unit_price: Decimal, alicuota: Decimal, iva_included: bool | None, terms: SupplierTerms | None) -> RegimeResult:
    """Valores unitarios (neto, desembolso, costo real β=1) y marcas según el régimen."""

    factor = ONE + alicuota / HUNDRED
    regime = regime_of(terms)

    if regime == REGIME_CASH:
        return RegimeResult(unit_price, unit_price, unit_price, [MARK_CASH])

    if regime == REGIME_PARTIAL:
        pct = terms.documented_pct

        if pct is None:
            return RegimeResult(unit_price, unit_price, unit_price, [MARK_PARTIAL_UNKNOWN])

        credit = (unit_price * pct / HUNDRED) / factor * (alicuota / HUNDRED)

        return RegimeResult(unit_price, unit_price, unit_price - credit, [f"factura el {format(pct.normalize(), 'f')}%"])

    marks = [] if regime == REGIME_INVOICED else [MARK_REGIME_UNCONFIRMED]

    if iva_included is True:
        neto = unit_price / factor
    else:
        # False o None: el precio se toma como neto. Con None se marca: es la suposición más
        # cara para el comprador, no un dato del proveedor.
        neto = unit_price

        if iva_included is None:
            marks.append(MARK_IVA_UNCONFIRMED)

    return RegimeResult(neto, neto * factor, neto, marks)
