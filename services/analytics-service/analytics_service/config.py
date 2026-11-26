"""Configuration for analytics-service."""

from pydantic import AliasChoices, Field
from shared.config import BaseServiceSettings


class AnalyticsSettings(BaseServiceSettings):
    """Settings specific to analytics-service."""

    service_name: str = "analytics-service"
    service_port: int = 8012
    database_url: str = Field(
        default="sqlite:///./analytics.db",
        validation_alias=AliasChoices("ANALYTICS_DATABASE_URL", "DATABASE_URL"),
    )
    silver_tier_threshold: float = 500.0
    gold_tier_threshold: float = 1500.0
