"""Domain logic for persistent cart synchronization, pricing validation, and checkout."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from cart_service.config import CartSettings
from cart_service.models import Cart, CartItem, ProcessedCartEvent
from cart_service.schemas import CartCheckoutRequest, CartItemView, CartViewResponse
from shared.errors import (
    BadRequestError,
    ConflictError,
    ExternalServiceError,
    NotFoundError,
    ServiceTimeoutError,
    ServiceUnavailableError,
)
from shared.logging import get_request_id


class CartDomainService:
    """Manages shopping cart items, live price/stock sync, coupons, and order checkout."""

    def __init__(self, settings: CartSettings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger

    def _service_headers(self, user_id: str, role: str) -> dict[str, str]:
        return {
            "X-Request-ID": get_request_id(),
            "X-User-Id": user_id,
            "X-User-Role": role,
            "X-Internal-Token": self.settings.internal_service_token,
        }

    def get_or_create_cart(self, session: Session, user_id: str) -> Cart:
        stmt = (
            select(Cart)
            .options(selectinload(Cart.items))
            .execution_options(populate_existing=True)
            .where(Cart.user_id == user_id)
        )
        cart = session.scalar(stmt)
        if cart is None:
            cart = Cart(user_id=user_id, shipping_method="standard")
            session.add(cart)
            session.commit()
            cart = session.scalar(stmt)
            assert cart is not None
        return cart

    async def _fetch_product(self, client: httpx.AsyncClient, product_id: str, headers: dict[str, str]) -> dict[str, Any]:
        url = f"{self.settings.product_service_url}/products/{product_id}"
        try:
            resp = await client.get(url, headers=headers)
        except httpx.TimeoutException as exc:
            raise ServiceTimeoutError("product-service timed out while syncing cart") from exc
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("product-service is unavailable") from exc

        if resp.status_code == 404:
            raise NotFoundError(f"Product '{product_id}' not found", error_code="PRODUCT_NOT_FOUND")
        if resp.status_code >= 500:
            raise ServiceUnavailableError("product-service returned an internal error")
        return resp.json()

    async def _fetch_product_soft(
        self,
        client: httpx.AsyncClient,
        product_id: str,
        headers: dict[str, str],
    ) -> tuple[str, dict[str, Any] | None]:
        """Fetch product details gracefully without failing GET /cart on transient outage."""
        url = f"{self.settings.product_service_url}/products/{product_id}"
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return "ok", resp.json()
            if resp.status_code == 404:
                return "missing", None
            return "degraded", None
        except httpx.HTTPError:
            return "degraded", None

    async def _fetch_loyalty_tier(self, client: httpx.AsyncClient, user_id: str, role: str) -> str:
        url = f"{self.settings.user_service_url}/users/{user_id}"
        try:
            resp = await client.get(url, headers=self._service_headers(user_id, role))
            if resp.status_code == 200:
                return str(resp.json().get("loyalty_tier", "standard"))
        except httpx.HTTPError:
            pass
        return "standard"

    async def _validate_coupon_remote(
        self,
        client: httpx.AsyncClient,
        *,
        code: str,
        user_id: str,
        role: str,
        subtotal: float,
        loyalty_tier: str,
        categories: list[str],
    ) -> dict[str, Any]:
        url = f"{self.settings.promotion_service_url}/promotions/validate"
        body = {
            "code": code,
            "order_subtotal": subtotal,
            "loyalty_tier": loyalty_tier,
            "categories": categories,
            "user_id": user_id,
        }
        try:
            resp = await client.post(url, json=body, headers=self._service_headers(user_id, role))
        except httpx.TimeoutException as exc:
            raise ServiceTimeoutError("promotion-service timed out") from exc
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("promotion-service is unavailable") from exc

        if resp.status_code >= 500:
            raise ServiceUnavailableError("promotion-service returned an internal error")
        return resp.json()

    async def add_or_update_item(
        self,
        session: Session,
        client: httpx.AsyncClient,
        *,
        user_id: str,
        role: str,
        product_id: str,
        quantity: int,
        increment: bool,
    ) -> CartViewResponse:
        headers = self._service_headers(user_id, role)
        cart = self.get_or_create_cart(session, user_id)
        existing_item = next((i for i in cart.items if i.product_id == product_id), None)

        if not increment and quantity == 0:
            if existing_item is not None:
                session.delete(existing_item)
                cart.updated_at = datetime.now(timezone.utc)
                session.commit()
            return await self.get_enriched_cart(session, client, user_id=user_id, role=role)

        prod = await self._fetch_product(client, product_id, headers)
        if not prod.get("is_active", True):
            raise BadRequestError(
                f"Product '{prod.get('name', product_id)}' is inactive",
                error_code="PRODUCT_INACTIVE",
            )

        desired_qty = (existing_item.quantity + quantity) if (increment and existing_item) else quantity
        available = int(prod.get("available_quantity", 0))
        if desired_qty > available:
            raise ConflictError(
                f"Insufficient stock for '{prod.get('name', product_id)}': available {available}, requested {desired_qty}",
                error_code="INSUFFICIENT_STOCK",
            )

        live_price = round(float(prod["price"]), 2)
        if existing_item is not None:
            existing_item.quantity = desired_qty
            existing_item.unit_price_snapshot = live_price
            existing_item.product_name = str(prod["name"])
            existing_item.sku = str(prod["sku"])
            existing_item.category = str(prod.get("category", ""))
        else:
            cart.items.append(
                CartItem(
                    product_id=product_id,
                    sku=str(prod["sku"]),
                    product_name=str(prod["name"]),
                    category=str(prod.get("category", "")),
                    unit_price_snapshot=live_price,
                    quantity=desired_qty,
                )
            )
        cart.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.expire(cart, ["items"])
        return await self.get_enriched_cart(session, client, user_id=user_id, role=role)

    async def remove_item(
        self,
        session: Session,
        client: httpx.AsyncClient,
        *,
        user_id: str,
        role: str,
        product_id: str,
    ) -> CartViewResponse:
        cart = self.get_or_create_cart(session, user_id)
        target = next((i for i in cart.items if i.product_id == product_id), None)
        if target is None:
            raise NotFoundError(f"Product '{product_id}' is not in cart", error_code="CART_ITEM_NOT_FOUND")
        session.delete(target)
        cart.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.expire(cart, ["items"])
        return await self.get_enriched_cart(session, client, user_id=user_id, role=role)

    def clear_cart(self, session: Session, user_id: str) -> Cart:
        cart = self.get_or_create_cart(session, user_id)
        session.execute(delete(CartItem).where(CartItem.cart_id == cart.id))
        cart.coupon_code = None
        cart.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.expire(cart, ["items"])
        return self.get_or_create_cart(session, user_id)

    def clear_cart_for_event(self, session: Session, *, event_id: str, user_id: str) -> bool:
        if session.get(ProcessedCartEvent, event_id) is not None:
            return False
        session.add(ProcessedCartEvent(event_id=event_id))
        cart = session.scalar(select(Cart).where(Cart.user_id == user_id))
        if cart is not None:
            session.execute(delete(CartItem).where(CartItem.cart_id == cart.id))
            cart.coupon_code = None
            cart.updated_at = datetime.now(timezone.utc)
        session.commit()
        return True

    def purge_product_from_carts(self, session: Session, product_id: str) -> int:
        res = session.execute(delete(CartItem).where(CartItem.product_id == product_id))
        session.commit()
        return int(res.rowcount or 0)

    async def get_enriched_cart(
        self,
        session: Session,
        client: httpx.AsyncClient,
        *,
        user_id: str,
        role: str,
    ) -> CartViewResponse:
        cart = self.get_or_create_cart(session, user_id)
        headers = self._service_headers(user_id, role)
        live_sync_degraded = False

        items = list(cart.items)
        fetched = await asyncio.gather(
            *(self._fetch_product_soft(client, item.product_id, headers) for item in items)
        )

        views: list[CartItemView] = []
        subtotal = 0.0
        categories: list[str] = []
        for item, (status_flag, prod) in zip(items, fetched):
            if status_flag == "ok" and prod is not None:
                live_price = round(float(prod["price"]), 2)
                avail_qty = int(prod.get("available_quantity", 0))
                is_avail = bool(prod.get("is_active", True)) and avail_qty >= item.quantity
                price_changed = abs(live_price - item.unit_price_snapshot) > 0.009
                category = str(prod.get("category", item.category))
            elif status_flag == "missing":
                live_price = item.unit_price_snapshot
                avail_qty = 0
                is_avail = False
                price_changed = False
                category = item.category
            else:
                live_sync_degraded = True
                live_price = item.unit_price_snapshot
                avail_qty = item.quantity
                is_avail = True
                price_changed = False
                category = item.category

            line_total = round(live_price * item.quantity, 2)
            if is_avail:
                subtotal += line_total
                if category:
                    categories.append(category)
            views.append(
                CartItemView(
                    id=item.id,
                    product_id=item.product_id,
                    sku=item.sku,
                    product_name=item.product_name,
                    category=category,
                    quantity=item.quantity,
                    unit_price_snapshot=item.unit_price_snapshot,
                    live_price=live_price,
                    price_changed=price_changed,
                    available_quantity=avail_qty,
                    is_available=is_avail,
                    line_total=line_total,
                )
            )

        subtotal = round(subtotal, 2)
        coupon_valid: bool | None = None
        coupon_message: str | None = None
        discount_amount = 0.0

        if cart.coupon_code and subtotal > 0:
            loyalty_tier = await self._fetch_loyalty_tier(client, user_id, role)
            try:
                val = await self._validate_coupon_remote(
                    client,
                    code=cart.coupon_code,
                    user_id=user_id,
                    role=role,
                    subtotal=subtotal,
                    loyalty_tier=loyalty_tier,
                    categories=categories,
                )
                coupon_valid = bool(val.get("valid", False))
                discount_amount = round(float(val.get("discount_amount", 0.0)), 2) if coupon_valid else 0.0
                coupon_message = val.get("reason")
            except (ServiceTimeoutError, ServiceUnavailableError):
                live_sync_degraded = True
                coupon_valid = None
                coupon_message = "promotion-service temporarily unavailable"

        return CartViewResponse(
            id=cart.id,
            user_id=cart.user_id,
            items=views,
            items_count=sum(v.quantity for v in views),
            subtotal=subtotal,
            coupon_code=cart.coupon_code,
            coupon_valid=coupon_valid,
            coupon_message=coupon_message,
            discount_amount=discount_amount,
            shipping_method=cart.shipping_method,
            estimated_total=round(max(0.0, subtotal - discount_amount), 2),
            live_sync_degraded=live_sync_degraded,
            updated_at=cart.updated_at,
        )

    async def apply_coupon(
        self,
        session: Session,
        client: httpx.AsyncClient,
        *,
        user_id: str,
        role: str,
        code: str,
    ) -> CartViewResponse:
        view = await self.get_enriched_cart(session, client, user_id=user_id, role=role)
        if not view.items or view.subtotal <= 0:
            raise BadRequestError("Cannot apply a coupon to an empty cart", error_code="EMPTY_CART")

        loyalty_tier = await self._fetch_loyalty_tier(client, user_id, role)
        categories = sorted({item.category for item in view.items if item.category})
        val = await self._validate_coupon_remote(
            client,
            code=code.strip().upper(),
            user_id=user_id,
            role=role,
            subtotal=view.subtotal,
            loyalty_tier=loyalty_tier,
            categories=categories,
        )
        if not val.get("valid"):
            raise BadRequestError(
                str(val.get("reason") or "Promotion code is not applicable to this cart"),
                error_code="INVALID_COUPON",
            )

        cart = self.get_or_create_cart(session, user_id)
        cart.coupon_code = str(val["code"])
        cart.updated_at = datetime.now(timezone.utc)
        session.commit()
        return await self.get_enriched_cart(session, client, user_id=user_id, role=role)

    async def remove_coupon(
        self,
        session: Session,
        client: httpx.AsyncClient,
        *,
        user_id: str,
        role: str,
    ) -> CartViewResponse:
        cart = self.get_or_create_cart(session, user_id)
        cart.coupon_code = None
        cart.updated_at = datetime.now(timezone.utc)
        session.commit()
        return await self.get_enriched_cart(session, client, user_id=user_id, role=role)

    async def set_shipping_method(
        self,
        session: Session,
        client: httpx.AsyncClient,
        *,
        user_id: str,
        role: str,
        shipping_method: str,
    ) -> CartViewResponse:
        cart = self.get_or_create_cart(session, user_id)
        cart.shipping_method = shipping_method
        cart.updated_at = datetime.now(timezone.utc)
        session.commit()
        return await self.get_enriched_cart(session, client, user_id=user_id, role=role)

    async def checkout(
        self,
        session: Session,
        client: httpx.AsyncClient,
        *,
        user_id: str,
        role: str,
        payload: CartCheckoutRequest,
    ) -> dict[str, Any]:
        view = await self.get_enriched_cart(session, client, user_id=user_id, role=role)
        if not view.items:
            raise BadRequestError("Your shopping cart is empty", error_code="EMPTY_CART")

        unavailable = [item.product_name for item in view.items if not item.is_available]
        if unavailable:
            raise ConflictError(
                f"Items unavailable or out of stock: {', '.join(unavailable)}",
                error_code="CART_ITEMS_UNAVAILABLE",
            )

        cart = self.get_or_create_cart(session, user_id)
        order_body = {
            "items": [{"product_id": item.product_id, "quantity": item.quantity} for item in view.items],
            "shipping_address": payload.shipping_address,
            "shipping_method": payload.shipping_method or cart.shipping_method,
            "coupon_code": payload.coupon_code if payload.coupon_code is not None else cart.coupon_code,
            "payment_method": payload.payment_method,
            "currency": payload.currency,
            "idempotency_key": payload.idempotency_key,
            "simulate_payment_failure": payload.simulate_payment_failure,
        }
        url = f"{self.settings.order_service_url}/orders"
        try:
            resp = await client.post(url, json=order_body, headers=self._service_headers(user_id, role))
        except httpx.TimeoutException as exc:
            raise ServiceTimeoutError("order-service timed out during cart checkout") from exc
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("order-service is unavailable") from exc

        if resp.status_code >= 500:
            raise ServiceUnavailableError("order-service failed during checkout")
        if resp.status_code >= 400:
            msg, code = self._extract_error_detail(resp, "Order checkout rejected")
            if resp.status_code == 409:
                raise ConflictError(msg, error_code=code or "CHECKOUT_CONFLICT")
            if resp.status_code == 404:
                raise NotFoundError(msg, error_code=code or "NOT_FOUND")
            raise BadRequestError(msg, error_code=code or "CHECKOUT_REJECTED")

        order_data = resp.json()
        if order_data.get("status") == "paid":
            self.clear_cart(session, user_id)
        return order_data

    @staticmethod
    def _extract_error_detail(resp: httpx.Response, fallback: str) -> tuple[str, str | None]:
        try:
            body = resp.json()
            if isinstance(body, dict) and "error" in body and isinstance(body["error"], dict):
                err = body["error"]
                return str(err.get("message", fallback)), str(err.get("code")) if err.get("code") else None
        except Exception:
            pass
        return fallback, None
