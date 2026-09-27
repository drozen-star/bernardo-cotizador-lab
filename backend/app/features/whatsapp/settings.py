"""Configuración del slice WhatsApp, separada de ``app.core.config``.

El lote no permite tocar ``config.py``, y estos valores solo los usa este slice. Se leen
del entorno y de ``backend/.env`` igual que el resto de la app (pydantic-settings), y los
tests los pisan con ``monkeypatch.setattr(whatsapp_settings, ...)``.
"""

from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict


class WhatsappSettings(BaseSettings):
    #: Modelo del agente conversador. SDK nativo de Anthropic (spec sección 7).
    BERNARDO_MODEL: str = "claude-sonnet-5"

    #: Tope de tokens de salida por llamada al modelo. 4096 porque cinco record_quote
    #: completos en una respuesta necesitan ~1.500-2.500 tokens: con 1024 el modelo se
    #: cortaba a mitad de camino (corrida 20260926-2041-2 del simulador).
    WHATSAPP_MAX_TOKENS: int = 4096

    #: Vueltas de herramientas por mensaje del proveedor. Si no cierra, needs_human.
    WHATSAPP_MAX_TOOL_ROUNDS: int = 5

    #: Frenos de entrada y salida, portados del spike (spec sección 8).
    WHATSAPP_MAX_INPUT_CHARS: int = 4000
    WHATSAPP_MAX_OUTPUT_CHARS: int = 700

    #: Etiqueta que encapsula el mensaje del proveedor como dato, no instrucción.
    WHATSAPP_INPUT_TAG: str = "supplier_message"

    #: Lo que sale cuando un freno de salida bloquea la respuesta del modelo.
    WHATSAPP_SAFE_REPLY: str = "Gracias. Lo reviso con el equipo y te confirmo por acá."

    # ------------------------------------------------------------ L4: bot -> lab
    #: Secreto compartido con el bot de Render (header X-Bernardo-Lab-Secret). Vacío = todo 401.
    LAB_SHARED_SECRET: str = ""
    #: Token de administración para aprobar borradores (header X-Bernardo-Lab-Admin). Distinto del anterior.
    LAB_ADMIN_TOKEN: str = ""

    # ------------------------------------------------------ L4: envío por Cloud API
    WHATSAPP_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    #: Versión de Graph (p. ej. v23.0). Mismo nombre que usa el bot de producción
    #: (bot/wa/cloudapi.js). Sin default a propósito: la fija el deploy.
    GRAPH_API_VERSION: str = ""
    WHATSAPP_SEND_TIMEOUT_SECONDS: float = 10.0
    #: Ventana de atención de Meta: solo se responde dentro de las 24 h del último inbound.
    WHATSAPP_WINDOW_HOURS: int = 24

    # ------------------------------------------------------------ L5e: adjuntos
    #: Tope del texto que sale de un adjunto (xlsx, PDF o foto) antes de entrar al historial.
    WHATSAPP_MAX_ATTACHMENT_CHARS: int = 12000
    #: Tope del archivo a descargar de Meta (10 MiB).
    WHATSAPP_MAX_ATTACHMENT_BYTES: int = 10485760
    #: Modelo para transcribir adjuntos. Vacío = el del agente (``loop.resolve_model()``).
    WHATSAPP_ATTACHMENT_MODEL: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


whatsapp_settings = WhatsappSettings()
