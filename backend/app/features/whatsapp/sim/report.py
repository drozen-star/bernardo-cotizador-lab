"""Reporte de una corrida: JSON para máquinas, texto para leer, transcript por persona."""

import json
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.features.whatsapp.sim.budget import Budget
from app.features.whatsapp.sim.judge import ConversationRecord
from app.features.whatsapp.sim.judge import Verdict


def _json_default(value: Any):
    if isinstance(value, Decimal):
        return float(value)

    if isinstance(value, datetime):
        return value.isoformat()

    return str(value)


def write_transcript(path: Path, persona_name: str, record: ConversationRecord, verdict: Verdict | None) -> None:
    lines = [f"PERSONA: {record.persona_id} ({persona_name})", f"ESTADO FINAL: {record.final_status}", ""]

    for index, turn in enumerate(record.turns):
        lines.append(f"--- turno {index} ---")
        lines.append(f"PROVEEDOR > {turn.inbound}")

        if turn.raw_text is not None and turn.raw_text != (turn.outbound or ""):
            lines.append(f"[modelo, antes del freno] {turn.raw_text}")

        lines.append(f"BERNARDO  > {turn.outbound if turn.outbound is not None else '(sin respuesta: conversación cerrada)'}")

        if turn.stop_reasons:
            lines.append(f"stop_reason por llamada del agente: {turn.stop_reasons}")

        if turn.flags:
            lines.append(f"guardrail_flags: {turn.flags}")

        for call in turn.tool_calls or []:
            status = "ok" if call.get("ok", True) else "RECHAZADA"
            lines.append(f"tool_call [{status}] {call.get('name')} {json.dumps(call.get('input'), ensure_ascii=False, default=_json_default)}")
            lines.append(f"   -> {call.get('result', '')}")

        lines.append(f"estado tras el turno: {turn.status_after}")
        lines.append("")

    lines.append("COTIZACIONES REGISTRADAS:")

    for quote in record.quotes:
        lines.append(
            f"- {quote.item_name}: {quote.unit_price} {quote.currency} | IVA {quote.iva_included} | flete {quote.freight_included}"
        )

    if not record.quotes:
        lines.append("- ninguna")

    if verdict is not None:
        lines += ["", f"VEREDICTO: {'PASS' if verdict.passed else 'FAIL'}"]

        for check in verdict.checks:
            lines.append(f"  {check.code} {check.status}: {check.reason}")

    path.write_text("\n".join(lines), encoding="utf-8")


def build_report(
    *,
    run_dir: Path,
    live: bool,
    agent_model: str,
    supplier_model: str,
    personas: list[dict],
    budget: Budget,
    stopped_reason: str | None,
) -> dict:
    judged = [p for p in personas if p.get("verdict") is not None]
    passed = [p for p in judged if p["verdict"]["passed"]]
    agent_costs = [p["usage"]["agent"]["cost_usd"] for p in personas if p.get("usage")]
    total_calls = sum(usage.calls for usage in budget.by_role.values())

    agent_usage = budget.by_role.get("agent")
    agent_total_input = agent_usage.total_input_tokens if agent_usage else 0
    cache_read_pct = (
        round(100 * agent_usage.cache_read_input_tokens / agent_total_input, 1)
        if agent_usage and agent_total_input
        else 0.0
    )

    return {
        "run_dir": str(run_dir),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "live": live,
        "models": {"agent": agent_model, "supplier": supplier_model},
        "summary": {
            "personas": len(personas),
            "judged": len(judged),
            "passed": len(passed),
            "verdict": f"{len(passed)}/{len(judged)}",
            "total_cost_usd": round(budget.spent_usd, 6),
            "total_calls": total_calls,
            "avg_agent_cost_per_conversation_usd": round(sum(agent_costs) / len(agent_costs), 6) if agent_costs else 0.0,
            "agent_total_input_tokens": agent_total_input,
            "agent_cache_read_pct": cache_read_pct,
            "avg_agent_input_tokens_per_call": round(agent_total_input / agent_usage.calls) if agent_usage and agent_usage.calls else 0,
            "stopped_reason": stopped_reason,
        },
        "budget": budget.snapshot(),
        "personas": personas,
    }


def render_text(report: dict) -> str:
    summary = report["summary"]
    lines = [
        f"SIMULADOR L3b — corrida {report['run_dir']}",
        f"modo: {'LIVE' if report['live'] else 'offline (clientes falsos)'} | agente: {report['models']['agent']} | proveedor: {report['models']['supplier']}",
        f"VEREDICTO: {summary['verdict']} personas PASS",
        f"costo total: {summary['total_cost_usd']:.4f} USD (tope {report['budget']['limit_usd']:.2f}) | llamadas: {summary['total_calls']}"
        f" | costo promedio del agente por conversación: {summary['avg_agent_cost_per_conversation_usd']:.4f} USD",
        f"agente: {summary.get('agent_total_input_tokens', 0)} tokens de entrada en total, "
        f"{summary.get('agent_cache_read_pct', 0.0)}% leídos de caché, "
        f"{summary.get('avg_agent_input_tokens_per_call', 0)} de entrada promedio por llamada",
    ]

    if summary.get("stopped_reason"):
        lines.append(f"CORRIDA CORTADA: {summary['stopped_reason']}")

    for persona in report["personas"]:
        verdict = persona.get("verdict")
        usage = persona.get("usage") or {}
        agent = usage.get("agent", {})
        supplier = usage.get("supplier", {})
        head = f"[{'PASS' if verdict and verdict['passed'] else ('FAIL' if verdict else '—')}] {persona['id']} ({persona['name']})"
        lines += ["", head, f"  estado final: {persona.get('final_status')} | turnos: {persona.get('turns')}"]
        lines.append(
            f"  agente: {agent.get('calls', 0)} llamadas, {agent.get('total_input_tokens', agent.get('input_tokens', 0))} in "
            f"(caché: {agent.get('cache_read_input_tokens', 0)} leídos, {agent.get('cache_creation_input_tokens', 0)} escritos) / "
            f"{agent.get('output_tokens', 0)} out, {agent.get('cost_usd', 0):.4f} USD | "
            f"proveedor: {supplier.get('calls', 0)} llamadas, {supplier.get('cost_usd', 0):.4f} USD"
        )

        for check in (verdict or {}).get("checks", []):
            lines.append(f"  {check['code']} {check['status']}: {check['reason']}")

        if persona.get("error"):
            lines.append(f"  ERROR: {persona['error']}")

    return "\n".join(lines)


def write_report(run_dir: Path, report: dict) -> tuple[Path, Path]:
    json_path = run_dir / "report.json"
    text_path = run_dir / "report.txt"

    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    text_path.write_text(render_text(report), encoding="utf-8")

    return json_path, text_path


def verdict_to_dict(verdict: Verdict) -> dict:
    return asdict(verdict)
