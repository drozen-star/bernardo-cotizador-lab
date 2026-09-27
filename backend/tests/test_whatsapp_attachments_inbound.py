"""Lab L5e (fase 1): contrato del inbound con adjuntos y el provisorio guardado en el request."""

# ruff: noqa: F811 - el fixture `lab` se importa y se recibe como parámetro

from app.features.whatsapp import inbound as inbound_flow
from app.features.whatsapp.attachments import pending
from app.features.whatsapp.inbound import InboundPayload
from app.features.whatsapp.model import WhatsappMessage
from app.features.whatsapp.service import handle_inbound
from tests.test_whatsapp_agent import FakeClient
from tests.test_whatsapp_agent import reply
from tests.test_whatsapp_agent import text
from tests.test_whatsapp_inbound import _conversation
from tests.test_whatsapp_inbound import _post
from tests.test_whatsapp_inbound import lab  # noqa: F401 - fixture
from tests.test_whatsapp_inbound import payload


def media_payload(wa_id="wamid.doc.1", type_="document", media_id="MEDIA123", filename="lista.pdf", caption=None, mime="application/pdf"):
    body = payload(wa_id=wa_id, text=None, type_=type_)
    body.update({"media_id": media_id, "mime_type": mime, "filename": filename, "caption": caption})

    return body


def _stored(db_session, wa_id):
    db_session.expire_all()
    return db_session.query(WhatsappMessage).filter(WhatsappMessage.wa_message_id == wa_id).one()


# ---------------------------------------------------------------- contrato
def test_payload_accepts_media_fields_and_keeps_text_only_valid():
    plain = InboundPayload(**payload())
    assert plain.media_id is None and plain.caption is None and plain.is_text

    with_media = InboundPayload(**media_payload(caption="ahí va la lista"))
    assert with_media.media_id == "MEDIA123" and with_media.filename == "lista.pdf"
    assert with_media.mime_type == "application/pdf" and with_media.caption == "ahí va la lista"
    assert not with_media.is_text


def test_pending_helpers_round_trip():
    body = pending.pending_body("lista.pdf", "te mando la lista")

    assert body == "[adjunto: lista.pdf] (procesando)\nte mando la lista"
    assert pending.split_body(body) == ("lista.pdf", "te mando la lista")
    assert pending.split_body("[adjunto: image] (procesando)") == ("image", "")
    assert pending.split_body("texto raro") == ("adjunto", "")


# ---------------------------------------------------------------- inbound
def test_attachment_is_stored_as_pending_in_the_request(client, db_session, lab, monkeypatch):
    _post(client, payload(wa_id="wamid.a.1"))
    calls = []
    monkeypatch.setattr(inbound_flow, "run_agent_job", lambda *args: calls.append(args))

    response = _post(client, media_payload(wa_id="wamid.a.2", caption="ahí te mandé la lista"))

    assert response.json() == {"owned": True, "reason": "open_conversation"}
    assert calls == []  # el agente no corre en el request: lo hace el job del adjunto

    stored = _stored(db_session, "wamid.a.2")
    assert stored.direction == "inbound"
    assert stored.body == "[adjunto: lista.pdf] (procesando)\nahí te mandé la lista"
    assert stored.media_type == "document"
    assert stored.media_url == "wa-media:MEDIA123"
    assert stored.guardrail_flags == ["attachment_pending"]
    assert pending.media_id_of(stored) == "MEDIA123"


def test_image_attachment_without_filename_uses_the_type(client, db_session, lab):
    _post(client, payload(wa_id="wamid.i.1"))

    _post(client, media_payload(wa_id="wamid.i.2", type_="image", filename=None, mime="image/jpeg"))

    stored = _stored(db_session, "wamid.i.2")
    assert stored.body == "[adjunto: image] (procesando)"
    assert stored.media_type == "image"
    assert stored.guardrail_flags == ["attachment_pending"]


def test_attachment_as_first_message_opens_the_conversation(client, db_session, lab):
    response = _post(client, media_payload(wa_id="wamid.first", caption="Hola Bernardo, te paso precios"))

    assert response.json() == {"owned": True, "reason": "opened_now"}
    conversation = _conversation(db_session)
    assert conversation.status == "open"
    assert _stored(db_session, "wamid.first").guardrail_flags == ["attachment_pending"]


def test_duplicate_wa_message_id_with_a_pending_attachment(client, db_session, lab):
    _post(client, payload(wa_id="wamid.d.1"))
    _post(client, media_payload(wa_id="wamid.d.2"))
    before = db_session.query(WhatsappMessage).count()

    response = _post(client, media_payload(wa_id="wamid.d.2", filename="otro.pdf"))

    assert response.json() == {"owned": True, "reason": "duplicate"}
    assert db_session.query(WhatsappMessage).count() == before


def test_audio_is_still_unsupported_media(client, db_session, lab, monkeypatch):
    _post(client, payload(wa_id="wamid.au.1"))
    calls = []
    monkeypatch.setattr(inbound_flow, "run_agent_job", lambda *args: calls.append(args))

    response = _post(client, media_payload(wa_id="wamid.au.2", type_="audio", filename=None, mime="audio/ogg"))

    assert response.json() == {"owned": True, "reason": "open_conversation"}
    assert calls == []
    stored = _stored(db_session, "wamid.au.2")
    assert stored.guardrail_flags == ["unsupported_media"]
    assert stored.body == "[audio]" and stored.media_url is None


def test_image_without_media_id_is_still_unsupported_media(client, db_session, lab):
    _post(client, payload(wa_id="wamid.old.1"))

    _post(client, payload(wa_id="wamid.old.2", text=None, type_="image"))

    stored = _stored(db_session, "wamid.old.2")
    assert stored.guardrail_flags == ["unsupported_media"]
    assert stored.body == "[image]" and stored.media_url is None


def test_needs_human_holds_the_attachment_without_processing(client, db_session, lab):
    _post(client, payload(wa_id="wamid.nh.1"))
    conversation = _conversation(db_session)
    conversation.status = "needs_human"
    db_session.commit()

    response = _post(client, media_payload(wa_id="wamid.nh.2"))

    assert response.json() == {"owned": True, "reason": "needs_human_hold"}
    stored = _stored(db_session, "wamid.nh.2")
    assert stored.body == "[document]" and stored.guardrail_flags == [] and stored.media_url is None


# ---------------------------------------------------------------- service
def test_handle_inbound_with_existing_inbound_does_not_create_another(client, db_session, lab):
    _post(client, payload(wa_id="wamid.e.1"))
    conversation = _conversation(db_session)
    opening = [m for m in conversation.messages if m.direction == "outbound"][0]
    opening.sent_at = opening.created_at
    db_session.commit()

    existing = WhatsappMessage(
        conversation_id=conversation.id, direction="inbound", body="[adjunto: lista.pdf]\nCemento 12.900",
        wa_message_id="wamid.e.2", guardrail_flags=["attachment_pdf"],
    )
    db_session.add(existing)
    db_session.commit()
    before = db_session.query(WhatsappMessage).filter(WhatsappMessage.direction == "inbound").count()

    result = handle_inbound(
        db_session, conversation.id, "", client=FakeClient([reply(text("Lo tengo."))]),
        wa_message_id="wamid.e.2", existing_inbound=existing,
    )

    assert result.inbound.id == existing.id
    assert db_session.query(WhatsappMessage).filter(WhatsappMessage.direction == "inbound").count() == before
    assert result.outbound is not None and result.outbound.body == "Lo tengo."
