"""CQRS event projection and KPI aggregation logic for analytics-service."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analytics_service.config import AnalyticsSettings
from analytics_service.models import (
    CustomerLtv,
    DailyPlatformMetric,
    OrderAnalyticsSnapshot,
    ProcessedAnalyticsEvent,
    ProductAnalytics,
)
from analytics_service.schemas import (
    ConversionFunnelResponse,
    CustomerSummaryResponse,
    DailyMetricResponse,
    PlatformOverviewResponse,
    ProductAnalyticsResponse,
)

_TIER_RANKS = {"standard": 0, "silver": 1, "gold": 2}


class AnalyticsDomainService:
    """Maintains real-time CQRS projections from domain events and computes loyalty upgrades."""

    def __init__(self, settings: AnalyticsSettings) -> None:
        self.settings = settings

    def _get_or_create_daily(self, session: Session, metric_date: str) -> DailyPlatformMetric:
        row = session.get(DailyPlatformMetric, metric_date)
        if row is None:
            row = DailyPlatformMetric(metric_date=metric_date)
            session.add(row)
            session.flush()
        return row

    def _get_or_create_ltv(self, session: Session, user_id: str) -> CustomerLtv:
        row = session.get(CustomerLtv, user_id)
        if row is None:
            row = CustomerLtv(user_id=user_id, computed_tier="standard")
            session.add(row)
            session.flush()
        return row

    def _get_or_create_product(self, session: Session, product_id: str, product_name: str | None = None) -> ProductAnalytics:
        row = session.get(ProductAnalytics, product_id)
        if row is None:
            row = ProductAnalytics(
                product_id=product_id,
                product_name=product_name or "Unknown Product",
            )
            session.add(row)
            session.flush()
        elif product_name and row.product_name == "Unknown Product":
            row.product_name = product_name
        return row

    def _resolve_tier_for_spend(self, net_spend: float) -> str:
        if net_spend >= self.settings.gold_tier_threshold:
            return "gold"
        if net_spend >= self.settings.silver_tier_threshold:
            return "silver"
        return "standard"

    def ingest_event(
        self,
        session: Session,
        *,
        event_id: str,
        event_type: str,
        timestamp_iso: str | None,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Idempotently project a domain event and optionally return a loyalty_upgrade payload."""
        if session.get(ProcessedAnalyticsEvent, event_id) is not None:
            return None

        session.add(ProcessedAnalyticsEvent(event_id=event_id, event_type=event_type))
        metric_date = (timestamp_iso or datetime.now(timezone.utc).isoformat())[:10]
        daily = self._get_or_create_daily(session, metric_date)
        loyalty_upgrade: dict[str, Any] | None = None

        if event_type == "user.registered":
            daily.registered_users += 1
            user_id = str(payload.get("user_id", ""))
            if user_id:
                self._get_or_create_ltv(session, user_id)

        elif event_type == "order.created":
            daily.orders_created += 1
            order_id = str(payload.get("order_id", ""))
            user_id = str(payload.get("user_id", ""))
            if order_id and user_id and session.get(OrderAnalyticsSnapshot, order_id) is None:
                session.add(
                    OrderAnalyticsSnapshot(
                        order_id=order_id,
                        user_id=user_id,
                        subtotal_amount=float(payload.get("subtotal_amount") or payload.get("total_amount") or 0.0),
                        discount_amount=float(payload.get("discount_amount") or 0.0),
                        shipping_cost=float(payload.get("shipping_cost") or 0.0),
                        total_amount=float(payload.get("total_amount") or 0.0),
                        items_json=json.dumps(payload.get("items", [])),
                    )
                )

        elif event_type == "order.cancelled":
            daily.orders_cancelled += 1

        elif event_type == "payment.succeeded":
            daily.orders_paid += 1
            amount = round(float(payload.get("amount", 0.0)), 2)
            order_id = str(payload.get("order_id", ""))
            user_id = str(payload.get("user_id", ""))

            snap = session.get(OrderAnalyticsSnapshot, order_id) if order_id else None
            discount_amt = snap.discount_amount if snap else 0.0
            ship_amt = snap.shipping_cost if snap else 0.0

            daily.gross_revenue = round(daily.gross_revenue + amount, 2)
            daily.discounts_total = round(daily.discounts_total + discount_amt, 2)
            daily.shipping_revenue = round(daily.shipping_revenue + ship_amt, 2)
            daily.net_revenue = round(daily.net_revenue + amount, 2)

            if snap and snap.items_json:
                try:
                    items = json.loads(snap.items_json)
                except json.JSONDecodeError:
                    items = []
                for item in items:
                    pid = str(item.get("product_id", ""))
                    if not pid:
                        continue
                    qty = int(item.get("quantity", 0))
                    unit_price = float(item.get("unit_price", 0.0))
                    prod = self._get_or_create_product(session, pid, item.get("product_name"))
                    prod.units_sold += qty
                    prod.gross_revenue = round(prod.gross_revenue + (qty * unit_price), 2)
                    prod.updated_at = datetime.now(timezone.utc)

            if user_id:
                ltv = self._get_or_create_ltv(session, user_id)
                old_tier = ltv.computed_tier
                ltv.orders_count += 1
                ltv.lifetime_spend = round(ltv.lifetime_spend + amount, 2)
                ltv.net_spend = round(max(0.0, ltv.lifetime_spend - ltv.refunded_amount), 2)
                candidate_tier = self._resolve_tier_for_spend(ltv.net_spend)
                if _TIER_RANKS[candidate_tier] > _TIER_RANKS.get(old_tier, 0):
                    ltv.computed_tier = candidate_tier
                    loyalty_upgrade = {
                        "user_id": user_id,
                        "old_tier": old_tier,
                        "new_tier": candidate_tier,
                        "net_spend": ltv.net_spend,
                    }
                ltv.updated_at = datetime.now(timezone.utc)

        elif event_type == "payment.failed":
            daily.orders_failed += 1

        elif event_type == "payment.refunded":
            amount = round(float(payload.get("amount", 0.0)), 2)
            user_id = str(payload.get("user_id", ""))
            daily.refunds_total = round(daily.refunds_total + amount, 2)
            daily.net_revenue = round(max(0.0, daily.net_revenue - amount), 2)
            if user_id:
                ltv = self._get_or_create_ltv(session, user_id)
                ltv.refunded_amount = round(ltv.refunded_amount + amount, 2)
                ltv.net_spend = round(max(0.0, ltv.lifetime_spend - ltv.refunded_amount), 2)
                ltv.updated_at = datetime.now(timezone.utc)

        elif event_type == "shipment.created":
            daily.shipments_created += 1

        elif event_type == "shipment.delivered":
            daily.shipments_delivered += 1

        elif event_type in {"review.published", "review.updated", "review.deleted"}:
            pid = str(payload.get("product_id", ""))
            if pid:
                prod = self._get_or_create_product(session, pid)
                prod.average_rating = round(float(payload.get("average_rating", 0.0)), 2)
                prod.review_count = max(0, int(payload.get("review_count", 0)))
                prod.updated_at = datetime.now(timezone.utc)

        elif event_type == "ticket.created":
            daily.tickets_opened += 1

        session.commit()
        return loyalty_upgrade

    def get_overview(self, session: Session) -> PlatformOverviewResponse:
        daily_rows = list(session.scalars(select(DailyPlatformMetric).order_by(DailyPlatformMetric.metric_date.desc())).all())
        reg = sum(r.registered_users for r in daily_rows)
        created = sum(r.orders_created for r in daily_rows)
        paid = sum(r.orders_paid for r in daily_rows)
        failed = sum(r.orders_failed for r in daily_rows)
        cancelled = sum(r.orders_cancelled for r in daily_rows)
        ship_created = sum(r.shipments_created for r in daily_rows)
        ship_deliv = sum(r.shipments_delivered for r in daily_rows)
        tickets = sum(r.tickets_opened for r in daily_rows)
        gross = round(sum(r.gross_revenue for r in daily_rows), 2)
        discounts = round(sum(r.discounts_total for r in daily_rows), 2)
        shipping = round(sum(r.shipping_revenue for r in daily_rows), 2)
        refunds = round(sum(r.refunds_total for r in daily_rows), 2)
        net = round(sum(r.net_revenue for r in daily_rows), 2)

        aov = round(gross / paid, 2) if paid > 0 else 0.0
        attempts = paid + failed
        pay_rate = round((paid / attempts) * 100.0, 2) if attempts > 0 else 0.0

        return PlatformOverviewResponse(
            registered_users=reg,
            orders_created=created,
            orders_paid=paid,
            orders_failed=failed,
            orders_cancelled=cancelled,
            shipments_created=ship_created,
            shipments_delivered=ship_deliv,
            tickets_opened=tickets,
            gross_revenue=gross,
            discounts_total=discounts,
            shipping_revenue=shipping,
            refunds_total=refunds,
            net_revenue=net,
            average_order_value=aov,
            payment_success_rate_pct=pay_rate,
            daily_metrics=[DailyMetricResponse.model_validate(r) for r in daily_rows],
        )

    def get_top_products(self, session: Session, *, limit: int = 10, trending_mode: bool = False) -> list[ProductAnalyticsResponse]:
        rows = list(session.scalars(select(ProductAnalytics)).all())
        enriched: list[ProductAnalyticsResponse] = []
        for row in rows:
            score = round(row.units_sold * 2.0 + row.average_rating * row.review_count, 2)
            resp = ProductAnalyticsResponse.model_validate(row)
            resp.trending_score = score
            enriched.append(resp)

        if trending_mode:
            enriched.sort(key=lambda r: (r.trending_score, r.gross_revenue), reverse=True)
        else:
            enriched.sort(key=lambda r: (r.gross_revenue, r.units_sold), reverse=True)
        return enriched[:limit]

    def get_customer_summary(self, session: Session, user_id: str) -> CustomerSummaryResponse:
        ltv = session.get(CustomerLtv, user_id)
        orders_count = ltv.orders_count if ltv else 0
        lifetime_spend = ltv.lifetime_spend if ltv else 0.0
        refunded = ltv.refunded_amount if ltv else 0.0
        net_spend = ltv.net_spend if ltv else 0.0
        tier = ltv.computed_tier if ltv else "standard"

        if tier == "standard":
            next_tier: str | None = "silver"
            remaining = round(max(0.0, self.settings.silver_tier_threshold - net_spend), 2)
        elif tier == "silver":
            next_tier = "gold"
            remaining = round(max(0.0, self.settings.gold_tier_threshold - net_spend), 2)
        else:
            next_tier = None
            remaining = 0.0

        return CustomerSummaryResponse(
            user_id=user_id,
            orders_count=orders_count,
            lifetime_spend=lifetime_spend,
            refunded_amount=refunded,
            net_spend=net_spend,
            computed_tier=tier,
            next_tier=next_tier,
            amount_to_next_tier=remaining,
        )

    def get_funnel(self, session: Session) -> ConversionFunnelResponse:
        overview = self.get_overview(session)
        reg = overview.registered_users
        created = overview.orders_created
        paid = overview.orders_paid
        delivered = overview.shipments_delivered

        return ConversionFunnelResponse(
            registered_users=reg,
            orders_created=created,
            orders_paid=paid,
            shipments_delivered=delivered,
            registration_to_order_pct=round((created / reg) * 100.0, 2) if reg > 0 else 0.0,
            order_to_paid_pct=round((paid / created) * 100.0, 2) if created > 0 else 0.0,
            paid_to_delivered_pct=round((delivered / paid) * 100.0, 2) if paid > 0 else 0.0,
        )
