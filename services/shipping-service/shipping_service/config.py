"""Configuration for shipping-service."""

from pydantic import AliasChoices, Field
from shared.config import BaseServiceSettings


class ShippingSettings(BaseServiceSettings):
    """Settings specific to shipping-service."""

    service_name: str = "shipping-service"
    service_port: int = 8009
    database_url: str = Field(
        default="sqlite:///./shipping.db",
        validation_alias=AliasChoices("SHIPPING_DATABASE_URL", "DATABASE_URL"),
    )
