"""Configuration for promotion-service."""

from pydantic import AliasChoices, Field
from shared.config import BaseServiceSettings


class PromotionSettings(BaseServiceSettings):
    """Settings specific to promotion-service."""

    service_name: str = "promotion-service"
    service_port: int = 8008
    database_url: str = Field(
        default="sqlite:///./promotion.db",
        validation_alias=AliasChoices("PROMOTION_DATABASE_URL", "DATABASE_URL"),
    )
