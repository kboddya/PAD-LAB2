"""Sliding-window in-memory rate limiter and ASGI middleware for API Gateway."""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from shared.logging import get_request_id


@dataclass(frozen=True)
class RateLimitDecision:
    """Outcome of a single rate-limit evaluation."""

    allowed: bool
    limit: int
    remaining: int
    reset_after_seconds: int


class SlidingWindowRateLimiter:
    """Thread/async-safe per-key sliding-window counter."""

    def __init__(self, max_requests: int = 60, window_seconds: int = 60) -> None:
        self.max_requests = max(1, max_requests)
        self.window_seconds = max(1, window_seconds)
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, now: float | None = None) -> RateLimitDecision:
        """Evaluate and record one request for the given client key."""
        current = time.monotonic() if now is None else now
        cutoff = current - self.window_seconds
        bucket = self._hits[key]

        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) >= self.max_requests:
            oldest = bucket[0]
            reset_after = max(1, math.ceil(self.window_seconds - (current - oldest)))
            return RateLimitDecision(
                allowed=False,
                limit=self.max_requests,
                remaining=0,
                reset_after_seconds=reset_after,
            )

        bucket.append(current)
        remaining = max(0, self.max_requests - len(bucket))
        reset_after = (
            max(1, math.ceil(self.window_seconds - (current - bucket[0])))
            if bucket
            else self.window_seconds
        )
        return RateLimitDecision(
            allowed=True,
            limit=self.max_requests,
            remaining=remaining,
            reset_after_seconds=reset_after,
        )

    def reset(self) -> None:
        """Clear all tracked client buckets."""
        self._hits.clear()


class RateLimitMiddleware:
    """Pure ASGI middleware enforcing rate limits on /api/* endpoints."""

    def __init__(self, app: Any, limiter: SlidingWindowRateLimiter, service_name: str) -> None:
        self.app = app
        self.limiter = limiter
        self.service_name = service_name

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "/")
        if not path.startswith("/api"):
            await self.app(scope, receive, send)
            return

        client_key = self._extract_client_key(scope)
        decision = self.limiter.check(client_key)

        rate_headers = [
            (b"x-ratelimit-limit", str(decision.limit).encode("latin-1")),
            (b"x-ratelimit-remaining", str(decision.remaining).encode("latin-1")),
            (b"x-ratelimit-reset", str(decision.reset_after_seconds).encode("latin-1")),
        ]

        if not decision.allowed:
            req_id = scope.get("state", {}).get("request_id") or get_request_id()
            body = json.dumps(
                {
                    "error": {
                        "code": "RATE_LIMIT_EXCEEDED",
                        "message": "Too many requests; rate limit exceeded",
                        "details": {
                            "limit": decision.limit,
                            "retry_after_seconds": decision.reset_after_seconds,
                        },
                        "request_id": req_id,
                        "service": self.service_name,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                }
            ).encode("utf-8")

            headers = [
                (b"content-type", b"application/json"),
                (b"retry-after", str(decision.reset_after_seconds).encode("latin-1")),
                *rate_headers,
            ]
            await send({"type": "http.response.start", "status": 429, "headers": headers})
            await send({"type": "http.response.body", "body": body})
            return

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                existing = list(message.get("headers", []))
                existing.extend(rate_headers)
                message["headers"] = existing
            await send(message)

        await self.app(scope, receive, send_wrapper)

    @staticmethod
    def _extract_client_key(scope: dict[str, Any]) -> str:
        headers = {k.lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if forwarded_for := headers.get("x-forwarded-for"):
            return forwarded_for.split(",")[0].strip()
        if auth := headers.get("authorization"):
            return f"token:{auth[-24:]}"
        client = scope.get("client")
        if client and len(client) >= 1:
            return str(client[0])
        return "anonymous"
