"""Presupuesto de la corrida: costo acumulado de agente y proveedor, con corte previo.

Antes de cada llamada se estima el peor caso (tokens de entrada estimados por largo del
texto más ``max_tokens`` de salida). Si sumado a lo gastado supera el tope, se lanza
``BudgetExceeded`` **antes** de llamar, y el runner corta la corrida limpia.

``BudgetedClient`` envuelve cualquier cliente con la forma del SDK (real o falso), así que
el agente y el proveedor no saben que están contados. Además guarda por llamada el texto
que devolvió el modelo, para que el transcript y el juez vean la respuesta *antes* de los
frenos de salida.
"""

from dataclasses import dataclass
from dataclasses import field
from typing import Any

#: Caracteres por token, conservador para castellano con acentos.
CHARS_PER_TOKEN = 3.0

#: Multiplicadores sobre el precio de entrada (doc "Prompt caching", sección "Pricing"):
#: escritura de caché de 5 min 1,25x; lectura 0,1x para Sonnet y Haiku.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1


class BudgetExceeded(RuntimeError):
    """El próximo llamado, en el peor caso, superaría el tope."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    calls: int = 0
    cost_usd: float = 0.0

    def add(
        self,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
    ) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_creation_input_tokens += cache_creation_input_tokens
        self.cache_read_input_tokens += cache_read_input_tokens
        self.calls += 1
        self.cost_usd += cost_usd

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens

    def as_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "total_input_tokens": self.total_input_tokens,
            "calls": self.calls,
            "cost_usd": round(self.cost_usd, 6),
        }


def estimate_input_tokens(system: Any, messages: list[dict]) -> int:
    """Estimación por largo de texto. Solo para el corte previo; el costo real usa ``usage``."""

    chars = len(str(system or ""))

    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else None

        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    chars += len(str(block.get("content") or block.get("text") or block.get("input") or ""))
                else:
                    chars += len(str(getattr(block, "text", "") or getattr(block, "input", "") or ""))

    return int(chars / CHARS_PER_TOKEN) + 1


class Budget:
    def __init__(self, limit_usd: float, prices: dict[str, tuple[float, float]]):
        self.limit_usd = float(limit_usd)
        self.prices = dict(prices)
        self.by_role: dict[str, Usage] = {}
        self.exceeded = False

    # ----------------------------------------------------------------- precios
    def price_for(self, model: str) -> tuple[float, float]:
        try:
            return self.prices[model]
        except KeyError as exc:
            known = ", ".join(sorted(self.prices)) or "(ninguno)"
            raise ValueError(
                f"No hay precio configurado para el modelo '{model}'. Conocidos: {known}. "
                "Agregalo en SIM_PRICES_JSON."
            ) from exc

    def cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
    ) -> float:
        price_in, price_out = self.price_for(model)

        return (
            input_tokens * price_in
            + cache_creation_input_tokens * price_in * CACHE_WRITE_MULTIPLIER
            + cache_read_input_tokens * price_in * CACHE_READ_MULTIPLIER
            + output_tokens * price_out
        ) / 1_000_000

    @property
    def spent_usd(self) -> float:
        return sum(usage.cost_usd for usage in self.by_role.values())

    # ------------------------------------------------------------------- corte
    def check(self, model: str, estimated_input_tokens: int, max_output_tokens: int) -> None:
        worst_case = self.cost(model, estimated_input_tokens, max_output_tokens)

        if self.spent_usd + worst_case > self.limit_usd:
            self.exceeded = True
            raise BudgetExceeded(
                f"Gastado {self.spent_usd:.4f} USD; el próximo llamado a {model} podría costar "
                f"{worst_case:.4f} USD y el tope es {self.limit_usd:.2f} USD."
            )

    def add(
        self,
        role: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
    ) -> float:
        cost = self.cost(model, input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens)
        self.by_role.setdefault(role, Usage()).add(
            input_tokens, output_tokens, cost, cache_creation_input_tokens, cache_read_input_tokens
        )

        return cost

    def snapshot(self) -> dict:
        return {
            "limit_usd": self.limit_usd,
            "spent_usd": round(self.spent_usd, 6),
            "exceeded": self.exceeded,
            "by_role": {role: usage.as_dict() for role, usage in self.by_role.items()},
        }


# ------------------------------------------------------------ cliente envuelto
@dataclass
class CallRecord:
    role: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    #: Texto que devolvió el modelo en esta llamada, antes de cualquier freno.
    text: str
    tools: list[str] = field(default_factory=list)
    #: "end_turn" | "tool_use" | "max_tokens" ...: la evidencia de un corte por largo.
    stop_reason: str | None = None
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class BudgetedMessages:
    def __init__(self, inner: Any, budget: Budget, role: str):
        self._inner = inner
        self._budget = budget
        self._role = role
        self.records: list[CallRecord] = []

    def create(self, **kwargs: Any) -> Any:
        model = kwargs["model"]
        max_tokens = int(kwargs.get("max_tokens") or 0)

        self._budget.check(model, estimate_input_tokens(kwargs.get("system"), kwargs.get("messages") or []), max_tokens)

        response = self._inner.messages.create(**kwargs)

        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cache_creation = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cost = self._budget.add(self._role, model, input_tokens, output_tokens, cache_creation, cache_read)

        content = list(getattr(response, "content", []) or [])
        self.records.append(
            CallRecord(
                role=self._role,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
                text="".join(getattr(b, "text", "") for b in content if getattr(b, "type", "") == "text").strip(),
                tools=[getattr(b, "name", "") for b in content if getattr(b, "type", "") == "tool_use"],
                stop_reason=getattr(response, "stop_reason", None),
                cache_creation_input_tokens=cache_creation,
                cache_read_input_tokens=cache_read,
            )
        )

        return response


class BudgetedClient:
    """Cliente con la forma del SDK (``.messages.create``) que descuenta del presupuesto."""

    def __init__(self, inner: Any, budget: Budget, role: str):
        self.messages = BudgetedMessages(inner, budget, role)

    @property
    def records(self) -> list[CallRecord]:
        return self.messages.records
