"""Shared pytest fixtures and ASGI routing transport for ShopFlow microservice tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from analytics_service.config import AnalyticsSettings
from analytics_service.main import create_app as create_analytics_app
from auth_service.config import AuthSettings
from auth_service.main import create_app as create_auth_app
from cart_service.config import CartSettings
from cart_service.main import create_app as create_cart_app
from gateway_app.config import GatewaySettings
from gateway_app.main import create_app as create_gateway_app
from notification_service.config import NotificationSettings
from notification_service.main import create_app as create_notification_app
from order_service.config import OrderSettings
from order_service.main import create_app as create_order_app
from payment_service.config import PaymentSettings
from payment_service.main import create_app as create_payment_app
from product_service.config import ProductSettings
from product_service.main import create_app as create_product_app
from promotion_service.config import PromotionSettings
from promotion_service.main import create_app as create_promotion_app
from review_service.config import ReviewSettings
from review_service.main import create_app as create_review_app
from shared.messaging import InMemoryEventBus
from shipping_service.config import ShippingSettings
from shipping_service.main import create_app as create_shipping_app
from support_service.config import SupportSettings
from support_service.main import create_app as create_support_app
from user_service.config import UserSettings
from user_service.main import create_app as create_user_app


class ServiceRouterTransport(httpx.AsyncBaseTransport):
    """Routes outbound HTTP calls to in-process ASGI apps by service hostname."""

    def __init__(self, routes: dict[str, FastAPI]) -> None:
        self._transports: dict[str, httpx.ASGITransport] = {
            host: httpx.ASGITransport(app=app, raise_app_exceptions=False)
            for host, app in routes.items()
        }
        self.faults: dict[str, str] = {}
        self.delays: dict[str, float] = {}

    def register(self, host: str, app: FastAPI) -> None:
        self._transports[host] = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        fault = self.faults.get(host)
        if fault == "connect_error":
            raise httpx.ConnectError(f"Simulated connection error for {host}", request=request)
        if fault == "timeout":
            raise httpx.ReadTimeout(f"Simulated timeout for {host}", request=request)

        delay = self.delays.get(host, 0.0)
        if delay > 0:
            await asyncio.sleep(delay)

        transport = self._transports.get(host)
        if transport is None:
            raise httpx.ConnectError(f"Unknown service host: {host}", request=request)
        return await transport.handle_async_request(request)

    async def aclose(self) -> None:
        for transport in self._transports.values():
            await transport.aclose()


@dataclass
class ShopFlowCluster:
    """In-process cluster of all 12 microservices + API Gateway for integration & E2E tests."""

    bus: InMemoryEventBus
    transport: ServiceRouterTransport
    apps: dict[str, FastAPI]
    gateway_app: FastAPI
    service_clients: dict[str, httpx.AsyncClient] = field(default_factory=dict)
    gateway_client: httpx.AsyncClient | None = None
    extra_clients: list[httpx.AsyncClient] = field(default_factory=list)

    async def aclose(self) -> None:
        if self.gateway_client is not None:
            await self.gateway_client.aclose()
        for client in self.extra_clients:
            await client.aclose()
        for client in self.service_clients.values():
            await client.aclose()
        await self.transport.aclose()


INTERNAL_SERVICE_TOKEN = "test-internal-service-token-32chars"


def internal_headers(user_id: str = "system", role: str = "admin") -> dict[str, str]:
    """Build trusted service-to-service headers with valid X-Internal-Token."""
    return {
        "X-User-Id": user_id,
        "X-User-Role": role,
        "X-Internal-Token": INTERNAL_SERVICE_TOKEN,
    }


def build_test_cluster(
    db_dir: Path,
    *,
    rate_limit_requests: int = 100,
    rate_limit_window_seconds: int = 60,
    trust_proxy_headers: bool = False,
    log_json: bool = True,
    log_level: str = "INFO",
) -> ShopFlowCluster:
    """Instantiate all 12 microservices + gateway wired via InMemoryEventBus and ASGITransport."""
    bus = InMemoryEventBus()
    jwt_secret = "test-secret-key-with-at-least-32-chars"

    def _base_kwargs(db_name: str) -> dict[str, Any]:
        return {
            "database_url": f"sqlite:///{db_dir / db_name}",
            "rabbitmq_url": "memory://",
            "jwt_secret": jwt_secret,
            "internal_service_token": INTERNAL_SERVICE_TOKEN,
            "log_json": log_json,
            "log_level": log_level,
        }

    apps: dict[str, FastAPI] = {
        "auth-service": create_auth_app(settings=AuthSettings(**_base_kwargs("auth.db")), event_bus=bus),
        "user-service": create_user_app(settings=UserSettings(**_base_kwargs("user.db")), event_bus=bus),
        "product-service": create_product_app(settings=ProductSettings(**_base_kwargs("product.db")), event_bus=bus),
        "payment-service": create_payment_app(
            settings=PaymentSettings(**_base_kwargs("payment.db"), max_single_payment_amount=5000.0),
            event_bus=bus,
        ),
        "notification-service": create_notification_app(
            settings=NotificationSettings(**_base_kwargs("notification.db")), event_bus=bus
        ),
        "promotion-service": create_promotion_app(
            settings=PromotionSettings(**_base_kwargs("promotion.db")), event_bus=bus
        ),
        "shipping-service": create_shipping_app(
            settings=ShippingSettings(**_base_kwargs("shipping.db")), event_bus=bus
        ),
        "analytics-service": create_analytics_app(
            settings=AnalyticsSettings(**_base_kwargs("analytics.db")), event_bus=bus
        ),
    }

    router_transport = ServiceRouterTransport(apps)
    extra_clients: list[httpx.AsyncClient] = []

    def _new_downstream_client() -> httpx.AsyncClient:
        c = httpx.AsyncClient(transport=router_transport, timeout=5.0)
        extra_clients.append(c)
        return c

    order_app = create_order_app(
        settings=OrderSettings(**_base_kwargs("order.db")),
        event_bus=bus,
        http_client=_new_downstream_client(),
    )
    apps["order-service"] = order_app
    router_transport.register("order-service", order_app)

    cart_app = create_cart_app(
        settings=CartSettings(**_base_kwargs("cart.db")),
        event_bus=bus,
        http_client=_new_downstream_client(),
    )
    apps["cart-service"] = cart_app
    router_transport.register("cart-service", cart_app)

    review_app = create_review_app(
        settings=ReviewSettings(**_base_kwargs("review.db")),
        event_bus=bus,
        http_client=_new_downstream_client(),
    )
    apps["review-service"] = review_app
    router_transport.register("review-service", review_app)

    support_app = create_support_app(
        settings=SupportSettings(**_base_kwargs("support.db")),
        event_bus=bus,
        http_client=_new_downstream_client(),
    )
    apps["support-service"] = support_app
    router_transport.register("support-service", support_app)

    gateway_app = create_gateway_app(
        settings=GatewaySettings(
            jwt_secret=jwt_secret,
            internal_service_token=INTERNAL_SERVICE_TOKEN,
            rate_limit_requests=rate_limit_requests,
            rate_limit_window_seconds=rate_limit_window_seconds,
            trust_proxy_headers=trust_proxy_headers,
            log_json=log_json,
            log_level=log_level,
        ),
        http_client=_new_downstream_client(),
    )

    service_clients = {
        name: httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url=f"http://{name}",
        )
        for name, app in apps.items()
    }
    gateway_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway_app, raise_app_exceptions=False),
        base_url="http://api-gateway",
    )

    return ShopFlowCluster(
        bus=bus,
        transport=router_transport,
        apps=apps,
        gateway_app=gateway_app,
        service_clients=service_clients,
        gateway_client=gateway_client,
        extra_clients=extra_clients,
    )


@pytest.fixture
async def cluster(tmp_path: Path) -> ShopFlowCluster:
    """Provide an isolated full ShopFlow cluster per test function."""
    built = build_test_cluster(tmp_path)
    try:
        yield built
    finally:
        await built.aclose()


async def register_user_via_gateway(
    client: httpx.AsyncClient,
    *,
    email: str,
    username: str,
    password: str = "secret123",
    role: str = "customer",
    full_name: str | None = None,
) -> dict[str, Any]:
    """Helper to register a user via API Gateway and return the TokenResponse payload."""
    resp = await client.post(
        "/api/auth/register",
        json={
            "email": email,
            "username": username,
            "password": password,
            "role": role,
            "full_name": full_name or username,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()
