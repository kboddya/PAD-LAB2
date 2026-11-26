"""Domain logic for tariff quoting, shipment provisioning, and tracking state machine."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from shared.errors import ConflictError, NotFoundError
from shipping_service.models import OrderShippingIntent, Shipment, ShipmentCheckpoint
from shipping_service.schemas import (
    ShippingQuoteRequest,
    ShippingQuoteResponse,
    ShipmentStatusUpdateRequest,
)

_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "preparing": {"dispatched", "cancelled"},
    "dispatched": {"in_transit", "delivered"},
    "in_transit": {"delivered"},
    "delivered": set(),
    "cancelled": set(),
}

_CARRIERS: dict[str, tuple[str, int]] = {
    "pickup": ("ShopFlow Pickup Point", 1),
    "standard": ("ShopFlow Standard", 4),
    "express": ("ShopFlow Express", 1),
}


class ShippingDomainService:
    """Calculates delivery rates and manages fulfillment shipments & tracking."""

    def calculate_quote(self, payload: ShippingQuoteRequest) -> ShippingQuoteResponse:
        method = payload.shipping_method
        carrier, estimated_days = _CARRIERS[method]
        free_applied = False

        if method == "pickup":
            cost = 0.0
            free_applied = True
        elif method == "standard":
            if payload.loyalty_tier == "gold" or payload.order_subtotal >= 100.0:
                cost = 0.0
                free_applied = True
            else:
                cost = round(7.99 + max(0, payload.items_count - 1) * 1.5, 2)
        else:  # express
            base = round(19.99 + max(0, payload.items_count - 1) * 2.5, 2)
            cost = round(base * 0.5, 2) if payload.loyalty_tier == "gold" else base

        return ShippingQuoteResponse(
            shipping_method=method,
            carrier=carrier,
            cost=cost,
            estimated_days=estimated_days,
            free_shipping_applied=free_applied,
        )

    def record_order_intent(
        self,
        session: Session,
        *,
        order_id: str,
        user_id: str,
        shipping_address: str,
        shipping_method: str,
        shipping_cost: float,
    ) -> OrderShippingIntent:
        existing = session.get(OrderShippingIntent, order_id)
        if existing is not None:
            return existing
        intent = OrderShippingIntent(
            order_id=order_id,
            user_id=user_id,
            shipping_address=shipping_address,
            shipping_method=shipping_method if shipping_method in _CARRIERS else "standard",
            shipping_cost=round(float(shipping_cost), 2),
        )
        session.add(intent)
        session.commit()
        session.refresh(intent)
        return intent

    def create_shipment_idempotent(
        self,
        session: Session,
        *,
        order_id: str,
        user_id: str,
    ) -> tuple[Shipment, bool]:
        stmt = select(Shipment).options(selectinload(Shipment.checkpoints)).where(Shipment.order_id == order_id)
        existing = session.scalar(stmt)
        if existing is not None:
            return existing, False

        intent = session.get(OrderShippingIntent, order_id)
        method = intent.shipping_method if intent else "standard"
        carrier, estimated_days = _CARRIERS.get(method, ("ShopFlow Standard", 4))
        address = intent.shipping_address if intent else "Address on file"
        cost = intent.shipping_cost if intent else 0.0

        tracking_number = f"SF-TRK-{uuid.uuid4().hex[:8].upper()}"
        shipment = Shipment(
            order_id=order_id,
            user_id=user_id,
            tracking_number=tracking_number,
            shipping_method=method,
            carrier=carrier,
            shipping_cost=cost,
            shipping_address=address,
            status="preparing",
            estimated_days=estimated_days,
        )
        shipment.checkpoints.append(
            ShipmentCheckpoint(
                status="preparing",
                location="ShopFlow Fulfillment Center",
                description="Payment confirmed; order packed and awaiting carrier pickup.",
            )
        )
        session.add(shipment)
        session.commit()
        return self.get_shipment(session, shipment.id), True

    def list_shipments(
        self,
        session: Session,
        *,
        user_id: str | None = None,
        status: str | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> list[Shipment]:
        stmt = select(Shipment).options(selectinload(Shipment.checkpoints))
        if user_id:
            stmt = stmt.where(Shipment.user_id == user_id)
        if status:
            stmt = stmt.where(Shipment.status == status)
        stmt = stmt.order_by(Shipment.created_at.desc()).offset(skip).limit(limit)
        return list(session.scalars(stmt).all())

    def get_shipment(self, session: Session, shipment_id: str) -> Shipment:
        stmt = select(Shipment).options(selectinload(Shipment.checkpoints)).where(Shipment.id == shipment_id)
        shipment = session.scalar(stmt)
        if shipment is None:
            raise NotFoundError(f"Shipment '{shipment_id}' not found", error_code="SHIPMENT_NOT_FOUND")
        return shipment

    def get_by_order(self, session: Session, order_id: str) -> Shipment:
        stmt = select(Shipment).options(selectinload(Shipment.checkpoints)).where(Shipment.order_id == order_id)
        shipment = session.scalar(stmt)
        if shipment is None:
            raise NotFoundError(f"Shipment for order '{order_id}' not found", error_code="SHIPMENT_NOT_FOUND")
        return shipment

    def track_by_number(self, session: Session, tracking_number: str) -> Shipment:
        normalized = tracking_number.strip().upper()
        stmt = (
            select(Shipment)
            .options(selectinload(Shipment.checkpoints))
            .where(Shipment.tracking_number == normalized)
        )
        shipment = session.scalar(stmt)
        if shipment is None:
            raise NotFoundError(f"Tracking number '{normalized}' not found", error_code="TRACKING_NOT_FOUND")
        return shipment

    def advance_status(
        self,
        session: Session,
        shipment_id: str,
        payload: ShipmentStatusUpdateRequest,
    ) -> Shipment:
        shipment = self.get_shipment(session, shipment_id)
        allowed = _ALLOWED_TRANSITIONS.get(shipment.status, set())
        if payload.status not in allowed:
            raise ConflictError(
                f"Cannot transition shipment from '{shipment.status}' to '{payload.status}'",
                error_code="INVALID_SHIPMENT_TRANSITION",
            )

        default_desc = {
            "dispatched": "Shipment handed over to carrier and departed origin facility.",
            "in_transit": "Shipment arrived at regional sorting transit hub.",
            "delivered": "Shipment delivered to recipient.",
            "cancelled": "Shipment cancelled prior to dispatch.",
        }[payload.status]

        shipment.status = payload.status
        shipment.updated_at = datetime.now(timezone.utc)
        shipment.checkpoints.append(
            ShipmentCheckpoint(
                status=payload.status,
                location=payload.location.strip(),
                description=(payload.description.strip() if payload.description else default_desc),
            )
        )
        session.commit()
        return self.get_shipment(session, shipment.id)

    def cancel_for_order_if_preparing(self, session: Session, order_id: str) -> Shipment | None:
        stmt = select(Shipment).options(selectinload(Shipment.checkpoints)).where(Shipment.order_id == order_id)
        shipment = session.scalar(stmt)
        if shipment is None or shipment.status != "preparing":
            return None
        shipment.status = "cancelled"
        shipment.updated_at = datetime.now(timezone.utc)
        shipment.checkpoints.append(
            ShipmentCheckpoint(
                status="cancelled",
                location="ShopFlow Fulfillment Center",
                description="Order was cancelled prior to dispatch.",
            )
        )
        session.commit()
        return self.get_shipment(session, shipment.id)
