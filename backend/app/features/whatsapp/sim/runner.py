"""Runner del simulador: SQLite propio por corrida, 7 conversaciones, juez y reporte.

Cada corrida crea ``backend/.sim/runs/<YYYYMMDD-HHMM>/sim.db`` con ``create_all`` (se niega
si el engine no es SQLite: jamás toca Supabase), carga comprador + proveedores + batch con
el intake de L2 y el Excel de ejemplo, y por persona hace: ``open_conversation`` -> primer
inbound -> ``handle_inbound`` <-> proveedor simulado hasta cierre, ``[FIN]`` o
``SIM_MAX_TURNS`` turnos. El borrador outbound se le pasa al proveedor tal cual: simula una
aprobación humana sin cambios.
"""

import logging
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from app import models  # noqa: F401 - registra todas las tablas en Base.metadata
from app.core.database import Base
from app.core.security import hash_password
from app.features.auth.model import User
from app.features.quote.model import SupplierQuote
from app.features.rfq.batch_model import RFQBatch
from app.features.rfq.batch_service import create_batch_from_rows
from app.features.rfq.intake import parse_materials_xlsx
from app.features.rfq.model import RFQ
from app.features.supplier.model import Supplier
from app.features.whatsapp.loop import resolve_model
from app.features.whatsapp.model import WhatsappConversation
from app.features.whatsapp.service import handle_inbound
from app.features.whatsapp.service import open_conversation
from app.features.whatsapp.sim import report as reporting
from app.features.whatsapp.sim.budget import Budget
from app.features.whatsapp.sim.budget import BudgetExceeded
from app.features.whatsapp.sim.budget import BudgetedClient
from app.features.whatsapp.sim.fake_clients import FakeAgentClient
from app.features.whatsapp.sim.fake_clients import FakeSupplierClient
from app.features.whatsapp.sim.judge import ConversationRecord
from app.features.whatsapp.sim.judge import QuoteRecord
from app.features.whatsapp.sim.judge import TurnRecord
from app.features.whatsapp.sim.judge import judge
from app.features.whatsapp.sim.personas import Persona
from app.features.whatsapp.sim.personas import load_personas
from app.features.whatsapp.sim.settings import sim_settings
from app.features.whatsapp.sim.supplier_bot import SupplierBot

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parents[4]
EXAMPLE_XLSX = BACKEND_DIR / "examples" / "materiales_mamposteria.xlsx"

BUYER_EMAIL = "compras@sim.example.com"
BUYER_COMPANY = "Constructora Palermo SRL (simulación)"
BATCH_NAME = "Obra Palermo - mampostería (sim)"


@dataclass
class RunConfig:
    live: bool = False
    only: str | None = None
    budget_usd: float | None = None
    runs_dir: Path | None = None
    #: Solo para tests: una URL distinta a SQLite se rechaza.
    database_url: str | None = None
    max_turns: int | None = None


@dataclass
class RunResult:
    run_dir: Path
    report: dict
    report_paths: tuple[Path, Path] | None = None
    stopped_reason: str | None = None
    verdicts: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------- infraestructura
def _make_run_dir(config: RunConfig) -> Path:
    base = Path(config.runs_dir) if config.runs_dir else BACKEND_DIR / sim_settings.SIM_RUNS_DIR
    run_dir = base / datetime.now().strftime("%Y%m%d-%H%M")

    suffix = 1
    candidate = run_dir

    while candidate.exists():
        suffix += 1
        candidate = run_dir.with_name(f"{run_dir.name}-{suffix}")

    candidate.mkdir(parents=True)

    return candidate


def _make_session(run_dir: Path, database_url: str | None) -> Session:
    url = database_url or "sqlite:///" + (run_dir / "sim.db").as_posix()

    if not url.startswith("sqlite"):
        raise ValueError(f"El simulador solo corre sobre SQLite; se recibió {url.split('://')[0]}://...")

    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)

    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=True)()


def _seed(db: Session, personas: list[Persona]) -> tuple[User, dict[str, Supplier], RFQBatch]:
    buyer = User(
        email=BUYER_EMAIL,
        hashed_password=hash_password("simulacion-no-es-una-clave-real"),
        full_name="Compras Simulación",
        company_name=BUYER_COMPANY,
        contact_email=BUYER_EMAIL,
        is_active=True,
    )
    db.add(buyer)
    db.flush()

    suppliers: dict[str, Supplier] = {}

    for persona in personas:
        supplier = Supplier(
            user_id=buyer.id,
            name=persona.name,
            contact_name=persona.contact_name,
            contact_email=f"{persona.id}@sim.example.com",
            whatsapp_phone=f"54911{abs(hash(persona.id)) % 10_000_000:07d}",
        )
        db.add(supplier)
        suppliers[persona.id] = supplier

    db.commit()

    batch = create_batch_from_rows(
        db=db,
        user=buyer,
        rows=parse_materials_xlsx(EXAMPLE_XLSX),
        name=BATCH_NAME,
        site_name="Edificio Palermo Soho",
        site_address="Gorriti 4800, CABA",
        delivery_expectation=date(2026, 10, 15),
    )

    return buyer, suppliers, batch


# --------------------------------------------------------------- una persona
def _quotes_for(db: Session, conversation_id: int, rfqs_by_id: dict[int, RFQ]) -> list[QuoteRecord]:
    quotes = db.scalars(select(SupplierQuote).where(SupplierQuote.conversation_id == conversation_id)).all()

    return [
        QuoteRecord(
            item_name=rfqs_by_id[quote.rfq_id].item_name if quote.rfq_id in rfqs_by_id else f"rfq {quote.rfq_id}",
            unit_price=quote.unit_price,
            currency=quote.currency,
            iva_included=quote.iva_included,
            freight_included=quote.freight_included,
        )
        for quote in quotes
    ]


def _turn_record(result, conversation: WhatsappConversation, inbound_text: str, agent: BudgetedClient, start: int) -> TurnRecord:
    new_calls = agent.records[start:]
    raw_text = new_calls[-1].text if new_calls else None

    return TurnRecord(
        inbound=inbound_text,
        outbound=result.outbound.body if result.outbound else None,
        flags=list(result.outbound.guardrail_flags or []) if result.outbound else [],
        tool_calls=list(result.outbound.tool_calls or []) if result.outbound else [],
        raw_text=raw_text,
        status_after=conversation.status,
        stop_reasons=[str(call.stop_reason) for call in new_calls],
    )


FIN_MARKER = "[proveedor cerró con FIN]"


def mark_fin_turns(transcript_path: Path, fin_turns: list[int]) -> None:
    """Inserta el marcador de [FIN] debajo del inbound de cada turno indicado.

    Lista paralela al ConversationRecord: el runner sabe qué turnos terminaron con [FIN]
    (el token se quita del texto guardado) y lo anota en el transcript ya escrito, sin
    tocar TurnRecord ni el juez.
    """

    if not fin_turns:
        return

    lines = transcript_path.read_text(encoding="utf-8").splitlines()
    output: list[str] = []
    current = None

    for line in lines:
        output.append(line)

        if line.startswith("--- turno "):
            current = int(line.split()[2])
        elif line.startswith("PROVEEDOR > ") and current in fin_turns:
            output.append(FIN_MARKER)

    output.append(f"TURNOS EN QUE EL PROVEEDOR CERRÓ CON [FIN]: {fin_turns}")
    transcript_path.write_text("\n".join(output), encoding="utf-8")


def run_persona(
    db: Session,
    persona: Persona,
    supplier: Supplier,
    batch: RFQBatch,
    *,
    agent: BudgetedClient,
    supplier_client: BudgetedClient,
    max_turns: int,
    fin_turns: list[int] | None = None,
) -> ConversationRecord:
    rfqs_by_id = {rfq.id: rfq for rfq in batch.rfqs}
    conversation = open_conversation(db, batch.id, supplier.id)
    bot = SupplierBot(persona, supplier_client)
    turns: list[TurnRecord] = []
    fin_turns = fin_turns if fin_turns is not None else []

    greeting = f"Hola Bernardo, soy {persona.name}, mandame el pedido {batch.name}"
    start = len(agent.records)
    result = handle_inbound(db, conversation.id, greeting, client=agent)
    db.refresh(conversation)
    turns.append(_turn_record(result, conversation, greeting, agent, start))

    history = [{"role": "user", "content": result.outbound.body}] if result.outbound else []

    for _ in range(max_turns):
        if not conversation.is_open:
            break

        text, finished = bot.reply(history)

        if text:
            history.append({"role": "assistant", "content": text})
            start = len(agent.records)
            result = handle_inbound(db, conversation.id, text, client=agent)
            db.refresh(conversation)
            turns.append(_turn_record(result, conversation, text, agent, start))

            if finished:
                fin_turns.append(len(turns) - 1)

            if result.outbound:
                history.append({"role": "user", "content": result.outbound.body})
        elif finished:
            # Solo "[FIN]", sin texto: no hay inbound, pero el cierre queda registrado.
            fin_turns.append(len(turns) - 1)

        if finished:
            break

    db.refresh(conversation)

    return ConversationRecord(
        persona_id=persona.id,
        final_status=conversation.status,
        turns=turns,
        quotes=_quotes_for(db, conversation.id, rfqs_by_id),
    )


# ---------------------------------------------------------------- la corrida
def _clients(config: RunConfig, persona: Persona, budget: Budget, sdk_client: Any) -> tuple[BudgetedClient, BudgetedClient]:
    if config.live:
        return BudgetedClient(sdk_client, budget, "agent"), BudgetedClient(sdk_client, budget, "supplier")

    return (
        BudgetedClient(FakeAgentClient(), budget, "agent"),
        BudgetedClient(FakeSupplierClient(persona.offline_turns), budget, "supplier"),
    )


def run_simulation(config: RunConfig, *, sdk_client: Any = None) -> RunResult:
    personas = load_personas()

    if config.only:
        personas = [persona for persona in personas if persona.id == config.only]

        if not personas:
            raise ValueError(f"No existe la persona {config.only!r}")

    run_dir = _make_run_dir(config)
    db = _make_session(run_dir, config.database_url)

    budget = Budget(config.budget_usd if config.budget_usd is not None else sim_settings.SIM_BUDGET_USD, sim_settings.prices)
    max_turns = config.max_turns or sim_settings.SIM_MAX_TURNS
    agent_model = resolve_model()
    supplier_model = sim_settings.SIM_SUPPLIER_MODEL

    if config.live and sdk_client is None:
        import anthropic  # noqa: PLC0415 - import diferido: offline no lo necesita

        sdk_client = anthropic.Anthropic()

    _, suppliers, batch = _seed(db, personas)

    rows: list[dict] = []
    stopped_reason: str | None = None
    verdicts: dict[str, Any] = {}

    for persona in personas:
        agent, supplier_client = _clients(config, persona, budget, sdk_client)
        row: dict[str, Any] = {"id": persona.id, "name": persona.name}
        fin_turns: list[int] = []

        try:
            record = run_persona(
                db, persona, suppliers[persona.id], batch,
                agent=agent, supplier_client=supplier_client, max_turns=max_turns, fin_turns=fin_turns,
            )
            verdict = judge(record, persona)
            verdicts[persona.id] = verdict
            row.update(
                final_status=record.final_status,
                turns=len(record.turns),
                supplier_fin_turns=list(fin_turns),
                verdict=reporting.verdict_to_dict(verdict),
                quotes=[vars(quote) for quote in record.quotes],
            )
            transcript_path = run_dir / f"transcript_{persona.id}.txt"
            reporting.write_transcript(transcript_path, persona.name, record, verdict)
            mark_fin_turns(transcript_path, fin_turns)
        except BudgetExceeded as exc:
            stopped_reason = f"budget_exceeded en {persona.id}: {exc}"
            row.update(error=str(exc), final_status="budget_exceeded")
            logger.warning(stopped_reason)
        finally:
            row["usage"] = {
                "agent": _usage_of(agent, agent_model),
                "supplier": _usage_of(supplier_client, supplier_model),
            }

        rows.append(row)

        if stopped_reason:
            break

    report = reporting.build_report(
        run_dir=run_dir, live=config.live, agent_model=agent_model, supplier_model=supplier_model,
        personas=rows, budget=budget, stopped_reason=stopped_reason,
    )
    paths = reporting.write_report(run_dir, report)
    db.close()

    return RunResult(run_dir=run_dir, report=report, report_paths=paths, stopped_reason=stopped_reason, verdicts=verdicts)


def _usage_of(client: BudgetedClient, model: str) -> dict:
    records = client.records

    cache_creation = sum(r.cache_creation_input_tokens for r in records)
    cache_read = sum(r.cache_read_input_tokens for r in records)
    uncached = sum(r.input_tokens for r in records)

    return {
        "model": model,
        "calls": len(records),
        "input_tokens": uncached,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
        "total_input_tokens": uncached + cache_creation + cache_read,
        "output_tokens": sum(r.output_tokens for r in records),
        "cost_usd": round(sum(r.cost_usd for r in records), 6),
    }
