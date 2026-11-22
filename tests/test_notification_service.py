"""Tests for notification-service (event-driven notifications, dedup, read tracking)."""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import internal_headers


@pytest.mark.asyncio
async def test_notifications_from_events_and_read_management(cluster) -> None:
    notifs: httpx.AsyncClient = cluster.service_clients["notification-service"]

    await cluster.bus.publish(
        "user.registered",
        {"user_id": "u-notif-1", "username": "diana"},
        "auth-service",
    )
    await cluster.bus.publish(
        "order.created",
        {"user_id": "u-notif-1", "order_id": "ord-1", "total_amount": 99.5, "currency": "USD"},
        "order-service",
    )
    await cluster.bus.publish(
        "payment.succeeded",
        {"user_id": "u-notif-1", "payment_id": "pay-1", "order_id": "ord-1", "amount": 99.5, "currency": "USD"},
        "payment-service",
    )

    headers = internal_headers("u-notif-1", "customer")
    count = await notifs.get("/notifications/unread-count", headers=headers)
    assert count.status_code == 200
    assert count.json()["unread_count"] == 3

    listed = await notifs.get("/notifications", headers=headers)
    assert listed.status_code == 200
    items = listed.json()
    assert len(items) == 3
    categories = {item["category"] for item in items}
    assert categories == {"welcome", "order_created", "payment_succeeded"}

    # Mark one read
    first_id = items[0]["id"]
    marked = await notifs.patch(f"/notifications/{first_id}/read", headers=headers)
    assert marked.status_code == 200
    assert marked.json()["is_read"] is True

    unread = await notifs.get("/notifications", headers=headers, params={"unread_only": True})
    assert len(unread.json()) == 2

    # Mark all read
    mark_all = await notifs.post("/notifications/mark-all-read", headers=headers)
    assert mark_all.status_code == 200
    assert mark_all.json()["marked_read"] == 2

    final_count = await notifs.get("/notifications/unread-count", headers=headers)
    assert final_count.json()["unread_count"] == 0

    # Other user cannot mark notification read
    forbidden = await notifs.patch(
        f"/notifications/{first_id}/read",
        headers=internal_headers("other-user", "customer"),
    )
    assert forbidden.status_code == 403
