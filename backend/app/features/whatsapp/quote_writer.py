"""Ejecutor de herramientas: lo que el agente escribe en la base.

``record_quote`` crea o actualiza ``supplier_quotes`` con clave (conversation_id, rfq_id),
``source = "whatsapp"`` y los flags de IVA y flete. Dos rechazos, que vuelven al modelo
como ``tool_result`` de error (no como excepción):

* el ``rfq_id`` no pertenece al batch de la conversación;
* ``evidence`` no aparece literalmente (normalizando espacios y mayúsculas) en ningún
  mensaje entrante de la conversación. Es lo que impide que el modelo "recuerde" un precio
  que el proveedor no escribió.

``ask_buyer`` queda en ``tool_calls`` del mensaje (no hay tabla de consultas al comprador
en L3a). ``set_status`` pide el estado; el service lo aplica al cerrar el turno.
"""

import re
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from datetime import datetime
from decimal import Decimal
from decimal import InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.mixins import utcnow
from app.features.quote.model import SupplierQuote
from app.features.rfq.model import RFQ
from app.features.supplier.model import Supplier
from app.features.whatsapp import tools
from app.features.whatsapp.loop import ToolOutcome
from app.features.whatsapp.model import WhatsappConversation

_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


#: Marcado de WhatsApp/markdown y el signo de pesos: no cuentan para la literalidad.
#: Un proveedor real escribe "*Cemento:* $12.500" y el modelo cita "Cemento: 12.500".
_IGNORED_FOR_LITERALITY = re.compile(r"[*_~`$]")


def normalize_text(text: str) -> str:
    """Minúsculas, sin marcado ni "$", espacios colapsados: la comparación literal tolerante."""

    return " ".join(_IGNORED_FOR_LITERALITY.sub("", text or "").lower().split())


def evidence_is_literal(evidence: str, inbound_bodies: list[str]) -> bool:
    needle = normalize_text(evidence)

    if len(needle) < 3:
        return False

    return any(needle in normalize_text(body) for body in inbound_bodies)


#: Un número con separadores opcionales: "9800", "9.800", "9.800,50", "9,800.00", "9,80".
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")

#: Tolerancia al comparar el precio declarado con los números de la evidencia.
PRICE_TOLERANCE = Decimal("0.01")


def _number_candidates(token: str) -> set[Decimal]:
    """Interpretaciones posibles de un token numérico, en formato argentino y en el de EE. UU.

    Con los dos separadores el último es el decimal ("9.800,50" -> 9800.50; "9,800.00" ->
    9800.00). Con uno solo hay ambigüedad ("9.800" es 9800 en Argentina y 9.8 en EE. UU.), así
    que se devuelven las dos lecturas y gana la que coincida con lo declarado.
    """

    candidates: set[Decimal] = set()

    def add(text: str) -> None:
        try:
            candidates.add(Decimal(text))
        except InvalidOperation:
            pass

    if "." in token and "," in token:
        decimal_sep = "," if token.rfind(",") > token.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        add(token.replace(thousands_sep, "").replace(decimal_sep, "."))
    elif "." in token or "," in token:
        sep = "." if "." in token else ","
        add(token.replace(sep, ""))  # lectura "separador de miles"
        head, _, tail = token.rpartition(sep)
        if token.count(sep) == 1:
            add(f"{head}.{tail}")  # lectura "separador decimal"
    else:
        add(token)

    return candidates


def extract_prices(text: str) -> set[Decimal]:
    """Todos los valores numéricos que se pueden leer en ``text``, en cualquiera de sus lecturas."""

    values: set[Decimal] = set()

    for token in _NUMBER.findall(text or ""):
        values |= _number_candidates(token)

    return values


def price_in_evidence(unit_price: Decimal, evidence: str) -> bool:
    return any(abs(value - unit_price) <= PRICE_TOLERANCE for value in extract_prices(evidence))


def _parse_validity(value: str | None) -> date | None:
    """``dd/mm/yyyy`` dentro del texto -> fecha; cualquier otra cosa queda en remarks."""

    if not value:
        return None

    match = _DATE.search(value)

    if not match:
        return None

    day, month, year = (int(part) for part in match.groups())

    try:
        return date(year, month, day)
    except ValueError:
        return None


def _to_decimal(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None

    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


@dataclass
class QuoteToolExecutor:
    """Estado del turno: la conversación, su batch y lo que dijo el proveedor."""

    db: Session
    conversation: WhatsappConversation
    rfqs_by_id: dict[int, RFQ]
    supplier: Supplier
    inbound_bodies: list[str]
    #: Consultas al comprador pedidas en este turno (auditoría; van a tool_calls).
    buyer_questions: list[str] = field(default_factory=list)

    # ------------------------------------------------------------- dispatch
    def __call__(self, name: str, tool_input: dict) -> ToolOutcome:
        if name == tools.RECORD_QUOTE:
            return self.record_quote(tool_input)

        if name == tools.ASK_BUYER:
            return self.ask_buyer(tool_input)

        if name == tools.SET_STATUS:
            return self.set_status(tool_input)

        return ToolOutcome(content=f"Herramienta desconocida: {name}.", ok=False)

    # ---------------------------------------------------------- record_quote
    def record_quote(self, tool_input: dict) -> ToolOutcome:
        try:
            rfq_id = int(tool_input.get("rfq_id"))
        except (TypeError, ValueError):
            return ToolOutcome(content="rfq_id tiene que ser el entero de la ficha.", ok=False)

        rfq = self.rfqs_by_id.get(rfq_id)

        if rfq is None:
            valid = ", ".join(str(key) for key in sorted(self.rfqs_by_id))
            return ToolOutcome(
                content=f"Rechazado: rfq_id {rfq_id} no pertenece a este pedido. Válidos: {valid}.",
                ok=False,
            )

        evidence = str(tool_input.get("evidence") or "").strip()

        if not evidence_is_literal(evidence, self.inbound_bodies):
            return ToolOutcome(
                content=(
                    "Rechazado: evidence no aparece literalmente en ningún mensaje del proveedor. "
                    "Copiá el fragmento exacto que escribió, sin parafrasear. Si el proveedor no "
                    "dijo el precio, no lo registres: pedíselo."
                ),
                ok=False,
            )

        unit_price = _to_decimal(tool_input.get("unit_price"))

        quote = self._find_quote(rfq_id)
        created = quote is None

        # Con precio declarado, la evidencia además tiene que contener ese número: la cita
        # literal sola no alcanza para respaldar un valor que el proveedor no escribió.
        # Excepción (L3b): si el quote ya existe con ese mismo precio, se está corrigiendo otra
        # cosa (flete, pago, validez) y la evidencia nueva no tiene por qué repetir el número.
        same_price = (
            not created
            and quote.unit_price is not None
            and unit_price is not None
            and abs(quote.unit_price - unit_price) <= PRICE_TOLERANCE
        )

        if unit_price is not None and not same_price and not price_in_evidence(unit_price, evidence):
            return ToolOutcome(
                content=(
                    f"Rechazado: el precio no figura en la evidencia citada. Declaraste {unit_price} "
                    f"pero en '{evidence}' no aparece ese número. Citá el fragmento que trae el "
                    "precio tal como lo escribió el proveedor."
                ),
                ok=False,
            )

        if created:
            quote = SupplierQuote(
                rfq_id=rfq_id,
                supplier_id=self.supplier.id,
                supplier_name=self.supplier.name,
                contact_email=self.supplier.contact_email,
                conversation_id=self.conversation.id,
                source="whatsapp",
                unit=rfq.unit,
            )
            self.db.add(quote)

        currency = tool_input.get("currency")
        lead_time = tool_input.get("lead_time_days")
        payment_terms = tool_input.get("payment_terms")
        validity = tool_input.get("validity")

        quote.unit_price = unit_price
        quote.currency = str(currency).upper() if currency else (quote.currency or rfq.currency)
        quote.iva_included = tool_input.get("iva_included")
        quote.freight_included = tool_input.get("freight_included")
        quote.lead_time = int(lead_time) if isinstance(lead_time, int) and not isinstance(lead_time, bool) else None
        quote.payment_terms = str(payment_terms)[:255] if payment_terms else None
        quote.validity_date = _parse_validity(validity)
        quote.remarks = f"Validez: {validity}"[:2000] if validity and quote.validity_date is None else None
        quote.unparsed_notes = evidence[:4000]
        quote.completeness = "complete" if unit_price is not None else "incomplete"
        quote.missing_fields = [] if unit_price is not None else ["unit_price"]
        quote.submitted_at = utcnow()  # se pisa en cada actualización, a propósito

        self.db.flush()

        return ToolOutcome(content=self._record_summary(quote, rfq, created))

    def _find_quote(self, rfq_id: int) -> SupplierQuote | None:
        return self.db.scalar(
            select(SupplierQuote).where(
                SupplierQuote.conversation_id == self.conversation.id,
                SupplierQuote.rfq_id == rfq_id,
            )
        )

    def _record_summary(self, quote: SupplierQuote, rfq: RFQ, created: bool) -> str:
        registered = {
            row[0]
            for row in self.db.execute(
                select(SupplierQuote.rfq_id).where(SupplierQuote.conversation_id == self.conversation.id)
            )
        }
        pending = [
            f"{key} ({self.rfqs_by_id[key].item_name})" for key in sorted(self.rfqs_by_id) if key not in registered
        ]
        missing = [
            label
            for label, value in (
                ("precio", quote.unit_price),
                ("IVA", quote.iva_included),
                ("flete", quote.freight_included),
                ("plazo", quote.lead_time),
                ("forma de pago", quote.payment_terms),
                ("validez", quote.validity_date or quote.remarks),
            )
            if value is None
        ]

        verb = "Registrado" if created else "Actualizado"
        price = f"{quote.unit_price} {quote.currency}" if quote.unit_price is not None else "sin precio"

        return (
            f"{verb} rfq_id {rfq.id} ({rfq.item_name}): {price}. "
            f"Sin dato en este ítem: {', '.join(missing) or 'nada'}. "
            f"Ítems sin registrar todavía: {', '.join(pending) or 'ninguno'}."
        )

    # -------------------------------------------------------------- ask_buyer
    def ask_buyer(self, tool_input: dict) -> ToolOutcome:
        question = str(tool_input.get("question") or "").strip()

        if len(question) < 3:
            return ToolOutcome(content="La pregunta al comprador no puede estar vacía.", ok=False)

        self.buyer_questions.append(question)

        return ToolOutcome(
            content="Consulta registrada para el comprador. Avisale al proveedor que lo consultás y le confirmás."
        )

    # ------------------------------------------------------------- set_status
    def set_status(self, tool_input: dict) -> ToolOutcome:
        status = tool_input.get("status")
        reason = str(tool_input.get("reason") or "").strip()

        if status not in tools.STATUS_VALUES:
            return ToolOutcome(
                content=f"Estado inválido. Usá uno de: {', '.join(tools.STATUS_VALUES)}.",
                ok=False,
            )

        return ToolOutcome(content=f"Estado pedido: {status}.", status=status, reason=reason or status)


def close_conversation(conversation: WhatsappConversation, status: str, reason: str, *, now: datetime | None = None) -> None:
    conversation.status = status
    conversation.closed_at = now or utcnow()
    conversation.closed_reason = (reason or status)[:500]
