"""Configuration for user-service."""

from pydantic import AliasChoices, Field
from shared.config import BaseServiceSettings


class UserSettings(BaseServiceSettings):
    """Settings specific to user-service."""

    service_name: str = "user-service"
    service_port: int = 8002
    database_url: str = Field(
        default="sqlite:///./user.db",
        validation_alias=AliasChoices("USER_DATABASE_URL", "DATABASE_URL"),
    )
