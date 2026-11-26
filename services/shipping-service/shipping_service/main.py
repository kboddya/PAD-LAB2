"""FastAPI application entrypoint for shipping-service."""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from sqlalchemy.orm import Session

from shared.app_factory import build_service_app
from shared.database import DatabaseManager
from shared.errors import ForbiddenError
from shared.messaging import EventBus, EventEnvelope, create_event_bus
from shared.security import (
    UserContext,
    extract_user_context,
    is_valid_internal_token,
    require_roles,
)
from shipping_service.config import ShippingSettings
from shipping_service.models import ShippingBase
from shipping_service.schemas import (
    PublicTrackingResponse,
    ShipmentResponse,
    ShipmentStatusUpdateRequest,
    ShippingQuoteRequest,
    ShippingQuoteResponse,
)
from shipping_service.service import ShippingDomainService


def create_app(
    settings: ShippingSettings | None = None,
    event_bus: EventBus | None = None,
) -> FastAPI:
    """Create and configure the shipping-service FastAPI application."""
    cfg = settings or ShippingSettings()
    db = DatabaseManager(cfg.database_url, ShippingBase)
    bus = event_bus or create_event_bus(cfg.rabbitmq_url, cfg.service_name)
    domain = ShippingDomainService()

    app, logger = build_service_app(
        title="ShopFlow Shipping Service",
        description="Tariff calculation, automated shipment provisioning, and tracking checkpoints.",
        settings=cfg,
        db=db,
        event_bus=bus,
    )

    def _on_order_created(envelope: EventEnvelope) -> None:
        data = envelope.payload
        order_id = str(data.get("order_id", ""))
        user_id = str(data.get("user_id", ""))
        if not order_id or not user_id:
            return
        with db.session_scope() as session:
            domain.record_order_intent(
                session,
                order_id=order_id,
                user_id=user_id,
                shipping_address=str(data.get("shipping_address") or "Address on file"),
                shipping_method=str(data.get("shipping_method") or "standard"),
                shipping_cost=float(data.get("shipping_cost") or 0.0),
            )

    async def _on_payment_succeeded(envelope: EventEnvelope) -> None:
        data = envelope.payload
        order_id = str(data.get("order_id", ""))
        user_id = str(data.get("user_id", ""))
        if not order_id or not user_id:
            return
        with db.session_scope() as session:
            shipment, is_new = domain.create_shipment_idempotent(
                session,
                order_id=order_id,
                user_id=user_id,
            )
        if is_new:
            await bus.publish(
                event_type="shipment.created",
                payload={
                    "shipment_id": shipment.id,
                    "order_id": shipment.order_id,
                    "user_id": shipment.user_id,
                    "tracking_number": shipment.tracking_number,
                    "shipping_method": shipment.shipping_method,
                    "carrier": shipment.carrier,
                },
                source_service=cfg.service_name,
                request_id=envelope.request_id,
            )
            logger.info(
                "Created shipment after payment.succeeded",
                extra={"order_id": order_id, "tracking_number": shipment.tracking_number},
            )

    def _on_order_cancelled(envelope: EventEnvelope) -> None:
        order_id = str(envelope.payload.get("order_id", ""))
        if not order_id:
            return
        with db.session_scope() as session:
            cancelled = domain.cancel_for_order_if_preparing(session, order_id)
        if cancelled is not None:
            logger.info("Cancelled shipment for cancelled order", extra={"order_id": order_id})

    bus.on("order.created", _on_order_created)
    bus.on("payment.succeeded", _on_payment_succeeded)
    bus.on("order.cancelled", _on_order_cancelled)

    def _get_caller(request: Request) -> UserContext:
        return extract_user_context(
            request,
            jwt_secret=cfg.jwt_secret,
            jwt_algorithm=cfg.jwt_algorithm,
            internal_service_token=cfg.internal_service_token,
        )

    shipping_router = APIRouter(prefix="/shipping", tags=["Shipping"])
    shipments_router = APIRouter(prefix="/shipments", tags=["Shipments"])

    @shipping_router.post("/quotes", response_model=ShippingQuoteResponse, summary="Calculate shipping quote")
    async def quote_shipping(payload: ShippingQuoteRequest) -> ShippingQuoteResponse:
        return domain.calculate_quote(payload)

    @shipments_router.get("", response_model=list[ShipmentResponse], summary="List shipments")
    async def list_shipments(
        request: Request,
        status_filter: str | None = Query(default=None, alias="status"),
        user_id: str | None = Query(default=None),
        skip: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
        session: Session = Depends(db.get_db),
    ) -> list[ShipmentResponse]:
        caller = _get_caller(request)
        if caller.role in {"seller", "admin"}:
            target_user = user_id
        else:
            if user_id and user_id != caller.user_id:
                raise ForbiddenError("Cannot view other users' shipments")
            target_user = caller.user_id
        rows = domain.list_shipments(
            session,
            user_id=target_user,
            status=status_filter,
            skip=skip,
            limit=limit,
        )
        return [ShipmentResponse.model_validate(s) for s in rows]

    @shipments_router.get(
        "/track/{tracking_number}",
        response_model=PublicTrackingResponse,
        summary="Public shipment tracking by tracking number",
    )
    async def track_shipment(
        tracking_number: str,
        session: Session = Depends(db.get_db),
    ) -> PublicTrackingResponse:
        shipment = domain.track_by_number(session, tracking_number)
        return PublicTrackingResponse.model_validate(shipment)

    @shipments_router.get(
        "/by-order/{order_id}",
        response_model=ShipmentResponse,
        summary="Get shipment by order ID",
    )
    async def get_shipment_by_order(
        order_id: str,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> ShipmentResponse:
        shipment = domain.get_by_order(session, order_id)
        if not is_valid_internal_token(request, cfg.internal_service_token):
            caller = _get_caller(request)
            if caller.role not in {"seller", "admin"} and shipment.user_id != caller.user_id:
                raise ForbiddenError("Access denied to another user's shipment")
        return ShipmentResponse.model_validate(shipment)

    @shipments_router.get("/{shipment_id}", response_model=ShipmentResponse, summary="Get shipment by ID")
    async def get_shipment(
        shipment_id: str,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> ShipmentResponse:
        shipment = domain.get_shipment(session, shipment_id)
        if not is_valid_internal_token(request, cfg.internal_service_token):
            caller = _get_caller(request)
            if caller.role not in {"seller", "admin"} and shipment.user_id != caller.user_id:
                raise ForbiddenError("Access denied to another user's shipment")
        return ShipmentResponse.model_validate(shipment)

    @shipments_router.post(
        "/{shipment_id}/status",
        response_model=ShipmentResponse,
        summary="Advance shipment status and record checkpoint (seller/admin)",
    )
    async def update_shipment_status(
        shipment_id: str,
        payload: ShipmentStatusUpdateRequest,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> ShipmentResponse:
        caller = _get_caller(request)
        require_roles(caller, {"seller", "admin"})
        shipment = domain.advance_status(session, shipment_id, payload)

        if shipment.status == "dispatched":
            await bus.publish(
                event_type="shipment.dispatched",
                payload={
                    "shipment_id": shipment.id,
                    "order_id": shipment.order_id,
                    "user_id": shipment.user_id,
                    "tracking_number": shipment.tracking_number,
                    "carrier": shipment.carrier,
                },
                source_service=cfg.service_name,
            )
        elif shipment.status == "delivered":
            await bus.publish(
                event_type="shipment.delivered",
                payload={
                    "shipment_id": shipment.id,
                    "order_id": shipment.order_id,
                    "user_id": shipment.user_id,
                    "tracking_number": shipment.tracking_number,
                    "carrier": shipment.carrier,
                },
                source_service=cfg.service_name,
            )

        logger.info("Updated shipment status", extra={"shipment_id": shipment.id, "status": shipment.status})
        return ShipmentResponse.model_validate(shipment)

    app.include_router(shipping_router)
    app.include_router(shipments_router)
    return app
