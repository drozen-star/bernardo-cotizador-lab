"""Lab L4: POST /whatsapp/inbound, la decisión de propiedad y el agente en background.

Solo SQLite. El agente nunca llama a Anthropic: la fábrica del cliente se reemplaza por el
cliente falso de test_whatsapp_agent, y donde solo importa que se encoló, se registra la
llamada al job en vez de correrlo.
"""

from datetime import date
from types import SimpleNamespace as NS

import pytest

from app.features.auth.model import User
from app.features.rfq.batch_invite import invite_suppliers_to_batch
from app.features.rfq.batch_service import create_batch_from_rows
from app.features.rfq.intake import parse_materials_xlsx
from app.features.supplier.model import Supplier
from app.features.whatsapp import inbound as inbound_flow
from app.features.whatsapp.model import WhatsappConversation
from app.features.whatsapp.model import WhatsappMessage
from app.features.whatsapp.settings import whatsapp_settings
from app.features.whatsapp.turn_gate import gate
from tests.test_rfq_batch import OK_FILE
from tests.test_rfq_batch import _create_supplier
from tests.test_rfq_batch import _register
from tests.test_whatsapp_agent import FakeClient

SECRET = "lab-secret-de-prueba"
HEADERS = {"X-Bernardo-Lab-Secret": SECRET}
RAW_FROM = "5491155551234"
BATCH_NAME = "Obra Palermo - mampostería"


def payload(wa_id="wamid.1", frm=RAW_FROM, text=f"Hola Bernardo, soy Corralón Norte. Mandame el pedido {BATCH_NAME}.", type_="text"):
    return {"wa_message_id": wa_id, "from": frm, "timestamp": "1790000000", "type": type_, "text": text}


@pytest.fixture(autouse=True)
def _clean_in_flight():
    inbound_flow._in_flight.clear()
    gate.reset()
    yield
    inbound_flow._in_flight.clear()
    gate.reset()


@pytest.fixture
def lab(client, db_session, monkeypatch):
    monkeypatch.setattr(whatsapp_settings, "LAB_SHARED_SECRET", SECRET)
    monkeypatch.setattr(inbound_flow, "agent_client_factory", lambda: FakeClient([]))

    data = _register(client, "compras@constructora.example.com")
    user = db_session.get(User, data["user"]["id"])
    supplier_data = _create_supplier(client, data["access_token"], "Corralón Norte", "ventas@norte.example.com", "Marta Pérez")
    supplier = db_session.get(Supplier, supplier_data["id"])
    supplier.whatsapp_phone = "+54 9 11 5555-1234"
    db_session.commit()

    batch = create_batch_from_rows(
        db=db_session, user=user, rows=parse_materials_xlsx(OK_FILE), name=BATCH_NAME,
        site_name="Edificio Palermo Soho", site_address="Gorriti 4800, CABA", delivery_expectation=date(2026, 10, 15),
    )
    invite_suppliers_to_batch(db=db_session, batch=batch, supplier_ids=[supplier.id])

    return NS(user=user, supplier=supplier, batch=batch, token=data["access_token"])


def _post(client, body, headers=HEADERS):
    return client.post("/whatsapp/inbound", json=body, headers=headers)


def _conversation(db_session):
    db_session.expire_all()
    return db_session.query(WhatsappConversation).order_by(WhatsappConversation.id.desc()).first()


# ------------------------------------------------------------------ auth
def test_missing_or_wrong_secret_is_401(client, lab):
    assert _post(client, payload(), headers={}).status_code == 401
    assert _post(client, payload(), headers={"X-Bernardo-Lab-Secret": "otro"}).status_code == 401
    assert _post(client, payload(), headers={"X-Bernardo-Lab-Secret": "otro"}).json() == {"detail": "Unauthorized"}


def test_empty_configured_secret_rejects_everything(client, lab, monkeypatch):
    monkeypatch.setattr(whatsapp_settings, "LAB_SHARED_SECRET", "")

    assert _post(client, payload(), headers={"X-Bernardo-Lab-Secret": ""}).status_code == 401


def test_invalid_payload_is_422(client, lab):
    assert _post(client, {"wa_message_id": "x", "type": "text"}).status_code == 422


# ------------------------------------------------------------- propiedad
def test_unknown_phone_is_not_a_supplier(client, db_session, lab):
    response = _post(client, payload(frm="5491100000000"))

    assert response.status_code == 200
    assert response.json() == {"owned": False, "reason": "not_a_supplier"}
    assert db_session.query(WhatsappConversation).count() == 0
    assert db_session.query(WhatsappMessage).count() == 0


@pytest.mark.parametrize("frm", [RAW_FROM, "541155551234", "+54 9 11 5555-1234"])
def test_first_message_opens_conversation_and_drafts_the_list(client, db_session, lab, frm):
    response = _post(client, payload(frm=frm))

    assert response.status_code == 200
    assert response.json() == {"owned": True, "reason": "opened_now"}

    conversation = _conversation(db_session)
    assert conversation.status == "open"
    assert conversation.rfq_batch_id == lab.batch.id
    assert conversation.supplier_id == lab.supplier.id
    assert conversation.wa_from == frm  # crudo, tal cual llegó

    # El job de background corrió (TestClient lo ejecuta antes de devolver): inbound + borrador.
    messages = sorted(conversation.messages, key=lambda m: m.id)
    assert [m.direction for m in messages] == ["inbound", "outbound"]
    assert messages[0].wa_message_id == "wamid.1"
    assert messages[1].is_draft
    assert "1. Ladrillo hueco portante 18x19x33: 1.200 un" in messages[1].body


def test_duplicate_wa_message_id_is_not_processed_twice(client, db_session, lab):
    _post(client, payload(wa_id="wamid.dup"))
    before = db_session.query(WhatsappMessage).count()

    response = _post(client, payload(wa_id="wamid.dup", text="otro texto"))

    assert response.json() == {"owned": True, "reason": "duplicate"}
    assert db_session.query(WhatsappMessage).count() == before


def test_open_conversation_enqueues_the_agent_in_background(client, db_session, lab, monkeypatch):
    _post(client, payload(wa_id="wamid.open.1"))
    calls = []
    monkeypatch.setattr(inbound_flow, "run_agent_job", lambda *args: calls.append(args))

    response = _post(client, payload(wa_id="wamid.open.2", text="Cemento 12.900 con IVA"))

    assert response.json() == {"owned": True, "reason": "open_conversation"}
    conversation = _conversation(db_session)
    assert calls == [(conversation.id, "Cemento 12.900 con IVA", "wamid.open.2")]


def test_needs_human_holds_the_message_without_the_agent(client, db_session, lab, monkeypatch):
    _post(client, payload(wa_id="wamid.h.1"))
    conversation = _conversation(db_session)
    conversation.status = "needs_human"
    db_session.commit()
    calls = []
    monkeypatch.setattr(inbound_flow, "run_agent_job", lambda *args: calls.append(args))

    response = _post(client, payload(wa_id="wamid.h.2", text="hola? sigue ahí?"))

    assert response.json() == {"owned": True, "reason": "needs_human_hold"}
    assert calls == []
    db_session.expire_all()
    held = db_session.query(WhatsappMessage).filter(WhatsappMessage.wa_message_id == "wamid.h.2").one()
    assert held.direction == "inbound" and held.body == "hola? sigue ahí?"
    assert held.received_at is not None


@pytest.mark.parametrize("status", ["complete", "supplier_declined", "expired"])
def test_terminal_conversation_is_not_owned(client, db_session, lab, status):
    _post(client, payload(wa_id="wamid.t.1"))
    conversation = _conversation(db_session)
    conversation.status = status
    db_session.commit()

    response = _post(client, payload(wa_id="wamid.t.2", text="che, algo más?"))

    assert response.json() == {"owned": False, "reason": "no_active_conversation"}
    assert db_session.query(WhatsappMessage).filter(WhatsappMessage.wa_message_id == "wamid.t.2").count() == 0


def test_media_in_open_conversation_is_flagged_and_skips_the_agent(client, db_session, lab, monkeypatch):
    _post(client, payload(wa_id="wamid.m.1"))
    calls = []
    monkeypatch.setattr(inbound_flow, "run_agent_job", lambda *args: calls.append(args))

    response = _post(client, payload(wa_id="wamid.m.2", text=None, type_="image"))

    assert response.json() == {"owned": True, "reason": "open_conversation"}
    assert calls == []
    db_session.expire_all()
    stored = db_session.query(WhatsappMessage).filter(WhatsappMessage.wa_message_id == "wamid.m.2").one()
    assert stored.guardrail_flags == ["unsupported_media"]
    assert stored.media_type == "image"
    assert stored.body == "[image]"


# ------------------------------------------------------- más de un batch
def test_two_open_batches_resolve_by_name_then_by_recency(client, db_session, lab):
    other = create_batch_from_rows(
        db=db_session, user=lab.user, rows=parse_materials_xlsx(OK_FILE)[:2], name="Obra Norte - hierro",
        site_name=None, site_address=None, delivery_expectation=date(2026, 11, 1),
    )
    invite_suppliers_to_batch(db=db_session, batch=other, supplier_ids=[lab.supplier.id])

    # Nombra el primero: gana el nombre, no la recencia.
    response = _post(client, payload(wa_id="wamid.b.1", text=f"Hola Bernardo, mandame el pedido {BATCH_NAME}"))
    assert response.json()["reason"] == "opened_now"
    assert _conversation(db_session).rfq_batch_id == lab.batch.id

    # Cerrada esa, otro mensaje sin nombre: queda el único candidato sin conversación previa.
    first = _conversation(db_session)
    first.status = "complete"
    db_session.commit()

    response = _post(client, payload(wa_id="wamid.b.2", text="hola, tengo precios"))
    assert response.json()["reason"] == "opened_now"
    assert _conversation(db_session).rfq_batch_id == other.id
