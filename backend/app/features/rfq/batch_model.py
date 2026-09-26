"""RFQ batch (laboratorio Bernardo, lote L1).

La base modela un ``RFQ`` = un ítem. El laboratorio agrupa N ítems en un *batch*, que
lleva una sola vez lo que hoy cada RFQ repite: la obra (site), la expectativa de
entrega y el plazo para cotizar. Cada ``RFQ`` apunta a su batch por
``rfqs.rfq_batch_id`` (nullable, para que todos los caminos de la base sigan creando
RFQs sueltos).

Regla anti-acumulación del lab: la tabla nueva vive en su propio archivo; las
columnas nuevas viven en los modelos existentes.
"""

from datetime import date
from datetime import datetime
from datetime import timedelta

from sqlalchemy import Date
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.core.database import Base
from app.core.mixins import TimestampMixin
from app.core.mixins import utcnow

BATCH_STATUSES = ("open", "closed", "expired")

#: Plazo por defecto para cotizar, en horas, cuando el comprador no fija uno.
DEFAULT_BATCH_DEADLINE_HOURS = 72


def default_batch_deadline() -> datetime:
    """``now + 72 h``, evaluado al insertar (no al importar el módulo)."""

    return utcnow() + timedelta(hours=DEFAULT_BATCH_DEADLINE_HOURS)


class RFQBatch(TimestampMixin, Base):
    __tablename__ = "rfq_batches"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    # ---------------------------------------------------------------- tenancy
    #: El comprador dueño del batch. Nullable SOLO por la migración (lote L2): toda
    #: fila nueva lo lleva (``batch_service`` lo exige) y pasa a NOT NULL cuando
    #: haya que limpiar filas viejas. CASCADE como ``rfqs.user_id`` en la base.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "users.id",
            ondelete="CASCADE",
            name="fk_rfq_batches_user_id_users",
        ),
        nullable=True,
        index=True,
    )

    #: Nombre con el que el comprador reconoce el pedido, p. ej. "Obra Palermo - hierro".
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    # ------------------------------------------------------------------- obra
    site_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    site_address: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
    )

    #: Cuándo espera el comprador tener los materiales en obra.
    delivery_expectation: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
    )

    # ------------------------------------------------------------------ plazo
    #: Hasta cuándo se aceptan cotizaciones. Default: 72 h desde la creación.
    deadline: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=default_batch_deadline,
        index=True,
    )

    #: open | closed | expired (ver ``BATCH_STATUSES``).
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="open",
        server_default="open",
        index=True,
    )

    notes: Mapped[str | None] = mapped_column(
        String(2000),
        nullable=True,
    )

    # -------------------------------------------------------------- relations
    #: Los ítems del batch. Sin cascade de borrado: borrar un batch deja los RFQs
    #: sueltos (``rfq_batch_id`` pasa a NULL por el ``ondelete`` de la FK).
    rfqs = relationship(
        "RFQ",
        back_populates="batch",
    )

    #: Sin ``back_populates``: no se agrega un ``batches`` al modelo ``User`` de la
    #: base (regla del lab: nada de lógica nueva en archivos existentes).
    owner = relationship("User")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<RFQBatch id={self.id} name={self.name!r} status={self.status!r}>"
