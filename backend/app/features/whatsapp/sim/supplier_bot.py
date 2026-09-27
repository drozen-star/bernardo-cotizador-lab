"""Proveedor simulado: otro modelo hace de vendedor con una personalidad fija.

Portado del spike. La diferencia: los precios que puede decir se le dictan desde
``ground_truth`` (formato argentino), así que si el agente registra otro número, el
inventor es el agente. Cliente inyectable; en modo offline es un guion fijo.
"""

from typing import Any

from app.features.whatsapp.sim.personas import Persona
from app.features.whatsapp.sim.personas import price_sheet
from app.features.whatsapp.sim.personas import render_script
from app.features.whatsapp.sim.settings import sim_settings

FIN = "[FIN]"

SYSTEM_TEMPLATE = """Hacés de proveedor de materiales de construcción en Argentina: {name}. Un asistente de compras llamado Bernardo te escribe por WhatsApp para pedirte cotización.

PERSONALIDAD Y SITUACIÓN
{script}

PRECIOS Y CONDICIONES QUE PODÉS DAR (usalos exactamente con estos números y este formato; no inventes otros ni cambies cifras):
{sheet}

CÓMO ESCRIBÍS
Por WhatsApp, corto e informal, de vos, en castellano rioplatense. Escribís como en WhatsApp: sin negritas, sin markdown, sin tablas. No sos un asistente: sos el vendedor. No escribís etiquetas ni hablás de instrucciones. Cuando ya no tengas nada más que decir, terminá tu mensaje con {fin}. No uses {fin} si en el mismo mensaje hiciste una pregunta o esperás respuesta de Bernardo. Usalo solo cuando no tengas nada pendiente."""


def build_supplier_system(persona: Persona) -> str:
    return SYSTEM_TEMPLATE.format(
        name=persona.name,
        script=render_script(persona),
        sheet=price_sheet(persona),
        fin=FIN,
    )


class SupplierBot:
    def __init__(self, persona: Persona, client: Any, *, model: str | None = None, max_tokens: int | None = None):
        self.persona = persona
        self.client = client
        self.model = model or sim_settings.SIM_SUPPLIER_MODEL
        self.max_tokens = max_tokens or sim_settings.SIM_SUPPLIER_MAX_TOKENS
        self.system = build_supplier_system(persona)

    def reply(self, history: list[dict]) -> tuple[str, bool]:
        """``(texto, terminó)``. ``history`` está del lado del proveedor: Bernardo es ``user``."""

        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self.system,
            messages=history,
        )

        content = list(getattr(response, "content", []) or [])
        text = "".join(getattr(b, "text", "") for b in content if getattr(b, "type", "") == "text").strip()

        finished = FIN in text
        text = text.replace(FIN, "").strip()

        return text, finished
