"""Pydantic response schemas for analytics-service CQRS projections."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DailyMetricResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    metric_date: str
    registered_users: int
    orders_created: int
    orders_paid: int
    orders_failed: int
    orders_cancelled: int
    shipments_created: int
    shipments_delivered: int
    tickets_opened: int
    gross_revenue: float
    discounts_total: float
    shipping_revenue: float
    refunds_total: float
    net_revenue: float


class PlatformOverviewResponse(BaseModel):
    registered_users: int
    orders_created: int
    orders_paid: int
    orders_failed: int
    orders_cancelled: int
    shipments_created: int
    shipments_delivered: int
    tickets_opened: int
    gross_revenue: float
    discounts_total: float
    shipping_revenue: float
    refunds_total: float
    net_revenue: float
    average_order_value: float
    payment_success_rate_pct: float
    daily_metrics: list[DailyMetricResponse] = Field(default_factory=list)


class ProductAnalyticsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    product_id: str
    product_name: str
    units_sold: int
    gross_revenue: float
    average_rating: float
    review_count: int
    trending_score: float = 0.0
    updated_at: datetime


class CustomerSummaryResponse(BaseModel):
    user_id: str
    orders_count: int
    lifetime_spend: float
    refunded_amount: float
    net_spend: float
    computed_tier: str
    next_tier: str | None
    amount_to_next_tier: float


class ConversionFunnelResponse(BaseModel):
    registered_users: int
    orders_created: int
    orders_paid: int
    shipments_delivered: int
    registration_to_order_pct: float
    order_to_paid_pct: float
    paid_to_delivered_pct: float
