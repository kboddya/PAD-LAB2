"""Configuration for support-service."""

from pydantic import AliasChoices, Field
from shared.config import BaseServiceSettings


class SupportSettings(BaseServiceSettings):
    """Settings specific to support-service."""

    service_name: str = "support-service"
    service_port: int = 8011
    database_url: str = Field(
        default="sqlite:///./support.db",
        validation_alias=AliasChoices("SUPPORT_DATABASE_URL", "DATABASE_URL"),
    )
    user_service_url: str = "http://user-service:8002"
    order_service_url: str = "http://order-service:8004"
    shipping_service_url: str = "http://shipping-service:8009"
    payment_service_url: str = "http://payment-service:8005"
    downstream_timeout_seconds: float = 5.0
