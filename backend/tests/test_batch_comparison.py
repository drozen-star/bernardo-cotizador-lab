"""Lab L5d: modelo fiscal y estrategias del comparativo, sin base de datos."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from app.features.batch_comparison import fiscal
from app.features.batch_comparison import strategies
from app.features.batch_comparison.fiscal import compute_item_cost
from app.features.batch_comparison.fiscal import format_money
from app.features.batch_comparison.fiscal import format_pct
from app.features.batch_comparison.fiscal import parse_alicuotas

_ids = iter(range(1, 10_000))


def _rfq(rfq_id: int, name: str = "Ítem", quantity: int = 10):
    return NS(id=rfq_id, item_name=name, quantity=quantity, unit="un")


def _quote(price, **overrides):
    fields = dict(
        id=next(_ids), unit_price=Decimal(str(price)) if price is not None else None, currency="ARS",
        iva_included=True, freight_included=True, lead_time=3, payment_terms="contado",
        validity_date=date(2026, 10, 15), remarks=None, shipping_cost=None,
    )
    fields.update(overrides)

    return NS(**fields)


def _cost(price, rfq=None, supplier="id:1", alicuota=fiscal.DEFAULT_ALICUOTA, **overrides):
    return compute_item_cost(_quote(price, **overrides), rfq or _rfq(1), supplier, alicuota)


# ------------------------------------------------------------------- fiscal
def test_iva_included_divides_by_the_factor():
    cost = _cost("1210", iva_included=True)

    assert cost.comparable is True
    assert cost.neto_unit == Decimal("1000")
    assert cost.desembolso_unit == Decimal("1210")
    assert cost.costo_real_unit == Decimal("1000")
    assert cost.costo_real_total == Decimal("10000")
    assert cost.desembolso_total == Decimal("12100")
    assert cost.marks == []


def test_iva_excluded_keeps_the_price_as_net():
    cost = _cost("1000", iva_included=False)

    assert cost.neto_unit == Decimal("1000")
    assert cost.desembolso_unit == Decimal("1210")
    assert cost.marks == []


def test_iva_unconfirmed_assumes_without_iva_and_marks():
    cost = _cost("1000", iva_included=None)

    assert cost.comparable is True
    assert cost.neto_unit == Decimal("1000")  # el caso más caro para el comprador
    assert cost.desembolso_unit == Decimal("1210")
    assert fiscal.MARK_IVA_UNCONFIRMED in cost.marks


def test_alicuota_override_10_5():
    cost = _cost("1105", alicuota=Decimal("10.5"))

    assert cost.alicuota == Decimal("10.5")
    assert cost.neto_unit == Decimal("1000")
    assert cost.desembolso_total == Decimal("11050")


def test_freight_not_included_without_cost_is_marked_to_quote():
    cost = _cost("100", iva_included=False, freight_included=False)

    assert fiscal.MARK_FREIGHT_TO_QUOTE in cost.marks
    assert cost.costo_real_total == Decimal("1000")  # el costo queda sin flete


def test_shipping_cost_is_added_to_the_net_total():
    cost = _cost("100", iva_included=False, freight_included=False, shipping_cost=Decimal("5000"))

    assert cost.neto_total == Decimal("6000")
    assert cost.costo_real_total == Decimal("6000")
    assert cost.desembolso_total == Decimal("7260")
    assert any(mark.startswith("flete $ 5.000,00 aparte") for mark in cost.marks)
    assert fiscal.MARK_FREIGHT_TO_QUOTE not in cost.marks


def test_freight_unconfirmed_is_marked():
    assert fiscal.MARK_FREIGHT_UNCONFIRMED in _cost("100", freight_included=None).marks


def test_usd_quote_is_shown_but_not_comparable():
    cost = _cost("10", currency="USD")

    assert cost.comparable is False
    assert "moneda USD, no comparada" in cost.marks
    assert cost.unit_price == Decimal("10")
    assert cost.costo_real_total is None


def test_quote_without_price_does_not_participate():
    cost = _cost(None)

    assert cost.comparable is False
    assert fiscal.MARK_NO_PRICE in cost.marks
    assert _cost("0").comparable is False


def test_missing_lead_payment_and_validity_are_marked_without_changing_cost():
    cost = _cost("100", iva_included=False, lead_time=None, payment_terms="  ", validity_date=None)

    assert cost.comparable is True
    assert cost.costo_real_total == Decimal("1000")
    assert cost.marks == ["falta plazo", "falta forma de pago", "falta validez"]


def test_validity_from_remarks_text():
    cost = _cost("100", validity_date=None, remarks="Validez: 48 horas")

    assert cost.validity == "48 horas"
    assert "falta validez" not in cost.marks


def test_everything_is_decimal_even_from_ints():
    cost = _cost(890, iva_included=True, shipping_cost=1500)

    for name in ("unit_price", "neto_unit", "desembolso_unit", "costo_real_unit", "neto_total",
                 "desembolso_total", "costo_real_total", "shipping_cost", "alicuota"):
        assert isinstance(getattr(cost, name), Decimal), name


def test_parse_alicuotas_ok():
    assert parse_alicuotas(None) == {}
    assert parse_alicuotas(" ") == {}
    assert parse_alicuotas("12:10.5, 13:21") == {12: Decimal("10.5"), 13: Decimal("21")}


@pytest.mark.parametrize("raw", ["abc", "1:", "1:x", "1:150", "1;21", ":21", "1:-1", "a:21"])
def test_parse_alicuotas_rejects_malformed(raw):
    with pytest.raises(ValueError):
        parse_alicuotas(raw)


def test_money_formatting_is_argentine():
    assert format_money(Decimal("41200")) == "$ 41.200,00"
    assert format_money(Decimal("2065421.494")) == "$ 2.065.421,49"
    assert format_money(Decimal("-5.5")) == "-$ 5,50"
    assert format_pct(Decimal("2.14")) == "2,1%"
    assert format_pct(Decimal("12")) == "12,0%"


# --------------------------------------------------------------- estrategias
NAMES = {"id:1": "Corralón Norte", "id:2": "Materiales del Sur", "id:3": "Ferretería Oeste", "id:4": "Zeta"}


def _compare(items, costs, names=NAMES):
    return strategies.compare(items, costs, names)


def test_lowest_cost_tie_by_price_is_broken_by_lead_time_then_name():
    rfq = _rfq(1, "Cemento")
    costs = [
        _cost("100", rfq, "id:1", iva_included=False, lead_time=5),
        _cost("100", rfq, "id:2", iva_included=False, lead_time=3),
        _cost("100", rfq, "id:3", iva_included=False, lead_time=3),
    ]

    result = _compare([rfq], costs).lowest_cost

    assert result.assignments[0].supplier_key == "id:3"  # mismo precio y plazo: Ferretería < Materiales

    result = _compare([rfq], costs[:2]).lowest_cost
    assert result.assignments[0].supplier_key == "id:2"  # menor plazo


def _three_items_four_suppliers():
    items = [_rfq(1, "Ladrillo", 100), _rfq(2, "Cemento", 10), _rfq(3, "Arena", 2)]
    costs = [
        # id:1 barato en ladrillo y cemento; id:2 barato en arena; id:3 y id:4 cubren todo más caro.
        _cost("10", items[0], "id:1", iva_included=False, lead_time=2),
        _cost("100", items[1], "id:1", iva_included=False, lead_time=2),
        _cost("1000", items[2], "id:2", iva_included=False, lead_time=4),
        _cost("12", items[0], "id:3", iva_included=False, lead_time=3),
        _cost("110", items[1], "id:3", iva_included=False, lead_time=3),
        _cost("1100", items[2], "id:3", iva_included=False, lead_time=3),
        _cost("13", items[0], "id:4", iva_included=False, lead_time=1),
        _cost("120", items[1], "id:4", iva_included=False, lead_time=1),
        _cost("1200", items[2], "id:4", iva_included=False, lead_time=1),
    ]

    return items, costs


def test_fewer_suppliers_finds_min_k_and_lowest_total():
    items, costs = _three_items_four_suppliers()
    comparison = _compare(items, costs)
    lowest, fewer = comparison.lowest_cost, comparison.fewer_suppliers

    assert lowest.total_costo_real == Decimal("4000")  # 1000 + 1000 + 2000
    assert lowest.supplier_count == 2 and lowest.supplier_names == ["Corralón Norte", "Materiales del Sur"]
    assert lowest.max_lead_time == 4

    assert fewer.supplier_count == 1 and fewer.supplier_names == ["Ferretería Oeste"]
    assert fewer.total_costo_real == Decimal("4500")  # 1200 + 1100 + 2200 < Zeta 4900
    assert fewer.total_desembolso == Decimal("5445")
    assert fewer.approximate is False
    assert comparison.difference_amount == Decimal("500")
    assert format_pct(comparison.difference_pct) == "12,5%"


def test_porque_uses_exact_numbers_in_bernardo_voice():
    items, costs = _three_items_four_suppliers()
    comparison = _compare(items, costs)
    fewer = comparison.fewer_suppliers.porque
    lowest = comparison.lowest_cost.porque

    assert "Comprando a 1 proveedor (Ferretería Oeste) en lugar de 2 pagás $ 500,00 más (12,5%)." in fewer
    assert "A cambio coordinás una entrega menos." in fewer
    assert "$ 4.500,00" in fewer and "$ 5.445,00" in fewer
    assert "$ 4.000,00" in lowest and "$ 4.840,00" in lowest
    assert "2 proveedores: Corralón Norte y Materiales del Sur" in lowest
    assert "El plazo más largo es de 4 días." in lowest

    for text in (fewer, lowest):
        assert "!" not in text
        assert "dashboard" not in text.lower()


def test_item_without_comparable_quote_is_marked_and_kept_in_the_list():
    items = [_rfq(1, "Ladrillo", 100), _rfq(2, "Hierro", 5)]
    costs = [
        _cost("10", items[0], "id:1", iva_included=False),
        _cost("50", items[1], "id:1", currency="USD"),  # solo en dólares: no compite
    ]

    comparison = _compare(items, costs)

    for result in comparison.strategies:
        assert [a.covered for a in result.assignments] == [True, False]
        assert "Hierro: sin cotización" in result.marks

    assert "Quedan sin cotización comparable: Hierro." in comparison.lowest_cost.porque
    assert "Comprar al menor costo ya implica 1 proveedor: Corralón Norte." in comparison.fewer_suppliers.porque
    assert comparison.difference_amount == Decimal("0")


def test_more_than_twelve_suppliers_uses_greedy_and_marks_approximate():
    items = [_rfq(i, f"Ítem {i}", 1) for i in range(1, 14)]
    names = {f"id:{i}": f"Proveedor {i:02d}" for i in range(1, 15)}
    costs = [_cost("100", items[i - 1], f"id:{i}", iva_included=False) for i in range(1, 14)]
    costs += [_cost("150", rfq, "id:14", iva_included=False) for rfq in items]

    comparison = _compare(items, costs, names)
    fewer = comparison.fewer_suppliers

    assert fewer.approximate is True
    assert strategies.MARK_APPROXIMATE in fewer.marks
    assert fewer.supplier_names == ["Proveedor 14"]
    assert fewer.total_costo_real == Decimal("1950")
    assert "aproximado" in fewer.porque
    assert comparison.lowest_cost.supplier_count == 13 and comparison.lowest_cost.approximate is False


# ----------------------------------------------------------------- salvedades
def test_porque_mentions_freight_to_quote_of_the_chosen_supplier():
    items, costs = _three_items_four_suppliers()
    # Ferretería Oeste (id:3) gana "menos proveedores" y no incluye el flete en nada.
    for cost in costs:
        if cost.supplier_key == "id:3":
            cost.marks.append(fiscal.MARK_FREIGHT_TO_QUOTE)

    comparison = _compare(items, costs)

    assert comparison.fewer_suppliers.porque.endswith(
        "Ferretería Oeste no incluyó el flete en ningún ítem: el total no lo contempla."
    )
    assert "flete" not in comparison.lowest_cost.porque  # sus elegidos no tienen la marca


def test_porque_mentions_unconfirmed_iva_naming_item_and_supplier():
    items, costs = _three_items_four_suppliers()
    arena_sur = next(c for c in costs if c.supplier_key == "id:2" and c.rfq_id == 3)
    arena_sur.marks.append(fiscal.MARK_IVA_UNCONFIRMED)

    porque = _compare(items, costs).lowest_cost.porque

    assert porque.endswith("En Arena, Materiales del Sur no aclaró el IVA: se tomó sin IVA, el caso más caro.")


def test_porque_ignores_completeness_marks():
    items, costs = _three_items_four_suppliers()
    baseline = _compare(items, costs).lowest_cost.porque

    for cost in costs:
        cost.marks.extend(["falta plazo", "falta forma de pago", "falta validez"])

    porque = _compare(items, costs).lowest_cost.porque

    assert porque == baseline
    assert "falta" not in porque


def test_porque_caveats_are_capped_at_three_suppliers():
    items = [_rfq(i, f"Ítem {i}", 1) for i in range(1, 6)]
    names = {f"id:{i}": f"Proveedor {i}" for i in range(1, 6)}
    costs = [
        _cost("100", items[i - 1], f"id:{i}", iva_included=False, freight_included=False) for i in range(1, 6)
    ]

    porque = _compare(items, costs, names).lowest_cost.porque

    assert porque.count("no incluyó el flete") == 3
    assert "Proveedor 1 no incluyó el flete en Ítem 1: el total no lo contempla." in porque
    assert porque.endswith("Proveedor 3 no incluyó el flete en Ítem 3: el total no lo contempla, y 2 casos más en la hoja Matriz.")
    assert "Proveedor 4" not in porque.split("El plazo más largo")[1]


def test_no_comparable_quotes_at_all():
    items = [_rfq(1, "Ladrillo")]
    comparison = _compare(items, [_cost("10", items[0], "id:1", currency="USD")])

    assert comparison.lowest_cost.total_costo_real == Decimal("0")
    assert comparison.difference_pct is None
    assert "no puedo armar la compra" in comparison.fewer_suppliers.porque
