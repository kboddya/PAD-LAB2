"""FastAPI application entrypoint for notification-service."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Query, Request, status
from sqlalchemy.orm import Session

from notification_service.config import NotificationSettings
from notification_service.models import NotificationBase
from notification_service.schemas import (
    NotificationCreateRequest,
    NotificationResponse,
    UnreadCountResponse,
)
from notification_service.service import NotificationDomainService
from shared.app_factory import build_service_app
from shared.database import DatabaseManager
from shared.errors import ForbiddenError
from shared.messaging import EventBus, EventEnvelope, create_event_bus
from shared.security import UserContext, extract_user_context, require_roles

_EventFormatter = Callable[[dict[str, Any]], tuple[str, str, str]]


def _build_event_formatters() -> dict[str, _EventFormatter]:
    return {
        "user.registered": lambda p: (
            "welcome",
            "Welcome to ShopFlow!",
            f"Hi {p.get('username', 'there')}, your ShopFlow account is ready.",
        ),
        "order.created": lambda p: (
            "order_created",
            "Order placed",
            f"Order {p.get('order_id')} for {p.get('total_amount')} {p.get('currency', 'USD')} has been created.",
        ),
        "order.cancelled": lambda p: (
            "order_cancelled",
            "Order cancelled",
            f"Order {p.get('order_id')} has been cancelled and reserved items were released.",
        ),
        "payment.succeeded": lambda p: (
            "payment_succeeded",
            "Payment confirmed",
            f"Payment {p.get('payment_id')} of {p.get('amount')} {p.get('currency', 'USD')} for order {p.get('order_id')} succeeded.",
        ),
        "payment.failed": lambda p: (
            "payment_failed",
            "Payment declined",
            f"Payment for order {p.get('order_id')} failed: {p.get('reason', 'declined')}. Stock reservation released.",
        ),
        "payment.refunded": lambda p: (
            "payment_refunded",
            "Payment refunded",
            f"Payment {p.get('payment_id')} of {p.get('amount')} {p.get('currency', 'USD')} was refunded.",
        ),
        "shipment.dispatched": lambda p: (
            "shipment_dispatched",
            "Order shipped",
            f"Order {p.get('order_id')} has been dispatched (tracking {p.get('tracking_number', '-')}).",
        ),
        "shipment.delivered": lambda p: (
            "shipment_delivered",
            "Order delivered",
            f"Order {p.get('order_id')} (tracking {p.get('tracking_number', '-')}) has been delivered.",
        ),
        "review.published": lambda p: (
            "review_published",
            "New verified review received",
            f"Product {p.get('product_id')} received a {p.get('rating')}/5 verified review.",
        ),
        "ticket.resolved": lambda p: (
            "ticket_resolved",
            "Support ticket resolved",
            f"Support ticket {p.get('ticket_id')} has been resolved: {p.get('resolution_note') or 'completed'}.",
        ),
        "loyalty.upgraded": lambda p: (
            "loyalty_upgraded",
            f"Loyalty tier upgraded to {str(p.get('new_tier', '')).upper()}!",
            f"Congratulations! Your loyalty tier is now {p.get('new_tier')}.",
        ),
    }


def create_app(
    settings: NotificationSettings | None = None,
    event_bus: EventBus | None = None,
) -> FastAPI:
    """Create and configure the notification-service FastAPI application."""
    cfg = settings or NotificationSettings()
    db = DatabaseManager(cfg.database_url, NotificationBase)
    bus = event_bus or create_event_bus(cfg.rabbitmq_url, cfg.service_name)
    domain = NotificationDomainService()

    app, logger = build_service_app(
        title="ShopFlow Notification Service",
        description="Event-driven user notification feed and unread alert tracking.",
        settings=cfg,
        db=db,
        event_bus=bus,
    )

    formatters = _build_event_formatters()

    def _handle_domain_event(envelope: EventEnvelope) -> None:
        formatter = formatters.get(envelope.event_type)
        user_id = envelope.payload.get("recipient_user_id") or envelope.payload.get("user_id")
        if formatter is None or not user_id:
            return
        category, title, message = formatter(envelope.payload)
        with db.session_scope() as session:
            domain.create_notification(
                session,
                NotificationCreateRequest(
                    user_id=str(user_id),
                    category=category,
                    title=title,
                    message=message,
                    metadata=envelope.payload,
                    event_id=envelope.event_id,
                ),
            )
        logger.info(
            "Stored notification from event",
            extra={"user_id": str(user_id), "event_type": envelope.event_type},
        )

    for routing_key in formatters:
        bus.on(routing_key, _handle_domain_event)

    def _get_caller(request: Request) -> UserContext:
        return extract_user_context(
            request,
            jwt_secret=cfg.jwt_secret,
            jwt_algorithm=cfg.jwt_algorithm,
            internal_service_token=cfg.internal_service_token,
        )

    router = APIRouter(prefix="/notifications", tags=["Notifications"])

    @router.post(
        "",
        response_model=NotificationResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Create a notification manually (admin/internal)",
    )
    async def create_notification(
        payload: NotificationCreateRequest,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> NotificationResponse:
        caller = _get_caller(request)
        require_roles(caller, {"admin"})
        created = domain.create_notification(session, payload)
        return NotificationResponse.from_orm_model(created)

    @router.get("", response_model=list[NotificationResponse], summary="List notifications for user")
    async def list_notifications(
        request: Request,
        unread_only: bool = Query(default=False),
        user_id: str | None = Query(default=None),
        skip: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
        session: Session = Depends(db.get_db),
    ) -> list[NotificationResponse]:
        caller = _get_caller(request)
        if user_id and caller.role != "admin" and user_id != caller.user_id:
            raise ForbiddenError("Cannot view other users' notifications")
        target_user = user_id if (user_id and caller.role == "admin") else caller.user_id
        rows = domain.list_notifications(
            session,
            user_id=target_user,
            unread_only=unread_only,
            skip=skip,
            limit=limit,
        )
        return [NotificationResponse.from_orm_model(n) for n in rows]

    @router.get("/unread-count", response_model=UnreadCountResponse, summary="Get unread notification count")
    async def get_unread_count(
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> UnreadCountResponse:
        caller = _get_caller(request)
        count = domain.get_unread_count(session, caller.user_id)
        return UnreadCountResponse(user_id=caller.user_id, unread_count=count)

    @router.patch("/{notification_id}/read", response_model=NotificationResponse, summary="Mark notification read")
    async def mark_read(
        notification_id: str,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> NotificationResponse:
        caller = _get_caller(request)
        updated = domain.mark_read(
            session,
            notification_id,
            actor_id=caller.user_id,
            actor_role=caller.role,
        )
        return NotificationResponse.from_orm_model(updated)

    @router.post("/mark-all-read", summary="Mark all user notifications as read")
    async def mark_all_read(
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> dict[str, int]:
        caller = _get_caller(request)
        updated_count = domain.mark_all_read(session, caller.user_id)
        return {"marked_read": updated_count}

    app.include_router(router)
    return app
