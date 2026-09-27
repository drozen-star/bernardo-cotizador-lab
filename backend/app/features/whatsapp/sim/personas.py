"""Personas del simulador: carga de ``personas.json`` y formato argentino de precios.

Cada persona trae la personalidad en texto libre (portada del spike), la verdad de
referencia por ítem del Excel (``ground_truth``), lo que el juez espera (``expected``) y
un guion fijo para el modo offline (``offline_turns``). Los precios del texto salen de
``ground_truth`` con placeholders ``{price:<ítem>}``: una sola fuente de verdad.
"""

import json
import re
from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from pathlib import Path

PERSONAS_PATH = Path(__file__).with_name("personas.json")

_PLACEHOLDER = re.compile(r"\{price:([^}]+)\}")


@dataclass
class GroundTruth:
    unit_price: Decimal | None
    currency: str | None
    iva_included: bool | None
    freight_included: bool | None
    lead_time_days: int | None

    @classmethod
    def from_dict(cls, data: dict) -> "GroundTruth":
        price = data.get("unit_price")

        return cls(
            unit_price=Decimal(str(price)) if price is not None else None,
            currency=data.get("currency"),
            iva_included=data.get("iva_included"),
            freight_included=data.get("freight_included"),
            lead_time_days=data.get("lead_time_days"),
        )


@dataclass
class Persona:
    id: str
    name: str
    contact_name: str | None
    script: str
    #: ítem del Excel -> verdad de referencia, o None si la persona no cotiza ese ítem.
    ground_truth: dict[str, GroundTruth | None]
    expected: dict
    offline_turns: list[str] = field(default_factory=list)

    @property
    def quoted_items(self) -> list[str]:
        return [item for item, truth in self.ground_truth.items() if truth is not None]

    @property
    def unquoted_items(self) -> list[str]:
        return [item for item, truth in self.ground_truth.items() if truth is None]


# ------------------------------------------------------------------ formato AR
def format_ar(value: Decimal | int | float) -> str:
    """1450 -> "1.450"; 9800.5 -> "9.800,50"."""

    number = Decimal(str(value))
    integer_part, _, decimal_part = f"{number:f}".partition(".")
    grouped = f"{int(integer_part):,}".replace(",", ".")

    decimal_part = decimal_part.rstrip("0")

    if decimal_part:
        return f"{grouped},{decimal_part.ljust(2, '0')}"

    return grouped


def price_text(truth: GroundTruth) -> str:
    """"12.900 con IVA" / "13.400 + IVA" / "12.900" según lo que la persona aclara."""

    if truth.unit_price is None:
        return "sin precio"

    text = format_ar(truth.unit_price)

    if truth.iva_included is True:
        text += " con IVA"
    elif truth.iva_included is False:
        text += " + IVA"

    return text


def render_script(persona: Persona) -> str:
    """El guion con los placeholders ``{price:<ítem>}`` resueltos desde ground_truth."""

    def replace(match: re.Match[str]) -> str:
        item = match.group(1).strip()
        truth = persona.ground_truth.get(item)

        if truth is None:
            raise ValueError(f"La persona {persona.id!r} cita precio de {item!r}, que no cotiza.")

        return price_text(truth)

    return _PLACEHOLDER.sub(replace, persona.script)


def price_sheet(persona: Persona) -> str:
    """Lo único que el proveedor simulado puede decir sobre precios y condiciones."""

    lines = []

    for item, truth in persona.ground_truth.items():
        if truth is None:
            continue

        details = [price_text(truth)]

        if truth.freight_included is True:
            details.append("flete incluido")
        elif truth.freight_included is False:
            details.append("flete aparte")

        if truth.lead_time_days is not None:
            details.append(f"entrega en {truth.lead_time_days * 24} horas")

        lines.append(f"- {item}: {', '.join(details)}")

    unquoted = persona.unquoted_items

    if unquoted:
        lines.append("- No cotizás ni inventás precio de: " + "; ".join(unquoted))

    if not persona.quoted_items:
        lines.append("- No pasás ningún precio.")

    return "\n".join(lines)


# ------------------------------------------------------------------------ carga
def load_personas(path: Path | None = None) -> list[Persona]:
    raw = json.loads((path or PERSONAS_PATH).read_text(encoding="utf-8"))
    personas = []

    for entry in raw:
        personas.append(
            Persona(
                id=entry["id"],
                name=entry["name"],
                contact_name=entry.get("contact_name"),
                script=entry["script"],
                ground_truth={
                    item: (GroundTruth.from_dict(truth) if truth is not None else None)
                    for item, truth in entry["ground_truth"].items()
                },
                expected=dict(entry.get("expected") or {}),
                offline_turns=list(entry.get("offline_turns") or []),
            )
        )

    ids = [persona.id for persona in personas]

    if len(ids) != len(set(ids)):
        raise ValueError("Hay ids de persona repetidos en personas.json")

    return personas
