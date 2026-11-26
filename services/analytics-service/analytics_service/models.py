"""SQLAlchemy CQRS read-model ORM definitions for analytics_db."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class AnalyticsBase(DeclarativeBase):
    """Isolated declarative base for analytics-service."""


class ProcessedAnalyticsEvent(AnalyticsBase):
    """Event deduplication table ensuring exactly-once metric projection."""

    __tablename__ = "processed_analytics_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class OrderAnalyticsSnapshot(AnalyticsBase):
    """Snapshot of order financials and line items captured from order.created."""

    __tablename__ = "order_analytics_snapshots"

    order_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    subtotal_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    discount_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    shipping_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    items_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)


class DailyPlatformMetric(AnalyticsBase):
    """Daily aggregated e-commerce KPIs updated asynchronously via RabbitMQ events."""

    __tablename__ = "daily_platform_metrics"

    metric_date: Mapped[str] = mapped_column(String(16), primary_key=True)
    registered_users: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    orders_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    orders_paid: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    orders_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    orders_cancelled: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    shipments_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    shipments_delivered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tickets_opened: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    gross_revenue: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    discounts_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    shipping_revenue: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    refunds_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    net_revenue: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


class ProductAnalytics(AnalyticsBase):
    """Per-product sales and review projection."""

    __tablename__ = "product_analytics"

    product_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_name: Mapped[str] = mapped_column(String(200), default="Unknown Product", nullable=False)
    units_sold: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    gross_revenue: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    average_rating: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    review_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class CustomerLtv(AnalyticsBase):
    """Customer lifetime value and automated loyalty tier progression state."""

    __tablename__ = "customer_ltv"

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    orders_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lifetime_spend: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    refunded_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    net_spend: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    computed_tier: Mapped[str] = mapped_column(String(24), default="standard", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
