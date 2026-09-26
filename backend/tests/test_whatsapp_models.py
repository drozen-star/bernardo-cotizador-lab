"""Lab L3a: tablas nuevas, FK de supplier_quotes y la unicidad (conversation_id, rfq_id)."""

from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from app.features.auth.model import User
from app.features.quote.model import QUOTE_SOURCES
from app.features.quote.model import SupplierQuote
from app.features.rfq.batch_service import create_batch_from_rows
from app.features.rfq.intake import parse_materials_xlsx
from app.features.whatsapp.model import WhatsappConversation
from app.features.whatsapp.model import WhatsappMessage
from app.features.whatsapp.service import text_history
from tests.test_rfq_batch import OK_FILE
from tests.test_rfq_batch import _create_supplier
from tests.test_rfq_batch import _register


@pytest.fixture
def world(client, db_session):
    data = _register(client, "compras@constructora.example.com")
    user = db_session.get(User, data["user"]["id"])
    supplier = _create_supplier(client, data["access_token"], "Corralón Norte", "ventas@norte.example.com")
    batch = create_batch_from_rows(
        db=db_session,
        user=user,
        rows=parse_materials_xlsx(OK_FILE),
        name="Obra Palermo",
        site_name="Edificio Palermo Soho",
        site_address="Gorriti 4800, CABA",
        delivery_expectation=date(2026, 10, 15),
    )
    conversation = WhatsappConversation(rfq_batch_id=batch.id, supplier_id=supplier["id"])
    db_session.add(conversation)
    db_session.commit()

    return {"user": user, "supplier_id": supplier["id"], "batch": batch, "conversation": conversation}


def test_whatsapp_is_a_known_quote_source():
    assert "whatsapp" in QUOTE_SOURCES


def test_conversation_defaults(world):
    conversation = world["conversation"]

    assert conversation.status == "open"
    assert conversation.opened_by == "supplier"
    assert conversation.opened_at is not None
    assert (conversation.input_tokens, conversation.output_tokens, conversation.model_calls) == (0, 0, 0)
    assert conversation.is_open


def test_message_draft_flag(world, db_session):
    message = WhatsappMessage(conversation_id=world["conversation"].id, direction="outbound", body="hola")
    db_session.add(message)
    db_session.commit()

    assert message.is_draft
    assert message.tool_calls is None
    assert world["conversation"].messages == [message]


def test_two_form_quotes_for_the_same_rfq_are_still_allowed(world, db_session):
    rfq_id = world["batch"].rfqs[0].id

    for name in ("Uno", "Dos"):
        db_session.add(SupplierQuote(rfq_id=rfq_id, supplier_name=name, source="form", unit_price=100))

    db_session.commit()

    assert db_session.query(SupplierQuote).filter(SupplierQuote.rfq_id == rfq_id).count() == 2


def test_same_conversation_and_rfq_twice_is_rejected(world, db_session):
    rfq_id = world["batch"].rfqs[0].id
    conversation_id = world["conversation"].id

    db_session.add(
        SupplierQuote(rfq_id=rfq_id, supplier_name="A", source="whatsapp", conversation_id=conversation_id)
    )
    db_session.commit()

    db_session.add(
        SupplierQuote(rfq_id=rfq_id, supplier_name="A", source="whatsapp", conversation_id=conversation_id)
    )

    with pytest.raises(IntegrityError):
        db_session.commit()

    db_session.rollback()

    assert world["conversation"].quotes[0].rfq_id == rfq_id


def test_text_history_alternates_roles_and_wraps_inbound():
    messages = [
        WhatsappMessage(direction="inbound", body="hola"),
        WhatsappMessage(direction="inbound", body="soy el corralón"),
        WhatsappMessage(direction="outbound", body="Hola. Te paso el pedido."),
        WhatsappMessage(direction="inbound", body="cemento 9800"),
    ]

    history = text_history(messages, tag="supplier_message")

    assert [entry["role"] for entry in history] == ["user", "assistant", "user"]
    assert history[0]["content"].startswith("<supplier_message>\nhola\n</supplier_message>")
    assert "soy el corralón" in history[0]["content"]
    assert history[1]["content"] == "Hola. Te paso el pedido."
    assert history[2]["content"] == "<supplier_message>\ncemento 9800\n</supplier_message>"
