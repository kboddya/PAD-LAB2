"""Base configuration settings shared across all ShopFlow microservices."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseServiceSettings(BaseSettings):
    """Common environment-driven configuration for every microservice."""

    service_name: str = "base-service"
    service_port: int = 8000
    log_level: str = "INFO"
    log_json: bool = True

    database_url: str = "sqlite:///./service.db"
    rabbitmq_url: str = "memory://"

    jwt_secret: str = "dev-super-secret-change-in-production-32chars"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    internal_service_token: str = "dev-internal-service-token-32chars"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
