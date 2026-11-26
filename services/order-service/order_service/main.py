"""FastAPI application entrypoint for order-service."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, FastAPI, Query, Request, status
from sqlalchemy.orm import Session

from order_service.config import OrderSettings
from order_service.models import OrderBase
from order_service.schemas import OrderCreateRequest, OrderResponse, OrderStatusUpdateRequest
from order_service.service import OrderDomainService
from shared.app_factory import build_service_app
from shared.database import DatabaseManager
from shared.errors import ForbiddenError
from shared.messaging import EventBus, EventEnvelope, create_event_bus
from shared.security import UserContext, extract_user_context, require_roles


def create_app(
    settings: OrderSettings | None = None,
    event_bus: EventBus | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Create and configure the order-service FastAPI application."""
    cfg = settings or OrderSettings()
    db = DatabaseManager(cfg.database_url, OrderBase)
    bus = event_bus or create_event_bus(cfg.rabbitmq_url, cfg.service_name)
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=cfg.downstream_timeout_seconds)

    async def _shutdown_client() -> None:
        if owns_client:
            await client.aclose()

    app, logger = build_service_app(
        title="ShopFlow Order Service",
        description="Order lifecycle management, inventory reservation orchestration, and Saga handlers.",
        settings=cfg,
        db=db,
        event_bus=bus,
        on_shutdown=_shutdown_client,
    )
    domain = OrderDomainService(cfg, logger)

    def _on_payment_succeeded(envelope: EventEnvelope) -> None:
        data = envelope.payload
        order_id = str(data.get("order_id", ""))
        if not order_id:
            return
        with db.session_scope() as session:
            domain.apply_payment_result(
                session,
                order_id=order_id,
                payment_id=str(data.get("payment_id", "")),
                status="paid",
                failure_reason=None,
            )
        logger.info("Order marked as paid via event", extra={"order_id": order_id})

    def _on_payment_failed(envelope: EventEnvelope) -> None:
        data = envelope.payload
        order_id = str(data.get("order_id", ""))
        if not order_id:
            return
        with db.session_scope() as session:
            domain.apply_payment_result(
                session,
                order_id=order_id,
                payment_id=str(data.get("payment_id", "")),
                status="payment_failed",
                failure_reason=str(data.get("reason", "Payment failed")),
            )
        logger.warning("Order marked as payment_failed via event", extra={"order_id": order_id})

    bus.on("payment.succeeded", _on_payment_succeeded)
    bus.on("payment.failed", _on_payment_failed)

    def _on_shipment_dispatched(envelope: EventEnvelope) -> None:
        order_id = str(envelope.payload.get("order_id", ""))
        if not order_id:
            return
        with db.session_scope() as session:
            domain.apply_shipment_status(session, order_id=order_id, new_status="shipped")
        logger.info("Order marked as shipped via shipment event", extra={"order_id": order_id})

    def _on_shipment_delivered(envelope: EventEnvelope) -> None:
        order_id = str(envelope.payload.get("order_id", ""))
        if not order_id:
            return
        with db.session_scope() as session:
            domain.apply_shipment_status(session, order_id=order_id, new_status="delivered")
        logger.info("Order marked as delivered via shipment event", extra={"order_id": order_id})

    def _on_payment_refunded(envelope: EventEnvelope) -> None:
        order_id = str(envelope.payload.get("order_id", ""))
        if not order_id:
            return
        with db.session_scope() as session:
            domain.apply_refund_status(session, order_id=order_id)
        logger.info("Order marked as refunded via payment event", extra={"order_id": order_id})

    bus.on("shipment.dispatched", _on_shipment_dispatched)
    bus.on("shipment.delivered", _on_shipment_delivered)
    bus.on("payment.refunded", _on_payment_refunded)

    def _get_caller(request: Request) -> UserContext:
        return extract_user_context(
            request,
            jwt_secret=cfg.jwt_secret,
            jwt_algorithm=cfg.jwt_algorithm,
            internal_service_token=cfg.internal_service_token,
        )

    router = APIRouter(prefix="/orders", tags=["Orders"])

    @router.post(
        "",
        response_model=OrderResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Create order, reserve stock, and initiate payment",
    )
    async def create_order(
        payload: OrderCreateRequest,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> OrderResponse:
        caller = _get_caller(request)
        headers = domain._service_headers(caller.user_id, caller.role)

        # 1. Verify buyer profile exists in user-service
        user_profile = await domain.verify_user_exists(client, caller.user_id, caller.role)
        loyalty_tier = str(user_profile.get("loyalty_tier", "standard"))

        # 2. Reserve stock in product-service (rolls back on partial failure)
        order_id = domain.new_order_id()
        reserved_items = await domain.reserve_items(client, order_id, payload, headers)
        subtotal = round(sum(item["quantity"] * item["unit_price"] for item in reserved_items), 2)
        categories = sorted({str(item.get("category", "")) for item in reserved_items if item.get("category")})
        total_units = sum(int(item["quantity"]) for item in reserved_items)

        # 3. Optionally redeem promotional coupon & calculate shipping quote
        discount_amount = 0.0
        applied_coupon: str | None = None
        if payload.coupon_code:
            try:
                redeemed = await domain.redeem_coupon(
                    client,
                    order_id=order_id,
                    user_id=caller.user_id,
                    coupon_code=payload.coupon_code,
                    subtotal=subtotal,
                    loyalty_tier=loyalty_tier,
                    categories=categories,
                    headers=headers,
                )
                discount_amount = float(redeemed.get("discount_amount", 0.0))
                applied_coupon = str(redeemed.get("code", payload.coupon_code.strip().upper()))
            except Exception:
                await domain.release_items_best_effort(client, order_id, reserved_items, headers)
                raise

        shipping_cost = 0.0
        effective_shipping_method = payload.shipping_method or "standard"
        if payload.shipping_method is not None:
            try:
                shipping_cost = await domain.quote_shipping(
                    client,
                    shipping_method=payload.shipping_method,
                    items_count=total_units,
                    subtotal=subtotal,
                    loyalty_tier=loyalty_tier,
                    headers=headers,
                )
            except Exception:
                await domain.release_items_best_effort(client, order_id, reserved_items, headers)
                if applied_coupon:
                    await domain.release_coupon_best_effort(client, order_id, headers)
                raise

        # 4. Persist pending order and emit order.created
        order = domain.create_order_record(
            session,
            order_id=order_id,
            user_id=caller.user_id,
            payload=payload,
            reserved_items=reserved_items,
            subtotal_amount=subtotal,
            discount_amount=discount_amount,
            shipping_cost=shipping_cost,
            coupon_code=applied_coupon,
            shipping_method=effective_shipping_method,
        )
        await bus.publish(
            event_type="order.created",
            payload={
                "order_id": order.id,
                "user_id": order.user_id,
                "subtotal_amount": order.subtotal_amount,
                "discount_amount": order.discount_amount,
                "shipping_cost": order.shipping_cost,
                "total_amount": order.total_amount,
                "currency": order.currency,
                "coupon_code": order.coupon_code,
                "shipping_method": order.shipping_method,
                "shipping_address": order.shipping_address,
                "items": reserved_items,
            },
            source_service=cfg.service_name,
        )

        # 5. Initiate payment; compensate inventory and coupon if payment-service is unavailable
        try:
            payment_data = await domain.initiate_payment(client, order, payload, headers)
        except Exception as exc:
            await domain.release_items_best_effort(client, order.id, reserved_items, headers)
            if applied_coupon:
                await domain.release_coupon_best_effort(client, order.id, headers)
            domain.apply_payment_result(
                session,
                order_id=order.id,
                payment_id=None,
                status="payment_failed",
                failure_reason=str(exc),
            )
            raise

        next_status = "paid" if payment_data.get("status") == "succeeded" else "payment_failed"
        updated_order = domain.apply_payment_result(
            session,
            order_id=order.id,
            payment_id=str(payment_data.get("id", "")),
            status=next_status,
            failure_reason=payment_data.get("failure_reason"),
        )
        return OrderResponse.model_validate(updated_order or order)

    @router.get("", response_model=list[OrderResponse], summary="List orders for current user or all (admin)")
    async def list_orders(
        request: Request,
        status_filter: str | None = Query(default=None, alias="status"),
        user_id: str | None = Query(default=None),
        skip: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
        session: Session = Depends(db.get_db),
    ) -> list[OrderResponse]:
        caller = _get_caller(request)
        if user_id and caller.role != "admin" and user_id != caller.user_id:
            raise ForbiddenError("Cannot view other users' orders")
        effective_user = user_id if caller.role == "admin" else caller.user_id
        orders = domain.list_orders(
            session,
            user_id=effective_user,
            status=status_filter,
            skip=skip,
            limit=limit,
        )
        return [OrderResponse.model_validate(o) for o in orders]

    @router.get("/{order_id}", response_model=OrderResponse, summary="Get order details by ID")
    async def get_order(
        order_id: str,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> OrderResponse:
        caller = _get_caller(request)
        order = domain.get_order(session, order_id)
        if caller.role != "admin" and order.user_id != caller.user_id:
            raise ForbiddenError("Access denied to another user's order")
        return OrderResponse.model_validate(order)

    @router.post("/{order_id}/cancel", response_model=OrderResponse, summary="Cancel order and trigger stock release")
    async def cancel_order(
        order_id: str,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> OrderResponse:
        caller = _get_caller(request)
        order = domain.cancel_order(
            session,
            order_id,
            actor_id=caller.user_id,
            actor_role=caller.role,
        )
        await bus.publish(
            event_type="order.cancelled",
            payload={
                "order_id": order.id,
                "user_id": order.user_id,
                "total_amount": order.total_amount,
                "currency": order.currency,
            },
            source_service=cfg.service_name,
        )
        logger.info("Order cancelled", extra={"order_id": order.id})
        return OrderResponse.model_validate(order)

    @router.patch("/{order_id}/status", response_model=OrderResponse, summary="Advance order fulfillment status (admin)")
    async def update_status(
        order_id: str,
        payload: OrderStatusUpdateRequest,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> OrderResponse:
        caller = _get_caller(request)
        require_roles(caller, {"admin"})
        order = domain.update_order_status(session, order_id, payload.status)
        return OrderResponse.model_validate(order)

    app.include_router(router)
    return app
