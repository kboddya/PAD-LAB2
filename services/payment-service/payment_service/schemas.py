"""Pydantic request and response schemas for payment-service."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PaymentCreateRequest(BaseModel):
    order_id: str = Field(..., min_length=1, max_length=36)
    user_id: str = Field(..., min_length=1, max_length=36)
    amount: float = Field(..., gt=0)
    currency: str = Field(default="USD", min_length=3, max_length=8)
    payment_method: str = Field(default="card", min_length=2, max_length=32)
    idempotency_key: str | None = Field(default=None, max_length=128)
    simulate_failure: bool = False


class PaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    order_id: str
    user_id: str
    amount: float
    currency: str
    payment_method: str
    status: str
    idempotency_key: str
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime
