"""CLI del simulador.

    uv run python -m app.features.whatsapp.sim.run [--only <persona>] [--live] [--budget 3]

Sin ``--live`` usa clientes falsos (costo 0). Con ``--live`` exige ``ANTHROPIC_API_KEY`` en
el entorno o en ``backend/.env``; la key nunca se imprime ni se loguea.
"""

import argparse
import os
import sys
from pathlib import Path

from app.features.whatsapp.sim.runner import RunConfig
from app.features.whatsapp.sim.runner import run_simulation


def _load_api_key_from_dotenv() -> bool:
    """Copia ANTHROPIC_API_KEY de backend/.env al entorno si falta. Devuelve si quedó disponible."""

    if os.environ.get("ANTHROPIC_API_KEY"):
        return True

    from dotenv import dotenv_values  # noqa: PLC0415 - solo en modo live

    env_path = Path(__file__).resolve().parents[4] / ".env"
    value = dotenv_values(env_path).get("ANTHROPIC_API_KEY") if env_path.exists() else None

    if value:
        os.environ["ANTHROPIC_API_KEY"] = value
        return True

    return False


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # acentos en PowerShell

    parser = argparse.ArgumentParser(description="Simulador de 7 proveedores para el agente de WhatsApp")
    parser.add_argument("--only", default=None, help="id de una persona (p. ej. directo)")
    parser.add_argument("--live", action="store_true", help="usa la API real; sin esto, clientes falsos")
    parser.add_argument("--budget", type=float, default=None, help="tope en USD (default SIM_BUDGET_USD)")
    parser.add_argument("--max-turns", type=int, default=None)
    args = parser.parse_args(argv)

    if args.live and not _load_api_key_from_dotenv():
        print("Falta ANTHROPIC_API_KEY (entorno o backend/.env). No se corre en vivo.")
        return 2

    result = run_simulation(
        RunConfig(live=args.live, only=args.only, budget_usd=args.budget, max_turns=args.max_turns)
    )

    print((result.run_dir / "report.txt").read_text(encoding="utf-8"))
    print()
    print(f"report.json: {result.run_dir / 'report.json'}")

    return 1 if result.stopped_reason else 0


if __name__ == "__main__":
    raise SystemExit(main())
