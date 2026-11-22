"""Shared pytest fixtures and ASGI routing transport for ShopFlow microservice tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from auth_service.config import AuthSettings
from auth_service.main import create_app as create_auth_app
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
from shared.messaging import InMemoryEventBus
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
    """In-process cluster of all 6 microservices + API Gateway for integration & E2E tests."""

    bus: InMemoryEventBus
    transport: ServiceRouterTransport
    apps: dict[str, FastAPI]
    gateway_app: FastAPI
    service_clients: dict[str, httpx.AsyncClient] = field(default_factory=dict)
    gateway_client: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        if self.gateway_client is not None:
            await self.gateway_client.aclose()
        for client in self.service_clients.values():
            await client.aclose()
        await self.transport.aclose()


def build_test_cluster(
    db_dir: Path,
    *,
    rate_limit_requests: int = 100,
    rate_limit_window_seconds: int = 60,
    log_json: bool = True,
) -> ShopFlowCluster:
    """Instantiate all 6 microservices + gateway wired via InMemoryEventBus and ASGITransport."""
    bus = InMemoryEventBus()
    jwt_secret = "test-secret-key-with-at-least-32-chars"

    auth_app = create_auth_app(
        settings=AuthSettings(
            database_url=f"sqlite:///{db_dir / 'auth.db'}",
            rabbitmq_url="memory://",
            jwt_secret=jwt_secret,
            log_json=log_json,
        ),
        event_bus=bus,
    )
    user_app = create_user_app(
        settings=UserSettings(
            database_url=f"sqlite:///{db_dir / 'user.db'}",
            rabbitmq_url="memory://",
            jwt_secret=jwt_secret,
            log_json=log_json,
        ),
        event_bus=bus,
    )
    product_app = create_product_app(
        settings=ProductSettings(
            database_url=f"sqlite:///{db_dir / 'product.db'}",
            rabbitmq_url="memory://",
            jwt_secret=jwt_secret,
            log_json=log_json,
        ),
        event_bus=bus,
    )
    payment_app = create_payment_app(
        settings=PaymentSettings(
            database_url=f"sqlite:///{db_dir / 'payment.db'}",
            rabbitmq_url="memory://",
            jwt_secret=jwt_secret,
            log_json=log_json,
            max_single_payment_amount=5000.0,
        ),
        event_bus=bus,
    )
    notification_app = create_notification_app(
        settings=NotificationSettings(
            database_url=f"sqlite:///{db_dir / 'notification.db'}",
            rabbitmq_url="memory://",
            jwt_secret=jwt_secret,
            log_json=log_json,
        ),
        event_bus=bus,
    )

    apps: dict[str, FastAPI] = {
        "auth-service": auth_app,
        "user-service": user_app,
        "product-service": product_app,
        "payment-service": payment_app,
        "notification-service": notification_app,
    }

    router_transport = ServiceRouterTransport(apps)
    order_http_client = httpx.AsyncClient(transport=router_transport, timeout=5.0)
    order_app = create_order_app(
        settings=OrderSettings(
            database_url=f"sqlite:///{db_dir / 'order.db'}",
            rabbitmq_url="memory://",
            jwt_secret=jwt_secret,
            log_json=log_json,
        ),
        event_bus=bus,
        http_client=order_http_client,
    )
    apps["order-service"] = order_app
    router_transport._transports["order-service"] = httpx.ASGITransport(
        app=order_app,
        raise_app_exceptions=False,
    )

    gateway_http_client = httpx.AsyncClient(transport=router_transport, timeout=5.0)
    gateway_app = create_gateway_app(
        settings=GatewaySettings(
            jwt_secret=jwt_secret,
            rate_limit_requests=rate_limit_requests,
            rate_limit_window_seconds=rate_limit_window_seconds,
            log_json=log_json,
        ),
        http_client=gateway_http_client,
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
