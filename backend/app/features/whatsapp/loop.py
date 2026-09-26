"""Loop del agente con el SDK nativo de Anthropic (spec sección 7).

Un turno = un mensaje del proveedor. El modelo puede pedir herramientas hasta
``max_rounds`` veces; cada pedido se ejecuta con el ``execute_tool`` que inyecta el
service (es el que toca la base) y el resultado vuelve como ``tool_result``. Si se agotan
las vueltas sin una respuesta final, el turno queda marcado como agotado y el service lo
deriva a humano.

El cliente es inyectable (``ModelClient``): los tests pasan uno falso que devuelve
respuestas con la misma forma que el SDK. Nada acá llama a la red por su cuenta.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Protocol

from app.features.whatsapp.settings import whatsapp_settings


class MessagesAPI(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class ModelClient(Protocol):
    """Lo mínimo que el loop usa de ``anthropic.Anthropic``."""

    messages: MessagesAPI


@dataclass
class ToolOutcome:
    """Lo que devuelve el ejecutor de herramientas del service."""

    content: str
    ok: bool = True
    #: Estado que la herramienta pidió para la conversación (set_status), si alguno.
    status: str | None = None
    reason: str | None = None


ToolExecutor = Callable[[str, dict], ToolOutcome]


@dataclass
class TurnResult:
    #: Texto final del modelo (puede ser vacío si cerró solo con una herramienta).
    text: str = ""
    #: Bloques tool_use y tool_result del turno, completos, para auditoría.
    tool_calls: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    #: True si se agotaron las vueltas sin respuesta final.
    exhausted: bool = False
    #: Último estado pedido por set_status en el turno, si alguno.
    requested_status: str | None = None
    requested_reason: str | None = None


def default_client() -> Any:
    """``anthropic.Anthropic()`` con import diferido: los tests no lo necesitan."""

    import anthropic  # noqa: PLC0415 - import diferido a propósito

    return anthropic.Anthropic()


def resolve_model() -> str:
    # El env gana sobre .env, y .env sobre el default, como en el resto de la app.
    return os.environ.get("BERNARDO_MODEL") or whatsapp_settings.BERNARDO_MODEL


def _text_of(content: list[Any]) -> str:
    return "".join(getattr(block, "text", "") for block in content if getattr(block, "type", "") == "text").strip()


def _tool_uses(content: list[Any]) -> list[Any]:
    return [block for block in content if getattr(block, "type", "") == "tool_use"]


def _usage(response: Any, attribute: str) -> int:
    usage = getattr(response, "usage", None)

    return int(getattr(usage, attribute, 0) or 0) if usage is not None else 0


def run_turn(
    *,
    client: ModelClient,
    system: str,
    history: list[dict],
    tools: list[dict],
    execute_tool: ToolExecutor,
    model: str | None = None,
    max_rounds: int | None = None,
    max_tokens: int | None = None,
) -> TurnResult:
    """Corre un turno completo y devuelve texto, herramientas y consumo.

    ``history`` es el historial de texto (user/assistant) que termina en el mensaje del
    proveedor. Los bloques de herramientas de este turno se agregan a una copia local:
    el historial persistido es solo texto (decisión de L3a).
    """

    model = model or resolve_model()
    max_rounds = max_rounds or whatsapp_settings.WHATSAPP_MAX_TOOL_ROUNDS
    max_tokens = max_tokens or whatsapp_settings.WHATSAPP_MAX_TOKENS

    messages = list(history)
    result = TurnResult()

    for _ in range(max_rounds):
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            messages=messages,
        )

        result.model_calls += 1
        result.input_tokens += _usage(response, "input_tokens")
        result.output_tokens += _usage(response, "output_tokens")

        content = list(getattr(response, "content", []) or [])
        messages.append({"role": "assistant", "content": content})

        result.text = _text_of(content)
        uses = _tool_uses(content)

        if getattr(response, "stop_reason", None) != "tool_use" or not uses:
            return result

        tool_results = []

        for use in uses:
            name = getattr(use, "name", "")
            tool_input = dict(getattr(use, "input", None) or {})

            outcome = execute_tool(name, tool_input)

            if outcome.status:
                result.requested_status = outcome.status
                result.requested_reason = outcome.reason

            result.tool_calls.append(
                {
                    "tool_use_id": getattr(use, "id", None),
                    "name": name,
                    "input": tool_input,
                    "ok": outcome.ok,
                    "result": outcome.content,
                }
            )

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": getattr(use, "id", None),
                    "content": outcome.content,
                    **({"is_error": True} if not outcome.ok else {}),
                }
            )

        messages.append({"role": "user", "content": tool_results})

    # Se agotaron las vueltas con el modelo todavía pidiendo herramientas.
    result.exhausted = True
    result.text = ""

    return result
