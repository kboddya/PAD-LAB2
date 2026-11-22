"""Pydantic request and response schemas for notification-service."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class NotificationCreateRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=36)
    category: str = Field(default="system", min_length=2, max_length=48)
    title: str = Field(..., min_length=1, max_length=200)
    message: str = Field(..., min_length=1, max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    event_id: str | None = Field(default=None, max_length=64)


class NotificationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    category: str
    title: str
    message: str
    is_read: bool
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    @classmethod
    def from_orm_model(cls, obj: Any) -> "NotificationResponse":
        try:
            meta = json.loads(obj.metadata_json) if obj.metadata_json else {}
        except json.JSONDecodeError:
            meta = {}
        return cls(
            id=obj.id,
            user_id=obj.user_id,
            category=obj.category,
            title=obj.title,
            message=obj.message,
            is_read=obj.is_read,
            metadata=meta,
            created_at=obj.created_at,
        )


class UnreadCountResponse(BaseModel):
    user_id: str
    unread_count: int
