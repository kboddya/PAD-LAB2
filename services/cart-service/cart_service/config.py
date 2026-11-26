"""Configuration for cart-service."""

from pydantic import AliasChoices, Field
from shared.config import BaseServiceSettings


class CartSettings(BaseServiceSettings):
    """Settings specific to cart-service."""

    service_name: str = "cart-service"
    service_port: int = 8007
    database_url: str = Field(
        default="sqlite:///./cart.db",
        validation_alias=AliasChoices("CART_DATABASE_URL", "DATABASE_URL"),
    )
    user_service_url: str = "http://user-service:8002"
    product_service_url: str = "http://product-service:8003"
    promotion_service_url: str = "http://promotion-service:8008"
    order_service_url: str = "http://order-service:8004"
    downstream_timeout_seconds: float = 5.0
