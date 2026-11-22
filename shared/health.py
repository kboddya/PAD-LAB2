"""Reusable /health and /ready probes for ShopFlow microservices."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from shared.database import DatabaseManager


def create_health_router(
    service_name: str,
    db: DatabaseManager | None = None,
    extra_ready_check: Callable[[], Awaitable[dict[str, str]]] | None = None,
) -> APIRouter:
    """Create standard Kubernetes-compatible /health and /ready endpoints."""
    router = APIRouter(tags=["Health"])

    @router.get("/health", summary="Liveness probe")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": service_name}

    @router.get("/ready", summary="Readiness probe")
    async def ready() -> JSONResponse:
        checks: dict[str, str] = {}
        all_ready = True

        if db is not None:
            db_ok = db.check_health()
            checks["database"] = "ok" if db_ok else "unavailable"
            if not db_ok:
                all_ready = False

        if extra_ready_check is not None:
            extra = await extra_ready_check()
            checks.update(extra)
            if any(status != "ok" for status in extra.values()):
                all_ready = False

        status_code = 200 if all_ready else 503
        return JSONResponse(
            status_code=status_code,
            content={
                "status": "ready" if all_ready else "not_ready",
                "service": service_name,
                "checks": checks,
            },
        )

    return router
