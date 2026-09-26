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

    #: Tope de tokens de salida por llamada al modelo.
    WHATSAPP_MAX_TOKENS: int = 1024

    #: Vueltas de herramientas por mensaje del proveedor. Si no cierra, needs_human.
    WHATSAPP_MAX_TOOL_ROUNDS: int = 5

    #: Frenos de entrada y salida, portados del spike (spec sección 8).
    WHATSAPP_MAX_INPUT_CHARS: int = 4000
    WHATSAPP_MAX_OUTPUT_CHARS: int = 700

    #: Etiqueta que encapsula el mensaje del proveedor como dato, no instrucción.
    WHATSAPP_INPUT_TAG: str = "supplier_message"

    #: Lo que sale cuando un freno de salida bloquea la respuesta del modelo.
    WHATSAPP_SAFE_REPLY: str = "Gracias. Lo reviso con el equipo y te confirmo por acá."

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


whatsapp_settings = WhatsappSettings()
