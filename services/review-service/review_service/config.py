"""Configuration for review-service."""

from pydantic import AliasChoices, Field
from shared.config import BaseServiceSettings


class ReviewSettings(BaseServiceSettings):
    """Settings specific to review-service."""

    service_name: str = "review-service"
    service_port: int = 8010
    database_url: str = Field(
        default="sqlite:///./review.db",
        validation_alias=AliasChoices("REVIEW_DATABASE_URL", "DATABASE_URL"),
    )
    order_service_url: str = "http://order-service:8004"
    product_service_url: str = "http://product-service:8003"
    downstream_timeout_seconds: float = 5.0
