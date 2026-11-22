"""Tests for payment-service (idempotency, simulated decline, limit, refunds)."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_payment_processing_idempotency_and_refund(cluster) -> None:
    payments: httpx.AsyncClient = cluster.service_clients["payment-service"]

    req_body = {
        "order_id": "ord-100",
        "user_id": "usr-1",
        "amount": 250.0,
        "currency": "usd",
        "idempotency_key": "idem-100",
    }
    first = await payments.post("/payments", json=req_body)
    assert first.status_code == 201
    p_data = first.json()
    assert p_data["status"] == "succeeded"
    assert p_data["currency"] == "USD"

    # Idempotent replay returns HTTP 200 with exact same payment ID and no extra event
    before_events = len(cluster.bus.published_events)
    replay = await payments.post("/payments", json=req_body)
    assert replay.status_code == 200
    assert replay.json()["id"] == p_data["id"]
    assert len(cluster.bus.published_events) == before_events

    # Lookup by order_id
    by_order = await payments.get(
        "/payments/by-order/ord-100",
        headers={"X-User-Id": "usr-1", "X-User-Role": "customer"},
    )
    assert by_order.status_code == 200
    assert by_order.json()["id"] == p_data["id"]

    # Refund succeeded payment
    refund = await payments.post(
        f"/payments/{p_data['id']}/refund",
        headers={"X-User-Id": "usr-1", "X-User-Role": "customer"},
    )
    assert refund.status_code == 200
    assert refund.json()["status"] == "refunded"
    assert any(e.event_type == "payment.refunded" for e in cluster.bus.published_events)

    # Second refund rejected
    dup_refund = await payments.post(
        f"/payments/{p_data['id']}/refund",
        headers={"X-User-Id": "usr-1", "X-User-Role": "customer"},
    )
    assert dup_refund.status_code == 409


@pytest.mark.asyncio
async def test_payment_failure_scenarios(cluster) -> None:
    payments: httpx.AsyncClient = cluster.service_clients["payment-service"]

    declined = await payments.post(
        "/payments",
        json={"order_id": "ord-fail-1", "user_id": "usr-1", "amount": 50.0, "simulate_failure": True},
    )
    assert declined.status_code == 201
    assert declined.json()["status"] == "failed"
    assert "declined" in declined.json()["failure_reason"].lower()

    # Over single transaction limit (5000.0 in test cluster)
    over_limit = await payments.post(
        "/payments",
        json={"order_id": "ord-fail-2", "user_id": "usr-1", "amount": 9999.0},
    )
    assert over_limit.status_code == 201
    assert over_limit.json()["status"] == "failed"
    assert "limit" in over_limit.json()["failure_reason"].lower()
