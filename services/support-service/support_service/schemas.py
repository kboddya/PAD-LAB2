"""Pydantic request and response schemas for support-service."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TicketCreateRequest(BaseModel):
    order_id: str | None = Field(default=None, max_length=36)
    category: Literal["order_issue", "refund_request", "shipping_delay", "product_question"]
    subject: str = Field(..., min_length=3, max_length=200)
    message: str = Field(..., min_length=5, max_length=2000)


class TicketMessageCreateRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    is_internal_note: bool = False


class TicketStatusUpdateRequest(BaseModel):
    status: Literal["in_progress", "resolved", "rejected", "closed"]
    resolution_note: str | None = Field(default=None, max_length=1000)


class TicketRefundApproveRequest(BaseModel):
    resolution_note: str | None = Field(default=None, max_length=1000)


class TicketMessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    ticket_id: str
    author_id: str
    author_role: str
    message: str
    is_internal_note: bool
    created_at: datetime


class TicketResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    order_id: str | None
    payment_id: str | None
    shipment_status: str | None
    category: str
    priority: str
    status: str
    subject: str
    sla_hours: int
    sla_due_at: datetime
    sla_breached: bool
    refund_amount: float | None
    resolution_note: str | None
    created_at: datetime
    updated_at: datetime
    messages: list[TicketMessageResponse] = Field(default_factory=list)
