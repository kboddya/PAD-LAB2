"""Shared core library for ShopFlow microservices."""

from shared.app_factory import build_service_app
from shared.config import BaseServiceSettings
from shared.database import DatabaseManager
from shared.health import create_health_router
from shared.errors import (
    AppError,
    BadRequestError,
    ConflictError,
    DatabaseError,
    ExternalServiceError,
    ForbiddenError,
    NotFoundError,
    RateLimitError,
    ServiceTimeoutError,
    ServiceUnavailableError,
    UnauthorizedError,
    register_exception_handlers,
)
from shared.logging import get_request_id, request_id_var, setup_logging
from shared.messaging import EventBus, EventEnvelope, InMemoryEventBus, RabbitMQEventBus, create_event_bus
from shared.middleware import RequestContextMiddleware
from shared.security import (
    create_token,
    decode_token,
    extract_user_context,
    hash_password,
    verify_password,
)

__all__ = [
    "AppError",
    "BadRequestError",
    "BaseServiceSettings",
    "ConflictError",
    "DatabaseError",
    "DatabaseManager",
    "EventBus",
    "EventEnvelope",
    "ExternalServiceError",
    "ForbiddenError",
    "InMemoryEventBus",
    "NotFoundError",
    "RabbitMQEventBus",
    "RateLimitError",
    "RequestContextMiddleware",
    "ServiceTimeoutError",
    "ServiceUnavailableError",
    "UnauthorizedError",
    "build_service_app",
    "create_event_bus",
    "create_health_router",
    "create_token",
    "decode_token",
    "extract_user_context",
    "get_request_id",
    "hash_password",
    "register_exception_handlers",
    "request_id_var",
    "setup_logging",
    "verify_password",
]
