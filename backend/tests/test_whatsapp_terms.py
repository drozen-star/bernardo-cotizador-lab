"""Lab L5f (fase 1): record_terms, evidencia angosta, completitud con régimen y flete por conversación."""

# ruff: noqa: F811 - fixtures importados y recibidos como parámetro

from decimal import Decimal

import pytest
from sqlalchemy import inspect

from app.core.database import engine
from app.features.whatsapp import prompt as prompts
from app.features.whatsapp import tools
from app.features.whatsapp.model import WhatsappConversation
from app.features.whatsapp.quote_completeness import REQUIRED
from app.features.whatsapp.quote_completeness import assess
from app.features.whatsapp.quote_completeness import missing_fields
from app.features.whatsapp.service import handle_inbound
from app.features.whatsapp.service import open_conversation
from app.features.whatsapp.terms_writer import check_evidence_shape
from tests.test_whatsapp_agent import FakeClient
from tests.test_whatsapp_agent import _greet
from tests.test_whatsapp_agent import _quotes
from tests.test_whatsapp_agent import record
from tests.test_whatsapp_agent import reply
from tests.test_whatsapp_agent import system_of
from tests.test_whatsapp_agent import text
from tests.test_whatsapp_agent import tool_use
from tests.test_whatsapp_agent import world  # noqa: F401 - fixture
from tests.test_whatsapp_completeness import FULL
from tests.test_whatsapp_completeness import _quote

TERMS_MESSAGE = "Todo facturado con factura A. El flete son 45.000 por pedido, sin cargo arriba de 500.000."


def terms(evidence, **overrides):
    payload = {
        "billing_regime": None, "documented_pct": None, "freight_included": None, "freight_cost": None,
        "freight_basis": None, "freight_free_over": None, "evidence": evidence,
    }
    payload.update(overrides)

    return tool_use("record_terms", payload, overrides.pop("id_", "tu_terms"))


def _turn(db, conversation, message, *blocks, closing="Lo tengo."):
    result = handle_inbound(db, conversation.id, message, client=FakeClient([reply(*blocks), reply(text(closing))]))

    return result.outbound.tool_calls


# ---------------------------------------------------------------- migración
def test_conversation_terms_columns_exist_in_sqlite(db_session):
    columns = {column["name"] for column in inspect(engine).get_columns("whatsapp_conversations")}

    assert {"billing_regime", "documented_pct", "freight_cost", "freight_basis", "freight_free_over", "terms_evidence"} <= columns
    assert "record_terms" in tools.TOOL_NAMES
    assert tools.TOOLS[0]["input_schema"]["properties"]["evidence"]["maxLength"] == 300


def test_check_constraints_reject_bad_values(db_session, world):
    conversation = open_conversation(db_session, world.batch.id, world.supplier.id)
    conversation.billing_regime = "trueque"

    with pytest.raises(Exception):
        db_session.commit()

    db_session.rollback()


# -------------------------------------------------------------- record_terms
def test_record_terms_registers_regime_and_freight_for_the_conversation(db_session, world):
    conversation, _ = _greet(db_session, world)

    calls = _turn(
        db_session, conversation, TERMS_MESSAGE,
        terms("El flete son 45.000 por pedido, sin cargo arriba de 500.000", billing_regime="facturado",
              freight_included=False, freight_cost=45000, freight_basis="pedido", freight_free_over=500000),
    )

    assert calls[0]["ok"] is True
    assert "régimen facturado" in calls[0]["result"] and "flete 45000 por pedido" in calls[0]["result"]
    assert "sin cargo arriba de 500000" in calls[0]["result"] and "Falta: nada" in calls[0]["result"]
    db_session.refresh(conversation)
    assert conversation.billing_regime == "facturado"
    assert conversation.freight_cost == Decimal("45000") and conversation.freight_basis == "pedido"
    assert conversation.freight_free_over == Decimal("500000")
    assert conversation.terms_evidence == "El flete son 45.000 por pedido, sin cargo arriba de 500.000"


def test_record_terms_rejects_non_literal_ellipsis_and_long_evidence(db_session, world):
    conversation, _ = _greet(db_session, world)

    calls = _turn(db_session, conversation, TERMS_MESSAGE, terms("flete 45.000 ... sin cargo", billing_regime="facturado"))
    assert calls[0]["ok"] is False and "'...'" in calls[0]["result"]

    calls = _turn(db_session, conversation, TERMS_MESSAGE, terms("Flete bonificado siempre", billing_regime="facturado", id_="t2"))
    assert calls[0]["ok"] is False and "no aparece literalmente" in calls[0]["result"]

    long_message = "Flete " + "x" * 320
    calls = _turn(db_session, conversation, long_message, terms(long_message, billing_regime="facturado", id_="t3"))
    assert calls[0]["ok"] is False and "300" in calls[0]["result"]

    db_session.refresh(conversation)
    assert conversation.billing_regime is None  # ninguno registró nada

    assert check_evidence_shape("Cemento 12.900 + IVA") is None
    assert check_evidence_shape("Cemento 12.900 … validez 48 horas") is not None


def test_record_terms_null_keeps_previous_values(db_session, world):
    conversation, _ = _greet(db_session, world)
    _turn(db_session, conversation, TERMS_MESSAGE, terms("Todo facturado con factura A", billing_regime="facturado", freight_cost=45000, freight_basis="pedido"))

    _turn(db_session, conversation, "Ah, y el flete es por viaje", terms("el flete es por viaje", freight_basis="viaje", id_="t2"))

    db_session.refresh(conversation)
    assert conversation.billing_regime == "facturado"
    assert conversation.freight_cost == Decimal("45000") and conversation.freight_basis == "viaje"


def test_freight_included_propagates_to_every_quote(db_session, world):
    conversation, _ = _greet(db_session, world)
    _turn(
        db_session, conversation, "Cemento 12.900 + IVA, cal 7.800 + IVA",
        record(world.cemento.id, "Cemento 12.900 + IVA", price=12900, iva_included=False, freight_included=None, id_="r1"),
        record(world.rfqs[2].id, "cal 7.800 + IVA", price=7800, iva_included=False, freight_included=None, id_="r2"),
    )
    assert all(q.freight_included is None for q in _quotes(db_session, conversation))

    _turn(db_session, conversation, "El flete está incluido en todo", terms("El flete está incluido", freight_included=True))

    db_session.refresh(conversation)
    assert conversation.freight_cost == Decimal("0") and conversation.freight_basis == "pedido"
    assert all(q.freight_included is True for q in _quotes(db_session, conversation))


def test_freight_cost_marks_quotes_without_freight_as_not_included(db_session, world):
    conversation, _ = _greet(db_session, world)
    _turn(
        db_session, conversation, "Cemento 12.900 + IVA con flete incluido, cal 7.800 + IVA",
        record(world.cemento.id, "Cemento 12.900 + IVA", price=12900, iva_included=False, freight_included=True, id_="r1"),
        record(world.rfqs[2].id, "cal 7.800 + IVA", price=7800, iva_included=False, freight_included=None, id_="r2"),
    )

    _turn(db_session, conversation, "El flete sale 30.000 por viaje", terms("El flete sale 30.000 por viaje", freight_cost=30000, freight_basis="viaje"))

    by_rfq = {q.rfq_id: q for q in _quotes(db_session, conversation)}
    assert by_rfq[world.cemento.id].freight_included is True  # lo dicho por ítem no se pisa
    assert by_rfq[world.rfqs[2].id].freight_included is False


def test_record_terms_rejects_invalid_enums(db_session, world):
    conversation, _ = _greet(db_session, world)

    calls = _turn(db_session, conversation, "Facturamos todo", terms("Facturamos todo", billing_regime="cheque"))
    assert calls[0]["ok"] is False and "billing_regime inválido" in calls[0]["result"]


# ------------------------------------------------------------ record_quote
def test_record_quote_rejects_glued_evidence(db_session, world):
    conversation, _ = _greet(db_session, world)
    message = "Cemento 12.900 la bolsa. Precios sin IVA. Flete aparte. Validez 48 horas."

    calls = _turn(db_session, conversation, message, record(world.cemento.id, "Cemento 12.900 la bolsa ... Validez 48 horas", price=12900))

    assert calls[0]["ok"] is False
    assert "une partes con '...'" in calls[0]["result"] and "record_terms" in calls[0]["result"]
    assert _quotes(db_session, conversation) == []


# --------------------------------------------------------------- completitud
def test_required_quote_fields_unchanged_and_regime_is_the_seventh():
    assert REQUIRED == ("unit_price", "iva_included", "freight_included", "lead_time", "payment_terms", "validity_date")
    assert assess(_quote()) == ("complete", [])  # sin conversación el régimen no se exige


def _conversation(**fields):
    conversation = WhatsappConversation(rfq_batch_id=1, supplier_id=1)

    for name, value in fields.items():
        setattr(conversation, name, value)

    return conversation


def test_completeness_four_regime_cases():
    assert missing_fields(_quote(), _conversation()) == ["billing_regime"]
    assert missing_fields(_quote(), _conversation(billing_regime="facturado")) == []
    assert missing_fields(_quote(iva_included=None), _conversation(billing_regime="facturado")) == ["iva_included"]
    assert missing_fields(_quote(iva_included=None), _conversation(billing_regime="efectivo")) == []
    assert missing_fields(_quote(iva_included=None), _conversation(billing_regime="parcial")) == []  # documented_pct nunca se exige


def test_completeness_freight_comes_from_the_conversation():
    assert missing_fields(_quote(freight_included=None), _conversation(billing_regime="facturado")) == ["freight_included"]
    assert missing_fields(_quote(freight_included=None), _conversation(billing_regime="facturado", freight_cost=Decimal("0"))) == []
    assert missing_fields(_quote(freight_included=None), _conversation(billing_regime="facturado", freight_free_over=Decimal("500000"))) == []


def test_set_status_complete_is_blocked_without_regime_and_accepted_with_it(db_session, world):
    conversation, _ = _greet(db_session, world)
    _turn(db_session, conversation, "Cemento 12900 con IVA, flete incluido, 3 días, contado, validez 48 horas",
          record(world.cemento.id, "cemento 12900 con iva", price=12900, **FULL))

    calls = _turn(db_session, conversation, "eso es todo", tool_use("set_status", {"status": "complete", "reason": "listo"}, id_="s1"))
    assert calls[0]["ok"] is False
    assert "falta régimen de facturación (factura A o efectivo)" in calls[0]["result"]
    assert conversation.status == "open"

    _turn(db_session, conversation, "Sí, con factura A", terms("con factura A", billing_regime="facturado"))
    calls = _turn(db_session, conversation, "eso es todo", tool_use("set_status", {"status": "complete", "reason": "listo"}, id_="s2"))

    assert calls[0]["ok"] is True
    assert conversation.status == "complete"


# -------------------------------------------------------------------- prompt
def test_system_prompt_shows_terms_validity_and_new_rules(db_session, world):
    conversation, _ = _greet(db_session, world)
    _turn(db_session, conversation, "Cemento 12900 con IVA, flete incluido, 3 días, contado, validez 48 horas",
          record(world.cemento.id, "cemento 12900 con iva", price=12900, **FULL))
    _turn(db_session, conversation, "Es en efectivo, factura una parte", terms("en efectivo", billing_regime="parcial"))

    client = FakeClient([reply(text("Lo tengo."))])
    handle_inbound(db_session, conversation.id, "algo más?", client=client)
    system = system_of(client.calls[0])

    assert "Condiciones del proveedor (todo el pedido): Condiciones registradas: régimen parcial. Falta: flete del pedido." in system
    assert "| validez: 48 horas" in system
    assert "record_terms" in system and "factura A o es en efectivo" in system
    assert 'Si dice "incluido arriba de $ X"' in system
    assert '"y" pasa a "e"' in system


def test_opening_asks_for_regime_and_freight_of_the_whole_order(db_session, world):
    conversation = open_conversation(db_session, world.batch.id, world.supplier.id)

    result = handle_inbound(db_session, conversation.id, "Hola Bernardo, mandame el pedido.", client=FakeClient())

    assert (
        "Para cada ítem necesito precio unitario, si incluye IVA, plazo de entrega, forma de pago y validez de la "
        "oferta. Del pedido completo: si facturás con factura A o es en efectivo, y el costo del flete a obra."
    ) in result.outbound.body
    assert prompts.build_terms_line(None).endswith("sin dato de régimen ni de flete.")
