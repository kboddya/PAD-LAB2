"""Tests for product-service (CRUD, filtering, stock reserve/release, Saga compensation)."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_product_crud_filtering_and_rbac(cluster) -> None:
    products: httpx.AsyncClient = cluster.service_clients["product-service"]
    seller_headers = {"X-User-Id": "seller-1", "X-User-Role": "seller"}
    customer_headers = {"X-User-Id": "cust-1", "X-User-Role": "customer"}

    # Customer cannot create product
    denied = await products.post(
        "/products",
        headers=customer_headers,
        json={"sku": "SKU-1", "name": "Mouse", "category": "peripherals", "price": 25.0, "stock_quantity": 5},
    )
    assert denied.status_code == 403

    p1 = await products.post(
        "/products",
        headers=seller_headers,
        json={
            "sku": "lap-001",
            "name": "Pro Laptop 14",
            "description": "High performance developer laptop",
            "category": "Electronics",
            "price": 1499.99,
            "stock_quantity": 4,
        },
    )
    assert p1.status_code == 201
    assert p1.json()["sku"] == "LAP-001"
    assert p1.json()["available_quantity"] == 4

    # Duplicate SKU returns 409
    dup = await products.post(
        "/products",
        headers=seller_headers,
        json={"sku": "LAP-001", "name": "Dup", "category": "Electronics", "price": 10.0, "stock_quantity": 1},
    )
    assert dup.status_code == 409

    # Create out-of-stock item
    await products.post(
        "/products",
        headers=seller_headers,
        json={"sku": "BOOK-01", "name": "Microservices Guide", "category": "Books", "price": 39.0, "stock_quantity": 0},
    )

    in_stock = await products.get("/products", params={"in_stock": True})
    assert in_stock.status_code == 200
    assert len(in_stock.json()) == 1
    assert in_stock.json()[0]["sku"] == "LAP-001"

    search = await products.get("/products", params={"search": "Microservices"})
    assert len(search.json()) == 1
    assert search.json()[0]["category"] == "books"


@pytest.mark.asyncio
async def test_inventory_reservation_idempotency_and_saga_events(cluster) -> None:
    products: httpx.AsyncClient = cluster.service_clients["product-service"]
    seller_headers = {"X-User-Id": "seller-1", "X-User-Role": "seller"}

    created = await products.post(
        "/products",
        headers=seller_headers,
        json={"sku": "KB-100", "name": "Mechanical Keyboard", "category": "peripherals", "price": 120.0, "stock_quantity": 5},
    )
    pid = created.json()["id"]

    # Reserve 2 units for order ord-1 (idempotent if retried)
    r1 = await products.post(f"/products/{pid}/reserve", json={"quantity": 2, "order_id": "ord-1"})
    assert r1.status_code == 200
    assert r1.json()["reserved_quantity"] == 2
    assert r1.json()["available_quantity"] == 3

    r1_retry = await products.post(f"/products/{pid}/reserve", json={"quantity": 2, "order_id": "ord-1"})
    assert r1_retry.status_code == 200
    assert r1_retry.json()["reserved_quantity"] == 2  # not doubled

    # Exceeding available stock fails with 409
    over = await products.post(f"/products/{pid}/reserve", json={"quantity": 4, "order_id": "ord-2"})
    assert over.status_code == 409

    # payment.failed triggers Saga compensation releasing ord-1 reservation
    await cluster.bus.publish("payment.failed", {"order_id": "ord-1", "reason": "card declined"}, "payment-service")
    after_fail = await products.get(f"/products/{pid}")
    assert after_fail.json()["reserved_quantity"] == 0
    assert after_fail.json()["stock_quantity"] == 5

    # Reserve 3 units for ord-3, then payment.succeeded commits them
    await products.post(f"/products/{pid}/reserve", json={"quantity": 3, "order_id": "ord-3"})
    await cluster.bus.publish("payment.succeeded", {"order_id": "ord-3"}, "payment-service")
    after_paid = await products.get(f"/products/{pid}")
    assert after_paid.json()["reserved_quantity"] == 0
    assert after_paid.json()["stock_quantity"] == 2
    assert after_paid.json()["available_quantity"] == 2
