"""Diagnóstico de una respuesta cortada por ``max_tokens`` (L5f, decisión D6).

Función pura: describe qué venía en la respuesta (texto y bloques de herramientas) para que
quede en ``tool_calls`` del outbound y se pueda ver por qué se gastaron los tokens, sin
guardar el input completo de las herramientas ni más de 2.000 caracteres de texto.
"""

import json
from typing import Any

DIAGNOSTIC_NAME = "max_tokens_diagnostic"
TEXT_HEAD_CHARS = 2000


def _block_summary(block: Any) -> dict:
    kind = getattr(block, "type", "")

    if kind == "tool_use":
        tool_input = getattr(block, "input", None)
        tool_input = tool_input if isinstance(tool_input, dict) else {}

        return {
            "type": "tool_use",
            "name": getattr(block, "name", None),
            "input_keys": sorted(tool_input),
            "input_chars": len(json.dumps(tool_input, ensure_ascii=False, default=str)),
        }

    return {"type": "text", "name": None, "input_keys": [], "input_chars": 0}


def truncation_diagnostic(content: list[Any], discarded_tool: str | None = None) -> dict:
    """Resumen de una respuesta con ``stop_reason == "max_tokens"``."""

    text = "".join(getattr(block, "text", "") for block in content if getattr(block, "type", "") == "text")

    return {
        "name": DIAGNOSTIC_NAME,
        "text_head": text[:TEXT_HEAD_CHARS],
        "text_chars": len(text),
        "blocks": [_block_summary(block) for block in content],
        "discarded_tool": discarded_tool,
    }
