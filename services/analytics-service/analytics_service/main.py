"""FastAPI application entrypoint for analytics-service."""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from sqlalchemy.orm import Session

from analytics_service.config import AnalyticsSettings
from analytics_service.models import AnalyticsBase
from analytics_service.schemas import (
    ConversionFunnelResponse,
    CustomerSummaryResponse,
    PlatformOverviewResponse,
    ProductAnalyticsResponse,
)
from analytics_service.service import AnalyticsDomainService
from shared.app_factory import build_service_app
from shared.database import DatabaseManager
from shared.messaging import EventBus, EventEnvelope, create_event_bus
from shared.security import UserContext, extract_user_context, require_roles

_SUBSCRIBED_EVENTS: tuple[str, ...] = (
    "user.registered",
    "order.created",
    "order.cancelled",
    "payment.succeeded",
    "payment.failed",
    "payment.refunded",
    "shipment.created",
    "shipment.delivered",
    "review.published",
    "review.updated",
    "review.deleted",
    "ticket.created",
)


def create_app(
    settings: AnalyticsSettings | None = None,
    event_bus: EventBus | None = None,
) -> FastAPI:
    """Create and configure the analytics-service FastAPI application."""
    cfg = settings or AnalyticsSettings()
    db = DatabaseManager(cfg.database_url, AnalyticsBase)
    bus = event_bus or create_event_bus(cfg.rabbitmq_url, cfg.service_name)
    domain = AnalyticsDomainService(cfg)

    app, logger = build_service_app(
        title="ShopFlow Analytics Service",
        description="CQRS event-driven analytics projections, KPIs, and automated loyalty progression.",
        settings=cfg,
        db=db,
        event_bus=bus,
    )

    async def _handle_analytics_event(envelope: EventEnvelope) -> None:
        with db.session_scope() as session:
            upgrade = domain.ingest_event(
                session,
                event_id=envelope.event_id,
                event_type=envelope.event_type,
                timestamp_iso=envelope.timestamp,
                payload=envelope.payload,
            )
        if upgrade is not None:
            await bus.publish(
                event_type="loyalty.upgraded",
                payload=upgrade,
                source_service=cfg.service_name,
                request_id=envelope.request_id,
            )
            logger.info(
                "Published loyalty.upgraded event",
                extra={"user_id": upgrade["user_id"], "new_tier": upgrade["new_tier"]},
            )

    for routing_key in _SUBSCRIBED_EVENTS:
        bus.on(routing_key, _handle_analytics_event)

    def _get_caller(request: Request) -> UserContext:
        return extract_user_context(
            request,
            jwt_secret=cfg.jwt_secret,
            jwt_algorithm=cfg.jwt_algorithm,
            internal_service_token=cfg.internal_service_token,
        )

    router = APIRouter(prefix="/analytics", tags=["Analytics"])

    @router.get("/trending", response_model=list[ProductAnalyticsResponse], summary="Get trending products (public)")
    async def get_trending(
        limit: int = Query(default=10, ge=1, le=50),
        session: Session = Depends(db.get_db),
    ) -> list[ProductAnalyticsResponse]:
        return domain.get_top_products(session, limit=limit, trending_mode=True)

    @router.get("/me/summary", response_model=CustomerSummaryResponse, summary="Get caller LTV & loyalty progression")
    async def get_my_summary(
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> CustomerSummaryResponse:
        caller = _get_caller(request)
        return domain.get_customer_summary(session, caller.user_id)

    @router.get("/overview", response_model=PlatformOverviewResponse, summary="Platform KPI overview (seller/admin)")
    async def get_overview(
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> PlatformOverviewResponse:
        caller = _get_caller(request)
        require_roles(caller, {"seller", "admin"})
        return domain.get_overview(session)

    @router.get(
        "/products/top",
        response_model=list[ProductAnalyticsResponse],
        summary="Top revenue-generating products (seller/admin)",
    )
    async def get_top_products(
        request: Request,
        limit: int = Query(default=10, ge=1, le=50),
        session: Session = Depends(db.get_db),
    ) -> list[ProductAnalyticsResponse]:
        caller = _get_caller(request)
        require_roles(caller, {"seller", "admin"})
        return domain.get_top_products(session, limit=limit, trending_mode=False)

    @router.get("/funnel", response_model=ConversionFunnelResponse, summary="Conversion funnel metrics (seller/admin)")
    async def get_funnel(
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> ConversionFunnelResponse:
        caller = _get_caller(request)
        require_roles(caller, {"seller", "admin"})
        return domain.get_funnel(session)

    app.include_router(router)
    return app
