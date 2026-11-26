"""FastAPI application entrypoint for support-service."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, FastAPI, Query, Request, status
from sqlalchemy.orm import Session

from shared.app_factory import build_service_app
from shared.database import DatabaseManager
from shared.errors import ForbiddenError
from shared.messaging import EventBus, create_event_bus
from shared.security import UserContext, extract_user_context, require_roles
from support_service.config import SupportSettings
from support_service.models import SupportBase
from support_service.schemas import (
    TicketCreateRequest,
    TicketMessageCreateRequest,
    TicketRefundApproveRequest,
    TicketResponse,
    TicketStatusUpdateRequest,
)
from support_service.service import SupportDomainService


def create_app(
    settings: SupportSettings | None = None,
    event_bus: EventBus | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Create and configure the support-service FastAPI application."""
    cfg = settings or SupportSettings()
    db = DatabaseManager(cfg.database_url, SupportBase)
    bus = event_bus or create_event_bus(cfg.rabbitmq_url, cfg.service_name)
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=cfg.downstream_timeout_seconds)

    async def _shutdown_client() -> None:
        if owns_client:
            await client.aclose()

    app, logger = build_service_app(
        title="ShopFlow Support Service",
        description="Customer support tickets, SLA routing, and automated dispute refund orchestration.",
        settings=cfg,
        db=db,
        event_bus=bus,
        on_shutdown=_shutdown_client,
    )
    domain = SupportDomainService(cfg, logger)

    def _get_caller(request: Request) -> UserContext:
        return extract_user_context(
            request,
            jwt_secret=cfg.jwt_secret,
            jwt_algorithm=cfg.jwt_algorithm,
            internal_service_token=cfg.internal_service_token,
        )

    router = APIRouter(prefix="/support/tickets", tags=["Support"])

    @router.post(
        "",
        response_model=TicketResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Open a customer support ticket or order dispute",
    )
    async def create_ticket(
        payload: TicketCreateRequest,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> TicketResponse:
        caller = _get_caller(request)
        ticket = await domain.create_ticket(
            session,
            client,
            user_id=caller.user_id,
            role=caller.role,
            payload=payload,
        )
        await bus.publish(
            event_type="ticket.created",
            payload={
                "ticket_id": ticket.id,
                "user_id": ticket.user_id,
                "order_id": ticket.order_id,
                "category": ticket.category,
                "priority": ticket.priority,
            },
            source_service=cfg.service_name,
        )
        logger.info("Support ticket created", extra={"ticket_id": ticket.id, "priority": ticket.priority})
        return domain.serialize_for_caller(ticket, viewer_role=caller.role)

    @router.get("", response_model=list[TicketResponse], summary="List support tickets")
    async def list_tickets(
        request: Request,
        status_filter: str | None = Query(default=None, alias="status"),
        priority: str | None = Query(default=None),
        skip: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
        session: Session = Depends(db.get_db),
    ) -> list[TicketResponse]:
        caller = _get_caller(request)
        target_user = None if caller.role in {"seller", "admin"} else caller.user_id
        rows = domain.list_tickets(
            session,
            user_id=target_user,
            status=status_filter,
            priority=priority,
            skip=skip,
            limit=limit,
        )
        return [domain.serialize_for_caller(t, viewer_role=caller.role) for t in rows]

    @router.get("/{ticket_id}", response_model=TicketResponse, summary="Get support ticket details")
    async def get_ticket(
        ticket_id: str,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> TicketResponse:
        caller = _get_caller(request)
        ticket = domain.get_ticket(session, ticket_id)
        if caller.role not in {"seller", "admin"} and ticket.user_id != caller.user_id:
            raise ForbiddenError("Access denied to another user's support ticket")
        return domain.serialize_for_caller(ticket, viewer_role=caller.role)

    @router.post("/{ticket_id}/messages", response_model=TicketResponse, summary="Add a message to ticket thread")
    async def add_ticket_message(
        ticket_id: str,
        payload: TicketMessageCreateRequest,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> TicketResponse:
        caller = _get_caller(request)
        ticket = domain.add_message(
            session,
            ticket_id,
            payload,
            actor_id=caller.user_id,
            actor_role=caller.role,
        )
        return domain.serialize_for_caller(ticket, viewer_role=caller.role)

    @router.patch(
        "/{ticket_id}/status",
        response_model=TicketResponse,
        summary="Update ticket status (seller/admin)",
    )
    async def update_ticket_status(
        ticket_id: str,
        payload: TicketStatusUpdateRequest,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> TicketResponse:
        caller = _get_caller(request)
        require_roles(caller, {"seller", "admin"})
        ticket = domain.update_status(session, ticket_id, payload)
        if ticket.status == "resolved":
            await bus.publish(
                event_type="ticket.resolved",
                payload={
                    "ticket_id": ticket.id,
                    "user_id": ticket.user_id,
                    "order_id": ticket.order_id,
                    "refund_amount": ticket.refund_amount,
                    "resolution_note": ticket.resolution_note,
                },
                source_service=cfg.service_name,
            )
        return domain.serialize_for_caller(ticket, viewer_role=caller.role)

    @router.post(
        "/{ticket_id}/approve-refund",
        response_model=TicketResponse,
        summary="Approve dispute and trigger payment refund (admin)",
    )
    async def approve_ticket_refund(
        ticket_id: str,
        request: Request,
        payload: TicketRefundApproveRequest | None = None,
        session: Session = Depends(db.get_db),
    ) -> TicketResponse:
        caller = _get_caller(request)
        require_roles(caller, {"admin"})
        ticket = await domain.approve_refund(
            session,
            client,
            ticket_id,
            actor_id=caller.user_id,
            resolution_note=payload.resolution_note if payload else None,
        )
        await bus.publish(
            event_type="ticket.resolved",
            payload={
                "ticket_id": ticket.id,
                "user_id": ticket.user_id,
                "order_id": ticket.order_id,
                "refund_amount": ticket.refund_amount,
                "resolution_note": ticket.resolution_note,
            },
            source_service=cfg.service_name,
        )
        logger.info(
            "Approved refund via support ticket",
            extra={"ticket_id": ticket.id, "order_id": ticket.order_id, "refund_amount": ticket.refund_amount},
        )
        return domain.serialize_for_caller(ticket, viewer_role=caller.role)

    app.include_router(router)
    return app
