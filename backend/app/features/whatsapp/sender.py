"""Envío de texto por WhatsApp Cloud API (L4).

Solo texto, solo dentro de la ventana de 24 h abierta por el proveedor (eso lo controla
``approval.py``). Sin templates en el MVP (spec sección 8). La versión de Graph viene del
entorno, sin default: si falta, no se manda nada y el error lo dice.
"""

import logging
from typing import Any

import httpx

from app.features.whatsapp.settings import whatsapp_settings

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com"


class SenderError(RuntimeError):
    """Meta rechazó el envío o el lab no está configurado para mandar."""

    def __init__(self, code: str, detail: str = "", status_code: int | None = None) -> None:
        self.code = code
        self.detail = detail
        self.status_code = status_code
        super().__init__(f"{code}: {detail}" if detail else code)


def _config() -> tuple[str, str, str]:
    settings = whatsapp_settings
    missing = [
        name
        for name, value in (
            ("WHATSAPP_TOKEN", settings.WHATSAPP_TOKEN),
            ("WHATSAPP_PHONE_NUMBER_ID", settings.WHATSAPP_PHONE_NUMBER_ID),
            ("GRAPH_API_VERSION", settings.GRAPH_API_VERSION),
        )
        if not value
    ]

    if missing:
        raise SenderError("whatsapp_not_configured", "faltan " + ", ".join(missing))

    return settings.WHATSAPP_TOKEN, settings.WHATSAPP_PHONE_NUMBER_ID, settings.GRAPH_API_VERSION


def messages_url(version: str, phone_number_id: str) -> str:
    return f"{GRAPH_BASE}/{version}/{phone_number_id}/messages"


def _rejection(response: httpx.Response) -> SenderError:
    """Loguea el motivo del rechazo de Meta y arma el SenderError.

    El error de Graph viene como ``{"error": {"code", "error_subcode", "type", "message",
    "fbtrace_id"}}``; puede no ser JSON o no traer "error". Nunca se vuelca el cuerpo crudo,
    ni el token, ni el payload, ni el destino: solo los campos del error, con el mensaje
    truncado a 200 caracteres.
    """

    try:
        error = response.json().get("error") or {}
    except (ValueError, AttributeError):
        error = None

    if not isinstance(error, dict) or not error:
        logger.warning("whatsapp.sender Meta rechazó HTTP %s: cuerpo no JSON o sin 'error'", response.status_code)
        return SenderError("meta_rejected", f"HTTP {response.status_code} sin detalle", status_code=response.status_code)

    code = error.get("code")
    message = str(error.get("message") or "")[:200]

    logger.warning(
        "whatsapp.sender Meta rechazó HTTP %s code=%s subcode=%s type=%s fbtrace_id=%s message=%s",
        response.status_code, code, error.get("error_subcode"), error.get("type"), error.get("fbtrace_id"), message,
    )

    # Meta suele repetir el código al inicio del mensaje: no se duplica el prefijo.
    prefix = f"(#{code})"
    detail = message if message.startswith(prefix) else f"{prefix} {message}".strip()

    return SenderError("meta_rejected", detail, status_code=response.status_code)


def send_text(to_raw_wa_id: str, body: str, *, client: httpx.Client | None = None) -> str:
    """Manda ``body`` a ``to_raw_wa_id`` y devuelve el wa_message_id de Meta.

    ``client`` es inyectable para los tests (``httpx.MockTransport``). Nunca se loguea el
    token ni el cuerpo del mensaje.
    """

    token, phone_number_id, version = _config()

    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": to_raw_wa_id,
        "type": "text",
        "text": {"body": body, "preview_url": False},
    }

    own_client = client is None
    client = client or httpx.Client(timeout=whatsapp_settings.WHATSAPP_SEND_TIMEOUT_SECONDS)

    try:
        response = client.post(
            messages_url(version, phone_number_id),
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=whatsapp_settings.WHATSAPP_SEND_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning("whatsapp.sender no se pudo alcanzar Graph: %s", type(exc).__name__)
        raise SenderError("meta_unreachable", type(exc).__name__) from exc
    finally:
        if own_client:
            client.close()

    if response.status_code >= 400:
        raise _rejection(response)

    try:
        return str(response.json()["messages"][0]["id"])
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise SenderError("meta_bad_response", "sin messages[0].id en la respuesta") from exc
