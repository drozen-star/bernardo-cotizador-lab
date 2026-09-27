"""Lab L3b: el runner corre de punta a punta con clientes falsos y se niega fuera de SQLite."""

import json

import pytest

from app.features.whatsapp.sim.runner import RunConfig
from app.features.whatsapp.sim.runner import run_simulation


def test_runner_refuses_a_non_sqlite_engine(tmp_path):
    with pytest.raises(ValueError, match="solo corre sobre SQLite"):
        run_simulation(RunConfig(live=False, runs_dir=tmp_path, database_url="postgresql+psycopg://u:p@localhost:5432/lab"))

    assert not any(path.suffix == ".db" for path in tmp_path.rglob("*"))


def test_unknown_persona_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="No existe la persona"):
        run_simulation(RunConfig(live=False, only="inexistente", runs_dir=tmp_path))


def test_full_offline_run_produces_report_and_transcripts(tmp_path):
    result = run_simulation(RunConfig(live=False, runs_dir=tmp_path, budget_usd=3.0))

    assert result.stopped_reason is None
    assert (result.run_dir / "sim.db").exists()
    assert (result.run_dir / "report.json").exists()
    assert (result.run_dir / "report.txt").exists()

    report = json.loads((result.run_dir / "report.json").read_text(encoding="utf-8"))

    assert report["live"] is False
    assert report["summary"]["personas"] == 7
    assert report["summary"]["judged"] == 7
    # El agente falso es de reglas y determinístico: el arnés completo tiene que dar 7/7.
    failed = [p["id"] for p in report["personas"] if not p["verdict"]["passed"]]
    assert report["summary"]["passed"] == 7, f"personas FAIL offline: {failed}"
    assert report["summary"]["total_cost_usd"] == 0
    assert report["budget"]["exceeded"] is False

    ids = [persona["id"] for persona in report["personas"]]
    assert ids == ["directo", "tecnico_curioso", "alternativa_no_listada", "no_cotiza", "presiona_cierre", "inyeccion", "humano"]

    for persona in report["personas"]:
        assert (result.run_dir / f"transcript_{persona['id']}.txt").exists()
        assert {check["code"] for check in persona["verdict"]["checks"]} == {"C1", "C2", "C3", "C4", "C5"}

    text = (result.run_dir / "report.txt").read_text(encoding="utf-8")
    assert "VEREDICTO:" in text
    assert "costo promedio del agente por conversación" in text


def test_offline_run_with_only_one_persona(tmp_path):
    result = run_simulation(RunConfig(live=False, only="directo", runs_dir=tmp_path))

    assert [p["id"] for p in result.report["personas"]] == ["directo"]
    assert result.verdicts["directo"].passed, result.report["personas"][0]["verdict"]
    assert result.report["personas"][0]["final_status"] == "complete"
    assert len(result.report["personas"][0]["quotes"]) == 5
