"""Tests for auth-service (registration, login, JWT rotation, revocation, probes)."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from shared.security import create_token


@pytest.mark.asyncio
async def test_register_login_and_me(cluster) -> None:
    auth: httpx.AsyncClient = cluster.service_clients["auth-service"]

    reg = await auth.post(
        "/auth/register",
        json={
            "email": "Alice@Example.com",
            "username": "alice",
            "password": "password123",
            "role": "customer",
            "full_name": "Alice Cooper",
        },
    )
    assert reg.status_code == 201
    data = reg.json()
    assert data["user"]["email"] == "alice@example.com"
    assert data["user"]["role"] == "customer"
    assert data["access_token"]
    assert data["refresh_token"]

    # Event published to bus
    assert any(e.event_type == "user.registered" for e in cluster.bus.published_events)

    # Duplicate email returns 409
    dup = await auth.post(
        "/auth/register",
        json={"email": "alice@example.com", "username": "alice2", "password": "password123"},
    )
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "EMAIL_ALREADY_EXISTS"

    # Login succeeds
    login = await auth.post(
        "/auth/login",
        json={"email": "alice@example.com", "password": "password123"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]

    # Wrong password returns 401
    bad_login = await auth.post(
        "/auth/login",
        json={"email": "alice@example.com", "password": "wrong"},
    )
    assert bad_login.status_code == 401

    # /auth/me returns profile
    me = await auth.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["username"] == "alice"


@pytest.mark.asyncio
async def test_refresh_rotation_and_logout_revocation(cluster) -> None:
    auth: httpx.AsyncClient = cluster.service_clients["auth-service"]

    reg = await auth.post(
        "/auth/register",
        json={"email": "bob@example.com", "username": "bob", "password": "password123"},
    )
    tokens = reg.json()
    access_token = tokens["access_token"]
    refresh_token = tokens["refresh_token"]

    # Refresh rotates old refresh token
    refreshed = await auth.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert refreshed.status_code == 200
    new_access = refreshed.json()["access_token"]
    new_refresh = refreshed.json()["refresh_token"]
    assert new_refresh != refresh_token

    # Reusing rotated refresh token fails
    reuse = await auth.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert reuse.status_code == 401
    assert reuse.json()["error"]["code"] == "TOKEN_REVOKED"

    # Verify works before logout
    verify_ok = await auth.post("/auth/verify", json={"token": new_access})
    assert verify_ok.status_code == 200
    assert verify_ok.json()["valid"] is True

    # Logout revokes both access and refresh
    logout = await auth.post(
        "/auth/logout",
        headers={"Authorization": f"Bearer {new_access}"},
        json={"refresh_token": new_refresh},
    )
    assert logout.status_code == 200

    # Revoked access token fails verify and /me
    verify_revoked = await auth.post("/auth/verify", json={"token": new_access})
    assert verify_revoked.status_code == 401
    assert verify_revoked.json()["error"]["code"] == "TOKEN_REVOKED"

    me_revoked = await auth.get("/auth/me", headers={"Authorization": f"Bearer {access_token}"})
    assert me_revoked.status_code == 200  # original access_token wasn't passed to logout


@pytest.mark.asyncio
async def test_auth_health_and_ready(cluster) -> None:
    auth: httpx.AsyncClient = cluster.service_clients["auth-service"]
    assert (await auth.get("/health")).status_code == 200
    ready = await auth.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"]["database"] == "ok"


@pytest.mark.asyncio
async def test_expired_jwt_is_rejected(cluster) -> None:
    auth: httpx.AsyncClient = cluster.service_clients["auth-service"]
    expired_token, _ = create_token(
        user_id="expired-u1",
        role="customer",
        token_type="access",
        secret="test-secret-key-with-at-least-32-chars",
        algorithm="HS256",
        expires_delta=timedelta(seconds=-10),
    )
    resp = await auth.post("/auth/verify", json={"token": expired_token})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "TOKEN_EXPIRED"

