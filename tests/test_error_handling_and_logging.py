"""Tests for structured JSON logging, X-Request-ID tracing, and safe error handling."""

from __future__ import annotations

import io
import json
import logging

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from auth_service.config import AuthSettings
from auth_service.main import create_app as create_auth_app
from shared.app_factory import build_service_app
from shared.config import BaseServiceSettings


@pytest.mark.asyncio
async def test_request_id_propagation_and_structured_json_logs(cluster) -> None:
    gw = cluster.gateway_client
    assert gw is not None

    auth_logger = logging.getLogger("shopflow.auth-service")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(auth_logger.handlers[0].formatter)
    auth_logger.addHandler(handler)

    try:
        trace_id = "trace-req-998877"
        resp = await gw.post(
            "/api/auth/register",
            headers={"X-Request-ID": trace_id},
            json={"email": "logtest@example.com", "username": "logtest", "password": "password123"},
        )
        assert resp.status_code == 201
        assert resp.headers["X-Request-ID"] == trace_id

        # Request ID propagated to EventBus envelope
        matching_events = [e for e in cluster.bus.published_events if e.event_type == "user.registered"]
        assert matching_events[-1].request_id == trace_id

        # Validate JSON log structure emitted by auth-service
        lines = [line for line in stream.getvalue().strip().splitlines() if line.strip()]
        assert lines
        parsed = [json.loads(line) for line in lines]
        assert any(
            entry["service"] == "auth-service"
            and entry["request_id"] == trace_id
            and "timestamp" in entry
            and "level" in entry
            and "message" in entry
            for entry in parsed
        )
    finally:
        auth_logger.removeHandler(handler)


@pytest.mark.asyncio
async def test_unhandled_error_and_validation_never_leak_stack_trace() -> None:
    app, _ = build_service_app(
        title="Test Service",
        description="Error handling test service",
        settings=BaseServiceSettings(service_name="test-service", log_json=True),
    )

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("sensitive internal secret=db_pass_123")

    @app.get("/items/{item_id}")
    async def typed_item(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test-service",
    ) as client:
        crash = await client.get("/boom", headers={"X-Request-ID": "err-trace-1"})
        assert crash.status_code == 500
        assert crash.headers["X-Request-ID"] == "err-trace-1"
        body_text = crash.text
        assert "Traceback" not in body_text
        assert "RuntimeError" not in body_text
        assert "db_pass_123" not in body_text
        err = crash.json()["error"]
        assert err["code"] == "INTERNAL_SERVER_ERROR"
        assert err["request_id"] == "err-trace-1"
        assert err["service"] == "test-service"

        validation = await client.get("/items/not-an-integer")
        assert validation.status_code == 422
        assert validation.json()["error"]["code"] == "VALIDATION_ERROR"
        assert "Traceback" not in validation.text


@pytest.mark.asyncio
async def test_sqlalchemy_error_returns_safe_503_and_lifespan_starts_cleanly(tmp_path) -> None:
    app, _ = build_service_app(
        title="DB Error Test",
        description="Tests SQLAlchemyError translation",
        settings=BaseServiceSettings(service_name="db-test-service", log_json=True),
    )

    @app.get("/db-fail")
    async def db_fail() -> None:
        raise OperationalError("SELECT * FROM secret_table", {}, Exception("connection refused"))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://db-test-service",
    ) as client:
        resp = await client.get("/db-fail")
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "DATABASE_ERROR"
        assert "secret_table" not in resp.text
        assert "Traceback" not in resp.text

    # Verify full ASGI lifespan startup/shutdown on a real service
    auth_app = create_auth_app(
        settings=AuthSettings(
            database_url=f"sqlite:///{tmp_path / 'lifespan_auth.db'}",
            rabbitmq_url="memory://",
        )
    )
    with TestClient(auth_app) as sync_client:
        health = sync_client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

