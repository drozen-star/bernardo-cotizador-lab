"""Normalización de números de WhatsApp argentinos (L4).

Meta manda el ``wa_id`` de distintas formas según el teléfono del proveedor y el país:
``5491155551234`` (con el 9 de celular), ``541155551234`` (sin el 9), y en la base el
comprador puede haber cargado ``+54 9 11 5555-1234``. Para comparar, todo se lleva a solo
dígitos con el 9: ``5491155551234``. El valor crudo nunca se pisa: se guarda tal cual en
``whatsapp_conversations.wa_from`` y es el destino de las respuestas.
"""

import re

_NON_DIGITS = re.compile(r"\D")

ARGENTINA = "54"
MOBILE_PREFIX = "9"


def normalize_phone(raw: str | None) -> str:
    """Solo dígitos; para Argentina, con el 9 de celular después del 54."""

    digits = _NON_DIGITS.sub("", raw or "")

    # Prefijo internacional escrito como 00.
    if digits.startswith("00"):
        digits = digits[2:]

    if digits.startswith(ARGENTINA):
        rest = digits[len(ARGENTINA):]

        # 54 + área + número = 12 dígitos sin el 9; con el 9 son 13.
        if len(rest) == 10 and not rest.startswith(MOBILE_PREFIX):
            return ARGENTINA + MOBILE_PREFIX + rest

    return digits


def last_digits(raw: str | None, count: int = 4) -> str:
    """Los últimos dígitos, para loguear sin exponer el número entero."""

    digits = _NON_DIGITS.sub("", raw or "")

    return digits[-count:] if digits else ""
