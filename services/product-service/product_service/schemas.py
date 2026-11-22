"""Pydantic request and response schemas for product-service."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProductCreateRequest(BaseModel):
    sku: str = Field(..., min_length=2, max_length=64)
    name: str = Field(..., min_length=2, max_length=200)
    description: str = Field(default="", max_length=2000)
    category: str = Field(..., min_length=2, max_length=80)
    price: float = Field(..., gt=0)
    stock_quantity: int = Field(..., ge=0)


class ProductUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, min_length=2, max_length=80)
    price: float | None = Field(default=None, gt=0)
    stock_quantity: int | None = Field(default=None, ge=0)
    is_active: bool | None = None


class InventoryAdjustRequest(BaseModel):
    quantity: int = Field(..., gt=0)
    order_id: str | None = Field(default=None, max_length=36)


class ProductResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    sku: str
    name: str
    description: str
    category: str
    price: float
    stock_quantity: int
    reserved_quantity: int
    available_quantity: int
    seller_id: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class InventoryActionResponse(BaseModel):
    product_id: str
    order_id: str | None
    action: str
    quantity: int
    stock_quantity: int
    reserved_quantity: int
    available_quantity: int
