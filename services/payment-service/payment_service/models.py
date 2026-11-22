"""SQLAlchemy ORM models for payment_db."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import DateTime, Float, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class PaymentBase(DeclarativeBase):
    """Isolated declarative base for payment-service."""


class Payment(PaymentBase):
    """Payment transaction with idempotency key and order linkage."""

    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    order_id: Mapped[str] = mapped_column(String(36), unique=True, index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    payment_method: Mapped[str] = mapped_column(String(32), default="card", nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
