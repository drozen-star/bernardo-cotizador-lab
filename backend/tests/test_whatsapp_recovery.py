"""Lab L3b: fallas técnicas del agente no cierran la conversación; los frenos de contenido sí.

Cubre las correcciones P2 (max_tokens) y P4 (technical_error) salidas de la corrida
20260926-2041-2 del simulador, con el mismo cliente falso de test_whatsapp_agent.
"""

import pytest

from app.features.quote.model import SupplierQuote
from app.features.whatsapp import guardrails
from app.features.whatsapp.service import FLAG_MAX_TOKENS_EXHAUSTED
from app.features.whatsapp.service import FLAG_TECHNICAL_ERROR
from app.features.whatsapp.service import handle_inbound
from app.features.whatsapp.settings import whatsapp_settings
from tests.test_whatsapp_agent import FakeClient
from tests.test_whatsapp_agent import _greet
from tests.test_whatsapp_agent import _quotes
from tests.test_whatsapp_agent import record
from tests.test_whatsapp_agent import reply
from tests.test_whatsapp_agent import system_of
from tests.test_whatsapp_agent import text
from tests.test_whatsapp_agent import tool_use
from tests.test_whatsapp_agent import world  # noqa: F401 - fixture

PRICES = "Ladrillo 1450 con IVA, cemento 12900 con IVA, cal 7800 con IVA, todo flete incluido"


def _three_records(world):  # noqa: F811 - fixture object
    by_name = {rfq.item_name: rfq for rfq in world.rfqs}

    return [
        record(by_name["Ladrillo hueco portante 18x19x33"].id, "ladrillo 1450 con iva", price=1450, id_="t1"),
        record(by_name["Cemento CPN40"].id, "cemento 12900 con iva", price=12900, id_="t2"),
        record(by_name["Cal hidráulica"].id, "cal 7800 con iva", price=7800, id_="t3"),
    ]


def test_max_tokens_with_complete_tool_uses_executes_them_and_continues(db_session, world):  # noqa: F811
    conversation, _ = _greet(db_session, world)
    fake = FakeClient([
        reply(*_three_records(world), stop="max_tokens"),
        reply(text("Anoté ladrillo, cemento y cal. ¿Me pasás arena y hierro?")),
    ])

    result = handle_inbound(db_session, conversation.id, PRICES, client=fake)

    assert len(_quotes(db_session, conversation)) == 3
    assert len(fake.calls) == 2  # el loop siguió después del corte
    assert result.outbound.body == "Anoté ladrillo, cemento y cal. ¿Me pasás arena y hierro?"
    assert conversation.status == "open"
    assert FLAG_MAX_TOKENS_EXHAUSTED not in result.outbound.guardrail_flags
    assert FLAG_TECHNICAL_ERROR not in result.outbound.guardrail_flags

    records = [c for c in result.outbound.tool_calls if c["name"] == "record_quote"]
    assert len(records) == 3
    assert all(c["stop_reason"] == "max_tokens" for c in records)

    model_calls = next(c for c in result.outbound.tool_calls if c["name"] == "model_calls")
    assert model_calls["stop_reasons"] == ["max_tokens", "end_turn"]


def test_max_tokens_without_tool_uses_is_a_technical_error_not_empty_reply(db_session, world):  # noqa: F811
    conversation, _ = _greet(db_session, world)
    fake = FakeClient([reply(text("Voy a registrar los cinco ít"), stop="max_tokens")])

    result = handle_inbound(db_session, conversation.id, PRICES, client=fake)

    assert conversation.status == "open"
    assert result.outbound.body == whatsapp_settings.WHATSAPP_SAFE_REPLY
    assert FLAG_MAX_TOKENS_EXHAUSTED in result.outbound.guardrail_flags
    assert FLAG_TECHNICAL_ERROR in result.outbound.guardrail_flags
    assert guardrails.FLAG_EMPTY_REPLY not in result.outbound.guardrail_flags
    assert _quotes(db_session, conversation) == []
    assert result.outbound.is_draft


def test_empty_reply_keeps_open_and_the_next_inbound_runs_the_agent_again(db_session, world):  # noqa: F811
    conversation, _ = _greet(db_session, world)

    first = handle_inbound(db_session, conversation.id, "hola?", client=FakeClient([reply(stop="end_turn")]))

    assert conversation.status == "open"
    assert first.outbound.body == whatsapp_settings.WHATSAPP_SAFE_REPLY
    assert guardrails.FLAG_EMPTY_REPLY in first.outbound.guardrail_flags
    assert FLAG_TECHNICAL_ERROR in first.outbound.guardrail_flags

    fake = FakeClient([reply(text("Acá estoy. ¿Me pasás precio del cemento?"))])
    second = handle_inbound(db_session, conversation.id, "sigue ahí?", client=fake)

    assert len(fake.calls) == 1  # el agente volvió a correr
    assert second.outbound.body == "Acá estoy. ¿Me pasás precio del cemento?"
    assert conversation.status == "open"


def test_freight_correction_updates_the_same_quote_with_new_evidence(db_session, world):  # noqa: F811
    """Turno A: flete "solo zona norte" -> null. Turno B: "te lo incluyo igual" -> true, mismo quote."""

    conversation, _ = _greet(db_session, world)
    cemento = next(rfq for rfq in world.rfqs if rfq.item_name == "Cemento CPN40")

    handle_inbound(
        db_session, conversation.id,
        "Cemento 12900 con IVA. Flete incluido solo zona norte, Palermo no entra.",
        client=FakeClient([
            reply(record(cemento.id, "cemento 12900 con iva", price=12900, freight_included=None, id_="a1")),
            reply(text("Anotado. ¿El flete a Gorriti 4800 lo cotizás aparte?")),
        ]),
    )

    quotes = _quotes(db_session, conversation)
    assert len(quotes) == 1
    assert quotes[0].freight_included is None
    first_id = quotes[0].id

    result = handle_inbound(
        db_session, conversation.id,
        "Te lo incluyo igual, flete a la obra sin cargo.",
        client=FakeClient([
            reply(record(cemento.id, "te lo incluyo igual", price=12900, freight_included=True, id_="b1")),
            reply(text("Gracias, queda con flete incluido.")),
        ]),
    )

    quotes = _quotes(db_session, conversation)
    assert len(quotes) == 1, "la corrección no debe duplicar el quote"
    assert quotes[0].id == first_id
    assert quotes[0].freight_included is True
    assert float(quotes[0].unit_price) == 12900
    assert quotes[0].unparsed_notes == "te lo incluyo igual"  # la evidencia nueva

    call = result.outbound.tool_calls[0]
    assert call["ok"] is True, call["result"]
    assert "Actualizado" in call["result"]


# ------------------------------------------------ Q3: ya consultado al comprador
def test_system_prompt_lists_asked_buyer_questions_once(db_session, world):  # noqa: F811
    conversation, _ = _greet(db_session, world)
    question = "El proveedor pregunta si se acepta CPC40 en vez de CPN40."

    handle_inbound(db_session, conversation.id, "aceptan CPC40?", client=FakeClient([
        reply(tool_use("ask_buyer", {"question": question}, id_="q1"), tool_use("ask_buyer", {"question": question.lower()}, id_="q2")),
        reply(text("Lo consulto y te confirmo.")),
    ]))

    fake = FakeClient([reply(text("Sigue pendiente con el comprador."))])
    handle_inbound(db_session, conversation.id, "y? aceptan CPC40?", client=fake)

    system = system_of(fake.calls[0])
    block = system.split("YA CONSULTADO AL COMPRADOR", 1)[1].split("REGLAS", 1)[0]
    assert "No vuelvas a consultar lo que ya figura acá" in block
    assert block.count("CPC40") == 1  # una sola vez, aunque se llamó dos veces
    assert f"- {question}" in block


# ------------------------------------------- Q4: cierre en la última vuelta permitida
def test_set_status_with_text_in_the_last_round_is_kept(db_session, world, monkeypatch):  # noqa: F811
    monkeypatch.setattr(whatsapp_settings, "WHATSAPP_MAX_TOOL_ROUNDS", 5)
    conversation, _ = _greet(db_session, world)
    fake = FakeClient([
        *[reply(tool_use("ask_buyer", {"question": f"pregunta {i}"}, id_=f"t{i}")) for i in range(4)],
        # L5a: complete exige cotizaciones completas; para probar el cierre en la última
        # vuelta alcanza con supplier_declined, que cierra igual.
        reply(
            text("Lo tengo. Constructora Palermo lo revisa y te escribimos."),
            tool_use("set_status", {"status": "supplier_declined", "reason": "no cotiza el rubro"}, id_="t5"),
        ),
    ])

    result = handle_inbound(db_session, conversation.id, "eso es todo", client=fake)

    assert conversation.status == "supplier_declined"
    assert result.outbound.body == "Lo tengo. Constructora Palermo lo revisa y te escribimos."
    assert "tool_rounds_exhausted" not in result.outbound.guardrail_flags
    assert FLAG_TECHNICAL_ERROR not in result.outbound.guardrail_flags
    assert conversation.model_calls == 5


# -------------------------------------------------------- Q7: prompt caching
def test_request_carries_cache_control_and_usage_is_recorded(db_session, world):  # noqa: F811
    conversation, _ = _greet(db_session, world)
    fake = FakeClient([
        reply(record(world.cemento.id, "cemento 12900 con iva", price=12900), usage=(300, 40), cache=(4200, 0)),
        reply(text("Gracias."), usage=(120, 10), cache=(0, 4200)),
    ])

    result = handle_inbound(db_session, conversation.id, "Cemento 12900 con IVA", client=fake)

    for call in fake.calls:
        system = call["system"]
        assert isinstance(system, list) and system[0]["type"] == "text"
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        assert call["tools"][-1]["cache_control"] == {"type": "ephemeral"}
        assert all("cache_control" not in tool for tool in call["tools"][:-1])

    model_calls = next(c for c in result.outbound.tool_calls if c["name"] == "model_calls")
    assert model_calls["cache_creation_input_tokens"] == 4200
    assert model_calls["cache_read_input_tokens"] == 4200
    assert [u["cache_read_input_tokens"] for u in model_calls["usage"]] == [0, 4200]
    # La conversación acumula todo lo que entró: 300 + 4200 + 120 + 4200.
    assert conversation.input_tokens == 8820
    assert conversation.output_tokens == 50


def test_purchase_commitment_still_closes_in_needs_human(db_session, world):  # noqa: F811
    conversation, _ = _greet(db_session, world)
    fake = FakeClient([reply(text("Dale, confirmo la compra y te transfiero la seña."))])

    result = handle_inbound(db_session, conversation.id, "confirmame", client=fake)

    assert conversation.status == "needs_human"
    assert guardrails.FLAG_PURCHASE_COMMITMENT in result.outbound.guardrail_flags
    assert FLAG_TECHNICAL_ERROR not in result.outbound.guardrail_flags
    assert result.outbound.body == whatsapp_settings.WHATSAPP_SAFE_REPLY
    assert db_session.query(SupplierQuote).count() == 0
