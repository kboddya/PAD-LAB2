"""Tests for cart-service (live product sync, price drift, coupons, checkout, event cleanup)."""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import register_user_via_gateway


@pytest.mark.asyncio
async def test_cart_live_sync_price_drift_coupon_and_checkout(cluster) -> None:
    gw = cluster.gateway_client
    assert gw is not None

    seller = await register_user_via_gateway(gw, email="csell@shop.io", username="csell", role="seller")
    buyer = await register_user_via_gateway(gw, email="cbuy@shop.io", username="cbuy", role="customer")
    s_hdr = {"Authorization": f"Bearer {seller['access_token']}"}
    b_hdr = {"Authorization": f"Bearer {buyer['access_token']}"}

    prod = (
        await gw.post(
            "/api/products",
            headers=s_hdr,
            json={"sku": "CART-1", "name": "Studio Headphones", "category": "audio", "price": 80.0, "stock_quantity": 4},
        )
    ).json()

    await gw.post(
        "/api/promotions",
        headers=s_hdr,
        json={
            "code": "AUDIO10",
            "description": "10% off audio",
            "discount_type": "percentage",
            "discount_value": 10.0,
            "min_order_amount": 50.0,
            "category_filter": "audio",
        },
    )

    # Exceeding available stock is rejected
    over = await gw.post("/api/cart/items", headers=b_hdr, json={"product_id": prod["id"], "quantity": 10})
    assert over.status_code == 409

    # Add 2 units to server-side cart
    added = await gw.post("/api/cart/items", headers=b_hdr, json={"product_id": prod["id"], "quantity": 2})
    assert added.status_code == 201
    assert added.json()["subtotal"] == 160.0
    assert added.json()["items"][0]["price_changed"] is False

    # Seller changes price -> GET /api/cart detects live price drift
    await gw.put(f"/api/products/{prod['id']}", headers=s_hdr, json={"price": 90.0})
    synced = await gw.get("/api/cart", headers=b_hdr)
    assert synced.status_code == 200
    assert synced.json()["subtotal"] == 180.0
    assert synced.json()["items"][0]["price_changed"] is True

    # Apply coupon and check discount calculation
    with_coupon = await gw.post("/api/cart/coupon", headers=b_hdr, json={"code": "AUDIO10"})
    assert with_coupon.status_code == 200
    assert with_coupon.json()["coupon_valid"] is True
    assert with_coupon.json()["discount_amount"] == 18.0
    assert with_coupon.json()["estimated_total"] == 162.0

    # Simulated payment failure preserves cart items so buyer can retry
    failed_checkout = await gw.post(
        "/api/cart/checkout",
        headers=b_hdr,
        json={
            "shipping_address": "15 Stefan cel Mare, Chisinau",
            "shipping_method": "express",
            "simulate_payment_failure": True,
        },
    )
    assert failed_checkout.status_code == 201
    assert failed_checkout.json()["status"] == "payment_failed"
    still_in_cart = await gw.get("/api/cart", headers=b_hdr)
    assert still_in_cart.json()["items_count"] == 2

    # Successful checkout clears cart and applies coupon + express shipping
    ok_checkout = await gw.post(
        "/api/cart/checkout",
        headers=b_hdr,
        json={
            "shipping_address": "15 Stefan cel Mare, Chisinau",
            "shipping_method": "express",
        },
    )
    assert ok_checkout.status_code == 201
    order_data = ok_checkout.json()
    assert order_data["status"] == "paid"
    assert order_data["subtotal_amount"] == 180.0
    assert order_data["discount_amount"] == 18.0
    assert order_data["shipping_cost"] > 0.0

    empty_cart = await gw.get("/api/cart", headers=b_hdr)
    assert empty_cart.json()["items_count"] == 0
    assert empty_cart.json()["coupon_code"] is None


@pytest.mark.asyncio
async def test_cart_purges_deactivated_products_via_event(cluster) -> None:
    gw = cluster.gateway_client
    assert gw is not None

    seller = await register_user_via_gateway(gw, email="csell2@shop.io", username="csell2", role="seller")
    buyer = await register_user_via_gateway(gw, email="cbuy2@shop.io", username="cbuy2", role="customer")
    s_hdr = {"Authorization": f"Bearer {seller['access_token']}"}
    b_hdr = {"Authorization": f"Bearer {buyer['access_token']}"}

    prod = (
        await gw.post(
            "/api/products",
            headers=s_hdr,
            json={"sku": "PURGE-1", "name": "USB Hub", "category": "tech", "price": 25.0, "stock_quantity": 5},
        )
    ).json()

    await gw.post("/api/cart/items", headers=b_hdr, json={"product_id": prod["id"], "quantity": 1})
    await gw.delete(f"/api/products/{prod['id']}", headers=s_hdr)

    cart_after: httpx.Response = await gw.get("/api/cart", headers=b_hdr)
    assert cart_after.status_code == 200
    assert cart_after.json()["items"] == []
