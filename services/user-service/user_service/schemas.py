"""Pydantic request and response schemas for user-service."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class UserCreateRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=36)
    email: str = Field(..., min_length=5, max_length=255)
    username: str = Field(..., min_length=2, max_length=64)
    full_name: str = Field(default="", max_length=120)
    phone: str | None = Field(default=None, max_length=32)
    role: Literal["customer", "seller", "admin"] = "customer"
    loyalty_tier: Literal["standard", "silver", "gold"] = "standard"


class UserUpdateRequest(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=120)
    phone: str | None = Field(default=None, max_length=32)
    loyalty_tier: Literal["standard", "silver", "gold"] | None = None


class AddressCreateRequest(BaseModel):
    label: str = Field(default="Home", min_length=1, max_length=64)
    country: str = Field(..., min_length=2, max_length=80)
    city: str = Field(..., min_length=1, max_length=80)
    street: str = Field(..., min_length=2, max_length=200)
    postal_code: str = Field(..., min_length=2, max_length=24)
    is_default: bool = False


class AddressResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    label: str
    country: str
    city: str
    street: str
    postal_code: str
    is_default: bool
    created_at: datetime


class UserProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: str
    email: str
    username: str
    full_name: str
    phone: str | None
    role: str
    loyalty_tier: str
    created_at: datetime
    updated_at: datetime
    addresses: list[AddressResponse] = Field(default_factory=list)
