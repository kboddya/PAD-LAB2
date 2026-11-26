"""Tests for support-service (SLA prioritization, ticket thread RBAC, automated refund approval)."""

from __future__ import annotations

import pytest

from tests.conftest import internal_headers, register_user_via_gateway


@pytest.mark.asyncio
async def test_support_ticket_sla_visibility_and_automated_refund(cluster) -> None:
    gw = cluster.gateway_client
    support_client = cluster.service_clients["support-service"]
    assert gw is not None

    seller = await register_user_via_gateway(gw, email="supsell@shop.io", username="supsell", role="seller")
    buyer = await register_user_via_gateway(gw, email="supbuy@shop.io", username="supbuy", role="customer")
    other = await register_user_via_gateway(gw, email="supoth@shop.io", username="supoth", role="customer")

    s_hdr = {"Authorization": f"Bearer {seller['access_token']}"}
    b_hdr = {"Authorization": f"Bearer {buyer['access_token']}"}
    o_hdr = {"Authorization": f"Bearer {other['access_token']}"}

    prod = (
        await gw.post(
            "/api/products",
            headers=s_hdr,
            json={"sku": "SUP-1", "name": "4K Monitor", "category": "displays", "price": 600.0, "stock_quantity": 3},
        )
    ).json()

    order = (
        await gw.post(
            "/api/orders",
            headers=b_hdr,
            json={"items": [{"product_id": prod["id"], "quantity": 1}], "shipping_address": "5 Victory Sq"},
        )
    ).json()

    # Other customer cannot open ticket against buyer's order (BOLA protection)
    cross_ticket = await gw.post(
        "/api/support/tickets",
        headers=o_hdr,
        json={
            "order_id": order["id"],
            "category": "refund_request",
            "subject": "Hijack attempt",
            "message": "Trying to refund someone else's order",
        },
    )
    assert cross_ticket.status_code == 403

    # High-value refund request ($600 >= $500) gets urgent priority and 2h SLA
    ticket_resp = await gw.post(
        "/api/support/tickets",
        headers=b_hdr,
        json={
            "order_id": order["id"],
            "category": "refund_request",
            "subject": "Dead pixels on screen",
            "message": "Screen arrived with flickering panel, requesting full refund.",
        },
    )
    assert ticket_resp.status_code == 201
    ticket = ticket_resp.json()
    assert ticket["priority"] == "urgent"
    assert ticket["sla_hours"] == 2
    assert ticket["shipment_status"] == "preparing"

    # Customer cannot post internal note
    bad_note = await gw.post(
        f"/api/support/tickets/{ticket['id']}/messages",
        headers=b_hdr,
        json={"message": "Internal fake note", "is_internal_note": True},
    )
    assert bad_note.status_code == 403

    # Seller posts internal note -> hidden from customer view
    await gw.post(
        f"/api/support/tickets/{ticket['id']}/messages",
        headers=s_hdr,
        json={"message": "Verified warehouse damage log", "is_internal_note": True},
    )
    buyer_view = (await gw.get(f"/api/support/tickets/{ticket['id']}", headers=b_hdr)).json()
    assert all(not m["is_internal_note"] for m in buyer_view["messages"])

    # Admin approves refund via support-service -> refunds payment & marks order refunded
    refunded_ticket = await support_client.post(
        f"/support/tickets/{ticket['id']}/approve-refund",
        headers=internal_headers("admin-1", "admin"),
        json={"resolution_note": "Defect confirmed, full refund issued"},
    )
    assert refunded_ticket.status_code == 200
    rt_data = refunded_ticket.json()
    assert rt_data["status"] == "resolved"
    assert rt_data["refund_amount"] == 600.0

    # Payment & Order both reflect refund
    pay = (await gw.get(f"/api/payments/by-order/{order['id']}", headers=b_hdr)).json()
    assert pay["status"] == "refunded"
    ord_after = (await gw.get(f"/api/orders/{order['id']}", headers=b_hdr)).json()
    assert ord_after["status"] == "refunded"
