"""Correcciones de voz que no bloquean (L5f): concordancia de las conjunciones "y" / "e".

Regla del castellano: antes de una palabra que empieza con "i" o "hi" (sin ser "hie"), "y"
pasa a "e" ("arena e hilo", "cemento e hidrófugo"); antes de "hie" se mantiene "y" ("arena y
hierro"). Se respeta la mayúscula inicial ("E hilo" / "Y hierro").
"""

import re

#: "e hierro" -> "y hierro".
_E_BEFORE_HIE = re.compile(r"\b([eE]) (?=[hH][iI][eE])")

#: "y hilo" / "y idea" -> "e hilo" / "e idea", sin tocar "y hierro".
_Y_BEFORE_I = re.compile(r"\b([yY]) (?=(?:[iI]|[hH][iI])(?![eE]))")


def _swap(match: re.Match[str], lower: str) -> str:
    original = match.group(1)

    return (lower.upper() if original.isupper() else lower) + " "


def fix_conjunctions(text: str) -> tuple[str, bool]:
    """``(texto corregido, hubo_cambio)``."""

    fixed, count_e = _E_BEFORE_HIE.subn(lambda match: _swap(match, "y"), text or "")
    fixed, count_y = _Y_BEFORE_I.subn(lambda match: _swap(match, "e"), fixed)

    return fixed, (count_e + count_y) > 0
