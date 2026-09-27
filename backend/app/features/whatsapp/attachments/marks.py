"""Flags de adjuntos en ``whatsapp_messages.guardrail_flags`` y marca en ``supplier_quotes.risk_flags``.

Sin migración (decisión B1): la marca ``from_attachment`` va en el JSON ``risk_flags`` que ya
existe en ``supplier_quotes``.
"""

#: El inbound provisorio: el archivo todavía no se descargó ni se leyó.
FLAG_PENDING = "attachment_pending"
FLAG_XLSX = "attachment_xlsx"
FLAG_PDF = "attachment_pdf"
FLAG_IMAGE = "attachment_image"
#: El texto lo produjo el modelo leyendo un PDF o una foto (no es literal del proveedor).
FLAG_TRANSCRIBED = "attachment_transcribed"
FLAG_FAILED = "attachment_failed"

#: Marca en ``supplier_quotes.risk_flags``: el precio salió de un adjunto transcripto.
FLAG_FROM_ATTACHMENT = "from_attachment"

#: Prefijo con el que se guarda el media_id de Meta en ``whatsapp_messages.media_url``.
MEDIA_URL_PREFIX = "wa-media:"


def apply_from_attachment(quote, evidence: str, inbound_bodies: list[str], attachment_bodies: list[str], *, literal) -> bool:
    """Suma ``from_attachment`` a ``risk_flags`` si la evidencia es literal SOLO en un adjunto
    transcripto; la quita si es literal en un mensaje de texto del proveedor. Nunca pisa las
    demás marcas. ``literal(evidence, bodies)`` es la comparación literal del quote_writer.
    Devuelve si la marca quedó puesta.
    """

    transcribed = set(attachment_bodies)
    text_bodies = [body for body in inbound_bodies if body not in transcribed]
    in_text = literal(evidence, text_bodies)
    in_attachment = literal(evidence, attachment_bodies)
    flags = list(quote.risk_flags or [])

    if in_attachment and not in_text:
        if FLAG_FROM_ATTACHMENT not in flags:
            flags.append(FLAG_FROM_ATTACHMENT)
            quote.risk_flags = flags  # lista nueva: el JSON se marca como modificado

        return True

    if in_text and FLAG_FROM_ATTACHMENT in flags:
        flags.remove(FLAG_FROM_ATTACHMENT)
        quote.risk_flags = flags

    return FLAG_FROM_ATTACHMENT in flags
