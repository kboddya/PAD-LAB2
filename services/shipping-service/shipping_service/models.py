"""SQLAlchemy ORM models for shipping_db."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class ShippingBase(DeclarativeBase):
    """Isolated declarative base for shipping-service."""


class OrderShippingIntent(ShippingBase):
    """Shipping details captured from order.created until payment confirmation."""

    __tablename__ = "order_shipping_intents"

    order_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    shipping_address: Mapped[str] = mapped_column(Text, nullable=False)
    shipping_method: Mapped[str] = mapped_column(String(32), default="standard", nullable=False)
    shipping_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class Shipment(ShippingBase):
    """Fulfillment shipment tracking record created upon successful payment."""

    __tablename__ = "shipments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    order_id: Mapped[str] = mapped_column(String(36), unique=True, index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    tracking_number: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    shipping_method: Mapped[str] = mapped_column(String(32), default="standard", nullable=False)
    carrier: Mapped[str] = mapped_column(String(80), nullable=False)
    shipping_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    shipping_address: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="preparing", index=True, nullable=False)
    estimated_days: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
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

    checkpoints: Mapped[list["ShipmentCheckpoint"]] = relationship(
        back_populates="shipment",
        cascade="all, delete-orphan",
        order_by="ShipmentCheckpoint.recorded_at",
    )


class ShipmentCheckpoint(ShippingBase):
    """Chronological tracking checkpoint along a shipment's delivery route."""

    __tablename__ = "shipment_checkpoints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    shipment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("shipments.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    location: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    shipment: Mapped[Shipment] = relationship(back_populates="checkpoints")
