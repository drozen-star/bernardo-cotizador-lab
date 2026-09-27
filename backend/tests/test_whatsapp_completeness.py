"""Lab L5a: completitud de supplier_quotes, null conserva en correcciones, set_status complete exigente."""

from decimal import Decimal

from app.features.quote.model import SupplierQuote
from app.features.whatsapp.quote_completeness import REQUIRED
from app.features.whatsapp.quote_completeness import assess
from app.features.whatsapp.service import handle_inbound
from tests.test_whatsapp_agent import FakeClient
from tests.test_whatsapp_agent import _greet
from tests.test_whatsapp_agent import _quotes
from tests.test_whatsapp_agent import record
from tests.test_whatsapp_agent import reply
from tests.test_whatsapp_agent import text
from tests.test_whatsapp_agent import tool_use
from tests.test_whatsapp_agent import world  # noqa: F401 - fixture

FULL = {"iva_included": True, "freight_included": True, "lead_time_days": 3, "payment_terms": "contado", "validity": "48 horas"}


def _quote(**overrides) -> SupplierQuote:
    base = {
        "rfq_id": 1, "supplier_name": "X", "unit_price": Decimal("9800"), "currency": "ARS",
        "iva_included": True, "freight_included": True, "lead_time": 3, "payment_terms": "contado",
        "validity_date": None, "remarks": "Validez: 48 horas",
    }
    base.update(overrides)

    return SupplierQuote(**base)


# ---------------------------------------------------------------- assess()
def test_required_fields_are_the_six_of_the_spec():
    assert REQUIRED == ("unit_price", "iva_included", "freight_included", "lead_time", "payment_terms", "validity_date")


def test_six_fields_answered_is_complete():
    assert assess(_quote()) == ("complete", [])


def test_false_flags_count_as_answered():
    assert assess(_quote(iva_included=False, freight_included=False)) == ("complete", [])


def test_missing_validity_is_incomplete():
    assert assess(_quote(remarks=None)) == ("incomplete", ["validity_date"])


def test_textual_validity_in_remarks_counts():
    assert assess(_quote(validity_date=None, remarks="Validez: hasta fin de mes")) == ("complete", [])
    assert assess(_quote(validity_date=None, remarks="otra nota")) == ("incomplete", ["validity_date"])


def test_blank_payment_terms_is_missing():
    assert assess(_quote(payment_terms="   ")) == ("incomplete", ["payment_terms"])
    assert assess(_quote(unit_price=None, lead_time=None)) == ("incomplete", ["unit_price", "lead_time"])


# ------------------------------------------------------- record_quote (update)
def _first_turn(db_session, world, **fields):  # noqa: F811
    conversation, _ = _greet(db_session, world)
    handle_inbound(db_session, conversation.id, "Cemento 12900 con IVA, flete incluido, 3 días, contado, validez 48 horas", client=FakeClient([
        reply(record(world.cemento.id, "cemento 12900 con iva", price=12900, **fields)),
        reply(text("Lo tengo.")),
    ]))

    return conversation


def test_update_with_nulls_keeps_previous_values(db_session, world):  # noqa: F811
    conversation = _first_turn(db_session, world, **FULL)
    before = _quotes(db_session, conversation)[0]
    assert before.completeness == "complete"

    result = handle_inbound(db_session, conversation.id, "confirmo el cemento 12900", client=FakeClient([
        reply(record(world.cemento.id, "cemento 12900", price=12900, iva_included=None, freight_included=None,
                     lead_time_days=None, payment_terms=None, validity=None, id_="u1")),
        reply(text("Ya está registrado.")),
    ]))

    quote = _quotes(db_session, conversation)[0]
    assert quote.iva_included is True and quote.freight_included is True
    assert quote.lead_time == 3 and quote.payment_terms == "contado"
    assert quote.remarks == "Validez: 48 horas"
    assert quote.completeness == "complete" and quote.missing_fields == []
    assert "Sin dato en este ítem: nada" in result.outbound.tool_calls[0]["result"]


def test_update_with_new_values_overwrites(db_session, world):  # noqa: F811
    conversation = _first_turn(db_session, world, **FULL)

    handle_inbound(db_session, conversation.id, "el flete va aparte y el pago es a 30 días", client=FakeClient([
        reply(record(world.cemento.id, "el flete va aparte", price=12900, iva_included=None, freight_included=False,
                     lead_time_days=None, payment_terms="30 días", validity=None, id_="u2")),
        reply(text("Lo tengo.")),
    ]))

    quote = _quotes(db_session, conversation)[0]
    assert quote.freight_included is False
    assert quote.payment_terms == "30 días"
    assert quote.iva_included is True and quote.lead_time == 3  # lo no dicho se conserva


def test_creation_with_nulls_stays_null_and_incomplete(db_session, world):  # noqa: F811
    conversation = _first_turn(db_session, world, iva_included=True, freight_included=None, lead_time_days=None,
                               payment_terms=None, validity=None)

    quote = _quotes(db_session, conversation)[0]
    assert quote.freight_included is None and quote.lead_time is None
    assert quote.completeness == "incomplete"
    assert quote.missing_fields == ["freight_included", "lead_time", "payment_terms", "validity_date"]


# ------------------------------------------------------- set_status complete
def _close(db_session, conversation):
    fake = FakeClient([
        reply(tool_use("set_status", {"status": "complete", "reason": "listo"}, id_="s1")),
        reply(text("Con esto tengo lo que necesito.")),
    ])
    result = handle_inbound(db_session, conversation.id, "eso es todo", client=fake)

    return result.outbound.tool_calls[0]


def test_set_status_complete_is_rejected_without_quotes(db_session, world):  # noqa: F811
    conversation, _ = _greet(db_session, world)

    call = _close(db_session, conversation)

    assert call["ok"] is False
    assert "no hay ninguna cotización registrada" in call["result"]
    assert conversation.status == "open"


def test_set_status_complete_is_rejected_with_incomplete_quotes(db_session, world):  # noqa: F811
    conversation = _first_turn(db_session, world, iva_included=True, freight_included=None, lead_time_days=None,
                               payment_terms="contado", validity=None)

    call = _close(db_session, conversation)

    assert call["ok"] is False
    assert f"rfq_id {world.cemento.id} (Cemento CPN40): falta flete, plazo, validez" in call["result"]
    assert "Pedíselos al proveedor, o usá needs_human si no los va a dar" in call["result"]
    assert conversation.status == "open"


def test_set_status_complete_is_accepted_when_everything_is_complete(db_session, world):  # noqa: F811
    conversation = _first_turn(db_session, world, **FULL)

    call = _close(db_session, conversation)

    assert call["ok"] is True
    assert conversation.status == "complete"
