"""Configuration for product-service."""

from pydantic import AliasChoices, Field
from shared.config import BaseServiceSettings


class ProductSettings(BaseServiceSettings):
    """Settings specific to product-service."""

    service_name: str = "product-service"
    service_port: int = 8003
    database_url: str = Field(
        default="sqlite:///./product.db",
        validation_alias=AliasChoices("PRODUCT_DATABASE_URL", "DATABASE_URL"),
    )
