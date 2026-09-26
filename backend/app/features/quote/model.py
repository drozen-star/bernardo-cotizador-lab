"""Supplier quotes.

Extended in place: ``supplier_name``, ``unit_price``, ``currency``, ``lead_time``,
``payment_terms`` and ``remarks`` keep their original names and semantics so the
base codebase's importer, manual form, and tests continue to work. Everything
added is either nullable or defaulted.

Two ideas are new here:

* **Source** — a quote can arrive via the public web form, manual entry, or bulk
  import. Only form quotes have an ``invitation``.
* **Normalization** — the raw submitted figures are never overwritten. The
  comparison engine's converted/landed-cost results are stored alongside them
  (``normalized_*``), so a buyer can always see what the supplier actually said.
"""

from datetime import date
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean
from sqlalchemy import Date
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import JSON
from sqlalchemy import Numeric
from sqlalchemy import String
from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.core.database import Base
from app.core.mixins import TimestampMixin

QUOTE_SOURCES = ("form", "manual", "import", "whatsapp")

#: complete | incomplete | flagged
COMPLETENESS_STATES = ("complete", "incomplete", "flagged")


class SupplierQuote(TimestampMixin, Base):
    __tablename__ = "supplier_quotes"

    # Lab L3a: una cotización por (conversación de WhatsApp, ítem). Con
    # conversation_id NULL (form, manual, import) la unicidad no aplica: los NULL son
    # distintos entre sí tanto en SQLite como en PostgreSQL.
    __table_args__ = (
        UniqueConstraint("conversation_id", "rfq_id", name="uq_supplier_quote_conversation_rfq"),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    rfq_id: Mapped[int] = mapped_column(
        ForeignKey(
            "rfqs.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    #: Set only for quotes submitted through a tokenized form link.
    invitation_id: Mapped[int | None] = mapped_column(
        ForeignKey("invitations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    supplier_id: Mapped[int | None] = mapped_column(
        ForeignKey("suppliers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    #: Lab L3a: de qué conversación de WhatsApp salió la cotización. NULL para las
    #: demás fuentes.
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "whatsapp_conversations.id",
            ondelete="SET NULL",
            name="fk_supplier_quotes_conversation_id_whatsapp_conversations",
        ),
        nullable=True,
        index=True,
    )

    #: Reference shown to the supplier on the confirmation page.
    reference_number: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        unique=True,
        index=True,
    )

    # ------------------------------------------------------- original columns
    supplier_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    #: Nullable because an "incomplete" submission is a real, tracked state: the
    #: supplier responded but left a required field blank. A null price is
    #: excluded from cost ranking rather than treated as zero.
    unit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2),
        nullable=True,
    )

    currency: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="SGD",
        server_default="SGD",
    )

    #: Mobilisation time in days for services, production lead time for goods.
    lead_time: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    payment_terms: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    remarks: Mapped[str | None] = mapped_column(
        String(2000),
        nullable=True,
    )

    # ------------------------------------------------------- form-collected
    contact_email: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    #: Mirrors the RFQ's unit or rate basis. The database default is the same one
    #: ``RFQCreate`` resolves for a services RFQ, because that is this product's
    #: default procurement type.
    unit: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="per job",
        server_default="per job",
    )

    incoterms: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
    )

    moq: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    validity_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
    )

    warranty_months: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    #: Supplier-quoted freight. Combined with duties/taxes/discount by the
    #: landed-cost model in comparison/cost.py.
    shipping_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2),
        nullable=True,
    )

    duties: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2),
        nullable=True,
    )

    taxes: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2),
        nullable=True,
    )

    discount: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2),
        nullable=True,
    )

    notes: Mapped[str | None] = mapped_column(
        String(4000),
        nullable=True,
    )

    #: [{key, filename, content_type, size, url}] — the key is storage-relative.
    attachments: Mapped[list | None] = mapped_column(
        JSON,
        nullable=True,
    )

    source: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="manual",
        server_default="manual",
    )

    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # ---------------------------------------------------------- normalization
    normalized_currency: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
    )

    #: Unit price converted into the RFQ's currency.
    normalized_unit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 4),
        nullable=True,
    )

    #: Unit price converted into the RFQ's currency *and* adjusted to the RFQ's
    #: Incoterms basis.
    normalized_incoterms: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
    )

    #: Full landed cost for the RFQ quantity at the RFQ's Incoterms basis.
    normalized_total_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(16, 2),
        nullable=True,
    )

    #: The itemised breakdown behind ``normalized_total_cost``.
    cost_breakdown: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
    )

    normalized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    #: {"price": 82.5, "lead_time": 70.0, ...} — per-criterion scores.
    scores: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
    )

    composite_score: Mapped[Decimal | None] = mapped_column(
        Numeric(6, 2),
        nullable=True,
    )

    completeness: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="complete",
        server_default="complete",
        index=True,
    )

    #: Required fields the supplier did not answer — drives targeted follow-ups.
    missing_fields: Mapped[list | None] = mapped_column(
        JSON,
        nullable=True,
    )

    #: Human-readable concerns surfaced to the buyer, e.g. long lead time,
    #: expired validity, unusual payment terms.
    risk_flags: Mapped[list | None] = mapped_column(
        JSON,
        nullable=True,
    )

    #: Free-text the supplier typed that the parser could not map to a field.
    unparsed_notes: Mapped[str | None] = mapped_column(
        String(4000),
        nullable=True,
    )

    #: Set when the supplier says they cannot finalize a value until the buyer
    #: supplies something. Such a gap is escalated, never chased.
    blocking_question: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
    )

    #: 0–1 confidence reported by the parser. Low values are surfaced for review.
    parse_confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(4, 2),
        nullable=True,
    )

    # ------------------------------------------------------------------ services
    #: Hours until someone is on site. The services counterpart of a lead time, and
    #: the number that most often decides who wins a maintenance job.
    response_time_hours: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    #: Attendance fee, charged whether or not billable work follows. The most
    #: common surprise on a maintenance invoice, which is why the comparison flags
    #: a quote that omits it.
    callout_charge: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2),
        nullable=True,
    )

    #: Hourly labour rate, for quotes that price labour and materials separately.
    labour_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2),
        nullable=True,
    )

    #: Percentage the contractor adds to materials they supply.
    materials_markup_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(6, 2),
        nullable=True,
    )

    #: Licences and certifications claimed, e.g. ["bizSAFE Level 3", "ISO 9001"].
    #: Scored against the RFQ's ``required_accreditations``: a missing one caps the
    #: score, because the work may not lawfully proceed without it.
    compliance_accreditations: Mapped[list | None] = mapped_column(
        JSON,
        nullable=True,
    )

    #: GST or equivalent as a percentage. When set and no explicit tax amount is
    #: given, tax is derived from it so the final cost is not a surprise.
    gst_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2),
        nullable=True,
    )

    # ------------------------------------------------------------------ lab L1
    #: Si el precio informado ya incluye IVA. NULL = el proveedor no lo aclaró, y
    #: eso es un dato en sí (dispara la repregunta). ``Boolean`` y no ``Integer``:
    #: ver test_postgres_compat sobre ``.is_(True)``.
    iva_included: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
    )

    #: Si el precio incluye el flete hasta la obra. Misma semántica del NULL.
    freight_included: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
    )

    # -------------------------------------------------------------- relations
    rfq = relationship(
        "RFQ",
        back_populates="quotes",
    )

    invitation = relationship(
        "Invitation",
        back_populates="quote",
    )

    supplier = relationship(
        "Supplier",
        back_populates="quotes",
    )

    # --------------------------------------------------------------- helpers
    @property
    def has_price(self) -> bool:
        return self.unit_price is not None and self.unit_price > 0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SupplierQuote id={self.id} supplier={self.supplier_name!r}>"
