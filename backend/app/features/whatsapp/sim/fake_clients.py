"""Clientes falsos para correr el simulador sin API (costo 0).

* ``FakeSupplierClient``: devuelve el guion ``offline_turns`` de la persona, un mensaje
  por llamada; cuando se acaba, ``[FIN]``.
* ``FakeAgentClient``: un "modelo" de reglas con la forma del SDK. Lee la ficha del system
  prompt para conocer los ``rfq_id``, saca precios del último mensaje del proveedor y
  emite ``record_quote`` con la evidencia literal, deriva preguntas con ``ask_buyer`` y
  cierra con ``set_status``. No pretende ser inteligente: sirve para que el runner, el juez
  y el reporte se ejerciten de punta a punta en los tests.

Las respuestas usan ``SimpleNamespace`` con los mismos atributos que los bloques del SDK.
"""

import re
from decimal import Decimal
from decimal import InvalidOperation
from types import SimpleNamespace as NS
from typing import Any

from app.features.whatsapp import tools
from app.features.whatsapp.loop import system_text

_USAGE = NS(input_tokens=0, output_tokens=0)

_SHEET_ROW = re.compile(r"- rfq_id (\d+) \| ([^|]+) \|")
_TAG = re.compile(r"</?\s*[a-z_]+\s*>", re.IGNORECASE)

# Condiciones comerciales en el mensaje del proveedor (L5a: sin ellas el ítem queda incompleto y
# set_status complete se rechaza). Solo se toma lo que está escrito; si no está, null.
_LEAD_TIME = re.compile(r"(?:entrega|plazo)[^.\n]*?(\d+)\s*(horas?|d[ií]as?)", re.IGNORECASE)
_LEAD_TIME_ALT = re.compile(r"(\d+)\s*(horas?|d[ií]as?)\s+(?:de entrega|h[aá]biles)", re.IGNORECASE)
_PAYMENT = re.compile(r"\b(contado(?:\s+(?:o|y)\s+transferencia)?|transferencia|cheque)\b", re.IGNORECASE)
_PAYMENT_DAYS = re.compile(r"pago[^.\n]*?(\d+\s*d[ií]as)", re.IGNORECASE)
_VALIDITY = re.compile(r"validez[^.\n]*?(\d+\s*(?:horas?|d[ií]as?)|hasta el \d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE)

# L5f: condiciones para todo el pedido (régimen y flete) -> record_terms, con evidencia literal.
_TERMS_SNIPPET = re.compile(r"((?:flete|factura|facturado|efectivo|con iva|\+ iva)[^.\n]{0,80})", re.IGNORECASE)
_FREIGHT_COST = re.compile(r"flete[^.\n\d]{0,30}?(\d[\d.,]*\d)", re.IGNORECASE)
_FREE_OVER = re.compile(r"(?:sin cargo|gratis|bonificado)[^.\n\d]{0,25}?(\d[\d.,]*\d)", re.IGNORECASE)


def _terms(low: str) -> dict | None:
    """Lo que el proveedor dijo del régimen y del flete, o None si no dijo nada de eso."""

    if "factura a" in low or "facturado" in low or "con factura" in low:
        regime = "facturado"
    elif "efectivo" in low or "sin factura" in low:
        regime = "efectivo"
    elif "una parte" in low and "factur" in low:
        regime = "parcial"
    elif "con iva" in low or "+ iva" in low:
        regime = "facturado"  # un precio con IVA discriminado es un precio facturado
    else:
        regime = None

    included = True if "flete incluido" in low else (False if "flete aparte" in low else None)
    cost_match = _FREIGHT_COST.search(low)
    cost = _ar_number(cost_match.group(1)) if cost_match else None
    over_match = _FREE_OVER.search(low)
    free_over = _ar_number(over_match.group(1)) if over_match else None

    if regime is None and included is None and cost is None and free_over is None:
        return None

    return {
        "billing_regime": regime,
        "documented_pct": None,
        "freight_included": included,
        "freight_cost": float(cost) if cost is not None else None,
        "freight_basis": ("viaje" if "por viaje" in low else ("pedido" if cost is not None else None)),
        "freight_free_over": float(free_over) if free_over is not None else None,
    }


def _lead_time_days(low: str) -> int | None:
    match = _LEAD_TIME.search(low) or _LEAD_TIME_ALT.search(low)

    if not match:
        return None

    amount = int(match.group(1))

    # Horas -> días redondeando hacia arriba (72 h = 3 días, 48 h = 2 días, 30 h = 2 días).
    return -(-amount // 24) if match.group(2).lower().startswith("h") else amount


def _payment_terms(low: str) -> str | None:
    match = _PAYMENT.search(low) or _PAYMENT_DAYS.search(low)

    return match.group(1) if match else None


def _validity(low: str) -> str | None:
    match = _VALIDITY.search(low)

    return match.group(1) if match else None


def text_block(value: str) -> NS:
    return NS(type="text", text=value)


def tool_block(name: str, tool_input: dict, id_: str) -> NS:
    return NS(type="tool_use", name=name, input=tool_input, id=id_)


def response(blocks: list, *, stop: str | None = None) -> NS:
    stop = stop or ("tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn")

    return NS(content=blocks, stop_reason=stop, usage=_USAGE)


# --------------------------------------------------------------- proveedor
class _FakeSupplierMessages:
    def __init__(self, turns: list[str]):
        self.turns = list(turns)
        self.calls = 0

    def create(self, **kwargs: Any) -> NS:
        self.calls += 1
        text = self.turns.pop(0) if self.turns else "[FIN]"

        return response([text_block(text)])


class FakeSupplierClient:
    def __init__(self, turns: list[str]):
        self.messages = _FakeSupplierMessages(turns)


# ------------------------------------------------------------------- agente
def _ar_number(token: str) -> Decimal | None:
    """Lectura argentina: punto de miles, coma decimal."""

    try:
        return Decimal(token.replace(".", "").replace(",", "."))
    except InvalidOperation:
        return None


def _last_user_text(messages: list[dict]) -> str | list:
    content = messages[-1]["content"]

    if isinstance(content, str):
        return _TAG.sub("", content).strip()

    return content


#: Por debajo de esto no es un precio en pesos: es una medida ("18x19x33", "hierro de 8").
MIN_PRICE = Decimal("100")


class _FakeAgentMessages:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._counter = 0
        self._closed = False
        self._registered = False

    def _id(self) -> str:
        self._counter += 1
        return f"fake_{self._counter}"

    def create(self, **kwargs: Any) -> NS:
        self.calls.append(kwargs)
        # El system llega como lista de bloques (prompt caching, L3b) o como string.
        items = {name.strip(): int(rfq_id) for rfq_id, name in _SHEET_ROW.findall(system_text(kwargs.get("system")))}
        last = _last_user_text(kwargs["messages"])

        if isinstance(last, list):
            return self._after_tools(last)

        return self._reply_to_supplier(last, items)

    # ---- después de un tool_result: cerrar si ya está todo, si no seguir --------
    def _after_tools(self, results: list) -> NS:
        joined = " ".join(str(r.get("content", "")) for r in results if isinstance(r, dict))

        if "sin registrar todavía: ninguno" in joined and not self._closed:
            self._closed = True
            return response([tool_block(tools.SET_STATUS, {"status": "complete", "reason": "todos los ítems cotizados"}, self._id())])

        if "Estado pedido:" in joined:
            return response([], stop="end_turn")

        return response([text_block("Gracias. Quedo atento a lo que falte, y cualquier duda me escribís.")])

    # ---- respuesta a un mensaje del proveedor ---------------------------------
    def _reply_to_supplier(self, text: str, items: dict[str, int]) -> NS:
        low = text.lower()

        if "ignorá" in low or "asistente general" in low:
            return response([text_block("Sigo con la cotización del pedido. Cuando puedas, pasame precio de los ítems de la lista.")])

        if any(phrase in low for phrase in ("no los trabajamos", "no trabajamos", "solo sanitarios", "no vendemos")):
            return response([tool_block(tools.SET_STATUS, {"status": "supplier_declined", "reason": "no trabaja el rubro"}, self._id())])

        if "persona" in low and any(word in low for word in ("bot", "teléfono", "telefono", "hablar")):
            return response([tool_block(tools.SET_STATUS, {"status": "needs_human", "reason": "pide hablar con una persona"}, self._id())])

        # El proveedor dio por terminado lo suyo y ya hay algo registrado: se cierra.
        if self._registered and not self._closed and any(p in low for p in ("eso es todo", "nada más", "listo.")):
            self._closed = True
            return response([tool_block(tools.SET_STATUS, {"status": "complete", "reason": "el proveedor cotizó lo que maneja"}, self._id())])

        blocks = []

        if "?" in text and any(word in low for word in ("sirve", "aceptan", "cortado", "hidrogrúa", "hidrogrua", "cerramiento", "va?")):
            blocks.append(tool_block(tools.ASK_BUYER, {"question": text[:300]}, self._id()))

        iva = True if "con iva" in low else (False if ("+ iva" in low or "sin iva" in low) else None)
        freight = True if "flete incluido" in low else (False if "flete aparte" in low else None)
        lead_time_days = _lead_time_days(low)
        payment_terms = _payment_terms(low)
        validity = _validity(low)
        terms = _terms(low)
        snippet = _TERMS_SNIPPET.search(text)

        if terms is not None and snippet is not None:
            blocks.append(tool_block(tools.RECORD_TERMS, {**terms, "evidence": snippet.group(1).strip()}, self._id()))

        for item, rfq_id in items.items():
            keyword = item.split()[0].lower()
            # El número no puede ir seguido de "x" (es una dimensión) ni ser menor a MIN_PRICE.
            match = re.search(rf"\b{re.escape(keyword)}\b[^\d\n]{{0,40}}?(\d[\d.,]*\d|\d)(?![\dx])", low)

            if not match:
                continue

            price = _ar_number(match.group(1))

            if price is None or price < MIN_PRICE:
                continue

            self._registered = True
            blocks.append(
                tool_block(
                    tools.RECORD_QUOTE,
                    {
                        "rfq_id": rfq_id,
                        "unit_price": float(price),
                        "currency": "ARS",
                        "iva_included": iva,
                        "freight_included": freight,
                        "lead_time_days": lead_time_days,
                        "payment_terms": payment_terms,
                        "validity": validity,
                        "evidence": match.group(0),
                    },
                    self._id(),
                )
            )

        if blocks:
            return response(blocks)

        return response([text_block("Gracias. Me pasás precio unitario, si incluye IVA y flete, y plazo de lo que tengas?")])


class FakeAgentClient:
    def __init__(self) -> None:
        self.messages = _FakeAgentMessages()
