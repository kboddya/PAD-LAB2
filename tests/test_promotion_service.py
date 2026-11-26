"""Tests for promotion-service (rules, welcome coupons, idempotent redeem, Saga rollback)."""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import internal_headers, register_user_via_gateway


@pytest.mark.asyncio
async def test_promotion_rules_welcome_coupon_and_saga_lifecycle(cluster) -> None:
    gw = cluster.gateway_client
    promos: httpx.AsyncClient = cluster.service_clients["promotion-service"]
    assert gw is not None

    seller = await register_user_via_gateway(gw, email="psell@shop.io", username="psell", role="seller")
    buyer1 = await register_user_via_gateway(gw, email="pbuy1@shop.io", username="pbuy1", role="customer")
    buyer2 = await register_user_via_gateway(gw, email="pbuy2@shop.io", username="pbuy2", role="customer")

    s_hdr = {"Authorization": f"Bearer {seller['access_token']}"}
    b1_hdr = {"Authorization": f"Bearer {buyer1['access_token']}"}
    b2_hdr = {"Authorization": f"Bearer {buyer2['access_token']}"}

    # Welcome coupon automatically provisioned for buyer1 on registration
    b1_promos = (await gw.get("/api/promotions", headers=b1_hdr)).json()
    welcome = next((p for p in b1_promos if p["code"].startswith("WELCOME-")), None)
    assert welcome is not None
    assert welcome["target_user_id"] == buyer1["user"]["id"]

    # buyer2 cannot validate buyer1's personal welcome coupon
    stolen = await gw.post(
        "/api/promotions/validate",
        headers=b2_hdr,
        json={"code": welcome["code"], "order_subtotal": 100.0},
    )
    assert stolen.status_code == 200
    assert stolen.json()["valid"] is False

    # Seller creates tier- and quota-restricted promo
    created = await gw.post(
        "/api/promotions",
        headers=s_hdr,
        json={
            "code": "GOLD50",
            "description": "$50 off orders over $200 for gold users",
            "discount_type": "fixed",
            "discount_value": 50.0,
            "min_order_amount": 200.0,
            "required_tier": "gold",
            "max_uses": 1,
        },
    )
    assert created.status_code == 201

    # Standard tier rejected
    low_tier = await gw.post(
        "/api/promotions/validate",
        headers=b1_hdr,
        json={"code": "GOLD50", "order_subtotal": 250.0, "loyalty_tier": "standard"},
    )
    assert low_tier.json()["valid"] is False

    # Unauthenticated direct call to internal /redeem rejected
    unauth_redeem = await promos.post(
        "/promotions/redeem",
        json={"order_id": "o1", "user_id": "u1", "code": "GOLD50", "order_subtotal": 250.0},
    )
    assert unauth_redeem.status_code == 401

    # Internal idempotent redemption + Saga failure rollback frees quota
    svc_hdr = internal_headers("order-service", "admin")
    r1 = await promos.post(
        "/promotions/redeem",
        headers=svc_hdr,
        json={
            "order_id": "ord-promo-1",
            "user_id": buyer1["user"]["id"],
            "code": "GOLD50",
            "order_subtotal": 250.0,
            "loyalty_tier": "gold",
        },
    )
    assert r1.status_code == 201
    assert r1.json()["discount_amount"] == 50.0

    # Idempotent replay returns 200 without incrementing uses
    r1_dup = await promos.post(
        "/promotions/redeem",
        headers=svc_hdr,
        json={
            "order_id": "ord-promo-1",
            "user_id": buyer1["user"]["id"],
            "code": "GOLD50",
            "order_subtotal": 250.0,
            "loyalty_tier": "gold",
        },
    )
    assert r1_dup.status_code == 200

    # Quota exhausted for second order while first is reserved
    exhausted = await promos.post(
        "/promotions/redeem",
        headers=svc_hdr,
        json={
            "order_id": "ord-promo-2",
            "user_id": buyer2["user"]["id"],
            "code": "GOLD50",
            "order_subtotal": 250.0,
            "loyalty_tier": "gold",
        },
    )
    assert exhausted.status_code == 409

    # payment.failed releases ord-promo-1 quota so buyer2 can redeem
    await cluster.bus.publish("payment.failed", {"order_id": "ord-promo-1"}, "payment-service")
    r2 = await promos.post(
        "/promotions/redeem",
        headers=svc_hdr,
        json={
            "order_id": "ord-promo-2",
            "user_id": buyer2["user"]["id"],
            "code": "GOLD50",
            "order_subtotal": 250.0,
            "loyalty_tier": "gold",
        },
    )
    assert r2.status_code == 201

    # payment.succeeded commits ord-promo-2 and publishes promotion.redeemed
    await cluster.bus.publish("payment.succeeded", {"order_id": "ord-promo-2"}, "payment-service")
    assert any(
        e.event_type == "promotion.redeemed" and e.payload.get("order_id") == "ord-promo-2"
        for e in cluster.bus.published_events
    )
