"""Configuration for payment-service."""

from pydantic import AliasChoices, Field
from shared.config import BaseServiceSettings


class PaymentSettings(BaseServiceSettings):
    """Settings specific to payment-service."""

    service_name: str = "payment-service"
    service_port: int = 8005
    database_url: str = Field(
        default="sqlite:///./payment.db",
        validation_alias=AliasChoices("PAYMENT_DATABASE_URL", "DATABASE_URL"),
    )
    max_single_payment_amount: float = 10000.0
