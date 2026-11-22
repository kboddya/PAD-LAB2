"""Tests for order-service (sync orchestration, partial rollback, Saga compensation)."""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import internal_headers


async def _seed_user_and_products(cluster):
    users = cluster.service_clients["user-service"]
    products = cluster.service_clients["product-service"]

    await users.post(
        "/users",
        headers=internal_headers("system", "admin"),
        json={"user_id": "buyer-1", "email": "buyer1@example.com", "username": "buyer1", "full_name": "Buyer One"},
    )
    seller_headers = internal_headers("seller-1", "seller")
    p1 = (
        await products.post(
            "/products",
            headers=seller_headers,
            json={"sku": "ITEM-A", "name": "Item A", "category": "gear", "price": 40.0, "stock_quantity": 5},
        )
    ).json()
    p2 = (
        await products.post(
            "/products",
            headers=seller_headers,
            json={"sku": "ITEM-B", "name": "Item B", "category": "gear", "price": 60.0, "stock_quantity": 1},
        )
    ).json()
    return p1, p2


@pytest.mark.asyncio
async def test_order_creation_happy_path_and_cancel(cluster) -> None:
    p1, _ = await _seed_user_and_products(cluster)
    orders: httpx.AsyncClient = cluster.service_clients["order-service"]
    products: httpx.AsyncClient = cluster.service_clients["product-service"]
    buyer_headers = internal_headers("buyer-1", "customer")

    created = await orders.post(
        "/orders",
        headers=buyer_headers,
        json={
            "items": [{"product_id": p1["id"], "quantity": 2}],
            "shipping_address": "10 Main St, Chisinau",
        },
    )
    assert created.status_code == 201
    order_data = created.json()
    assert order_data["status"] == "paid"
    assert order_data["total_amount"] == 80.0
    assert order_data["payment_id"]

    # Stock committed
    prod_after = (await products.get(f"/products/{p1['id']}")).json()
    assert prod_after["stock_quantity"] == 3
    assert prod_after["reserved_quantity"] == 0

    # Cancel order restores stock via order.cancelled event
    cancelled = await orders.post(f"/orders/{order_data['id']}/cancel", headers=buyer_headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    prod_restored = (await products.get(f"/products/{p1['id']}")).json()
    assert prod_restored["stock_quantity"] == 5


@pytest.mark.asyncio
async def test_order_partial_stock_failure_rolls_back_first_item(cluster) -> None:
    p1, p2 = await _seed_user_and_products(cluster)
    orders: httpx.AsyncClient = cluster.service_clients["order-service"]
    products: httpx.AsyncClient = cluster.service_clients["product-service"]
    buyer_headers = internal_headers("buyer-1", "customer")

    # Request 2 of ITEM-A (ok) and 5 of ITEM-B (only 1 in stock -> 409)
    resp = await orders.post(
        "/orders",
        headers=buyer_headers,
        json={
            "items": [
                {"product_id": p1["id"], "quantity": 2},
                {"product_id": p2["id"], "quantity": 5},
            ],
            "shipping_address": "10 Main St",
        },
    )
    assert resp.status_code == 409

    # ITEM-A reservation must have been rolled back
    p1_after = (await products.get(f"/products/{p1['id']}")).json()
    assert p1_after["reserved_quantity"] == 0
    assert p1_after["available_quantity"] == 5


@pytest.mark.asyncio
async def test_order_compensates_when_payment_service_unavailable(cluster) -> None:
    p1, _ = await _seed_user_and_products(cluster)
    orders: httpx.AsyncClient = cluster.service_clients["order-service"]
    products: httpx.AsyncClient = cluster.service_clients["product-service"]
    buyer_headers = internal_headers("buyer-1", "customer")

    cluster.transport.faults["payment-service"] = "connect_error"
    resp = await orders.post(
        "/orders",
        headers=buyer_headers,
        json={"items": [{"product_id": p1["id"], "quantity": 2}], "shipping_address": "10 Main St"},
    )
    assert resp.status_code == 503

    p1_after = (await products.get(f"/products/{p1['id']}")).json()
    assert p1_after["reserved_quantity"] == 0
    assert p1_after["available_quantity"] == 5
