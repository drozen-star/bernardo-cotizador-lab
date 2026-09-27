"""Lab L5f (fase 2): max_tokens descarta el último tool_use, deja diagnóstico; conjunciones y/e."""

# ruff: noqa: F811 - fixtures importados y recibidos como parámetro

import pytest

from app.features.whatsapp import guardrails
from app.features.whatsapp.loop import run_turn
from app.features.whatsapp.service import handle_inbound
from app.features.whatsapp.tools import TOOLS
from app.features.whatsapp.turn_diagnostics import truncation_diagnostic
from app.features.whatsapp.voice_fixes import fix_conjunctions
from tests.test_whatsapp_agent import FakeClient
from tests.test_whatsapp_agent import _greet
from tests.test_whatsapp_agent import _quotes
from tests.test_whatsapp_agent import record
from tests.test_whatsapp_agent import reply
from tests.test_whatsapp_agent import text
from tests.test_whatsapp_agent import tool_use
from tests.test_whatsapp_agent import world  # noqa: F401 - fixture


def _executor(calls):
    def execute(name, tool_input):
        calls.append((name, dict(tool_input)))
        from app.features.whatsapp.loop import ToolOutcome

        return ToolOutcome(content="ok")

    return execute


# ------------------------------------------------------------------ loop (D6)
def test_max_tokens_discards_the_last_tool_use_even_with_partial_input():
    partial = tool_use("record_quote", {"rfq_id": 2, "unit_price": 100}, id_="tu_partial")  # cortado: sin evidence
    complete = tool_use("record_quote", {"rfq_id": 1, "unit_price": 50, "evidence": "x 50"}, id_="tu_ok")
    client = FakeClient([
        reply(text("Registro."), complete, partial, stop="max_tokens"),
        reply(text("Lo tengo.")),
    ])
    calls = []

    result = run_turn(client=client, system="s", history=[{"role": "user", "content": "hola"}], tools=TOOLS, execute_tool=_executor(calls), max_rounds=3, max_tokens=100)

    assert [name for name, _ in calls] == ["record_quote"]
    assert calls[0][1]["rfq_id"] == 1
    assert result.max_tokens_exhausted is False and result.text == "Lo tengo."
    assert len(result.diagnostics) == 1
    diagnostic = result.diagnostics[0]
    assert diagnostic["name"] == "max_tokens_diagnostic"
    assert diagnostic["discarded_tool"] == "record_quote"
    assert diagnostic["text_head"] == "Registro." and diagnostic["text_chars"] == 9
    assert [b["type"] for b in diagnostic["blocks"]] == ["text", "tool_use", "tool_use"]
    assert diagnostic["blocks"][2]["input_keys"] == ["rfq_id", "unit_price"]
    assert diagnostic["blocks"][1]["input_chars"] > 0


def test_max_tokens_with_only_a_tool_use_is_exhausted_with_diagnostic():
    client = FakeClient([reply(tool_use("record_quote", {"rfq_id": 2, "unit_price": 100}), stop="max_tokens")])
    calls = []

    result = run_turn(client=client, system="s", history=[{"role": "user", "content": "hola"}], tools=TOOLS, execute_tool=_executor(calls), max_rounds=3, max_tokens=100)

    assert calls == [] and result.max_tokens_exhausted is True and result.text == ""
    assert result.diagnostics[0]["discarded_tool"] == "record_quote"


def test_max_tokens_with_only_text_keeps_the_head_in_the_diagnostic():
    long_text = "Te paso el detalle. " * 200
    client = FakeClient([reply(text(long_text), stop="max_tokens")])

    result = run_turn(client=client, system="s", history=[{"role": "user", "content": "hola"}], tools=TOOLS, execute_tool=_executor([]), max_rounds=3, max_tokens=100)

    assert result.max_tokens_exhausted is True and result.text == ""
    diagnostic = result.diagnostics[0]
    assert diagnostic["text_head"] == long_text[:2000] and diagnostic["text_chars"] == len(long_text)
    assert diagnostic["discarded_tool"] is None and diagnostic["blocks"] == [{"type": "text", "name": None, "input_keys": [], "input_chars": 0}]


def test_truncation_diagnostic_is_pure_and_handles_odd_blocks():
    diagnostic = truncation_diagnostic([tool_use("ask_buyer", None, id_="x")], discarded_tool="ask_buyer")

    assert diagnostic["blocks"] == [{"type": "tool_use", "name": "ask_buyer", "input_keys": [], "input_chars": 2}]
    assert diagnostic["text_head"] == "" and diagnostic["text_chars"] == 0


def test_diagnostic_is_saved_in_the_outbound_tool_calls(db_session, world):
    conversation, _ = _greet(db_session, world)
    partial = tool_use("record_quote", {"rfq_id": world.cemento.id, "unit_price": 12900}, id_="tu_partial")
    client = FakeClient([
        reply(record(world.cemento.id, "cemento 12900 con iva", price=12900), partial, stop="max_tokens"),
        reply(text("Lo tengo.")),
    ])

    result = handle_inbound(db_session, conversation.id, "Cemento 12900 con IVA", client=client)

    assert len(_quotes(db_session, conversation)) == 1  # el completo se ejecutó, el parcial no
    names = [call["name"] for call in result.outbound.tool_calls]
    assert names.count("record_quote") == 1 and "max_tokens_diagnostic" in names
    diagnostic = next(call for call in result.outbound.tool_calls if call["name"] == "max_tokens_diagnostic")
    assert diagnostic["discarded_tool"] == "record_quote"
    assert result.outbound.body == "Lo tengo."


# ------------------------------------------------------------------ voz (y/e)
@pytest.mark.parametrize(
    ("given", "expected", "changed"),
    [
        ("cal, arena e hierro", "cal, arena y hierro", True),
        ("arena y hierro", "arena y hierro", False),
        ("cemento y hidrófugo", "cemento e hidrófugo", True),
        ("Y hierro también", "Y hierro también", False),
        ("E hierro también", "Y hierro también", True),
        ("arena y hilo, cal y impermeabilizante", "arena e hilo, cal e impermeabilizante", True),
        ("y yeso y hierro y iva", "y yeso y hierro e iva", True),
    ],
)
def test_fix_conjunctions(given, expected, changed):
    assert fix_conjunctions(given) == (expected, changed)


def test_review_outbound_fixes_conjunctions_without_blocking():
    review = guardrails.review_outbound("Lo tengo: cal, arena e hierro y cemento y hidrófugo.", max_chars=700, safe_reply="seguro")

    assert review.blocked is False
    assert review.text == "Lo tengo: cal, arena y hierro y cemento e hidrófugo."
    assert review.flags == ["conjunction_fixed"]

    clean = guardrails.review_outbound("Lo tengo: arena y hierro.", max_chars=700, safe_reply="seguro")
    assert clean.flags == []
