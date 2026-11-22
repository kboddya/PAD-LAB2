"""End-to-end tests exercising all 6 microservices + API Gateway together."""

from __future__ import annotations

import pytest

from tests.conftest import register_user_via_gateway


@pytest.mark.asyncio
async def test_full_ecommerce_purchase_saga_and_cancellation_e2e(cluster) -> None:
    gw = cluster.gateway_client
    assert gw is not None

    # 1. Gateway /ready confirms all 6 downstream services are healthy
    ready = await gw.get("/ready")
    assert ready.status_code == 200
    assert len(ready.json()["checks"]) == 6

    # 2. Register seller and publish catalog items
    seller = await register_user_via_gateway(
        gw,
        email="merchant@shopflow.io",
        username="merchant",
        role="seller",
        full_name="ShopFlow Merchant",
    )
    seller_hdr = {"Authorization": f"Bearer {seller['access_token']}"}

    laptop = (
        await gw.post(
            "/api/products",
            headers=seller_hdr,
            json={
                "sku": "SF-LAP-15",
                "name": "ShopFlow Ultrabook 15",
                "description": "Developer workstation",
                "category": "laptops",
                "price": 1200.0,
                "stock_quantity": 3,
            },
        )
    ).json()

    mouse = (
        await gw.post(
            "/api/products",
            headers=seller_hdr,
            json={
                "sku": "SF-MSE-01",
                "name": "Wireless Precision Mouse",
                "description": "Ergonomic wireless mouse",
                "category": "accessories",
                "price": 45.0,
                "stock_quantity": 10,
            },
        )
    ).json()

    # 3. Register customer -> auto profile & welcome notification -> add address
    customer = await register_user_via_gateway(
        gw,
        email="buyer@shopflow.io",
        username="alice_buyer",
        role="customer",
        full_name="Alice Buyer",
    )
    cust_id = customer["user"]["id"]
    cust_hdr = {"Authorization": f"Bearer {customer['access_token']}"}

    addr_resp = await gw.post(
        f"/api/users/{cust_id}/addresses",
        headers=cust_hdr,
        json={
            "label": "Home",
            "country": "Moldova",
            "city": "Chisinau",
            "street": "Bd. Dacia 24",
            "postal_code": "MD-2032",
            "is_default": True,
        },
    )
    assert addr_resp.status_code == 201

    # 4. Happy path purchase: order 2 mice
    order_ok = await gw.post(
        "/api/orders",
        headers=cust_hdr,
        json={
            "items": [{"product_id": mouse["id"], "quantity": 2}],
            "shipping_address": "Bd. Dacia 24, Chisinau",
        },
    )
    assert order_ok.status_code == 201
    ok_data = order_ok.json()
    assert ok_data["status"] == "paid"
    assert ok_data["total_amount"] == 90.0

    mouse_after = (await gw.get(f"/api/products/{mouse['id']}")).json()
    assert mouse_after["stock_quantity"] == 8
    assert mouse_after["available_quantity"] == 8

    # 5. Saga compensation: order 1 laptop with simulated payment failure
    order_fail = await gw.post(
        "/api/orders",
        headers=cust_hdr,
        json={
            "items": [{"product_id": laptop["id"], "quantity": 1}],
            "shipping_address": "Bd. Dacia 24, Chisinau",
            "simulate_payment_failure": True,
        },
    )
    assert order_fail.status_code == 201
    fail_data = order_fail.json()
    assert fail_data["status"] == "payment_failed"

    # Verify laptop inventory remained completely unconsumed after Saga compensation
    laptop_after_fail = (await gw.get(f"/api/products/{laptop['id']}")).json()
    assert laptop_after_fail["stock_quantity"] == 3
    assert laptop_after_fail["reserved_quantity"] == 0
    assert laptop_after_fail["available_quantity"] == 3

    # 6. Order cancellation after successful payment restores stock
    order_cancel_target = (
        await gw.post(
            "/api/orders",
            headers=cust_hdr,
            json={
                "items": [{"product_id": laptop["id"], "quantity": 1}],
                "shipping_address": "Bd. Dacia 24, Chisinau",
            },
        )
    ).json()
    assert order_cancel_target["status"] == "paid"
    assert (await gw.get(f"/api/products/{laptop['id']}")).json()["stock_quantity"] == 2

    cancelled = await gw.post(f"/api/orders/{order_cancel_target['id']}/cancel", headers=cust_hdr)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert (await gw.get(f"/api/products/{laptop['id']}")).json()["stock_quantity"] == 3

    # 7. Dashboard & Notifications reflect the entire history
    dashboard = (await gw.get("/api/dashboard", headers=cust_hdr)).json()
    assert dashboard["degraded_services"] == []
    assert len(dashboard["recent_orders"]) == 3
    assert dashboard["unread_notifications"] >= 6

    notifs = (await gw.get("/api/notifications", headers=cust_hdr)).json()
    categories = {n["category"] for n in notifs}
    assert {"welcome", "order_created", "payment_succeeded", "payment_failed", "order_cancelled"}.issubset(
        categories
    )

    # 8. Logout invalidates token at API Gateway
    logout = await gw.post("/api/auth/logout", headers=cust_hdr)
    assert logout.status_code == 200
    after_logout = await gw.get("/api/dashboard", headers=cust_hdr)
    assert after_logout.status_code == 401
