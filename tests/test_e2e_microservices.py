"""End-to-end tests exercising all 12 microservices + API Gateway together."""

from __future__ import annotations

import pytest

from tests.conftest import internal_headers, register_user_via_gateway


@pytest.mark.asyncio
async def test_full_ecommerce_purchase_saga_and_cancellation_e2e(cluster) -> None:
    gw = cluster.gateway_client
    assert gw is not None

    # 1. Gateway /ready confirms all 12 downstream services are healthy
    ready = await gw.get("/ready")
    assert ready.status_code == 200
    assert len(ready.json()["checks"]) == 12

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


@pytest.mark.asyncio
async def test_extended_12_microservice_commerce_lifecycle_e2e(cluster) -> None:
    """Exercise all 12 microservices in one integrated commerce & post-order lifecycle."""
    gw = cluster.gateway_client
    support_client = cluster.service_clients["support-service"]
    assert gw is not None

    seller = await register_user_via_gateway(gw, email="e2e_s@shopflow.io", username="e2e_seller", role="seller")
    buyer = await register_user_via_gateway(gw, email="e2e_b@shopflow.io", username="e2e_buyer", role="customer")
    s_hdr = {"Authorization": f"Bearer {seller['access_token']}"}
    b_hdr = {"Authorization": f"Bearer {buyer['access_token']}"}

    # 1. Seller publishes product & creates promo; buyer receives personal welcome coupon
    laptop = (
        await gw.post(
            "/api/products",
            headers=s_hdr,
            json={"sku": "E2E-PRO-1", "name": "Pro Laptop X", "category": "laptops", "price": 1200.0, "stock_quantity": 2},
        )
    ).json()

    await gw.post(
        "/api/promotions",
        headers=s_hdr,
        json={
            "code": "VIP20",
            "description": "20% off laptops (max $250)",
            "discount_type": "percentage",
            "discount_value": 20.0,
            "min_order_amount": 100.0,
            "max_discount_amount": 250.0,
            "max_uses": 1,
        },
    )
    promos = (await gw.get("/api/promotions", headers=b_hdr)).json()
    assert any(p["code"].startswith("WELCOME-") for p in promos)

    # 2. Cart + Coupon + Shipping Quote
    await gw.post("/api/cart/items", headers=b_hdr, json={"product_id": laptop["id"], "quantity": 1})
    cart = (await gw.post("/api/cart/coupon", headers=b_hdr, json={"code": "VIP20"})).json()
    assert cart["discount_amount"] == 240.0
    assert cart["estimated_total"] == 960.0

    quote = (
        await gw.post(
            "/api/shipping/quotes",
            json={"shipping_method": "express", "items_count": 1, "order_subtotal": 1200.0, "loyalty_tier": "standard"},
        )
    ).json()
    assert quote["cost"] == 19.99

    # 3. Checkout failure rolls back BOTH stock and VIP20 promo quota, keeping cart
    failed = await gw.post(
        "/api/cart/checkout",
        headers=b_hdr,
        json={"shipping_address": "100 Main Ave", "shipping_method": "express", "simulate_payment_failure": True},
    )
    assert failed.status_code == 201
    assert failed.json()["status"] == "payment_failed"
    assert (await gw.get(f"/api/products/{laptop['id']}")).json()["stock_quantity"] == 2

    # 4. Successful checkout clears cart, creates shipment, and auto-upgrades loyalty tier to silver
    paid = (
        await gw.post(
            "/api/cart/checkout",
            headers=b_hdr,
            json={"shipping_address": "100 Main Ave", "shipping_method": "express"},
        )
    ).json()
    assert paid["status"] == "paid"
    assert paid["total_amount"] == 979.99
    assert (await gw.get("/api/cart", headers=b_hdr)).json()["items_count"] == 0
    assert (await gw.get(f"/api/users/{buyer['user']['id']}", headers=b_hdr)).json()["loyalty_tier"] == "silver"

    # 5. Fulfillment delivery advances order to delivered
    shipment = (await gw.get(f"/api/shipments/by-order/{paid['id']}", headers=b_hdr)).json()
    await gw.post(
        f"/api/shipments/{shipment['id']}/status",
        headers=s_hdr,
        json={"status": "dispatched", "location": "Central Hub"},
    )
    await gw.post(
        f"/api/shipments/{shipment['id']}/status",
        headers=s_hdr,
        json={"status": "delivered", "location": "100 Main Ave"},
    )
    assert (await gw.get(f"/api/orders/{paid['id']}", headers=b_hdr)).json()["status"] == "delivered"

    # 6. Verified purchase review updates product rating
    rev = await gw.post(
        "/api/reviews",
        headers=b_hdr,
        json={"product_id": laptop["id"], "rating": 5, "title": "Blazing fast", "comment": "Worth every cent!"},
    )
    assert rev.status_code == 201
    assert (await gw.get(f"/api/products/{laptop['id']}")).json()["average_rating"] == 5.0

    # 7. Support dispute -> admin refund approval -> analytics reflects refund
    ticket = (
        await gw.post(
            "/api/support/tickets",
            headers=b_hdr,
            json={
                "order_id": paid["id"],
                "category": "refund_request",
                "subject": "Return request",
                "message": "Found dead pixel after delivery, requesting refund.",
            },
        )
    ).json()
    assert ticket["priority"] == "urgent"

    approved = await support_client.post(
        f"/support/tickets/{ticket['id']}/approve-refund",
        headers=internal_headers("admin-1", "admin"),
        json={"resolution_note": "Full refund processed"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "resolved"
    assert (await gw.get(f"/api/orders/{paid['id']}", headers=b_hdr)).json()["status"] == "refunded"

    overview = (await gw.get("/api/analytics/overview", headers=s_hdr)).json()
    assert overview["refunds_total"] == 979.99
    assert overview["net_revenue"] == 0.0

    # 8. Dashboard aggregates all 7 sections cleanly
    dash = (await gw.get("/api/dashboard", headers=b_hdr)).json()
    assert dash["degraded_services"] == []
    assert dash["cart"] is not None
    assert len(dash["recent_shipments"]) == 1
    assert dash["loyalty_summary"]["computed_tier"] == "silver"

