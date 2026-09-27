"""Lab L3b: el presupuesto corta antes de llamar y contabiliza por rol."""

from types import SimpleNamespace as NS

import pytest

from app.features.whatsapp.sim.budget import Budget
from app.features.whatsapp.sim.budget import BudgetExceeded
from app.features.whatsapp.sim.budget import BudgetedClient
from app.features.whatsapp.sim.budget import estimate_input_tokens
from app.features.whatsapp.sim.settings import DEFAULT_PRICES

PRICES = {model: (pair[0], pair[1]) for model, pair in DEFAULT_PRICES.items()}


class RecordingClient:
    def __init__(self):
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        return NS(
            content=[NS(type="text", text="hola")],
            stop_reason="end_turn",
            usage=NS(input_tokens=1000, output_tokens=500),
        )


def test_cost_uses_the_price_table():
    budget = Budget(3.0, PRICES)

    # 1M de entrada a 2 USD + 1M de salida a 10 USD.
    assert budget.cost("claude-sonnet-5", 1_000_000, 1_000_000) == 12.0
    assert budget.cost("claude-haiku-4-5-20251001", 1_000_000, 0) == 1.0


def test_unknown_model_is_an_error_not_a_free_call():
    with pytest.raises(ValueError, match="SIM_PRICES_JSON"):
        Budget(3.0, PRICES).cost("modelo-desconocido", 10, 10)


def test_budget_stops_before_the_call_that_would_exceed_it():
    inner = RecordingClient()
    # Tope de 0,004 USD: la primera llamada (1000 in + 500 out de sonnet = 0,007) ya lo supera.
    budget = Budget(0.004, PRICES)
    client = BudgetedClient(inner, budget, "agent")

    with pytest.raises(BudgetExceeded):
        client.messages.create(model="claude-sonnet-5", max_tokens=1024, system="x" * 300, messages=[{"role": "user", "content": "hola"}])

    assert inner.calls == 0
    assert budget.exceeded
    assert budget.spent_usd == 0


def test_usage_is_accumulated_per_role_and_records_keep_the_text():
    inner = RecordingClient()
    budget = Budget(3.0, PRICES)
    agent = BudgetedClient(inner, budget, "agent")
    supplier = BudgetedClient(inner, budget, "supplier")

    agent.messages.create(model="claude-sonnet-5", max_tokens=100, system="s", messages=[{"role": "user", "content": "a"}])
    agent.messages.create(model="claude-sonnet-5", max_tokens=100, system="s", messages=[{"role": "user", "content": "a"}])
    supplier.messages.create(model="claude-haiku-4-5-20251001", max_tokens=100, system="s", messages=[{"role": "user", "content": "a"}])

    snapshot = budget.snapshot()
    assert snapshot["by_role"]["agent"]["calls"] == 2
    assert snapshot["by_role"]["agent"]["input_tokens"] == 2000
    assert snapshot["by_role"]["agent"]["cost_usd"] == pytest.approx(2 * (1000 * 2 + 500 * 10) / 1e6)
    assert snapshot["by_role"]["supplier"]["cost_usd"] == pytest.approx((1000 * 1 + 500 * 5) / 1e6)
    assert budget.spent_usd == pytest.approx(snapshot["by_role"]["agent"]["cost_usd"] + snapshot["by_role"]["supplier"]["cost_usd"])
    assert agent.records[0].text == "hola"
    assert inner.calls == 3


def test_cache_tokens_are_priced_at_write_1_25x_and_read_0_1x():
    budget = Budget(3.0, PRICES)

    # sonnet: entrada 2 USD/M. 1M escritos en caché = 2,5; 1M leídos = 0,2; 1M sin caché = 2.
    assert budget.cost("claude-sonnet-5", 0, 0, cache_creation_input_tokens=1_000_000) == pytest.approx(2.5)
    assert budget.cost("claude-sonnet-5", 0, 0, cache_read_input_tokens=1_000_000) == pytest.approx(0.2)
    assert budget.cost("claude-sonnet-5", 1_000_000, 0) == pytest.approx(2.0)
    assert budget.cost("claude-sonnet-5", 100, 50, 4000, 8000) == pytest.approx(
        (100 * 2 + 4000 * 2 * 1.25 + 8000 * 2 * 0.1 + 50 * 10) / 1e6
    )


def test_budgeted_client_records_cache_usage_per_call():
    class CachedClient:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            return NS(
                content=[NS(type="text", text="ok")],
                stop_reason="end_turn",
                usage=NS(input_tokens=100, output_tokens=10, cache_creation_input_tokens=4000, cache_read_input_tokens=0),
            )

    budget = Budget(3.0, PRICES)
    client = BudgetedClient(CachedClient(), budget, "agent")
    client.messages.create(model="claude-sonnet-5", max_tokens=50, system="s", messages=[{"role": "user", "content": "a"}])

    usage = budget.snapshot()["by_role"]["agent"]
    assert usage["cache_creation_input_tokens"] == 4000
    assert usage["total_input_tokens"] == 4100
    assert usage["cost_usd"] == pytest.approx((100 * 2 + 4000 * 2 * 1.25 + 10 * 10) / 1e6)
    assert client.records[0].cache_creation_input_tokens == 4000


def test_estimate_counts_system_and_messages():
    assert estimate_input_tokens("a" * 300, [{"role": "user", "content": "b" * 300}]) == 201
    assert estimate_input_tokens("", [{"role": "user", "content": [{"type": "tool_result", "content": "c" * 30}]}]) == 11
