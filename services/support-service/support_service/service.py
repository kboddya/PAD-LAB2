"""Domain logic for support tickets, SLA prioritization, and automated refund orchestration."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

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
from support_service.config import SupportSettings
from support_service.models import SupportTicket, TicketMessage
from support_service.schemas import (
    TicketCreateRequest,
    TicketMessageCreateRequest,
    TicketResponse,
    TicketStatusUpdateRequest,
)


class SupportDomainService:
    """Manages customer support tickets, SLA calculation, and dispute refund approval."""

    def __init__(self, settings: SupportSettings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger

    def _service_headers(self, user_id: str, role: str) -> dict[str, str]:
        return {
            "X-Request-ID": get_request_id(),
            "X-User-Id": user_id,
            "X-User-Role": role,
            "X-Internal-Token": self.settings.internal_service_token,
        }

    async def _fetch_loyalty_tier(self, client: httpx.AsyncClient, user_id: str, role: str) -> str:
        url = f"{self.settings.user_service_url}/users/{user_id}"
        try:
            resp = await client.get(url, headers=self._service_headers(user_id, role))
            if resp.status_code == 200:
                return str(resp.json().get("loyalty_tier", "standard"))
        except httpx.HTTPError:
            pass
        return "standard"

    async def _verify_order(
        self,
        client: httpx.AsyncClient,
        *,
        order_id: str,
        user_id: str,
        role: str,
    ) -> dict[str, Any]:
        url = f"{self.settings.order_service_url}/orders/{order_id}"
        try:
            resp = await client.get(url, headers=self._service_headers(user_id, role))
        except httpx.TimeoutException as exc:
            raise ServiceTimeoutError("order-service timed out verifying ticket order") from exc
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("order-service is unavailable") from exc

        if resp.status_code == 404:
            raise NotFoundError(f"Order '{order_id}' not found", error_code="ORDER_NOT_FOUND")
        if resp.status_code == 403:
            raise ForbiddenError("Cannot open a ticket for another user's order")
        if resp.status_code >= 500:
            raise ServiceUnavailableError("order-service returned an internal error")
        return resp.json()

    async def _fetch_shipment_status_soft(
        self,
        client: httpx.AsyncClient,
        *,
        order_id: str,
        user_id: str,
        role: str,
    ) -> str | None:
        url = f"{self.settings.shipping_service_url}/shipments/by-order/{order_id}"
        try:
            resp = await client.get(url, headers=self._service_headers(user_id, role))
            if resp.status_code == 200:
                return str(resp.json().get("status"))
        except httpx.HTTPError:
            pass
        return None

    @staticmethod
    def _compute_priority_and_sla(*, category: str, loyalty_tier: str, order_total: float) -> tuple[str, int]:
        if category == "refund_request" and (loyalty_tier == "gold" or order_total >= 500.0):
            return "urgent", 2
        if category in {"refund_request", "shipping_delay"} or loyalty_tier == "gold":
            return "high", 6
        if category == "order_issue" or loyalty_tier == "silver":
            return "normal", 24
        return "low", 48

    async def create_ticket(
        self,
        session: Session,
        client: httpx.AsyncClient,
        *,
        user_id: str,
        role: str,
        payload: TicketCreateRequest,
    ) -> SupportTicket:
        loyalty_tier = await self._fetch_loyalty_tier(client, user_id, role)
        order_total = 0.0
        payment_id: str | None = None
        shipment_status: str | None = None

        if payload.order_id:
            order = await self._verify_order(client, order_id=payload.order_id, user_id=user_id, role=role)
            order_total = float(order.get("total_amount", 0.0))
            payment_id = order.get("payment_id")
            shipment_status = await self._fetch_shipment_status_soft(
                client,
                order_id=payload.order_id,
                user_id=user_id,
                role=role,
            )

        priority, sla_hours = self._compute_priority_and_sla(
            category=payload.category,
            loyalty_tier=loyalty_tier,
            order_total=order_total,
        )
        now = datetime.now(timezone.utc)
        ticket = SupportTicket(
            user_id=user_id,
            order_id=payload.order_id,
            payment_id=payment_id,
            shipment_status=shipment_status,
            category=payload.category,
            priority=priority,
            status="open",
            subject=payload.subject.strip(),
            sla_hours=sla_hours,
            sla_due_at=now + timedelta(hours=sla_hours),
            created_at=now,
            updated_at=now,
        )
        ticket.messages.append(
            TicketMessage(
                author_id=user_id,
                author_role=role,
                message=payload.message.strip(),
                is_internal_note=False,
            )
        )
        session.add(ticket)
        session.commit()
        return self.get_ticket(session, ticket.id)

    def list_tickets(
        self,
        session: Session,
        *,
        user_id: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> list[SupportTicket]:
        stmt = select(SupportTicket).options(selectinload(SupportTicket.messages))
        if user_id:
            stmt = stmt.where(SupportTicket.user_id == user_id)
        if status:
            stmt = stmt.where(SupportTicket.status == status)
        if priority:
            stmt = stmt.where(SupportTicket.priority == priority)
        stmt = stmt.order_by(SupportTicket.created_at.desc()).offset(skip).limit(limit)
        return list(session.scalars(stmt).all())

    def get_ticket(self, session: Session, ticket_id: str) -> SupportTicket:
        stmt = (
            select(SupportTicket)
            .options(selectinload(SupportTicket.messages))
            .where(SupportTicket.id == ticket_id)
        )
        ticket = session.scalar(stmt)
        if ticket is None:
            raise NotFoundError(f"Support ticket '{ticket_id}' not found", error_code="TICKET_NOT_FOUND")
        return ticket

    def serialize_for_caller(self, ticket: SupportTicket, *, viewer_role: str) -> TicketResponse:
        resp = TicketResponse.model_validate(ticket)
        if viewer_role not in {"seller", "admin"}:
            resp.messages = [m for m in resp.messages if not m.is_internal_note]
        return resp

    def add_message(
        self,
        session: Session,
        ticket_id: str,
        payload: TicketMessageCreateRequest,
        *,
        actor_id: str,
        actor_role: str,
    ) -> SupportTicket:
        ticket = self.get_ticket(session, ticket_id)
        if actor_role not in {"seller", "admin"} and ticket.user_id != actor_id:
            raise ForbiddenError("Access denied to another user's support ticket")
        if payload.is_internal_note and actor_role not in {"seller", "admin"}:
            raise ForbiddenError("Customers cannot post internal notes", error_code="FORBIDDEN_FIELD")

        ticket.messages.append(
            TicketMessage(
                author_id=actor_id,
                author_role=actor_role,
                message=payload.message.strip(),
                is_internal_note=payload.is_internal_note,
            )
        )
        if actor_role in {"seller", "admin"} and ticket.status == "open":
            ticket.status = "in_progress"
        ticket.updated_at = datetime.now(timezone.utc)
        session.commit()
        return self.get_ticket(session, ticket.id)

    def update_status(
        self,
        session: Session,
        ticket_id: str,
        payload: TicketStatusUpdateRequest,
    ) -> SupportTicket:
        ticket = self.get_ticket(session, ticket_id)
        ticket.status = payload.status
        if payload.resolution_note is not None:
            ticket.resolution_note = payload.resolution_note.strip()
        ticket.updated_at = datetime.now(timezone.utc)
        session.commit()
        return self.get_ticket(session, ticket.id)

    async def approve_refund(
        self,
        session: Session,
        client: httpx.AsyncClient,
        ticket_id: str,
        *,
        actor_id: str,
        resolution_note: str | None = None,
    ) -> SupportTicket:
        ticket = self.get_ticket(session, ticket_id)
        if not ticket.order_id:
            raise BadRequestError("Ticket is not linked to an order", error_code="TICKET_MISSING_ORDER")
        if ticket.status in {"resolved", "rejected", "closed"}:
            raise ConflictError(f"Ticket is already '{ticket.status}'", error_code="TICKET_ALREADY_CLOSED")

        admin_headers = self._service_headers(actor_id, "admin")
        payment_id = ticket.payment_id
        if not payment_id:
            lookup_url = f"{self.settings.payment_service_url}/payments/by-order/{ticket.order_id}"
            try:
                lookup_resp = await client.get(lookup_url, headers=admin_headers)
            except httpx.TimeoutException as exc:
                raise ServiceTimeoutError("payment-service timed out during payment lookup") from exc
            except httpx.HTTPError as exc:
                raise ServiceUnavailableError("payment-service is unavailable") from exc

            if lookup_resp.status_code == 404:
                raise NotFoundError("No payment found for this ticket's order", error_code="PAYMENT_NOT_FOUND")
            if lookup_resp.status_code >= 500:
                raise ServiceUnavailableError("payment-service returned an internal error")
            payment_id = str(lookup_resp.json()["id"])

        refund_url = f"{self.settings.payment_service_url}/payments/{payment_id}/refund"
        try:
            refund_resp = await client.post(refund_url, headers=admin_headers)
        except httpx.TimeoutException as exc:
            raise ServiceTimeoutError("payment-service timed out during refund") from exc
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("payment-service is unavailable") from exc

        if refund_resp.status_code >= 500:
            raise ServiceUnavailableError("payment-service failed to process refund")
        if refund_resp.status_code == 409:
            raise ConflictError("Payment cannot be refunded in its current state", error_code="REFUND_CONFLICT")
        if refund_resp.status_code >= 400:
            raise ExternalServiceError(f"payment-service rejected refund ({refund_resp.status_code})")

        refund_data = refund_resp.json()
        refund_amount = float(refund_data["amount"])
        note = (
            resolution_note.strip()
            if resolution_note
            else f"Refund of {refund_amount:.2f} {refund_data.get('currency', 'USD')} approved and processed."
        )

        ticket.payment_id = payment_id
        ticket.status = "resolved"
        ticket.refund_amount = refund_amount
        ticket.resolution_note = note
        ticket.updated_at = datetime.now(timezone.utc)
        ticket.messages.append(
            TicketMessage(
                author_id=actor_id,
                author_role="admin",
                message=note,
                is_internal_note=False,
            )
        )
        session.commit()
        return self.get_ticket(session, ticket.id)
