"""Descarga de un adjunto desde la Cloud API de WhatsApp (L5e).

Dos pasos, con el mismo Bearer que usa ``sender``: ``GET /{version}/{media_id}`` devuelve la
URL firmada, el mime y el tamaño; ``GET url`` baja el archivo. La URL firmada expira, así que
el segundo paso va inmediato. Nunca se loguea la URL, el token ni el contenido: solo mime,
tamaño en bytes y motivo de falla.
"""

import logging

import httpx

from app.features.whatsapp.attachments import AttachmentError
from app.features.whatsapp.sender import GRAPH_BASE
from app.features.whatsapp.settings import whatsapp_settings

logger = logging.getLogger(__name__)

CODE_TOO_LARGE = "too_large"
CODE_DOWNLOAD_FAILED = "download_failed"


def media_url(version: str, media_id: str) -> str:
    return f"{GRAPH_BASE}/{version}/{media_id}"


def _config() -> tuple[str, str]:
    settings = whatsapp_settings

    if not settings.WHATSAPP_TOKEN or not settings.GRAPH_API_VERSION:
        raise AttachmentError(CODE_DOWNLOAD_FAILED, "whatsapp_not_configured")

    return settings.WHATSAPP_TOKEN, settings.GRAPH_API_VERSION


def _get(client: httpx.Client, url: str, token: str, what: str) -> httpx.Response:
    try:
        response = client.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=whatsapp_settings.WHATSAPP_SEND_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise AttachmentError(CODE_DOWNLOAD_FAILED, f"{what}: {type(exc).__name__}") from exc

    if response.status_code >= 400:
        raise AttachmentError(CODE_DOWNLOAD_FAILED, f"{what}: HTTP {response.status_code}")

    return response


def fetch_media(media_id: str, *, client: httpx.Client | None = None) -> tuple[bytes, str]:
    """``(bytes, mime)`` del adjunto. ``client`` es inyectable (``httpx.MockTransport``)."""

    token, version = _config()
    limit = whatsapp_settings.WHATSAPP_MAX_ATTACHMENT_BYTES
    own_client = client is None
    client = client or httpx.Client()

    try:
        meta = _get(client, media_url(version, media_id), token, "metadata")

        try:
            info = meta.json()
        except ValueError as exc:
            raise AttachmentError(CODE_DOWNLOAD_FAILED, "metadata: cuerpo no JSON") from exc

        url = str(info.get("url") or "")
        mime = str(info.get("mime_type") or "").split(";")[0].strip().lower()
        size = int(info.get("file_size") or 0)

        if not url:
            raise AttachmentError(CODE_DOWNLOAD_FAILED, "metadata: sin url")

        if size > limit:
            raise AttachmentError(CODE_TOO_LARGE, f"{size} bytes (tope {limit})")

        response = _get(client, url, token, "archivo")  # inmediato: la URL firmada expira
        data = response.content

        if len(data) > limit:
            raise AttachmentError(CODE_TOO_LARGE, f"{len(data)} bytes (tope {limit})")

        if not mime:
            mime = response.headers.get("content-type", "").split(";")[0].strip().lower()

        logger.info("whatsapp.attachment descargado mime=%s bytes=%s", mime, len(data))

        return data, mime
    finally:
        if own_client:
            client.close()
