"""Tests for API Gateway (JWT auth, RBAC, rate limiting, aggregation, fault handling)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tests.conftest import build_test_cluster, register_user_via_gateway


@pytest.mark.asyncio
async def test_gateway_auth_rbac_and_dashboard_graceful_degradation(cluster) -> None:
    gw = cluster.gateway_client
    assert gw is not None

    # Public catalog accessible without JWT
    pub = await gw.get("/api/products")
    assert pub.status_code == 200

    # Protected route rejects missing token
    unauth = await gw.get("/api/orders")
    assert unauth.status_code == 401

    # Register seller & customer
    seller = await register_user_via_gateway(gw, email="sell@shop.com", username="seller1", role="seller")
    customer = await register_user_via_gateway(gw, email="cust@shop.com", username="cust1", role="customer")
    seller_hdr = {"Authorization": f"Bearer {seller['access_token']}"}
    cust_hdr = {"Authorization": f"Bearer {customer['access_token']}"}

    # RBAC: customer cannot create product
    forbidden_prod = await gw.post(
        "/api/products",
        headers=cust_hdr,
        json={"sku": "S1", "name": "Gadget", "category": "tech", "price": 50.0, "stock_quantity": 5},
    )
    assert forbidden_prod.status_code == 403

    # Seller can create product
    ok_prod = await gw.post(
        "/api/products",
        headers=seller_hdr,
        json={"sku": "S1", "name": "Gadget", "category": "tech", "price": 50.0, "stock_quantity": 5},
    )
    assert ok_prod.status_code == 201

    # Aggregated dashboard works with all services healthy
    dash_ok = await gw.get("/api/dashboard", headers=cust_hdr)
    assert dash_ok.status_code == 200
    body = dash_ok.json()
    assert body["degraded_services"] == []
    assert body["profile"]["username"] == "cust1"
    assert body["unread_notifications"] == 1  # welcome notification
    assert len(body["featured_products"]) == 1

    # Graceful degradation: drop notification-service
    cluster.transport.faults["notification-service"] = "connect_error"
    dash_degraded = await gw.get("/api/dashboard", headers=cust_hdr)
    assert dash_degraded.status_code == 200
    deg_body = dash_degraded.json()
    assert deg_body["degraded_services"] == ["notification-service"]
    assert deg_body["unread_notifications"] is None
    assert deg_body["profile"]["username"] == "cust1"

    # Gateway /ready reflects partial failure with 503
    ready_degraded = await gw.get("/ready")
    assert ready_degraded.status_code == 503
    assert ready_degraded.json()["checks"]["notification-service"] == "unavailable"


@pytest.mark.asyncio
async def test_gateway_rate_limiting_and_timeout_mapping(tmp_path: Path) -> None:
    limited_cluster = build_test_cluster(
        tmp_path,
        rate_limit_requests=3,
        rate_limit_window_seconds=60,
        trust_proxy_headers=True,
    )
    try:
        gw = limited_cluster.gateway_client
        assert gw is not None

        for idx in range(3):
            resp = await gw.get("/api/products", headers={"X-Real-IP": "10.0.0.42"})
            assert resp.status_code == 200
            assert resp.headers["X-RateLimit-Limit"] == "3"
            assert resp.headers["X-RateLimit-Remaining"] == str(2 - idx)

        blocked = await gw.get("/api/products", headers={"X-Real-IP": "10.0.0.42"})
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
        assert "X-Request-ID" in blocked.headers

        # Timeout fault translation -> 504
        limited_cluster.transport.faults["product-service"] = "timeout"
        timed_out = await gw.get("/api/products", headers={"X-Real-IP": "10.0.0.99"})
        assert timed_out.status_code == 504
        assert timed_out.json()["error"]["code"] == "GATEWAY_TIMEOUT"
    finally:
        await limited_cluster.aclose()
