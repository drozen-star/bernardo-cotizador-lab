"""RFQ (Request for Quotation).

Extended in place from the base codebase: every original column is retained with
its original nullability so the existing manual-entry, CSV, and PDF import paths
keep working untouched. The new columns are the ones the autopilot needs —
deadline, required-field contract, and scoring weights.
"""

from datetime import date
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Date
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import JSON
from sqlalchemy import Numeric
from sqlalchemy import String
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.core.database import Base
from app.core.mixins import TimestampMixin
from app.core.mixins import generate_rfq_number

RFQ_STATUSES = ("draft", "open", "closed", "awarded", "cancelled")

#: The field contract a supplier's submission is checked against. Kept as plain
#: strings (not an Enum) so new fields can be added without a migration.
#:
#: The default here is the SERVICES contract, because this product exists for
#: building maintenance and minor works. ``taxonomy.py`` holds both contracts and
#: the goods one, which this list used to be.
DEFAULT_REQUIRED_FIELDS = [
    "unit_price",
    "currency",
    "unit",
    "response_time",
    "payment_terms",
    "validity_date",
]


class RFQ(TimestampMixin, Base):
    __tablename__ = "rfqs"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    # ---------------------------------------------------------------- tenancy
    # Nullable so the base codebase's direct constructions keep working; the
    # API always sets it, and unowned RFQs are legacy/imported records.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    #: Human-readable reference, e.g. RFQ-2026-8F3A21C4. Also the middle segment
    #: of a supplier's confirmation number (SQ-<rfq_number>-<invitation id>).
    rfq_number: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=generate_rfq_number,
        index=True,
    )

    # ------------------------------------------------------------- item detail
    item_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    specification: Mapped[str] = mapped_column(
        String(1000),
        nullable=False,
    )

    quantity: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    #: What the quantity counts, and what a price is quoted against: a rate basis for
    #: services ("per job", "per hour", "per sqm") or a unit of measure for goods
    #: ("pcs", "kg", "m"). ``RFQCreate`` resolves the right default for the
    #: procurement type — the two paths need different ones, and the ORM-level default
    #: here is only the fallback for a row inserted without saying.
    unit: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="per job",
        server_default="per job",
    )

    #: Buyer's expected delivery (kept from the base codebase).
    delivery_expectation: Mapped[date] = mapped_column(
        Date,
        nullable=False,
    )

    # --------------------------------------------------------------- commercial
    #: Preferred currency for comparison. Supplier quotes in other currencies are
    #: converted into this one (see comparison/fx.py). Defaults to SGD because this
    #: product is built for Singapore facilities and building-services procurement;
    #: ``BASE_CURRENCY`` overrides the application-level default for another market.
    currency: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="SGD",
        server_default="SGD",
    )

    #: Preferred Incoterms (EXW / FOB / CIF / DDP ...). Quotes on other terms are
    #: adjusted to this basis for comparison (see comparison/incoterms.py).
    incoterms: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
    )

    #: Submission deadline — the scheduler stops (and expires) at this instant.
    deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    #: Which fields a submission must carry to count as complete.
    required_fields: Mapped[list | None] = mapped_column(
        JSON,
        nullable=True,
        default=lambda: list(DEFAULT_REQUIRED_FIELDS),
    )

    #: Per-RFQ override of the comparison weights, e.g. {"price": 0.5, ...}.
    scoring_weights: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="open",
        server_default="open",
        index=True,
    )

    #: Printed on the public form so the supplier sees who is asking.
    buyer_company: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    category: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )

    # ------------------------------------------------------- what is being bought
    #: "service" or "goods". Selects the required-field contract, the default
    #: scoring weights, and the vocabulary the UI and follow-up emails use.
    #: Defaults to "service": this product is for maintenance and minor works.
    procurement_type: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="service",
        server_default="service",
        index=True,
    )

    # ------------------------------------------------ site (services)
    #: Where the work is. The services counterpart of a delivery address, and
    #: usually the first thing a contractor asks about.
    site_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    site_address: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
    )

    #: Access hours, permit-to-work requirements, escorts, lift booking — the
    #: details that decide whether a site visit is a two-hour job or a two-day one.
    site_access_notes: Mapped[str | None] = mapped_column(
        String(2000),
        nullable=True,
    )

    #: Response time the buyer requires, in hours. Recorded so the comparison can
    #: flag a supplier who quoted slower than asked, rather than only ranking them.
    required_response_hours: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    #: Accreditations the buyer requires (EMA LEW, bizSAFE, PUB, ISO…). A quote
    #: missing one is capped in scoring, because the work may not lawfully proceed.
    required_accreditations: Mapped[list | None] = mapped_column(
        JSON,
        nullable=True,
    )

    #: GST or equivalent, percent. 9% is the current Singapore rate and the default
    #: for an SGD RFQ; set to 0 for a buyer who is not GST-registered.
    gst_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2),
        nullable=True,
    )

    notes: Mapped[str | None] = mapped_column(
        String(2000),
        nullable=True,
    )

    # ------------------------------------------------------------ batch (lab L1)
    #: El batch (pedido de N ítems) al que pertenece este RFQ. Nullable para que
    #: los caminos de la base (manual, CSV, PDF, tests) sigan creando RFQs sueltos.
    #: SET NULL al borrar el batch: el ítem queda, solo pierde la agrupación.
    rfq_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "rfq_batches.id",
            ondelete="SET NULL",
            # Con nombre: la migración tiene que poder hacer drop_constraint en el
            # downgrade, y SQLite en batch mode solo lo encuentra si está nombrada.
            name="fk_rfqs_rfq_batch_id_rfq_batches",
        ),
        nullable=True,
        index=True,
    )

    # -------------------------------------------------------------- relations
    owner = relationship(
        "User",
        back_populates="rfqs",
    )

    batch = relationship(
        "RFQBatch",
        back_populates="rfqs",
    )

    quotes = relationship(
        "SupplierQuote",
        back_populates="rfq",
        cascade="all, delete-orphan",
    )

    invitations = relationship(
        "Invitation",
        back_populates="rfq",
        cascade="all, delete-orphan",
    )

    followups = relationship(
        "FollowUp",
        back_populates="rfq",
        cascade="all, delete-orphan",
    )

    comparisons = relationship(
        "Comparison",
        back_populates="rfq",
        cascade="all, delete-orphan",
    )

    # --------------------------------------------------------------- helpers
    @property
    def required_field_list(self) -> list[str]:
        return list(self.required_fields or DEFAULT_REQUIRED_FIELDS)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<RFQ id={self.id} number={self.rfq_number!r}>"
