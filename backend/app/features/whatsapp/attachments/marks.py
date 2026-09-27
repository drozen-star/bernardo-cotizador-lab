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
