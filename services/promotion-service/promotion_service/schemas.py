"""Pydantic request and response schemas for promotion-service."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PromotionCreateRequest(BaseModel):
    code: str = Field(..., min_length=2, max_length=64)
    description: str = Field(default="", max_length=500)
    discount_type: Literal["percentage", "fixed"]
    discount_value: float = Field(..., gt=0)
    min_order_amount: float = Field(default=0.0, ge=0.0)
    max_discount_amount: float | None = Field(default=None, gt=0.0)
    required_tier: Literal["standard", "silver", "gold"] = "standard"
    category_filter: str | None = Field(default=None, max_length=80)
    max_uses: int | None = Field(default=None, gt=0)
    per_user_limit: int = Field(default=1, gt=0)
    target_user_id: str | None = Field(default=None, max_length=36)
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def _validate_percentage_and_dates(self) -> "PromotionCreateRequest":
        if self.discount_type == "percentage" and self.discount_value > 100.0:
            raise ValueError("Percentage discount cannot exceed 100")
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        return self


class PromotionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    code: str
    description: str
    discount_type: str
    discount_value: float
    min_order_amount: float
    max_discount_amount: float | None
    required_tier: str
    category_filter: str | None
    max_uses: int | None
    current_uses: int
    per_user_limit: int
    target_user_id: str | None
    valid_from: datetime | None
    valid_until: datetime | None
    is_active: bool
    created_by: str
    created_at: datetime


class PromotionValidateRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)
    order_subtotal: float = Field(..., ge=0.0)
    loyalty_tier: Literal["standard", "silver", "gold"] = "standard"
    categories: list[str] = Field(default_factory=list)
    user_id: str | None = Field(default=None, max_length=36)


class PromotionValidationResponse(BaseModel):
    valid: bool
    code: str
    discount_type: str | None = None
    discount_amount: float = 0.0
    final_amount: float = 0.0
    reason: str | None = None


class PromotionRedeemRequest(BaseModel):
    order_id: str = Field(..., min_length=1, max_length=36)
    user_id: str = Field(..., min_length=1, max_length=36)
    code: str = Field(..., min_length=1, max_length=64)
    order_subtotal: float = Field(..., gt=0.0)
    loyalty_tier: Literal["standard", "silver", "gold"] = "standard"
    categories: list[str] = Field(default_factory=list)


class PromotionReleaseRequest(BaseModel):
    order_id: str = Field(..., min_length=1, max_length=36)


class CouponRedemptionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    promotion_id: str
    code: str
    order_id: str
    user_id: str
    discount_amount: float
    status: str
    created_at: datetime
    updated_at: datetime
