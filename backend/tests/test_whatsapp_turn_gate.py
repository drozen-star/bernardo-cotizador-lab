"""Lab L5b: un turno a la vez por conversación; una ráfaga corre el agente una sola vez."""

import threading
import time
from types import SimpleNamespace as NS

import pytest

from app.features.whatsapp import inbound as inbound_flow
from app.features.whatsapp.model import WhatsappMessage
from app.features.whatsapp.service import open_conversation
from app.features.whatsapp.turn_gate import TurnGate
from app.features.whatsapp.turn_gate import gate
from tests.test_whatsapp_agent import _greet
from tests.test_whatsapp_agent import world  # noqa: F401 - fixture


class SlowClient:
    """Cliente falso que tarda: cada llamada duerme y devuelve un texto."""

    def __init__(self, delay: float):
        self.delay = delay
        self.calls: list[float] = []
        self.messages = self
        self._lock = threading.Lock()

    def create(self, **kwargs):
        with self._lock:
            self.calls.append(time.monotonic())

        time.sleep(self.delay)

        return NS(
            content=[NS(type="text", text="Lo tengo. Me pasás precio del cemento?")],
            stop_reason="end_turn",
            usage=NS(input_tokens=10, output_tokens=5),
        )


@pytest.fixture(autouse=True)
def _reset_gate():
    gate.reset()
    inbound_flow._in_flight.clear()
    yield
    gate.reset()
    inbound_flow._in_flight.clear()


def _run_jobs(jobs):
    threads = [threading.Thread(target=inbound_flow.run_agent_job, args=args) for args in jobs]
    started = time.monotonic()

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join(timeout=15)

    return time.monotonic() - started


# ------------------------------------------------------------- unidad: gate
def test_gate_counts_and_cleans_up():
    own = TurnGate()

    assert own.enqueue(1) == 1 and own.enqueue(1) == 2
    assert own.acquire(1) == 1  # queda uno más nuevo esperando
    own.release(1)
    assert own.acquire(1) == 0
    own.release(1)
    assert own.pending(1) == 0
    assert own._entries == {}  # sin pendientes ni esperando: la entrada se borra


# ------------------------------------------------------------- ráfaga
def test_burst_of_three_runs_the_agent_once_and_keeps_all_inbounds(db_session, world, monkeypatch):  # noqa: F811
    conversation, _ = _greet(db_session, world)
    slow = SlowClient(delay=0.3)
    monkeypatch.setattr(inbound_flow, "agent_client_factory", lambda: slow)

    jobs = []

    for index in range(3):
        wa_id = f"wamid.burst.{index}"
        inbound_flow._in_flight.add(wa_id)
        gate.enqueue(conversation.id)  # lo que hace decide() por cada inbound
        jobs.append((conversation.id, f"mensaje {index}", wa_id))

    _run_jobs(jobs)

    assert len(slow.calls) == 1, "el agente tiene que correr una sola vez para la ráfaga"

    db_session.expire_all()
    inbounds = db_session.query(WhatsappMessage).filter(
        WhatsappMessage.conversation_id == conversation.id, WhatsappMessage.direction == "inbound"
    ).all()
    assert sorted(m.wa_message_id for m in inbounds if m.wa_message_id) == ["wamid.burst.0", "wamid.burst.1", "wamid.burst.2"]

    outbounds = db_session.query(WhatsappMessage).filter(
        WhatsappMessage.conversation_id == conversation.id, WhatsappMessage.direction == "outbound"
    ).all()
    assert len(outbounds) == 2  # apertura (enviada) + la única respuesta de la ráfaga
    assert gate.pending(conversation.id) == 0
    assert inbound_flow._in_flight == set()


def test_two_conversations_run_in_parallel(db_session, world, monkeypatch):  # noqa: F811
    first, _ = _greet(db_session, world)
    second = open_conversation(db_session, world.other_batch.id, world.supplier.id)
    # La segunda también necesita su apertura para que el agente corra en el turno siguiente.
    from app.features.whatsapp.service import handle_inbound
    from tests.test_whatsapp_agent import FakeClient

    opening = handle_inbound(db_session, second.id, "Hola Bernardo, mandame el otro pedido", client=FakeClient())
    opening.outbound.sent_at = opening.outbound.created_at
    db_session.commit()

    slow = SlowClient(delay=0.4)
    monkeypatch.setattr(inbound_flow, "agent_client_factory", lambda: slow)
    gate.enqueue(first.id)
    gate.enqueue(second.id)

    elapsed = _run_jobs([(first.id, "precio?", "wamid.p1"), (second.id, "precio?", "wamid.p2")])

    assert len(slow.calls) == 2
    assert elapsed < 0.4 * 1.8, f"las dos conversaciones tendrían que correr a la vez (tardó {elapsed:.2f}s)"
    assert gate.pending(first.id) == 0 and gate.pending(second.id) == 0
