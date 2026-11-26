"""Pydantic request and response schemas for shipping-service."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ShippingQuoteRequest(BaseModel):
    shipping_method: Literal["standard", "express", "pickup"] = "standard"
    items_count: int = Field(default=1, ge=1, le=500)
    order_subtotal: float = Field(default=0.0, ge=0.0)
    loyalty_tier: Literal["standard", "silver", "gold"] = "standard"


class ShippingQuoteResponse(BaseModel):
    shipping_method: str
    carrier: str
    cost: float
    estimated_days: int
    free_shipping_applied: bool


class ShipmentStatusUpdateRequest(BaseModel):
    status: Literal["dispatched", "in_transit", "delivered", "cancelled"]
    location: str = Field(default="Chisinau Distribution Hub", min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=300)


class ShipmentCheckpointResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    location: str
    description: str
    recorded_at: datetime


class ShipmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    order_id: str
    user_id: str
    tracking_number: str
    shipping_method: str
    carrier: str
    shipping_cost: float
    shipping_address: str
    status: str
    estimated_days: int
    created_at: datetime
    updated_at: datetime
    checkpoints: list[ShipmentCheckpointResponse] = Field(default_factory=list)


class PublicTrackingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tracking_number: str
    shipping_method: str
    carrier: str
    status: str
    estimated_days: int
    updated_at: datetime
    checkpoints: list[ShipmentCheckpointResponse] = Field(default_factory=list)
