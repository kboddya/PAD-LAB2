"""FastAPI application entrypoint for ShopFlow API Gateway."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from gateway_app.config import GatewaySettings
from gateway_app.proxy import DownstreamProxy
from gateway_app.rate_limiter import RateLimitMiddleware, SlidingWindowRateLimiter
from shared.errors import (
    ServiceTimeoutError,
    ServiceUnavailableError,
    UnauthorizedError,
    register_exception_handlers,
)
from shared.logging import get_request_id, setup_logging
from shared.middleware import RequestContextMiddleware
from shared.security import UserContext, decode_token, extract_bearer_token, require_roles


def create_app(
    settings: GatewaySettings | None = None,
    http_client: httpx.AsyncClient | None = None,
    rate_limiter: SlidingWindowRateLimiter | None = None,
) -> FastAPI:
    """Create and configure the API Gateway FastAPI application."""
    cfg = settings or GatewaySettings()
    logger = setup_logging(cfg.service_name, cfg.log_level, cfg.log_json)
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=cfg.proxy_timeout_seconds)
    proxy = DownstreamProxy(client)
    limiter = rate_limiter or SlidingWindowRateLimiter(
        max_requests=cfg.rate_limit_requests,
        window_seconds=cfg.rate_limit_window_seconds,
    )

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        logger.info("API Gateway started", extra={"port": cfg.service_port})
        try:
            yield
        finally:
            if owns_client:
                await client.aclose()
            logger.info("API Gateway stopped")

    app = FastAPI(
        title="ShopFlow API Gateway",
        description="Unified API Gateway with JWT auth, RBAC, rate limiting, and aggregation.",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Order matters in Starlette: last added middleware runs outermost.
    app.add_middleware(RateLimitMiddleware, limiter=limiter, service_name=cfg.service_name)
    app.add_middleware(RequestContextMiddleware, logger=logger)
    register_exception_handlers(app, cfg.service_name, logger)

    service_urls: dict[str, str] = {
        "auth-service": cfg.auth_service_url,
        "user-service": cfg.user_service_url,
        "product-service": cfg.product_service_url,
        "order-service": cfg.order_service_url,
        "payment-service": cfg.payment_service_url,
        "notification-service": cfg.notification_service_url,
    }

    async def _authenticate(request: Request) -> UserContext:
        token = extract_bearer_token(request)
        if not cfg.verify_tokens_remotely:
            claims = decode_token(
                token,
                secret=cfg.jwt_secret,
                algorithm=cfg.jwt_algorithm,
                expected_type="access",
            )
            return UserContext(user_id=str(claims["sub"]), role=str(claims.get("role", "customer")))

        verify_url = f"{cfg.auth_service_url.rstrip('/')}/auth/verify"
        try:
            resp = await client.post(
                verify_url,
                json={"token": token},
                headers={"X-Request-ID": get_request_id()},
            )
        except httpx.TimeoutException as exc:
            raise ServiceTimeoutError("auth-service timed out during token verification") from exc
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("auth-service is unavailable") from exc

        if resp.status_code == 200:
            data = resp.json()
            return UserContext(user_id=str(data["user_id"]), role=str(data["role"]))

        err_msg = "Invalid or expired token"
        err_code = "UNAUTHORIZED"
        try:
            err_body = resp.json().get("error", {})
            err_msg = err_body.get("message", err_msg)
            err_code = err_body.get("code", err_code)
        except Exception:
            pass
        raise UnauthorizedError(err_msg, error_code=err_code)

    async def _forward_with_auth(
        request: Request,
        *,
        base_url: str,
        target_path: str,
        require_auth: bool = True,
        allowed_roles: set[str] | None = None,
    ) -> Response:
        extra_headers: dict[str, str] = {}
        if require_auth:
            user = await _authenticate(request)
            if allowed_roles is not None:
                require_roles(user, allowed_roles)
            extra_headers = {"X-User-Id": user.user_id, "X-User-Role": user.role}
        return await proxy.forward(
            request,
            target_base_url=base_url,
            target_path=target_path,
            extra_headers=extra_headers,
        )

    @app.get("/health", tags=["Health"], summary="Gateway liveness probe")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": cfg.service_name}

    @app.get("/ready", tags=["Health"], summary="Aggregated readiness probe across all downstream services")
    async def ready() -> JSONResponse:
        async def _probe(name: str, base_url: str) -> tuple[str, str]:
            ok, _, reason = await proxy.fetch_json(f"{base_url.rstrip('/')}/health")
            return name, ("ok" if ok else (reason or "unavailable"))

        results = await asyncio.gather(*(_probe(n, u) for n, u in service_urls.items()))
        checks = dict(results)
        all_ok = all(status == "ok" for status in checks.values())
        return JSONResponse(
            status_code=200 if all_ok else 503,
            content={
                "status": "ready" if all_ok else "degraded",
                "service": cfg.service_name,
                "checks": checks,
            },
        )

    # --- Auth routes ---
    @app.post("/api/auth/register", tags=["Auth"], summary="Register a new user")
    async def auth_register(request: Request) -> Response:
        return await _forward_with_auth(
            request, base_url=cfg.auth_service_url, target_path="/auth/register", require_auth=False
        )

    @app.post("/api/auth/login", tags=["Auth"], summary="Login and obtain JWT pair")
    async def auth_login(request: Request) -> Response:
        return await _forward_with_auth(
            request, base_url=cfg.auth_service_url, target_path="/auth/login", require_auth=False
        )

    @app.post("/api/auth/refresh", tags=["Auth"], summary="Refresh JWT access token")
    async def auth_refresh(request: Request) -> Response:
        return await _forward_with_auth(
            request, base_url=cfg.auth_service_url, target_path="/auth/refresh", require_auth=False
        )

    @app.api_route("/api/auth/{path:path}", methods=["GET", "POST"], tags=["Auth"])
    async def auth_proxy(path: str, request: Request) -> Response:
        return await _forward_with_auth(
            request, base_url=cfg.auth_service_url, target_path=f"/auth/{path}", require_auth=True
        )

    # --- User routes ---
    @app.get("/api/users", tags=["Users"], summary="List all users (admin)")
    async def users_list(request: Request) -> Response:
        return await _forward_with_auth(
            request,
            base_url=cfg.user_service_url,
            target_path="/users",
            require_auth=True,
            allowed_roles={"admin"},
        )

    @app.api_route("/api/users/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], tags=["Users"])
    async def users_proxy(path: str, request: Request) -> Response:
        return await _forward_with_auth(
            request, base_url=cfg.user_service_url, target_path=f"/users/{path}", require_auth=True
        )

    # --- Product routes (GET public, mutations seller/admin) ---
    @app.get("/api/products", tags=["Products"], summary="List catalog products")
    async def products_list(request: Request) -> Response:
        return await _forward_with_auth(
            request, base_url=cfg.product_service_url, target_path="/products", require_auth=False
        )

    @app.post("/api/products", tags=["Products"], summary="Create product (seller/admin)")
    async def products_create(request: Request) -> Response:
        return await _forward_with_auth(
            request,
            base_url=cfg.product_service_url,
            target_path="/products",
            require_auth=True,
            allowed_roles={"seller", "admin"},
        )

    @app.get("/api/products/{product_id}", tags=["Products"], summary="Get product details")
    async def product_get(product_id: str, request: Request) -> Response:
        return await _forward_with_auth(
            request,
            base_url=cfg.product_service_url,
            target_path=f"/products/{product_id}",
            require_auth=False,
        )

    @app.api_route(
        "/api/products/{path:path}",
        methods=["POST", "PUT", "PATCH", "DELETE"],
        tags=["Products"],
    )
    async def products_mutate_proxy(path: str, request: Request) -> Response:
        return await _forward_with_auth(
            request,
            base_url=cfg.product_service_url,
            target_path=f"/products/{path}",
            require_auth=True,
            allowed_roles={"seller", "admin"},
        )

    # --- Order routes ---
    @app.api_route("/api/orders", methods=["GET", "POST"], tags=["Orders"])
    async def orders_root(request: Request) -> Response:
        return await _forward_with_auth(
            request, base_url=cfg.order_service_url, target_path="/orders", require_auth=True
        )

    @app.api_route("/api/orders/{path:path}", methods=["GET", "POST", "PUT", "PATCH"], tags=["Orders"])
    async def orders_proxy(path: str, request: Request) -> Response:
        return await _forward_with_auth(
            request, base_url=cfg.order_service_url, target_path=f"/orders/{path}", require_auth=True
        )

    # --- Payment routes ---
    @app.api_route("/api/payments", methods=["GET", "POST"], tags=["Payments"])
    async def payments_root(request: Request) -> Response:
        return await _forward_with_auth(
            request, base_url=cfg.payment_service_url, target_path="/payments", require_auth=True
        )

    @app.api_route("/api/payments/{path:path}", methods=["GET", "POST"], tags=["Payments"])
    async def payments_proxy(path: str, request: Request) -> Response:
        return await _forward_with_auth(
            request, base_url=cfg.payment_service_url, target_path=f"/payments/{path}", require_auth=True
        )

    # --- Notification routes ---
    @app.api_route("/api/notifications", methods=["GET", "POST"], tags=["Notifications"])
    async def notifications_root(request: Request) -> Response:
        return await _forward_with_auth(
            request,
            base_url=cfg.notification_service_url,
            target_path="/notifications",
            require_auth=True,
        )

    @app.api_route("/api/notifications/{path:path}", methods=["GET", "POST", "PATCH"], tags=["Notifications"])
    async def notifications_proxy(path: str, request: Request) -> Response:
        return await _forward_with_auth(
            request,
            base_url=cfg.notification_service_url,
            target_path=f"/notifications/{path}",
            require_auth=True,
        )

    # --- Aggregated Dashboard with parallel fan-out & graceful degradation ---
    @app.get(
        "/api/dashboard",
        tags=["Aggregation"],
        summary="Aggregate user profile, recent orders, unread notifications, and catalog in parallel",
    )
    async def dashboard(request: Request) -> dict[str, Any]:
        start = time.perf_counter()
        user = await _authenticate(request)
        headers = {"X-User-Id": user.user_id, "X-User-Role": user.role}

        calls = [
            ("user-service", f"{cfg.user_service_url.rstrip('/')}/users/{user.user_id}"),
            ("order-service", f"{cfg.order_service_url.rstrip('/')}/orders?limit=10"),
            (
                "notification-service",
                f"{cfg.notification_service_url.rstrip('/')}/notifications/unread-count",
            ),
            ("product-service", f"{cfg.product_service_url.rstrip('/')}/products?in_stock=true&limit=6"),
        ]

        results = await asyncio.gather(
            *(proxy.fetch_json(url, headers=headers) for _, url in calls),
            return_exceptions=False,
        )

        degraded: list[str] = []
        payloads: dict[str, Any] = {}
        for (svc_name, _), (ok, data, reason) in zip(calls, results):
            if ok:
                payloads[svc_name] = data
            else:
                payloads[svc_name] = None
                degraded.append(svc_name)
                logger.warning(
                    "Downstream service degraded during dashboard aggregation",
                    extra={"downstream_service": svc_name, "reason": reason},
                )

        notif_data = payloads.get("notification-service")
        unread_count = notif_data.get("unread_count") if isinstance(notif_data, dict) else None

        return {
            "user_id": user.user_id,
            "role": user.role,
            "profile": payloads.get("user-service"),
            "recent_orders": payloads.get("order-service"),
            "unread_notifications": unread_count,
            "featured_products": payloads.get("product-service"),
            "degraded_services": degraded,
            "aggregation_latency_ms": round((time.perf_counter() - start) * 1000, 2),
        }

    app.state.rate_limiter = limiter
    app.state.settings = cfg
    return app
