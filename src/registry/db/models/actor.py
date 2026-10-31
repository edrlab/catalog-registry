"""The people allowed into the admin API. Keyed by lowercased email, never by token subject."""

import datetime
import uuid

from sqlalchemy import Boolean, CheckConstraint, DateTime, Enum, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from registry.db.base import Base
from registry.domain.enums import ActorRole


class Actor(Base):
    __tablename__ = "actors"
    __table_args__ = (
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint("length(trim(email)) > 0", name="email_not_blank"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[ActorRole] = mapped_column(
        Enum(ActorRole, name="actor_role", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        server_default="viewer",
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    #: Who vouched for this person at their last login. Provenance only: lookups use `email`.
    identity_issuer: Mapped[str | None] = mapped_column(Text, nullable=True)
    identity_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
