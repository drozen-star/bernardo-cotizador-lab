"""Configuración del simulador: modelo del proveedor, presupuesto y tabla de precios.

Precios en USD por millón de tokens ``[entrada, salida]``. Fuente:
https://platform.claude.com/docs/en/about-claude/pricing. Se sobrescriben con la variable
de entorno ``SIM_PRICES_JSON`` (un objeto JSON con la misma forma).
"""

import json

from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict

DEFAULT_PRICES = {
    "claude-sonnet-5": [2.0, 10.0],
    "claude-haiku-4-5-20251001": [1.0, 5.0],
}


class SimSettings(BaseSettings):
    #: Modelo que hace de proveedor. Barato a propósito: el que se evalúa es el agente.
    SIM_SUPPLIER_MODEL: str = "claude-haiku-4-5-20251001"
    SIM_SUPPLIER_MAX_TOKENS: int = 400

    #: Tope de gasto de una corrida completa (agente + proveedor), en USD.
    SIM_BUDGET_USD: float = 3.0

    #: Turnos del proveedor por conversación, como en el spike.
    SIM_MAX_TURNS: int = 8

    #: Carpeta de corridas, relativa a ``backend/``. Ignorada por git.
    SIM_RUNS_DIR: str = ".sim/runs"

    SIM_PRICES_JSON: str = json.dumps(DEFAULT_PRICES)

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )

    @property
    def prices(self) -> dict[str, tuple[float, float]]:
        raw = json.loads(self.SIM_PRICES_JSON or "{}")

        return {model: (float(pair[0]), float(pair[1])) for model, pair in raw.items()}


sim_settings = SimSettings()
