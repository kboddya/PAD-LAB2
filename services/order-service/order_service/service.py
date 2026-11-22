"""Domain and orchestration logic for order-service."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from order_service.config import OrderSettings
from order_service.models import Order, OrderItem
from order_service.schemas import OrderCreateRequest
from shared.errors import (
    BadRequestError,
    ConflictError,
    ExternalServiceError,
    ForbiddenError,
    NotFoundError,
    ServiceTimeoutError,
    ServiceUnavailableError,
)
from shared.logging import get_request_id


class OrderDomainService:
    """Coordinates user validation, stock reservation, payment, and Saga updates."""

    def __init__(self, settings: OrderSettings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger

    def _service_headers(self, user_id: str, role: str) -> dict[str, str]:
        return {
            "X-Request-ID": get_request_id(),
            "X-User-Id": user_id,
            "X-User-Role": role,
        }

    async def verify_user_exists(
        self,
        client: httpx.AsyncClient,
        user_id: str,
        role: str,
    ) -> dict[str, Any]:
        url = f"{self.settings.user_service_url}/users/{user_id}"
        try:
            resp = await client.get(url, headers=self._service_headers(user_id, role))
        except httpx.TimeoutException as exc:
            raise ServiceTimeoutError("user-service timed out during order validation") from exc
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("user-service is unavailable") from exc

        if resp.status_code == 404:
            raise NotFoundError(f"User profile '{user_id}' not found", error_code="USER_NOT_FOUND")
        if resp.status_code >= 500:
            raise ServiceUnavailableError("user-service returned an internal error")
        if resp.status_code >= 400:
            raise ExternalServiceError(f"user-service rejected request ({resp.status_code})")
        return resp.json()

    async def reserve_items(
        self,
        client: httpx.AsyncClient,
        order_id: str,
        payload: OrderCreateRequest,
        headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Fetch product details and reserve inventory atomically with rollback on partial failure."""
        merged_quantities: dict[str, int] = {}
        for item in payload.items:
            merged_quantities[item.product_id] = merged_quantities.get(item.product_id, 0) + item.quantity

        reserved: list[dict[str, Any]] = []
        try:
            for product_id, qty in merged_quantities.items():
                product_data = await self._fetch_product(client, product_id, headers)
                if not product_data.get("is_active", True):
                    raise BadRequestError(
                        f"Product '{product_data.get('name', product_id)}' is inactive",
                        error_code="PRODUCT_INACTIVE",
                    )

                await self._reserve_single_product(client, product_id, qty, order_id, headers)
                reserved.append(
                    {
                        "product_id": product_id,
                        "product_name": str(product_data["name"]),
                        "quantity": qty,
                        "unit_price": float(product_data["price"]),
                    }
                )
        except Exception:
            if reserved:
                await self.release_items_best_effort(client, order_id, reserved, headers)
            raise

        return reserved

    async def _fetch_product(
        self,
        client: httpx.AsyncClient,
        product_id: str,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        url = f"{self.settings.product_service_url}/products/{product_id}"
        try:
            resp = await client.get(url, headers=headers)
        except httpx.TimeoutException as exc:
            raise ServiceTimeoutError("product-service timed out") from exc
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("product-service is unavailable") from exc

        if resp.status_code == 404:
            raise NotFoundError(f"Product '{product_id}' not found", error_code="PRODUCT_NOT_FOUND")
        if resp.status_code >= 500:
            raise ServiceUnavailableError("product-service returned an internal error")
        return resp.json()

    async def _reserve_single_product(
        self,
        client: httpx.AsyncClient,
        product_id: str,
        quantity: int,
        order_id: str,
        headers: dict[str, str],
    ) -> None:
        url = f"{self.settings.product_service_url}/products/{product_id}/reserve"
        try:
            resp = await client.post(
                url,
                json={"quantity": quantity, "order_id": order_id},
                headers=headers,
            )
        except httpx.TimeoutException as exc:
            raise ServiceTimeoutError("product-service timed out during stock reservation") from exc
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("product-service is unavailable during stock reservation") from exc

        if resp.status_code == 409:
            msg = self._extract_error_message(resp, "Insufficient stock for requested product")
            raise ConflictError(msg, error_code="INSUFFICIENT_STOCK")
        if resp.status_code == 400:
            msg = self._extract_error_message(resp, "Invalid product reservation request")
            raise BadRequestError(msg, error_code="PRODUCT_INACTIVE")
        if resp.status_code >= 500:
            raise ServiceUnavailableError("product-service failed to reserve stock")

    async def release_items_best_effort(
        self,
        client: httpx.AsyncClient,
        order_id: str,
        items: list[dict[str, Any]],
        headers: dict[str, str],
    ) -> None:
        for item in items:
            url = f"{self.settings.product_service_url}/products/{item['product_id']}/release"
            try:
                await client.post(
                    url,
                    json={"quantity": item["quantity"], "order_id": order_id},
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                self.logger.warning(
                    "Best-effort stock release failed for product %s: %s",
                    item["product_id"],
                    exc,
                )

    def create_order_record(
        self,
        session: Session,
        *,
        order_id: str,
        user_id: str,
        payload: OrderCreateRequest,
        reserved_items: list[dict[str, Any]],
    ) -> Order:
        total = round(sum(item["quantity"] * item["unit_price"] for item in reserved_items), 2)
        order = Order(
            id=order_id,
            user_id=user_id,
            status="pending",
            total_amount=total,
            currency=payload.currency.strip().upper(),
            shipping_address=payload.shipping_address.strip(),
        )
        for item in reserved_items:
            order.items.append(
                OrderItem(
                    product_id=item["product_id"],
                    product_name=item["product_name"],
                    quantity=item["quantity"],
                    unit_price=item["unit_price"],
                )
            )
        session.add(order)
        session.commit()
        return self.get_order(session, order_id)

    async def initiate_payment(
        self,
        client: httpx.AsyncClient,
        order: Order,
        payload: OrderCreateRequest,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        url = f"{self.settings.payment_service_url}/payments"
        body = {
            "order_id": order.id,
            "user_id": order.user_id,
            "amount": order.total_amount,
            "currency": order.currency,
            "payment_method": payload.payment_method,
            "idempotency_key": payload.idempotency_key or f"order-{order.id}",
            "simulate_failure": payload.simulate_payment_failure,
        }
        try:
            resp = await client.post(url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise ServiceTimeoutError("payment-service timed out") from exc
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("payment-service is unavailable") from exc

        if resp.status_code >= 500:
            raise ServiceUnavailableError("payment-service returned an internal error")
        if resp.status_code >= 400:
            msg = self._extract_error_message(resp, "Payment request rejected")
            raise BadRequestError(msg, error_code="PAYMENT_REJECTED")
        return resp.json()

    def apply_payment_result(
        self,
        session: Session,
        *,
        order_id: str,
        payment_id: str | None,
        status: str,
        failure_reason: str | None = None,
    ) -> Order | None:
        order = session.get(Order, order_id)
        if order is None:
            return None
        if order.status == "cancelled":
            return order
        if payment_id:
            order.payment_id = payment_id
        order.status = status
        order.failure_reason = failure_reason
        order.updated_at = datetime.now(timezone.utc)
        session.commit()
        return self.get_order(session, order_id)

    def list_orders(
        self,
        session: Session,
        *,
        user_id: str | None = None,
        status: str | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> list[Order]:
        stmt = select(Order).options(selectinload(Order.items))
        if user_id:
            stmt = stmt.where(Order.user_id == user_id)
        if status:
            stmt = stmt.where(Order.status == status)
        stmt = stmt.order_by(Order.created_at.desc()).offset(skip).limit(limit)
        return list(session.scalars(stmt).all())

    def get_order(self, session: Session, order_id: str) -> Order:
        stmt = select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
        order = session.scalar(stmt)
        if order is None:
            raise NotFoundError(f"Order '{order_id}' not found", error_code="ORDER_NOT_FOUND")
        return order

    def cancel_order(self, session: Session, order_id: str, *, actor_id: str, actor_role: str) -> Order:
        order = self.get_order(session, order_id)
        if actor_role != "admin" and order.user_id != actor_id:
            raise ForbiddenError("Only the order owner or an admin can cancel this order")
        if order.status == "cancelled":
            return order
        if order.status in {"shipped", "delivered"}:
            raise ConflictError(
                f"Cannot cancel order in status '{order.status}'",
                error_code="ORDER_CANNOT_BE_CANCELLED",
            )
        order.status = "cancelled"
        order.updated_at = datetime.now(timezone.utc)
        session.commit()
        return self.get_order(session, order_id)

    def update_order_status(self, session: Session, order_id: str, new_status: str) -> Order:
        order = self.get_order(session, order_id)
        if order.status not in {"paid", "shipped"}:
            raise ConflictError(
                f"Cannot transition order from '{order.status}' to '{new_status}'",
                error_code="INVALID_ORDER_TRANSITION",
            )
        order.status = new_status
        order.updated_at = datetime.now(timezone.utc)
        session.commit()
        return self.get_order(session, order_id)

    @staticmethod
    def new_order_id() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def _extract_error_message(resp: httpx.Response, fallback: str) -> str:
        try:
            body = resp.json()
            if isinstance(body, dict) and "error" in body and isinstance(body["error"], dict):
                return str(body["error"].get("message", fallback))
        except Exception:
            pass
        return fallback
