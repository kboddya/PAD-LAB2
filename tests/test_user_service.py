"""Tests for user-service (event-driven profile creation, RBAC, addresses)."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_profile_created_via_event_and_updated(cluster) -> None:
    auth: httpx.AsyncClient = cluster.service_clients["auth-service"]
    users: httpx.AsyncClient = cluster.service_clients["user-service"]

    reg = await auth.post(
        "/auth/register",
        json={
            "email": "claire@example.com",
            "username": "claire",
            "password": "password123",
            "full_name": "Claire Danes",
        },
    )
    user_id = reg.json()["user"]["id"]
    token = reg.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Profile automatically created via user.registered event
    profile = await users.get(f"/users/{user_id}", headers=headers)
    assert profile.status_code == 200
    assert profile.json()["full_name"] == "Claire Danes"
    assert profile.json()["loyalty_tier"] == "standard"

    # Update name & phone allowed for owner
    updated = await users.put(
        f"/users/{user_id}",
        headers=headers,
        json={"full_name": "Claire D.", "phone": "+1234567890"},
    )
    assert updated.status_code == 200
    assert updated.json()["full_name"] == "Claire D."
    assert updated.json()["phone"] == "+1234567890"

    # Customer cannot self-promote loyalty tier
    forbidden = await users.put(
        f"/users/{user_id}",
        headers=headers,
        json={"loyalty_tier": "gold"},
    )
    assert forbidden.status_code == 403

    # Admin can promote loyalty tier and list users
    admin_headers = {"X-User-Id": "admin-1", "X-User-Role": "admin"}
    promoted = await users.put(f"/users/{user_id}", headers=admin_headers, json={"loyalty_tier": "gold"})
    assert promoted.status_code == 200
    assert promoted.json()["loyalty_tier"] == "gold"

    listed = await users.get("/users", headers=admin_headers)
    assert listed.status_code == 200
    assert len(listed.json()) == 1


@pytest.mark.asyncio
async def test_shipping_addresses_and_access_control(cluster) -> None:
    users: httpx.AsyncClient = cluster.service_clients["user-service"]

    created = await users.post(
        "/users",
        json={
            "user_id": "u-100",
            "email": "u100@example.com",
            "username": "u100",
            "full_name": "User Hundred",
        },
    )
    assert created.status_code == 201

    user_headers = {"X-User-Id": "u-100", "X-User-Role": "customer"}
    addr1 = await users.post(
        "/users/u-100/addresses",
        headers=user_headers,
        json={
            "label": "Home",
            "country": "Moldova",
            "city": "Chisinau",
            "street": "Stefan cel Mare 1",
            "postal_code": "MD-2001",
            "is_default": True,
        },
    )
    assert addr1.status_code == 201
    assert addr1.json()["is_default"] is True

    addr2 = await users.post(
        "/users/u-100/addresses",
        headers=user_headers,
        json={
            "label": "Office",
            "country": "Moldova",
            "city": "Chisinau",
            "street": "Studentilor 7",
            "postal_code": "MD-2045",
            "is_default": True,
        },
    )
    assert addr2.status_code == 201

    all_addrs = await users.get("/users/u-100/addresses", headers=user_headers)
    assert all_addrs.status_code == 200
    assert len(all_addrs.json()) == 2
    defaults = [a for a in all_addrs.json() if a["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["label"] == "Office"

    # Another customer cannot read u-100 profile
    other_headers = {"X-User-Id": "u-999", "X-User-Role": "customer"}
    denied = await users.get("/users/u-100", headers=other_headers)
    assert denied.status_code == 403
