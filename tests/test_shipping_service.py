"""Tests for shipping-service (tariff quotes, auto-shipments, tracking, state machine)."""

from __future__ import annotations

import pytest

from tests.conftest import register_user_via_gateway


@pytest.mark.asyncio
async def test_shipping_quotes_lifecycle_and_order_sync(cluster) -> None:
    gw = cluster.gateway_client
    assert gw is not None

    # Quote comparisons across tiers & methods
    std_low = (
        await gw.post(
            "/api/shipping/quotes",
            json={"shipping_method": "standard", "items_count": 2, "order_subtotal": 40.0, "loyalty_tier": "standard"},
        )
    ).json()
    assert std_low["cost"] == 9.49
    assert std_low["free_shipping_applied"] is False

    std_gold = (
        await gw.post(
            "/api/shipping/quotes",
            json={"shipping_method": "standard", "items_count": 2, "order_subtotal": 40.0, "loyalty_tier": "gold"},
        )
    ).json()
    assert std_gold["cost"] == 0.0
    assert std_gold["free_shipping_applied"] is True

    exp_std = (
        await gw.post(
            "/api/shipping/quotes",
            json={"shipping_method": "express", "items_count": 1, "order_subtotal": 50.0, "loyalty_tier": "standard"},
        )
    ).json()
    exp_gold = (
        await gw.post(
            "/api/shipping/quotes",
            json={"shipping_method": "express", "items_count": 1, "order_subtotal": 50.0, "loyalty_tier": "gold"},
        )
    ).json()
    assert exp_gold["cost"] == round(exp_std["cost"] * 0.5, 2)

    # End-to-end order -> auto shipment -> dispatch -> deliver -> order status sync
    seller = await register_user_via_gateway(gw, email="shsell@shop.io", username="shsell", role="seller")
    buyer = await register_user_via_gateway(gw, email="shbuy@shop.io", username="shbuy", role="customer")
    s_hdr = {"Authorization": f"Bearer {seller['access_token']}"}
    b_hdr = {"Authorization": f"Bearer {buyer['access_token']}"}

    prod = (
        await gw.post(
            "/api/products",
            headers=s_hdr,
            json={"sku": "SHIP-1", "name": "Desk Lamp", "category": "home", "price": 60.0, "stock_quantity": 5},
        )
    ).json()

    order = (
        await gw.post(
            "/api/orders",
            headers=b_hdr,
            json={
                "items": [{"product_id": prod["id"], "quantity": 1}],
                "shipping_address": "Bd. Moscova 10, Chisinau",
                "shipping_method": "express",
            },
        )
    ).json()
    assert order["status"] == "paid"

    shipment_resp = await gw.get(f"/api/shipments/by-order/{order['id']}", headers=b_hdr)
    assert shipment_resp.status_code == 200
    shipment = shipment_resp.json()
    assert shipment["status"] == "preparing"
    assert shipment["tracking_number"].startswith("SF-TRK-")
    assert shipment["shipping_method"] == "express"

    # Public tracking endpoint works without JWT and hides PII
    track = await gw.get(f"/api/shipments/track/{shipment['tracking_number']}")
    assert track.status_code == 200
    assert "user_id" not in track.json()
    assert "shipping_address" not in track.json()

    # Invalid transition preparing -> delivered rejected with 409
    bad_jump = await gw.post(
        f"/api/shipments/{shipment['id']}/status",
        headers=s_hdr,
        json={"status": "delivered", "location": "Chisinau"},
    )
    assert bad_jump.status_code == 409

    # Valid transition preparing -> dispatched -> delivered updates order-service
    disp = await gw.post(
        f"/api/shipments/{shipment['id']}/status",
        headers=s_hdr,
        json={"status": "dispatched", "location": "Chisinau Hub"},
    )
    assert disp.status_code == 200
    assert (await gw.get(f"/api/orders/{order['id']}", headers=b_hdr)).json()["status"] == "shipped"

    deliv = await gw.post(
        f"/api/shipments/{shipment['id']}/status",
        headers=s_hdr,
        json={"status": "delivered", "location": "Recipient Door"},
    )
    assert deliv.status_code == 200
    assert len(deliv.json()["checkpoints"]) == 3
    assert (await gw.get(f"/api/orders/{order['id']}", headers=b_hdr)).json()["status"] == "delivered"
