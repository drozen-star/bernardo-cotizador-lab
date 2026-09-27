"""Lab L4: borradores, aprobación con ventana de 24 h y el sender con httpx mockeado."""

import json
from datetime import timedelta

import httpx
import pytest

from app.core.mixins import utcnow
from app.features.whatsapp import sender
from app.features.whatsapp.model import WhatsappMessage
from app.features.whatsapp.settings import whatsapp_settings
from tests.test_whatsapp_inbound import RAW_FROM
from tests.test_whatsapp_inbound import _conversation
from tests.test_whatsapp_inbound import _post
from tests.test_whatsapp_inbound import lab  # noqa: F401 - fixture
from tests.test_whatsapp_inbound import payload

ADMIN = "admin-token-de-prueba"
ADMIN_HEADERS = {"X-Bernardo-Lab-Admin": ADMIN}


@pytest.fixture
def conversation(client, db_session, lab, monkeypatch):  # noqa: F811
    monkeypatch.setattr(whatsapp_settings, "LAB_ADMIN_TOKEN", ADMIN)
    _post(client, payload(wa_id="wamid.first"))  # abre y deja el borrador con la lista

    return _conversation(db_session)


def _drafts(client, conversation_id, headers=ADMIN_HEADERS):
    return client.get(f"/whatsapp/conversations/{conversation_id}/drafts", headers=headers)


def _approve(client, conversation_id, body=None, headers=ADMIN_HEADERS):
    return client.post(f"/whatsapp/conversations/{conversation_id}/approve", json=body, headers=headers)


# ------------------------------------------------------------------ drafts
def test_drafts_require_admin_token(client, conversation):
    assert _drafts(client, conversation.id, headers={}).status_code == 401
    assert _drafts(client, conversation.id, headers={"X-Bernardo-Lab-Admin": "no"}).status_code == 401
    assert _drafts(client, conversation.id, headers={"X-Bernardo-Lab-Secret": ADMIN}).status_code == 401


def test_drafts_list_the_pending_outbound(client, conversation):
    response = _drafts(client, conversation.id)

    assert response.status_code == 200
    body = response.json()
    assert body["conversation_id"] == conversation.id
    assert body["status"] == "open"
    assert body["supplier_name"] == "Corralón Norte"
    assert body["destination"] == RAW_FROM
    assert body["window_open"] is True
    assert len(body["drafts"]) == 1
    assert body["drafts"][0]["sent_at"] is None and body["drafts"][0]["approved_by"] is None
    assert "Te paso el pedido" in body["drafts"][0]["body"]


def test_drafts_404_for_unknown_conversation(client, conversation):
    assert _drafts(client, 9999).status_code == 404


# ----------------------------------------------------------------- approve
def test_approve_sends_and_marks_the_draft(client, db_session, conversation, lab, monkeypatch):  # noqa: F811
    sent = []
    monkeypatch.setattr(sender, "send_text", lambda to, body: sent.append((to, body)) or "wamid.out.1")

    response = _approve(client, conversation.id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["wa_message_id"] == "wamid.out.1"
    assert body["sent_at"] is not None
    assert body["approved_by"] == lab.user.id  # FK a users: el comprador dueño del batch
    assert sent[0][0] == RAW_FROM and "Te paso el pedido" in sent[0][1]

    db_session.expire_all()
    stored = db_session.get(WhatsappMessage, body["id"])
    assert not stored.is_draft
    assert _drafts(client, conversation.id).json()["drafts"] == []


def test_approve_outside_the_24h_window_is_409_and_does_not_send(client, db_session, conversation, monkeypatch):
    inbound = db_session.query(WhatsappMessage).filter(WhatsappMessage.direction == "inbound").one()
    inbound.received_at = utcnow() - timedelta(hours=25)
    db_session.commit()
    monkeypatch.setattr(sender, "send_text", lambda *a: pytest.fail("no debe enviar"))

    response = _approve(client, conversation.id)

    assert response.status_code == 409
    assert response.json()["detail"] == "window_closed"
    assert _drafts(client, conversation.id).json()["window_open"] is False


def test_approve_an_already_sent_message_is_409(client, db_session, conversation, monkeypatch):
    monkeypatch.setattr(sender, "send_text", lambda to, body: "wamid.out.2")
    sent_id = _approve(client, conversation.id).json()["id"]

    response = _approve(client, conversation.id, body={"message_id": sent_id})

    assert response.status_code == 409
    assert response.json()["detail"] == "already_sent"


def test_approve_without_pending_drafts_is_404(client, db_session, conversation, monkeypatch):
    monkeypatch.setattr(sender, "send_text", lambda to, body: "wamid.out.3")
    _approve(client, conversation.id)

    assert _approve(client, conversation.id).status_code == 404
    assert _approve(client, conversation.id).json()["detail"] == "no_draft"


def test_meta_failure_is_502_and_the_draft_stays_pending(client, db_session, conversation, monkeypatch):
    def boom(to, body):
        raise sender.SenderError("meta_rejected", "(#131030) Recipient not in allowed list", status_code=400)

    monkeypatch.setattr(sender, "send_text", boom)

    response = _approve(client, conversation.id)

    assert response.status_code == 502
    assert response.json()["detail"] == "meta_send_failed:meta_rejected"
    drafts = _drafts(client, conversation.id).json()["drafts"]
    assert len(drafts) == 1 and drafts[0]["sent_at"] is None


def test_approve_requires_admin_token(client, conversation):
    assert _approve(client, conversation.id, headers={}).status_code == 401


# ------------------------------------------------------------------ sender
def _configured(monkeypatch):
    monkeypatch.setattr(whatsapp_settings, "WHATSAPP_TOKEN", "EAAB-token")
    monkeypatch.setattr(whatsapp_settings, "WHATSAPP_PHONE_NUMBER_ID", "123456789")
    monkeypatch.setattr(whatsapp_settings, "GRAPH_API_VERSION", "v23.0")


def test_sender_posts_to_graph_with_bearer_and_returns_the_message_id(monkeypatch):
    _configured(monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"messaging_product": "whatsapp", "messages": [{"id": "wamid.HBg"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    assert sender.send_text("5491155551234", "Hola, Raúl.", client=client) == "wamid.HBg"
    assert seen["url"] == "https://graph.facebook.com/v23.0/123456789/messages"
    assert seen["auth"] == "Bearer EAAB-token"
    assert seen["json"] == {
        "messaging_product": "whatsapp",
        "to": "5491155551234",
        "type": "text",
        "text": {"body": "Hola, Raúl.", "preview_url": False},
    }


def test_sender_raises_typed_error_when_meta_rejects(monkeypatch):
    _configured(monkeypatch)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"error": {"message": "bad"}})))

    with pytest.raises(sender.SenderError) as excinfo:
        sender.send_text("5491155551234", "x", client=client)

    assert excinfo.value.code == "meta_rejected"
    assert excinfo.value.status_code == 400


def test_sender_logs_meta_error_fields_without_secrets(monkeypatch, caplog):
    _configured(monkeypatch)
    meta_error = {
        "error": {
            "message": "(#131030) Recipient phone number not in allowed list",
            "type": "OAuthException",
            "code": 131030,
            "error_subcode": 2655007,
            "fbtrace_id": "AbCdEf123456",
        }
    }
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400, json=meta_error)))

    with caplog.at_level("WARNING", logger="app.features.whatsapp.sender"):
        with pytest.raises(sender.SenderError) as excinfo:
            sender.send_text("5491155551234", "Hola, Raúl, texto secreto del borrador", client=client)

    assert excinfo.value.code == "meta_rejected"
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "(#131030) Recipient phone number not in allowed list"

    log = "\n".join(record.getMessage() for record in caplog.records)
    assert "HTTP 400" in log
    assert "code=131030" in log and "subcode=2655007" in log and "type=OAuthException" in log
    assert "fbtrace_id=AbCdEf123456" in log
    assert "Recipient phone number not in allowed list" in log
    assert "EAAB-token" not in log
    assert "texto secreto del borrador" not in log
    assert "5491155551234" not in log


def test_sender_survives_a_non_json_rejection(monkeypatch, caplog):
    _configured(monkeypatch)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(502, text="<html>Bad Gateway</html>")))

    with caplog.at_level("WARNING", logger="app.features.whatsapp.sender"):
        with pytest.raises(sender.SenderError) as excinfo:
            sender.send_text("5491155551234", "x", client=client)

    assert excinfo.value.code == "meta_rejected"
    assert excinfo.value.status_code == 502
    log = "\n".join(record.getMessage() for record in caplog.records)
    assert "HTTP 502" in log and "cuerpo no JSON" in log
    assert "<html>" not in log


def test_sender_raises_when_graph_is_unreachable(monkeypatch):
    _configured(monkeypatch)

    def handler(request):
        raise httpx.ConnectTimeout("timeout")

    with pytest.raises(sender.SenderError) as excinfo:
        sender.send_text("5491155551234", "x", client=httpx.Client(transport=httpx.MockTransport(handler)))

    assert excinfo.value.code == "meta_unreachable"


def test_sender_refuses_without_configuration(monkeypatch):
    monkeypatch.setattr(whatsapp_settings, "WHATSAPP_TOKEN", "")
    monkeypatch.setattr(whatsapp_settings, "WHATSAPP_PHONE_NUMBER_ID", "")
    monkeypatch.setattr(whatsapp_settings, "GRAPH_API_VERSION", "")

    with pytest.raises(sender.SenderError) as excinfo:
        sender.send_text("5491155551234", "x", client=httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("no debe llamar"))))

    assert excinfo.value.code == "whatsapp_not_configured"
    assert "GRAPH_API_VERSION" in excinfo.value.detail
