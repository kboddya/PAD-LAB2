"""Pydantic request and response schemas for order-service."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class OrderItemInput(BaseModel):
    product_id: str = Field(..., min_length=1, max_length=36)
    quantity: int = Field(..., gt=0, le=1000)


class OrderCreateRequest(BaseModel):
    items: list[OrderItemInput] = Field(..., min_length=1)
    shipping_address: str = Field(..., min_length=5, max_length=500)
    shipping_method: Literal["standard", "express", "pickup"] | None = None
    coupon_code: str | None = Field(default=None, max_length=64)
    payment_method: str = Field(default="card", min_length=2, max_length=32)
    currency: str = Field(default="USD", min_length=3, max_length=8)
    idempotency_key: str | None = Field(default=None, max_length=128)
    simulate_payment_failure: bool = False


class OrderStatusUpdateRequest(BaseModel):
    status: Literal["shipped", "delivered"]


class OrderItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    product_id: str
    product_name: str
    quantity: int
    unit_price: float


class OrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    status: str
    subtotal_amount: float = 0.0
    discount_amount: float = 0.0
    shipping_cost: float = 0.0
    total_amount: float
    currency: str
    coupon_code: str | None = None
    shipping_method: str = "standard"
    shipping_address: str
    payment_id: str | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime
    items: list[OrderItemResponse] = Field(default_factory=list)
