"""Configuration for auth-service."""

from pydantic import AliasChoices, Field
from shared.config import BaseServiceSettings


class AuthSettings(BaseServiceSettings):
    """Settings specific to auth-service."""

    service_name: str = "auth-service"
    service_port: int = 8001
    database_url: str = Field(
        default="sqlite:///./auth.db",
        validation_alias=AliasChoices("AUTH_DATABASE_URL", "DATABASE_URL"),
    )
