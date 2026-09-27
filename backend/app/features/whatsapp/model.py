"""Conversaciones y mensajes de WhatsApp (spec sección 5).

Una conversación es (rfq_batch, supplier): la charla con un proveedor sobre una lista
de materiales. Los mensajes guardan lo que dijo cada lado; los salientes nacen como
**borrador** (``approved_by`` y ``sent_at`` en NULL) hasta que un humano los apruebe.

Tipos: ``tool_calls`` y ``guardrail_flags`` son ``JSON`` genérico, no ``jsonb`` ni
``ARRAY``, para que el mismo modelo corra en SQLite (tests) y en PostgreSQL. Es el
criterio de la base para ``rfqs.required_fields`` y compañía.
"""

from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import JSON
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.core.database import Base
from app.core.mixins import TimestampMixin
from app.core.mixins import utcnow

CONVERSATION_STATUSES = ("open", "complete", "supplier_declined", "needs_human", "expired")

#: Estados en los que el agente ya no corre: el inbound se guarda y nada más.
CLOSED_STATUSES = ("complete", "supplier_declined", "needs_human", "expired")

MESSAGE_DIRECTIONS = ("inbound", "outbound")


class WhatsappConversation(TimestampMixin, Base):
    __tablename__ = "whatsapp_conversations"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    rfq_batch_id: Mapped[int] = mapped_column(
        ForeignKey(
            "rfq_batches.id",
            ondelete="CASCADE",
            name="fk_whatsapp_conversations_rfq_batch_id_rfq_batches",
        ),
        nullable=False,
        index=True,
    )

    supplier_id: Mapped[int] = mapped_column(
        ForeignKey(
            "suppliers.id",
            ondelete="CASCADE",
            name="fk_whatsapp_conversations_supplier_id_suppliers",
        ),
        nullable=False,
        index=True,
    )

    #: open | complete | supplier_declined | needs_human | expired
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="open",
        server_default="open",
        index=True,
    )

    #: Quién abrió la charla. En el MVP siempre el proveedor, desde el link wa.me.
    opened_by: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="supplier",
        server_default="supplier",
    )

    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )

    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    closed_reason: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )

    #: L4: el ``from`` crudo de Meta (wa_id) del último inbound, tal cual llegó. Es el
    #: destino de las respuestas; ``suppliers.whatsapp_phone`` es solo para reconocerlo.
    wa_from: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )

    # ------------------------------------------------------------- consumo
    #: Acumulados por conversación: primer dato de costo por cotización (spec 9.5).
    input_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    output_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    model_calls: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    # -------------------------------------------------------------- relations
    batch = relationship("RFQBatch")

    supplier = relationship("Supplier")

    messages = relationship(
        "WhatsappMessage",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="WhatsappMessage.id",
    )

    #: Las cotizaciones que salieron de esta charla (``supplier_quotes.conversation_id``).
    #: Sin ``back_populates``: ``SupplierQuote`` no se toca más allá de la columna.
    quotes = relationship(
        "SupplierQuote",
        order_by="SupplierQuote.rfq_id",
    )

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WhatsappConversation id={self.id} batch={self.rfq_batch_id} status={self.status!r}>"


class WhatsappMessage(TimestampMixin, Base):
    __tablename__ = "whatsapp_messages"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    conversation_id: Mapped[int] = mapped_column(
        ForeignKey(
            "whatsapp_conversations.id",
            ondelete="CASCADE",
            name="fk_whatsapp_messages_conversation_id_whatsapp_conversations",
        ),
        nullable=False,
        index=True,
    )

    #: inbound (del proveedor) | outbound (de Bernardo)
    direction: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        index=True,
    )

    body: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    media_url: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
    )

    media_type: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    #: Id del mensaje en Cloud API. Único (L4): es la clave del dedupe de inbound y del
    #: envío. NULL en los borradores hasta que se mandan; los NULL no chocan entre sí.
    wa_message_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        unique=True,
        index=True,
    )

    #: Solo en outbound: los bloques tool_use y tool_result completos del turno,
    #: incluidas las llamadas rechazadas y su motivo. Auditoría, no historial del modelo.
    tool_calls: Mapped[list | None] = mapped_column(
        JSON,
        nullable=True,
    )

    #: Frenos que se dispararon sobre este mensaje, p. ej. ["emoji_removed"].
    guardrail_flags: Mapped[list | None] = mapped_column(
        JSON,
        nullable=True,
    )

    #: Quién aprobó el borrador. NULL = borrador sin aprobar.
    approved_by: Mapped[int | None] = mapped_column(
        ForeignKey(
            "users.id",
            ondelete="SET NULL",
            name="fk_whatsapp_messages_approved_by_users",
        ),
        nullable=True,
    )

    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # ------------------------------------------------------ borradores (L5b)
    #: Un borrador descartado no se manda ni entra al historial del modelo. Motivos:
    #: "manual" (Diego), "superseded" (llegó un inbound nuevo y el agente redactó otro).
    discarded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    discard_reason: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )

    #: Texto original del agente cuando Diego editó el borrador (solo la primera vez).
    original_body: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    edited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # -------------------------------------------------------------- relations
    conversation = relationship(
        "WhatsappConversation",
        back_populates="messages",
    )

    @property
    def is_draft(self) -> bool:
        """Pendiente de aprobación: outbound, sin enviar, sin aprobar y sin descartar."""

        return (
            self.direction == "outbound"
            and self.approved_by is None
            and self.sent_at is None
            and self.discarded_at is None
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WhatsappMessage id={self.id} {self.direction} conv={self.conversation_id}>"
