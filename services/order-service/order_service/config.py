"""Configuration for order-service."""

from pydantic import AliasChoices, Field
from shared.config import BaseServiceSettings


class OrderSettings(BaseServiceSettings):
    """Settings specific to order-service."""

    service_name: str = "order-service"
    service_port: int = 8004
    database_url: str = Field(
        default="sqlite:///./order.db",
        validation_alias=AliasChoices("ORDER_DATABASE_URL", "DATABASE_URL"),
    )
    user_service_url: str = "http://user-service:8002"
    product_service_url: str = "http://product-service:8003"
    payment_service_url: str = "http://payment-service:8005"
    promotion_service_url: str = "http://promotion-service:8008"
    shipping_service_url: str = "http://shipping-service:8009"
    downstream_timeout_seconds: float = 5.0
