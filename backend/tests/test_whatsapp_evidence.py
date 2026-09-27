"""Lab L3a (corrección post-revisión): la evidencia tiene que contener el precio declarado.

La cita literal sola no alcanza: si el modelo declara 9500 citando un fragmento que dice
9800, se rechaza. Formatos aceptados: "9800", "9.800", "$ 9.800,50", "9800 + IVA",
"9,800.00". Sin precio declarado se mantiene solo la validación literal.
"""

from decimal import Decimal

import pytest

from app.features.quote.model import SupplierQuote
from app.features.whatsapp.quote_writer import extract_prices
from app.features.whatsapp.quote_writer import price_in_evidence
from app.features.whatsapp.service import handle_inbound
from tests.test_whatsapp_agent import FakeClient
from tests.test_whatsapp_agent import _greet
from tests.test_whatsapp_agent import _quotes
from tests.test_whatsapp_agent import record
from tests.test_whatsapp_agent import reply
from tests.test_whatsapp_agent import text
from tests.test_whatsapp_agent import world  # noqa: F401 - fixture


# ------------------------------------------------------------- funciones puras
@pytest.mark.parametrize(
    "evidence, price",
    [
        ("cemento 9800", "9800"),
        ("cemento 9.800", "9800"),
        ("$ 9.800,50", "9800.50"),
        ("9800 + IVA", "9800"),
        ("9,800.00", "9800"),
        ("1.200.000 la tonelada", "1200000"),
        ("9,80 el kilo", "9.80"),
        ("son 9800,00 pesos", "9800"),
    ],
)
def test_price_in_evidence_accepts_argentine_and_us_formats(evidence, price):
    assert price_in_evidence(Decimal(price), evidence)


@pytest.mark.parametrize(
    "evidence, price",
    [
        ("cemento con iva, flete incluido", "9800"),
        ("cemento 9800 con iva", "9500"),
        ("bolsa de 50 kg a 9800", "50.05"),  # 50 está a 0.05: fuera de la tolerancia de 0.01
        ("", "9800"),
    ],
)
def test_price_in_evidence_rejects_when_the_number_is_missing(evidence, price):
    assert not price_in_evidence(Decimal(price), evidence)


def test_extract_prices_keeps_both_readings_of_an_ambiguous_token():
    values = extract_prices("9.800")

    assert Decimal("9800") in values
    assert Decimal("9.800") in values


# ------------------------------------------------------- a través del agente
def _turn(db_session, world, inbound, evidence, price):  # noqa: F811 - fixture object
    conversation, _ = _greet(db_session, world)
    fake = FakeClient([
        reply(record(world.cemento.id, evidence, price=price)),
        reply(text("Gracias.")),
    ])

    result = handle_inbound(db_session, conversation.id, inbound, client=fake)

    return conversation, result.outbound.tool_calls[0]


def test_literal_evidence_without_the_number_is_rejected(db_session, world):  # noqa: F811
    conversation, call = _turn(
        db_session, world, "Cemento 9800 con IVA, flete incluido", "con iva, flete incluido", 9800
    )

    assert call["ok"] is False
    assert "el precio no figura en la evidencia citada" in call["result"]
    assert _quotes(db_session, conversation) == []


def test_thousands_separator_matches_the_declared_price(db_session, world):  # noqa: F811
    conversation, call = _turn(db_session, world, "Cemento 9.800 con IVA", "cemento 9.800", 9800)

    assert call["ok"] is True
    assert float(_quotes(db_session, conversation)[0].unit_price) == 9800


def test_currency_symbol_and_decimal_comma_match(db_session, world):  # noqa: F811
    conversation, call = _turn(db_session, world, "Cemento $ 9.800,50 con IVA", "$ 9.800,50", 9800.5)

    assert call["ok"] is True
    assert _quotes(db_session, conversation)[0].unit_price == Decimal("9800.50")


def test_price_plus_iva_matches(db_session, world):  # noqa: F811
    conversation, call = _turn(db_session, world, "Cemento 9800 + IVA", "9800 + iva", 9800)

    assert call["ok"] is True
    assert float(_quotes(db_session, conversation)[0].unit_price) == 9800


def test_declared_price_different_from_the_evidence_is_rejected(db_session, world):  # noqa: F811
    conversation, call = _turn(db_session, world, "Cemento 9800 con IVA", "cemento 9800 con iva", 9500)

    assert call["ok"] is False
    assert "Declaraste 9500" in call["result"]
    assert db_session.query(SupplierQuote).count() == 0


# ------------------------------------------ Q2: marcado de WhatsApp en la literalidad
@pytest.mark.parametrize(
    "inbound",
    [
        "*Cemento:* 12.500 con IVA la bolsa",
        "**Cemento:** 12.500 con IVA la bolsa",
        "_Cemento:_ $12.500 con IVA la bolsa",
        "~Cemento:~ `12.500` con IVA la bolsa",
    ],
)
def test_markdown_in_the_inbound_does_not_break_literal_evidence(db_session, world, inbound):  # noqa: F811
    conversation, call = _turn(db_session, world, inbound, "cemento: 12.500 con iva", 12500)

    assert call["ok"] is True, call["result"]
    assert float(_quotes(db_session, conversation)[0].unit_price) == 12500


def test_invented_evidence_is_still_rejected_with_markdown_around(db_session, world):  # noqa: F811
    conversation, call = _turn(db_session, world, "*Cemento:* 12.500 con IVA la bolsa", "cemento: 12.900 con iva", 12900)

    assert call["ok"] is False
    assert call["result"].startswith("Rechazado: evidence")
    assert _quotes(db_session, conversation) == []


# --------------------------------- Q1: corrección con el mismo precio no exige el número
def test_existing_quote_with_a_different_price_still_needs_the_number(db_session, world):  # noqa: F811
    conversation, _ = _greet(db_session, world)

    handle_inbound(db_session, conversation.id, "Cemento 12900 con IVA", client=FakeClient([
        reply(record(world.cemento.id, "cemento 12900 con iva", price=12900)),
        reply(text("Anotado.")),
    ]))

    result = handle_inbound(db_session, conversation.id, "Perdón, el cemento me quedó en 12500, flete incluido", client=FakeClient([
        reply(record(world.cemento.id, "flete incluido", price=12500, freight_included=True, id_="fix")),
        reply(text("Gracias.")),
    ]))

    call = result.outbound.tool_calls[0]
    assert call["ok"] is False
    assert "el precio no figura en la evidencia citada" in call["result"]
    assert float(_quotes(db_session, conversation)[0].unit_price) == 12900  # no se pisó


def test_null_price_keeps_only_the_literal_check(db_session, world):  # noqa: F811
    conversation, call = _turn(db_session, world, "El cemento lo entrego en 3 días", "lo entrego en 3 días", None)

    assert call["ok"] is True
    quote = _quotes(db_session, conversation)[0]
    assert quote.unit_price is None
    assert quote.completeness == "incomplete"
