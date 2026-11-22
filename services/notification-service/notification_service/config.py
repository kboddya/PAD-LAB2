"""Configuration for notification-service."""

from pydantic import AliasChoices, Field
from shared.config import BaseServiceSettings


class NotificationSettings(BaseServiceSettings):
    """Settings specific to notification-service."""

    service_name: str = "notification-service"
    service_port: int = 8006
    database_url: str = Field(
        default="sqlite:///./notification.db",
        validation_alias=AliasChoices("NOTIFICATION_DATABASE_URL", "DATABASE_URL"),
    )
