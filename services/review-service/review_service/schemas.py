"""Pydantic request and response schemas for review-service."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReviewCreateRequest(BaseModel):
    product_id: str = Field(..., min_length=1, max_length=36)
    rating: int = Field(..., ge=1, le=5)
    title: str = Field(..., min_length=2, max_length=160)
    comment: str = Field(..., min_length=5, max_length=2000)


class ReviewReplyRequest(BaseModel):
    seller_reply: str = Field(..., min_length=2, max_length=1000)


class ReviewModerateRequest(BaseModel):
    moderation_status: Literal["approved", "rejected"]


class ReviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    product_id: str
    seller_id: str
    user_id: str
    order_id: str
    rating: int
    title: str
    comment: str
    verified_purchase: bool
    moderation_status: str
    helpful_count: int
    seller_reply: str | None
    created_at: datetime
    updated_at: datetime


class ProductRatingSummaryResponse(BaseModel):
    product_id: str
    average_rating: float
    review_count: int
    rating_distribution: dict[str, int]
