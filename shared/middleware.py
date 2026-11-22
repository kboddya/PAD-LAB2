"""Pure ASGI middleware for correlation X-Request-ID propagation and access logging."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Callable

from shared.logging import request_id_var


class RequestContextMiddleware:
    """Attach X-Request-ID to every request/response and log structured access records."""

    def __init__(self, app: Any, logger: logging.Logger) -> None:
        self.app = app
        self.logger = logger

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = list(scope.get("headers", []))
        request_id = ""
        for name, value in headers:
            if name.lower() == b"x-request-id":
                decoded = value.decode("latin-1").strip()
                if decoded:
                    request_id = decoded
                break

        if not request_id:
            request_id = uuid.uuid4().hex

        scope.setdefault("state", {})["request_id"] = request_id
        token = request_id_var.set(request_id)
        start = time.perf_counter()
        status_code = 500
        method = scope.get("method", "GET")
        path = scope.get("path", "/")

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message.get("status", 500))
                raw_headers = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"
                ]
                raw_headers.append((b"x-request-id", request_id.encode("latin-1")))
                message["headers"] = raw_headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            self.logger.info(
                "HTTP request completed",
                extra={
                    "method": method,
                    "path": path,
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                },
            )
            request_id_var.reset(token)
