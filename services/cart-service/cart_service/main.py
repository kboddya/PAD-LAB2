"""FastAPI application entrypoint for cart-service."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, FastAPI, Request, status
from sqlalchemy.orm import Session

from cart_service.config import CartSettings
from cart_service.models import CartBase
from cart_service.schemas import (
    CartCheckoutRequest,
    CartCouponRequest,
    CartItemAddRequest,
    CartItemUpdateRequest,
    CartShippingMethodRequest,
    CartViewResponse,
)
from cart_service.service import CartDomainService
from shared.app_factory import build_service_app
from shared.database import DatabaseManager
from shared.messaging import EventBus, EventEnvelope, create_event_bus
from shared.security import UserContext, extract_user_context


def create_app(
    settings: CartSettings | None = None,
    event_bus: EventBus | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Create and configure the cart-service FastAPI application."""
    cfg = settings or CartSettings()
    db = DatabaseManager(cfg.database_url, CartBase)
    bus = event_bus or create_event_bus(cfg.rabbitmq_url, cfg.service_name)
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=cfg.downstream_timeout_seconds)

    async def _shutdown_client() -> None:
        if owns_client:
            await client.aclose()

    app, logger = build_service_app(
        title="ShopFlow Cart Service",
        description="Persistent shopping cart with live product sync, coupon preview, and order checkout.",
        settings=cfg,
        db=db,
        event_bus=bus,
        on_shutdown=_shutdown_client,
    )
    domain = CartDomainService(cfg, logger)

    def _on_payment_succeeded(envelope: EventEnvelope) -> None:
        user_id = str(envelope.payload.get("user_id", ""))
        if not user_id:
            return
        with db.session_scope() as session:
            cleared = domain.clear_cart_for_event(session, event_id=envelope.event_id, user_id=user_id)
        if cleared:
            logger.info("Cleared cart after payment.succeeded", extra={"user_id": user_id})

    def _on_product_deactivated(envelope: EventEnvelope) -> None:
        product_id = str(envelope.payload.get("product_id", ""))
        if not product_id:
            return
        with db.session_scope() as session:
            removed = domain.purge_product_from_carts(session, product_id)
        if removed:
            logger.info("Removed deactivated product from carts", extra={"product_id": product_id, "removed": removed})

    bus.on("payment.succeeded", _on_payment_succeeded)
    bus.on("product.deactivated", _on_product_deactivated)

    def _get_caller(request: Request) -> UserContext:
        return extract_user_context(
            request,
            jwt_secret=cfg.jwt_secret,
            jwt_algorithm=cfg.jwt_algorithm,
            internal_service_token=cfg.internal_service_token,
        )

    router = APIRouter(prefix="/cart", tags=["Cart"])

    @router.get("", response_model=CartViewResponse, summary="Get live-synced shopping cart")
    async def get_cart(
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> CartViewResponse:
        caller = _get_caller(request)
        return await domain.get_enriched_cart(session, client, user_id=caller.user_id, role=caller.role)

    @router.post(
        "/items",
        response_model=CartViewResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Add item to shopping cart",
    )
    async def add_cart_item(
        payload: CartItemAddRequest,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> CartViewResponse:
        caller = _get_caller(request)
        return await domain.add_or_update_item(
            session,
            client,
            user_id=caller.user_id,
            role=caller.role,
            product_id=payload.product_id,
            quantity=payload.quantity,
            increment=True,
        )

    @router.patch("/items/{product_id}", response_model=CartViewResponse, summary="Set item quantity in cart")
    async def update_cart_item(
        product_id: str,
        payload: CartItemUpdateRequest,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> CartViewResponse:
        caller = _get_caller(request)
        return await domain.add_or_update_item(
            session,
            client,
            user_id=caller.user_id,
            role=caller.role,
            product_id=product_id,
            quantity=payload.quantity,
            increment=False,
        )

    @router.delete("/items/{product_id}", response_model=CartViewResponse, summary="Remove item from cart")
    async def remove_cart_item(
        product_id: str,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> CartViewResponse:
        caller = _get_caller(request)
        return await domain.remove_item(
            session,
            client,
            user_id=caller.user_id,
            role=caller.role,
            product_id=product_id,
        )

    @router.delete("", response_model=CartViewResponse, summary="Clear all items from cart")
    async def clear_cart(
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> CartViewResponse:
        caller = _get_caller(request)
        domain.clear_cart(session, caller.user_id)
        return await domain.get_enriched_cart(session, client, user_id=caller.user_id, role=caller.role)

    @router.post("/coupon", response_model=CartViewResponse, summary="Apply and validate a coupon code on cart")
    async def apply_coupon(
        payload: CartCouponRequest,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> CartViewResponse:
        caller = _get_caller(request)
        return await domain.apply_coupon(
            session,
            client,
            user_id=caller.user_id,
            role=caller.role,
            code=payload.code,
        )

    @router.delete("/coupon", response_model=CartViewResponse, summary="Remove coupon code from cart")
    async def remove_coupon(
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> CartViewResponse:
        caller = _get_caller(request)
        return await domain.remove_coupon(session, client, user_id=caller.user_id, role=caller.role)

    @router.patch("/shipping-method", response_model=CartViewResponse, summary="Persist preferred shipping method")
    async def set_shipping_method(
        payload: CartShippingMethodRequest,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> CartViewResponse:
        caller = _get_caller(request)
        return await domain.set_shipping_method(
            session,
            client,
            user_id=caller.user_id,
            role=caller.role,
            shipping_method=payload.shipping_method,
        )

    @router.post(
        "/checkout",
        status_code=status.HTTP_201_CREATED,
        summary="Convert cart items and coupon into an order",
    )
    async def checkout_cart(
        payload: CartCheckoutRequest,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> dict[str, Any]:
        caller = _get_caller(request)
        return await domain.checkout(
            session,
            client,
            user_id=caller.user_id,
            role=caller.role,
            payload=payload,
        )

    app.include_router(router)
    return app
