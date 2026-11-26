"""Pydantic request and response schemas for cart-service."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class CartItemAddRequest(BaseModel):
    product_id: str = Field(..., min_length=1, max_length=36)
    quantity: int = Field(default=1, gt=0, le=100)


class CartItemUpdateRequest(BaseModel):
    quantity: int = Field(..., ge=0, le=100)


class CartCouponRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)


class CartShippingMethodRequest(BaseModel):
    shipping_method: Literal["standard", "express", "pickup"]


class CartCheckoutRequest(BaseModel):
    shipping_address: str = Field(..., min_length=5, max_length=500)
    shipping_method: Literal["standard", "express", "pickup"] | None = None
    coupon_code: str | None = Field(default=None, max_length=64)
    payment_method: str = Field(default="card", min_length=2, max_length=32)
    currency: str = Field(default="USD", min_length=3, max_length=8)
    idempotency_key: str | None = Field(default=None, max_length=128)
    simulate_payment_failure: bool = False


class CartItemView(BaseModel):
    id: str
    product_id: str
    sku: str
    product_name: str
    category: str
    quantity: int
    unit_price_snapshot: float
    live_price: float
    price_changed: bool
    available_quantity: int
    is_available: bool
    line_total: float


class CartViewResponse(BaseModel):
    id: str
    user_id: str
    items: list[CartItemView] = Field(default_factory=list)
    items_count: int = 0
    subtotal: float = 0.0
    coupon_code: str | None = None
    coupon_valid: bool | None = None
    coupon_message: str | None = None
    discount_amount: float = 0.0
    shipping_method: str = "standard"
    estimated_total: float = 0.0
    live_sync_degraded: bool = False
    updated_at: datetime
