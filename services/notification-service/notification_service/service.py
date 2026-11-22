"""Domain business logic for notification creation, deduplication, and read tracking."""

from __future__ import annotations

import json

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from notification_service.models import Notification
from notification_service.schemas import NotificationCreateRequest
from shared.errors import ForbiddenError, NotFoundError


class NotificationDomainService:
    """Handles persistent event-driven notifications for users."""

    def create_notification(self, session: Session, payload: NotificationCreateRequest) -> Notification:
        if payload.event_id:
            existing = session.scalar(
                select(Notification).where(Notification.event_id == payload.event_id)
            )
            if existing is not None:
                return existing

        notification = Notification(
            user_id=payload.user_id,
            category=payload.category.strip().lower(),
            title=payload.title.strip(),
            message=payload.message.strip(),
            is_read=False,
            event_id=payload.event_id,
            metadata_json=json.dumps(payload.metadata, ensure_ascii=False),
        )
        session.add(notification)
        session.commit()
        session.refresh(notification)
        return notification

    def list_notifications(
        self,
        session: Session,
        *,
        user_id: str,
        unread_only: bool = False,
        skip: int = 0,
        limit: int = 50,
    ) -> list[Notification]:
        stmt = select(Notification).where(Notification.user_id == user_id)
        if unread_only:
            stmt = stmt.where(Notification.is_read.is_(False))
        stmt = stmt.order_by(Notification.created_at.desc()).offset(skip).limit(limit)
        return list(session.scalars(stmt).all())

    def get_unread_count(self, session: Session, user_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user_id, Notification.is_read.is_(False))
        )
        return int(session.scalar(stmt) or 0)

    def mark_read(
        self,
        session: Session,
        notification_id: str,
        *,
        actor_id: str,
        actor_role: str,
    ) -> Notification:
        notification = session.get(Notification, notification_id)
        if notification is None:
            raise NotFoundError(
                f"Notification '{notification_id}' not found",
                error_code="NOTIFICATION_NOT_FOUND",
            )
        if actor_role != "admin" and notification.user_id != actor_id:
            raise ForbiddenError("Cannot modify another user's notification")
        notification.is_read = True
        session.commit()
        session.refresh(notification)
        return notification

    def mark_all_read(self, session: Session, user_id: str) -> int:
        result = session.execute(
            update(Notification)
            .where(Notification.user_id == user_id, Notification.is_read.is_(False))
            .values(is_read=True)
        )
        session.commit()
        return int(result.rowcount or 0)
