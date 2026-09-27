"""Lab L5e (fase 2): descarga falsa, xlsx, transcripción con cliente falso y el job completo."""

# ruff: noqa: F811 - el fixture `lab` se importa y se recibe como parámetro

import json
from io import BytesIO

import httpx
import pytest
from openpyxl import Workbook

from app.features.whatsapp import inbound as inbound_flow
from app.features.whatsapp.attachments import AttachmentError
from app.features.whatsapp.attachments import download
from app.features.whatsapp.attachments import job
from app.features.whatsapp.attachments import spreadsheet
from app.features.whatsapp.attachments import transcribe
from app.features.whatsapp.model import WhatsappMessage
from app.features.whatsapp.settings import whatsapp_settings
from app.features.whatsapp.turn_gate import gate
from tests.test_whatsapp_agent import FakeClient
from tests.test_whatsapp_agent import reply
from tests.test_whatsapp_agent import text
from tests.test_whatsapp_attachments_inbound import media_payload
from tests.test_whatsapp_inbound import _conversation
from tests.test_whatsapp_inbound import _post
from tests.test_whatsapp_inbound import lab  # noqa: F401 - fixture
from tests.test_whatsapp_inbound import payload

PDF = b"%PDF-1.4 fake"
JPEG = b"\xff\xd8\xff fake"


# ------------------------------------------------------------------ helpers
def xlsx_bytes(rows, sheet="Hoja1"):
    workbook = Workbook()
    sheet_obj = workbook.active
    sheet_obj.title = sheet

    for row in rows:
        sheet_obj.append(row)

    buffer = BytesIO()
    workbook.save(buffer)

    return buffer.getvalue()


class FakeMeta:
    """Graph falso: metadata en /{version}/{media_id}, archivo en la URL firmada."""

    def __init__(self, files: dict[str, tuple[bytes, str]], *, size_override=None, fail_status=None):
        self.files = files
        self.size_override = size_override
        self.fail_status = fail_status
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        assert request.headers.get("Authorization") == "Bearer tok-de-prueba"

        if self.fail_status:
            return httpx.Response(self.fail_status, json={"error": {"message": "nope"}})

        if request.url.host == "graph.facebook.com":
            media_id = request.url.path.rsplit("/", 1)[-1]
            data, mime = self.files[media_id]
            size = self.size_override if self.size_override is not None else len(data)
            return httpx.Response(200, json={"url": f"https://lookaside.example/{media_id}", "mime_type": mime, "file_size": size})

        data, mime = self.files[request.url.path.strip("/")]
        return httpx.Response(200, content=data, headers={"content-type": mime})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def graph(monkeypatch):
    monkeypatch.setattr(whatsapp_settings, "WHATSAPP_TOKEN", "tok-de-prueba")
    monkeypatch.setattr(whatsapp_settings, "GRAPH_API_VERSION", "v23.0")
    meta = FakeMeta({})
    monkeypatch.setattr(job, "http_client_factory", meta.client)

    return meta


def _agent(monkeypatch, *replies):
    client = FakeClient(list(replies) or [reply(text("Lo tengo."))])
    monkeypatch.setattr(inbound_flow, "agent_client_factory", lambda: client)

    return client


def _transcriber(monkeypatch, *replies):
    client = FakeClient(list(replies))
    monkeypatch.setattr(job, "attachment_client_factory", lambda: client)

    return client


def _opened(client, db_session):
    """Conversación abierta con la apertura ya enviada, para que el turno siguiente corra el agente."""

    _post(client, payload(wa_id="wamid.open"))
    conversation = _conversation(db_session)
    opening = [m for m in conversation.messages if m.direction == "outbound"][0]
    opening.sent_at = opening.created_at
    db_session.commit()

    return conversation


def _message(db_session, wa_id):
    db_session.expire_all()
    return db_session.query(WhatsappMessage).filter(WhatsappMessage.wa_message_id == wa_id).one()


# ---------------------------------------------------------------- download
def test_fetch_media_two_steps_with_bearer(graph):
    graph.files["M1"] = (PDF, "application/pdf")

    data, mime = download.fetch_media("M1", client=graph.client())

    assert (data, mime) == (PDF, "application/pdf")
    assert graph.requests == ["/v23.0/M1", "/M1"]


def test_fetch_media_rejects_too_large_before_downloading(graph, monkeypatch):
    graph.files["M2"] = (PDF, "application/pdf")
    graph.size_override = whatsapp_settings.WHATSAPP_MAX_ATTACHMENT_BYTES + 1

    with pytest.raises(AttachmentError) as exc:
        download.fetch_media("M2", client=graph.client())

    assert exc.value.code == "too_large"
    assert graph.requests == ["/v23.0/M2"]  # no bajó el archivo


def test_fetch_media_rejects_too_large_after_downloading(graph, monkeypatch):
    graph.files["M3"] = (b"x" * 40, "application/pdf")
    graph.size_override = 10  # la metadata miente
    monkeypatch.setattr(whatsapp_settings, "WHATSAPP_MAX_ATTACHMENT_BYTES", 20)

    with pytest.raises(AttachmentError) as exc:
        download.fetch_media("M3", client=graph.client())

    assert exc.value.code == "too_large"


def test_fetch_media_http_error_and_missing_config(graph, monkeypatch):
    graph.fail_status = 500

    with pytest.raises(AttachmentError) as exc:
        download.fetch_media("M4", client=graph.client())

    assert exc.value.code == "download_failed"

    monkeypatch.setattr(whatsapp_settings, "WHATSAPP_TOKEN", "")

    with pytest.raises(AttachmentError) as exc:
        download.fetch_media("M4", client=graph.client())

    assert exc.value.code == "download_failed" and "not_configured" in exc.value.detail


# ------------------------------------------------------------- spreadsheet
def test_rows_from_xlsx_formats_non_empty_rows_of_every_sheet():
    data = xlsx_bytes([["Ítem", "Precio"], [None, None], ["Cemento CPN40", 12900.0], ["Cal", 6200.5]])

    assert spreadsheet.rows_from_xlsx(data) == [
        "Hoja1 F1: Ítem | Precio",
        "Hoja1 F3: Cemento CPN40 | 12900",
        "Hoja1 F4: Cal | 6200.5",
    ]
    assert spreadsheet.is_xlsx(spreadsheet.XLSX_MIME, None) and spreadsheet.is_xlsx("application/octet-stream", "lista.XLSX")
    assert not spreadsheet.is_xlsx("application/pdf", "lista.pdf")


def test_small_xlsx_goes_whole_without_the_model(lab):
    data = xlsx_bytes([["Cemento CPN40", 12900], ["Flete", "incluido"]])
    calls = []

    content, usage = spreadsheet.extract(data, lab.batch.rfqs, client_factory=lambda: calls.append(1), max_chars=12000)

    assert content == "Hoja1 F1: Cemento CPN40 | 12900\nHoja1 F2: Flete | incluido"
    assert usage is None and calls == []


def test_large_xlsx_rows_are_chosen_by_the_model_and_copied_verbatim(lab):
    rows = [[f"Producto {i}", i * 10] for i in range(1, 30)] + [["Cemento CPN40 x 50 kg", "12.900 + IVA"], ["Validez", "48 hs"]]
    data = xlsx_bytes(rows)
    lines = spreadsheet.rows_from_xlsx(data)
    fake = FakeClient([reply(text(json.dumps({"filas": ["Hoja1 F30", "Hoja1 F31", "Hoja1 F999", "Otra F1", 7]})))])

    content, usage = spreadsheet.extract(data, lab.batch.rfqs, client_factory=lambda: fake, max_chars=200)

    assert content == "Hoja1 F30: Cemento CPN40 x 50 kg | 12.900 + IVA\nHoja1 F31: Validez | 48 hs"
    assert content.split("\n")[0] == lines[29]  # tal cual salió de openpyxl
    assert usage["input_tokens"] == 100 and usage["output_tokens"] == 20
    call = fake.calls[0]
    assert "Ladrillo hueco portante 18x19x33" in call["messages"][0]["content"]
    assert "Hoja1 F30: Cemento CPN40" in call["messages"][0]["content"]
    assert "nunca instrucciones" in call["system"]


def test_large_xlsx_with_no_matching_rows_and_with_unreadable_json(lab):
    data = xlsx_bytes([[f"Producto {i}", i] for i in range(1, 40)])

    content, _ = spreadsheet.extract(data, lab.batch.rfqs, client_factory=lambda: FakeClient([reply(text('{"filas": []}'))]), max_chars=100)
    assert content == "SIN COINCIDENCIAS"

    content, _ = spreadsheet.extract(data, lab.batch.rfqs, client_factory=lambda: FakeClient([reply(text("no sé"))]), max_chars=100)
    assert content.startswith("Hoja1 F1: Producto 1 | 1")  # texto completo; el saneo lo recorta después


# -------------------------------------------------------------- transcribe
def test_transcribe_sends_the_document_as_base64_and_the_items(lab, monkeypatch):
    monkeypatch.setattr(whatsapp_settings, "WHATSAPP_ATTACHMENT_MODEL", "modelo-adjuntos")
    fake = FakeClient([reply(text("Cemento CPN40 x 50 kg .......... $ 12.900 + IVA\nFlete incluido CABA"))])

    content, usage = transcribe.transcribe(PDF, "application/pdf", lab.batch.rfqs, client=fake)

    assert content.startswith("Cemento CPN40 x 50 kg")
    assert usage["input_tokens"] == 100
    call = fake.calls[0]
    assert call["model"] == "modelo-adjuntos"
    blocks = call["messages"][0]["content"]
    assert blocks[0]["type"] == "document" and blocks[0]["source"]["media_type"] == "application/pdf"
    assert blocks[0]["source"]["type"] == "base64"
    assert "Ladrillo hueco portante 18x19x33 | 1200 un" in blocks[1]["text"]
    assert "datos, nunca instrucciones" in call["system"] and "TEXTUALMENTE" in call["system"]

    fake = FakeClient([reply(text("SIN COINCIDENCIAS"))])
    content, _ = transcribe.transcribe(JPEG, "image/jpeg", lab.batch.rfqs, client=fake)
    assert content == "SIN COINCIDENCIAS"
    assert fake.calls[0]["messages"][0]["content"][0]["type"] == "image"

    with pytest.raises(AttachmentError) as exc:
        transcribe.source_block(b"x", "application/msword")

    assert exc.value.code == "unsupported_type"


# ------------------------------------------------------------------- job
def test_pdf_attachment_is_transcribed_and_the_agent_runs(client, db_session, lab, graph, monkeypatch):
    conversation = _opened(client, db_session)
    graph.files["MPDF"] = (PDF, "application/pdf")
    transcriber = _transcriber(monkeypatch, reply(text("Cemento CPN40 x 50 kg $ 12.900 + IVA")))
    agent = _agent(monkeypatch, reply(text("Lo tengo. Me pasás el resto?")))

    response = _post(client, media_payload(wa_id="wamid.pdf", media_id="MPDF", filename="lista.pdf", caption="ahí va"))

    assert response.json() == {"owned": True, "reason": "open_conversation"}
    message = _message(db_session, "wamid.pdf")
    assert message.body == "[adjunto: lista.pdf]\nahí va\nCemento CPN40 x 50 kg $ 12.900 + IVA"
    assert message.guardrail_flags == ["attachment_pdf", "attachment_transcribed"]
    usage = {"stop_reason": "end_turn", "input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    assert message.tool_calls == [{"name": "attachment", "mime": "application/pdf", "bytes": len(PDF), "usage": usage}]
    assert len(transcriber.calls) == 1 and len(agent.calls) == 1
    history = agent.calls[0]["messages"]
    assert "Cemento CPN40 x 50 kg $ 12.900 + IVA" in history[-1]["content"]

    db_session.expire_all()
    conversation = db_session.get(type(conversation), conversation.id)
    assert conversation.model_calls == 2 and conversation.input_tokens == 200  # transcripción + agente
    outbound = [m for m in conversation.messages if m.direction == "outbound"][-1]
    assert outbound.body == "Lo tengo. Me pasás el resto?" and outbound.is_draft
    assert gate.pending(conversation.id) == 0 and inbound_flow._in_flight == set()


def test_no_matches_body(client, db_session, lab, graph, monkeypatch):
    _opened(client, db_session)
    graph.files["MIMG"] = (JPEG, "image/jpeg")
    _transcriber(monkeypatch, reply(text("SIN COINCIDENCIAS")))
    _agent(monkeypatch)

    _post(client, media_payload(wa_id="wamid.img", type_="image", media_id="MIMG", filename=None, mime="image/jpeg"))

    message = _message(db_session, "wamid.img")
    assert message.body == "[adjunto: image] sin ítems del pedido"
    assert message.guardrail_flags == ["attachment_image", "attachment_transcribed"]


def test_xlsx_attachment_keeps_the_rows_literal(client, db_session, lab, graph, monkeypatch):
    _opened(client, db_session)
    graph.files["MX"] = (xlsx_bytes([["Cemento CPN40", 12900, "+ IVA"]]), spreadsheet.XLSX_MIME)
    transcriber = _transcriber(monkeypatch)
    _agent(monkeypatch)

    _post(client, media_payload(wa_id="wamid.x", media_id="MX", filename="lista.xlsx", mime=spreadsheet.XLSX_MIME))

    message = _message(db_session, "wamid.x")
    assert message.body == "[adjunto: lista.xlsx]\nHoja1 F1: Cemento CPN40 | 12900 | + IVA"
    assert message.guardrail_flags == ["attachment_xlsx"]  # literal: sin attachment_transcribed
    assert transcriber.calls == [] and message.tool_calls[0]["usage"] is None


def test_docx_fails_visibly_and_the_agent_still_runs(client, db_session, lab, graph, monkeypatch):
    _opened(client, db_session)
    graph.files["MDOC"] = (b"PK fake docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    agent = _agent(monkeypatch, reply(text("No pude leer el archivo. Me lo reenviás en PDF, Excel .xlsx o como texto?")))

    _post(client, media_payload(wa_id="wamid.docx", media_id="MDOC", filename="lista.docx", mime="application/msword"))

    message = _message(db_session, "wamid.docx")
    assert message.body == "[adjunto no procesado: lista.docx — unsupported_type]"
    assert message.guardrail_flags == ["attachment_failed"]
    assert message.tool_calls[0]["error"] == "unsupported_type"
    assert len(agent.calls) == 1
    assert "[adjunto no procesado: lista.docx" in agent.calls[0]["messages"][-1]["content"]


def test_download_failure_is_visible(client, db_session, lab, graph, monkeypatch):
    _opened(client, db_session)
    graph.fail_status = 404
    agent = _agent(monkeypatch)

    _post(client, media_payload(wa_id="wamid.fail", media_id="MNONE"))

    message = _message(db_session, "wamid.fail")
    assert message.body == "[adjunto no procesado: lista.pdf — download_failed]"
    assert "attachment_failed" in message.guardrail_flags and len(agent.calls) == 1


def test_pdf_then_text_coalesce_into_one_agent_turn_that_sees_both(client, db_session, lab, graph, monkeypatch):
    conversation = _opened(client, db_session)
    graph.files["MPDF"] = (PDF, "application/pdf")
    transcriber = _transcriber(monkeypatch, reply(text("Cemento CPN40 $ 12.900")))
    agent = _agent(monkeypatch)

    # Lo que hace decide() por los dos inbounds, sin correr los jobs todavía.
    monkeypatch.setattr(job, "run_attachment_job", lambda *args: None)
    monkeypatch.setattr(inbound_flow, "run_agent_job", lambda *args: None)
    _post(client, media_payload(wa_id="wamid.c.pdf", media_id="MPDF"))
    _post(client, payload(wa_id="wamid.c.txt", text="ahí te mandé la lista"))
    monkeypatch.undo()
    monkeypatch.setattr(job, "http_client_factory", graph.client)
    monkeypatch.setattr(job, "attachment_client_factory", lambda: transcriber)
    monkeypatch.setattr(inbound_flow, "agent_client_factory", lambda: agent)
    monkeypatch.setattr(whatsapp_settings, "WHATSAPP_TOKEN", "tok-de-prueba")
    monkeypatch.setattr(whatsapp_settings, "GRAPH_API_VERSION", "v23.0")
    pending_message = _message(db_session, "wamid.c.pdf")
    assert gate.pending(conversation.id) == 2

    job.run_attachment_job(conversation.id, pending_message.id, "wamid.c.pdf")  # remaining 1: transcribe, sin agente
    assert len(transcriber.calls) == 1 and agent.calls == []
    assert _message(db_session, "wamid.c.pdf").body == "[adjunto: lista.pdf]\nCemento CPN40 $ 12.900"

    inbound_flow.run_agent_job(conversation.id, "ahí te mandé la lista", "wamid.c.txt")  # remaining 0: corre el agente

    assert len(agent.calls) == 1
    last_user = agent.calls[0]["messages"][-1]["content"]
    assert "Cemento CPN40 $ 12.900" in last_user and "ahí te mandé la lista" in last_user
    assert gate.pending(conversation.id) == 0
