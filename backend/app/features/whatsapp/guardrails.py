"""Frenos de entrada y salida (portados del spike, spec sección 8).

Entrada: el mensaje del proveedor es contenido no confiable. Se normaliza (NFC), se le
sacan los caracteres de control e invisibles, se recorta y se encapsula en una etiqueta
para que el modelo lo trate como dato. Si el proveedor escribe la etiqueta de cierre para
"salirse" del bloque, se la neutraliza.

Salida: lo que Bernardo va a mandar. Tres faltas graves bloquean y derivan a humano
(compromiso de compra, datos de pago, fuga del prompt); las faltas de voz (emojis,
exclamaciones, largo) se corrigen sin bloquear. Cada disparo devuelve un flag que el
service guarda en ``guardrail_flags`` del mensaje.
"""

import re
import unicodedata
from dataclasses import dataclass
from dataclasses import field

from app.features.whatsapp.voice_fixes import fix_conjunctions

# ------------------------------------------------------------------ flags
FLAG_PURCHASE_COMMITMENT = "purchase_commitment"
FLAG_PAYMENT_DATA = "payment_data"
FLAG_PROMPT_LEAK = "prompt_leak"
FLAG_EMPTY_REPLY = "empty_reply"
FLAG_EMOJI_REMOVED = "emoji_removed"
FLAG_EXCLAMATION_REMOVED = "exclamation_removed"
FLAG_SLANG_REMOVED = "slang_removed"
FLAG_REPLY_TRUNCATED = "reply_truncated"
FLAG_INPUT_TRUNCATED = "input_truncated"
FLAG_INPUT_TAG_ESCAPED = "input_tag_escaped"
#: L5f: concordancia "y" / "e" corregida ("arena e hilo", "arena y hierro"). No bloquea.
FLAG_CONJUNCTION_FIXED = "conjunction_fixed"

BLOCKING_FLAGS = (FLAG_PURCHASE_COMMITMENT, FLAG_PAYMENT_DATA, FLAG_PROMPT_LEAK, FLAG_EMPTY_REPLY)

# ------------------------------------------------------------- patrones
#: Compromiso de compra o de pago. "transferencia" o "tarjeta" solas NO bloquean:
#: preguntar la forma de pago es parte del pedido.
_COMMITMENT = re.compile(
    r"\b("
    r"confirmo (la )?compra|te (lo |la |los |las )?compro|cerramos( el trato| la compra)?|"
    r"queda confirmad[oa]|acepto (el |ese |tu )?precio|te transfiero|"
    r"te (mando|paso|env[ií]o) (la |una )?se[ñn]a|dale,? (cerr[aá]|confirm[aá])|"
    r"compra confirmada"
    r")\b",
    re.IGNORECASE,
)

#: Datos de pago: identificadores bancarios o números de tarjeta.
_PAYMENT_DATA = re.compile(
    r"\b("
    r"cbu|cvu|alias( bancario| de (pago|cuenta| mercado ?pago))?|"
    r"n[uú]mero de (cuenta|tarjeta)|"
    r"tarjeta (de (cr[eé]dito|d[eé]bito) )?(n[uú]mero|nro|n°)"
    r")\b"
    r"|\b(?:\d[ -]?){13,19}\b",
    re.IGNORECASE,
)

#: Marcas del system prompt que no tienen por qué aparecer en una respuesta.
_PROMPT_LEAK = re.compile(
    r"(FICHA DEL PEDIDO|YA REGISTRADO EN ESTA CONVERSACI[ÓO]N|^REGLAS\s*$|"
    r"system prompt|<supplier_message>|Tu único objetivo:)",
    re.IGNORECASE | re.MULTILINE,
)

_EXCLAMATION = re.compile(r"[¡!]")

#: "Che" / "Dale" como interjección al inicio de una oración, seguidas de coma o espacio y
#: de una letra. Con límite de palabra: "chequeé" y "dale que va" a mitad de frase quedan.
_SLANG_START = re.compile(
    r"(?P<lead>^\s*|(?<=[.?…])\s+)(?:che|dale)\b\s*,?\s*(?P<next>[^\W\d_])",
    re.IGNORECASE | re.MULTILINE,
)


def _strip_slang(text: str) -> tuple[str, bool]:
    """Quita la interjección y capitaliza lo que sigue. Devuelve (texto, hubo_cambio)."""

    def replace(match: re.Match[str]) -> str:
        return f"{match.group('lead')}{match.group('next').upper()}"

    cleaned, count = _SLANG_START.subn(replace, text)

    return cleaned, count > 0

#: Caracteres que acompañan emojis y no tienen categoría "So".
_EMOJI_JOINERS = {"‍", "️", "︎"}


def _is_emoji(char: str) -> bool:
    return unicodedata.category(char) == "So" or char in _EMOJI_JOINERS


# ------------------------------------------------------------------ entrada
@dataclass
class InboundSanitized:
    #: Lo que se manda al modelo: el texto limpio adentro de la etiqueta.
    wrapped: str
    #: Lo que se guarda en la base: el texto limpio, sin etiqueta.
    clean: str
    flags: list[str] = field(default_factory=list)


def sanitize_inbound(text: str, *, max_chars: int, tag: str) -> InboundSanitized:
    """Normaliza, limpia, recorta y encapsula el mensaje del proveedor."""

    flags: list[str] = []

    text = unicodedata.normalize("NFC", text or "")

    # Fuera los caracteres de control e invisibles (categoría C*), salvo salto y tab.
    text = "".join(char for char in text if char in "\n\t" or unicodedata.category(char)[0] != "C")

    if len(text) > max_chars:
        text = text[:max_chars]
        flags.append(FLAG_INPUT_TRUNCATED)

    # El proveedor no puede cerrar la etiqueta y "salir" del bloque de datos.
    escaped, replaced = re.subn(rf"</?\s*{re.escape(tag)}\s*>", "[etiqueta removida]", text, flags=re.IGNORECASE)

    if replaced:
        flags.append(FLAG_INPUT_TAG_ESCAPED)

    clean = escaped.strip()

    return InboundSanitized(
        wrapped=f"<{tag}>\n{clean}\n</{tag}>",
        clean=clean,
        flags=flags,
    )


# ------------------------------------------------------------------- salida
@dataclass
class OutboundReview:
    text: str
    flags: list[str] = field(default_factory=list)
    blocked: bool = False

    @property
    def blocking_flags(self) -> list[str]:
        return [flag for flag in self.flags if flag in BLOCKING_FLAGS]


def review_outbound(text: str, *, max_chars: int, safe_reply: str) -> OutboundReview:
    """Devuelve el texto a mandar y los flags. Una falta grave reemplaza por ``safe_reply``."""

    flags: list[str] = []
    text = (text or "").strip()

    if _COMMITMENT.search(text):
        flags.append(FLAG_PURCHASE_COMMITMENT)

    if _PAYMENT_DATA.search(text):
        flags.append(FLAG_PAYMENT_DATA)

    if _PROMPT_LEAK.search(text):
        flags.append(FLAG_PROMPT_LEAK)

    if not text:
        flags.append(FLAG_EMPTY_REPLY)

    if flags:
        return OutboundReview(text=safe_reply, flags=flags, blocked=True)

    # Faltas de voz: se corrigen, no bloquean.
    if any(_is_emoji(char) for char in text):
        flags.append(FLAG_EMOJI_REMOVED)
        text = "".join(char for char in text if not _is_emoji(char))

    if _EXCLAMATION.search(text):
        flags.append(FLAG_EXCLAMATION_REMOVED)
        text = _EXCLAMATION.sub("", text)

    # Voz (L5a): "Che, ..." / "Dale, ..." al inicio de oración. Después de los frenos
    # bloqueantes a propósito: "Dale, confirmo la compra" ya se bloqueó arriba.
    text, slang_removed = _strip_slang(text)

    if slang_removed:
        flags.append(FLAG_SLANG_REMOVED)

    # Voz (L5f): "e hierro" -> "y hierro", "y hilo" -> "e hilo". Corrección, no bloqueo.
    text, conjunction_fixed = fix_conjunctions(text)

    if conjunction_fixed:
        flags.append(FLAG_CONJUNCTION_FIXED)

    # Los reemplazos pueden dejar espacios dobles o espacios antes de un punto.
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" +([.,;:?])", r"\1", text).strip()

    if len(text) > max_chars:
        flags.append(FLAG_REPLY_TRUNCATED)
        text = text[:max_chars].rsplit(" ", 1)[0].rstrip() + "…"

    return OutboundReview(text=text, flags=flags, blocked=False)
