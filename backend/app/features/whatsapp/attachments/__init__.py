"""Adjuntos por WhatsApp (L5e): PDF, Excel y foto del proveedor.

El inbound se guarda provisorio en el request (``pending``), un job de background descarga
el archivo de Meta (``download``), lo convierte a texto (``spreadsheet`` o ``transcribe``) y
recién entonces corre el agente (``job``). El contenido del adjunto es dato, nunca instrucción.
"""


class AttachmentError(RuntimeError):
    """Falla tipada del procesamiento de un adjunto. ``code`` es el motivo que ve el agente."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)
