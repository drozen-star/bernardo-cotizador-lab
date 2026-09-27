"""Lab L5b: descartar, editar, reemplazo automático y aprobación con varios borradores."""

import pytest

from app.core.mixins import utcnow
from app.features.whatsapp import approval
from app.features.whatsapp import drafts
from app.features.whatsapp import sender
from app.features.whatsapp.model import WhatsappMessage
from app.features.whatsapp.service import handle_inbound
from app.features.whatsapp.service import text_history
from app.features.whatsapp.settings import whatsapp_settings
from tests.test_whatsapp_agent import FakeClient
from tests.test_whatsapp_agent import _greet
from tests.test_whatsapp_agent import reply
from tests.test_whatsapp_agent import text
from tests.test_whatsapp_agent import world  # noqa: F401 - fixture
from tests.test_whatsapp_approve import ADMIN
from tests.test_whatsapp_approve import ADMIN_HEADERS

TAG = "supplier_message"


def _pending_greeting(db, world):  # noqa: F811
    conversation, result = _greet(db, world, approve=False)

    return conversation, result.outbound


def _new_draft(db, conversation, body="Otro borrador."):
    message = WhatsappMessage(conversation_id=conversation.id, direction="outbound", body=body)
    db.add(message)
    db.commit()
    db.refresh(message)

    return message


class ExplodingClient:
    def __init__(self):
        self.messages = self

    def create(self, **kwargs):
        raise RuntimeError("proveedor de modelo caído")


# ------------------------------------------------------------------ discard
def test_discard_marks_the_draft_and_rejects_repeats(db_session, world):  # noqa: F811
    conversation, draft = _pending_greeting(db_session, world)

    discarded = drafts.discard(db_session, draft, "manual")

    assert discarded.discarded_at is not None and discarded.discard_reason == "manual"
    assert not discarded.is_draft
    assert approval.list_drafts(db_session, conversation) == []

    with pytest.raises(drafts.DraftError) as excinfo:
        drafts.discard(db_session, draft)

    assert excinfo.value.code == "already_discarded" and excinfo.value.status_code == 409


def test_discard_of_a_sent_message_is_rejected(db_session, world):  # noqa: F811
    conversation, _ = _greet(db_session, world)  # la apertura quedó enviada
    sent = next(m for m in conversation.messages if m.direction == "outbound")

    with pytest.raises(drafts.DraftError) as excinfo:
        drafts.discard(db_session, sent)

    assert excinfo.value.code == "already_sent"


# --------------------------------------------------------------------- edit
def test_edit_saves_original_body_only_once(db_session, world):  # noqa: F811
    conversation, draft = _pending_greeting(db_session, world)
    first = draft.body

    drafts.edit(db_session, draft, "Te paso el pedido. Necesito precio unitario, IVA, flete, plazo, pago y validez.")
    drafts.edit(db_session, draft, "Te paso el pedido y necesito precio, IVA, flete, plazo, pago y validez.")

    assert draft.original_body == first
    assert draft.body == "Te paso el pedido y necesito precio, IVA, flete, plazo, pago y validez."
    assert draft.edited_at is not None
    assert draft.is_draft


@pytest.mark.parametrize(
    "body, flag",
    [
        ("Che, te paso el precio del cemento.", "slang_removed"),
        ("Dale, confirmo la compra del cemento.", "purchase_commitment"),
        ("Perfecto! Te paso el precio.", "exclamation_removed"),
    ],
)
def test_edit_with_any_guardrail_flag_is_422_and_saves_nothing(db_session, world, body, flag):  # noqa: F811
    conversation, draft = _pending_greeting(db_session, world)
    before = draft.body

    with pytest.raises(drafts.DraftError) as excinfo:
        drafts.edit(db_session, draft, body)

    assert excinfo.value.code == "guardrail" and excinfo.value.status_code == 422
    assert flag in excinfo.value.flags
    db_session.refresh(draft)
    assert draft.body == before and draft.original_body is None and draft.edited_at is None


def test_edit_rejects_empty_too_long_and_sent(db_session, world):  # noqa: F811
    conversation, draft = _pending_greeting(db_session, world)

    for bad in ("   ", "x" * (whatsapp_settings.WHATSAPP_MAX_OUTPUT_CHARS + 1)):
        with pytest.raises(drafts.DraftError) as excinfo:
            drafts.edit(db_session, draft, bad)

        assert excinfo.value.code == "invalid_body" and excinfo.value.status_code == 422

    draft.sent_at = utcnow()
    db_session.commit()

    with pytest.raises(drafts.DraftError) as excinfo:
        drafts.edit(db_session, draft, "Texto nuevo.")

    assert excinfo.value.code == "already_sent"


# ---------------------------------------------------------------- supersede
def _pending_second_draft(db, world):  # noqa: F811
    """Apertura enviada + un borrador pendiente del agente (segundo turno)."""

    conversation, _ = _greet(db, world)  # la apertura queda enviada
    result = handle_inbound(db, conversation.id, "Hola", client=FakeClient([reply(text("Me pasás precio del cemento?"))]))

    return conversation, result.outbound


def test_new_turn_supersedes_the_pending_draft(db_session, world):  # noqa: F811
    conversation, previous = _pending_second_draft(db_session, world)

    result = handle_inbound(db_session, conversation.id, "Cemento 12.900", client=FakeClient([reply(text("Lo tengo. Y la cal?"))]))

    db_session.refresh(previous)
    assert previous.discarded_at is not None and previous.discard_reason == "superseded"
    assert result.outbound.is_draft
    assert approval.list_drafts(db_session, conversation) == [result.outbound]


def test_opening_draft_is_never_superseded(db_session, world):  # noqa: F811
    """Apertura pendiente + inbound nuevo: el agente corre, la apertura y el nuevo quedan pendientes."""

    conversation, greeting = _pending_greeting(db_session, world)
    fake = FakeClient([reply(text("Me pasás precio del cemento?"))])

    result = handle_inbound(db_session, conversation.id, "Cemento?", client=fake)

    assert len(fake.calls) == 1  # el agente corrió
    db_session.refresh(greeting)
    assert greeting.discarded_at is None and greeting.is_draft
    assert result.outbound.is_draft
    assert approval.list_drafts(db_session, conversation) == [greeting, result.outbound]

    # La apertura pendiente sigue en el historial del modelo.
    history = text_history(list(conversation.messages), tag=TAG)
    assert history[1]["role"] == "assistant" and greeting.body in history[1]["content"]


def test_pending_opening_needs_an_explicit_message_id_to_approve(db_session, world, monkeypatch):  # noqa: F811
    conversation, greeting = _pending_greeting(db_session, world)
    handle_inbound(db_session, conversation.id, "Cemento?", client=FakeClient([reply(text("Me pasás precio del cemento?"))]))
    world.supplier.whatsapp_phone = "5491155551234"  # destino: la fixture no lo carga
    db_session.commit()
    monkeypatch.setattr(sender, "send_text", lambda to, body: "wamid.opening")

    with pytest.raises(approval.ApprovalError) as excinfo:
        approval.approve(db_session, conversation)

    assert excinfo.value.code == "ambiguous_draft"

    sent = approval.approve(db_session, conversation, greeting.id, approved_by_user_id=world.user.id)

    assert sent.id == greeting.id and sent.wa_message_id == "wamid.opening" and sent.sent_at is not None


def test_failed_turn_keeps_the_previous_draft_alive(db_session, world):  # noqa: F811
    conversation, previous = _pending_second_draft(db_session, world)

    with pytest.raises(RuntimeError):
        handle_inbound(db_session, conversation.id, "Cemento?", client=ExplodingClient())

    db_session.rollback()
    db_session.refresh(previous)
    assert previous.discarded_at is None and previous.is_draft
    # El inbound sí quedó guardado (se commitea antes del turno).
    assert db_session.query(WhatsappMessage).filter(WhatsappMessage.body == "Cemento?").count() == 1


def test_text_history_skips_discarded_outbounds(db_session, world):  # noqa: F811
    conversation, previous = _pending_second_draft(db_session, world)
    handle_inbound(db_session, conversation.id, "Cemento 12.900", client=FakeClient([reply(text("Lo tengo. Y la cal?"))]))

    db_session.expire_all()
    history = text_history(list(conversation.messages), tag=TAG)

    assert [entry["role"] for entry in history] == ["user", "assistant", "user", "assistant"]
    assert previous.body not in "".join(entry["content"] for entry in history)
    assert history[-1]["content"] == "Lo tengo. Y la cal?"


# ------------------------------------------------------------------ approve
def test_approve_without_message_id_and_two_pending_is_ambiguous(db_session, world, monkeypatch):  # noqa: F811
    conversation, _ = _pending_greeting(db_session, world)
    _new_draft(db_session, conversation)
    monkeypatch.setattr(sender, "send_text", lambda *a: pytest.fail("no debe enviar"))

    with pytest.raises(approval.ApprovalError) as excinfo:
        approval.approve(db_session, conversation)

    assert excinfo.value.code == "ambiguous_draft" and excinfo.value.status_code == 409


def test_approve_a_discarded_draft_is_rejected(db_session, world, monkeypatch):  # noqa: F811
    conversation, draft = _pending_greeting(db_session, world)
    drafts.discard(db_session, draft)
    monkeypatch.setattr(sender, "send_text", lambda *a: pytest.fail("no debe enviar"))

    with pytest.raises(approval.ApprovalError) as excinfo:
        approval.approve(db_session, conversation, draft.id)

    assert excinfo.value.code == "draft_discarded" and excinfo.value.status_code == 409


# ---------------------------------------------------------------- endpoints
@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setattr(whatsapp_settings, "LAB_ADMIN_TOKEN", ADMIN)


def _url(conversation_id, message_id, suffix=""):
    return f"/whatsapp/conversations/{conversation_id}/drafts/{message_id}{suffix}"


def test_endpoints_require_admin_token(client, db_session, world, admin):  # noqa: F811
    conversation, draft = _pending_greeting(db_session, world)

    assert client.post(_url(conversation.id, draft.id, "/discard")).status_code == 401
    assert client.patch(_url(conversation.id, draft.id), json={"body": "x"}).status_code == 401


def test_endpoints_404_for_a_message_of_another_conversation(client, db_session, world, admin):  # noqa: F811
    conversation, draft = _pending_greeting(db_session, world)
    other_id = conversation.id + 100

    assert client.post(_url(other_id, draft.id, "/discard"), headers=ADMIN_HEADERS).status_code == 404
    inbound_id = next(m.id for m in conversation.messages if m.direction == "inbound")
    response = client.patch(_url(conversation.id, inbound_id), json={"body": "Texto."}, headers=ADMIN_HEADERS)
    assert response.status_code == 404 and response.json()["detail"] == "draft_not_found"


def test_discard_endpoint(client, db_session, world, admin):  # noqa: F811
    conversation, draft = _pending_greeting(db_session, world)

    response = client.post(_url(conversation.id, draft.id, "/discard"), json={"reason": "cambio de alcance"}, headers=ADMIN_HEADERS)

    assert response.status_code == 200, response.text
    assert response.json()["discard_reason"] == "cambio de alcance"
    assert response.json()["discarded_at"] is not None
    assert client.post(_url(conversation.id, draft.id, "/discard"), headers=ADMIN_HEADERS).json()["detail"] == "already_discarded"
    assert client.get(f"/whatsapp/conversations/{conversation.id}/drafts", headers=ADMIN_HEADERS).json()["drafts"] == []


def test_edit_endpoint_ok_and_guardrail_422(client, db_session, world, admin):  # noqa: F811
    conversation, draft = _pending_greeting(db_session, world)
    original = draft.body

    bad = client.patch(_url(conversation.id, draft.id), json={"body": "Che, te paso el pedido."}, headers=ADMIN_HEADERS)
    assert bad.status_code == 422
    assert bad.json()["detail"] == {"code": "guardrail", "flags": ["slang_removed"]}

    ok = client.patch(_url(conversation.id, draft.id), json={"body": "Te paso el pedido, decime precio, IVA y flete."}, headers=ADMIN_HEADERS)
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["body"] == "Te paso el pedido, decime precio, IVA y flete."
    assert body["original_body"] == original and body["edited_at"] is not None

    listed = client.get(f"/whatsapp/conversations/{conversation.id}/drafts", headers=ADMIN_HEADERS).json()["drafts"]
    assert listed[0]["body"] == "Te paso el pedido, decime precio, IVA y flete."
