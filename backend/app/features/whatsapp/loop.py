"""Loop del agente con el SDK nativo de Anthropic (spec sección 7).

Un turno = un mensaje del proveedor. El modelo puede pedir herramientas hasta
``max_rounds`` veces; cada pedido se ejecuta con el ``execute_tool`` que inyecta el
service (es el que toca la base) y el resultado vuelve como ``tool_result``.

Decisiones de L3b, salidas de las corridas del simulador:

* ``stop_reason == "max_tokens"``: si la respuesta cortada trae bloques ``tool_use``
  completos, se ejecutan igual y el loop sigue; si no, el turno termina con
  ``max_tokens_exhausted`` en vez de disfrazarse de respuesta vacía.
* Última vuelta permitida: si el modelo cerró con ``set_status`` (y/o texto), se conservan
  texto y estado; ``exhausted`` solo si la última respuesta seguía pidiendo herramientas
  sin cerrar.
* Prompt caching sobre tools + system con ``cache_control: {"type": "ephemeral"}`` en la
  última tool y en el bloque de system (doc "Prompt caching", secciones "Explicit cache
  breakpoints" y "Structuring your prompt": el prefijo se cachea en el orden tools, system,
  messages). Se registran por llamada ``cache_creation_input_tokens`` y
  ``cache_read_input_tokens`` ("Tracking cache performance": ``input_tokens`` excluye lo
  cacheado). El system supera los 1.024 tokens mínimos de Sonnet ("Cache limitations").

El cliente es inyectable (``ModelClient``): los tests pasan uno falso con la misma forma
que el SDK. Nada acá llama a la red por su cuenta.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Protocol

from app.features.whatsapp.settings import whatsapp_settings
from app.features.whatsapp.tools import SET_STATUS
from app.features.whatsapp.turn_diagnostics import truncation_diagnostic

CACHE_CONTROL = {"type": "ephemeral"}


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
    #: stop_reason de cada llamada, en orden.
    stop_reasons: list[str] = field(default_factory=list)
    #: Uso por llamada: input, output, cache_creation, cache_read.
    usage_per_call: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    model_calls: int = 0
    #: True si se agotaron las vueltas con el modelo todavía pidiendo herramientas.
    exhausted: bool = False
    #: True si el modelo se cortó por max_tokens sin dejar ninguna herramienta completa.
    max_tokens_exhausted: bool = False
    #: Último estado pedido por set_status en el turno, si alguno.
    requested_status: str | None = None
    requested_reason: str | None = None
    #: L5f: un diagnóstico por cada respuesta cortada por max_tokens (va a tool_calls).
    diagnostics: list[dict] = field(default_factory=list)

    @property
    def total_input_tokens(self) -> int:
        """Todo lo que entró: sin caché + escrito en caché + leído de caché."""

        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens


def default_client() -> Any:
    """``anthropic.Anthropic()`` con import diferido: los tests no lo necesitan."""

    import anthropic  # noqa: PLC0415 - import diferido a propósito

    return anthropic.Anthropic()


def resolve_model() -> str:
    # El env gana sobre .env, y .env sobre el default, como en el resto de la app.
    return os.environ.get("BERNARDO_MODEL") or whatsapp_settings.BERNARDO_MODEL


# ------------------------------------------------------------------ caching
def with_cache_control(system: str, tools: list[dict]) -> tuple[list[dict], list[dict]]:
    """System como bloque cacheado y la última tool marcada: el prefijo entero queda en caché."""

    system_blocks = [{"type": "text", "text": system, "cache_control": dict(CACHE_CONTROL)}]

    if not tools:
        return system_blocks, []

    cached_tools = [dict(tool) for tool in tools]
    cached_tools[-1]["cache_control"] = dict(CACHE_CONTROL)

    return system_blocks, cached_tools


def system_text(system: Any) -> str:
    """El texto del system, venga como string o como lista de bloques."""

    if isinstance(system, str):
        return system

    return "".join(str(block.get("text", "")) for block in (system or []) if isinstance(block, dict))


# ------------------------------------------------------------------ helpers
def _text_of(content: list[Any]) -> str:
    return "".join(getattr(block, "text", "") for block in content if getattr(block, "type", "") == "text").strip()


def _complete_tool_uses(content: list[Any]) -> list[Any]:
    """Bloques tool_use con input usable. Un corte por max_tokens puede dejar uno vacío."""

    return [
        block
        for block in content
        if getattr(block, "type", "") == "tool_use" and isinstance(getattr(block, "input", None), dict) and block.input
    ]


def _usage(response: Any, attribute: str) -> int:
    usage = getattr(response, "usage", None)

    return int(getattr(usage, attribute, 0) or 0) if usage is not None else 0


def _record_usage(result: TurnResult, response: Any, stop_reason: Any) -> None:
    call = {
        "stop_reason": str(stop_reason),
        "input_tokens": _usage(response, "input_tokens"),
        "output_tokens": _usage(response, "output_tokens"),
        "cache_creation_input_tokens": _usage(response, "cache_creation_input_tokens"),
        "cache_read_input_tokens": _usage(response, "cache_read_input_tokens"),
    }

    result.model_calls += 1
    result.stop_reasons.append(call["stop_reason"])
    result.usage_per_call.append(call)
    result.input_tokens += call["input_tokens"]
    result.output_tokens += call["output_tokens"]
    result.cache_creation_input_tokens += call["cache_creation_input_tokens"]
    result.cache_read_input_tokens += call["cache_read_input_tokens"]


def _execute(uses: list[Any], execute_tool: ToolExecutor, result: TurnResult, stop_reason: Any) -> list[dict]:
    """Ejecuta las herramientas de una respuesta; devuelve los tool_result para el modelo."""

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
                "stop_reason": str(stop_reason),
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

    return tool_results


# ---------------------------------------------------------------------- turno
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

    system_blocks, cached_tools = with_cache_control(system, tools)
    messages = list(history)
    result = TurnResult()
    closed_in_last_round = False

    for _ in range(max_rounds):
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            tools=cached_tools,
            messages=messages,
        )

        stop_reason = getattr(response, "stop_reason", None)
        _record_usage(result, response, stop_reason)

        content = list(getattr(response, "content", []) or [])
        kept = content

        if stop_reason == "max_tokens":
            # D6 (doc "Handling stop reasons"): el último bloque tool_use de una respuesta cortada
            # está incompleto, esté vacío o no; nunca se ejecuta ni queda en el historial.
            discarded = None

            if content and getattr(content[-1], "type", "") == "tool_use":
                discarded = getattr(content[-1], "name", None)
                kept = content[:-1]

            result.diagnostics.append(truncation_diagnostic(content, discarded))

        messages.append({"role": "assistant", "content": kept})

        result.text = _text_of(content)
        uses = _complete_tool_uses(kept)

        if stop_reason == "max_tokens" and not uses:
            # Cortado a mitad de camino y sin nada ejecutable: el texto, si hay, está
            # truncado y no se manda. El service lo trata como falla técnica.
            result.max_tokens_exhausted = True
            result.text = ""
            return result

        if stop_reason != "max_tokens" and (stop_reason != "tool_use" or not uses):
            return result

        tool_results = _execute(uses, execute_tool, result, stop_reason)
        messages.append({"role": "user", "content": tool_results})

        closed_in_last_round = any(getattr(use, "name", "") == SET_STATUS for use in uses)

    if closed_in_last_round:
        # La última vuelta cerró la conversación: texto y estado se conservan.
        return result

    # Se agotaron las vueltas con el modelo todavía pidiendo herramientas sin cerrar.
    result.exhausted = True
    result.text = ""

    return result
