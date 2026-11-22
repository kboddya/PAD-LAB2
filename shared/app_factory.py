"""Factory helper to construct consistently configured FastAPI microservices."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from shared.config import BaseServiceSettings
from shared.database import DatabaseManager
from shared.errors import register_exception_handlers
from shared.health import create_health_router
from shared.logging import setup_logging
from shared.messaging import EventBus
from shared.middleware import RequestContextMiddleware


def build_service_app(
    *,
    title: str,
    description: str,
    version: str = "1.0.0",
    settings: BaseServiceSettings,
    db: DatabaseManager | None = None,
    event_bus: EventBus | None = None,
    on_startup: Callable[[], Awaitable[None]] | None = None,
    on_shutdown: Callable[[], Awaitable[None]] | None = None,
    extra_ready_check: Callable[[], Awaitable[dict[str, str]]] | None = None,
) -> tuple[FastAPI, logging.Logger]:
    """Build a FastAPI microservice with logging, health probes, middleware, and error handlers."""
    logger = setup_logging(settings.service_name, settings.log_level, settings.log_json)

    if db is not None:
        db.create_all()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if db is not None:
            db.create_all()
        if event_bus is not None:
            await event_bus.connect()
        if on_startup is not None:
            await on_startup()
        logger.info("Service started", extra={"port": settings.service_port})
        try:
            yield
        finally:
            if on_shutdown is not None:
                await on_shutdown()
            if event_bus is not None:
                await event_bus.close()
            if db is not None:
                db.dispose()
            logger.info("Service stopped")

    app = FastAPI(
        title=title,
        description=description,
        version=version,
        lifespan=lifespan,
    )
    app.add_middleware(RequestContextMiddleware, logger=logger)
    register_exception_handlers(app, settings.service_name, logger)
    app.include_router(
        create_health_router(
            settings.service_name,
            db=db,
            extra_ready_check=extra_ready_check,
        )
    )

    app.state.logger = logger
    app.state.db = db
    app.state.event_bus = event_bus
    app.state.settings = settings
    return app, logger
