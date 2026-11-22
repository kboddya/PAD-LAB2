"""HTTP reverse-proxy and aggregation client utilities for API Gateway."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import Request, Response

from shared.errors import ServiceTimeoutError, ServiceUnavailableError
from shared.logging import get_request_id

_FORWARDED_REQUEST_HEADERS = ("authorization", "content-type", "accept")
_STRIPPED_RESPONSE_HEADERS = frozenset({"content-length", "transfer-encoding", "connection"})


class DownstreamProxy:
    """Forwards HTTP requests to downstream microservices with fault translation."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def forward(
        self,
        request: Request,
        *,
        target_base_url: str,
        target_path: str,
        extra_headers: dict[str, str] | None = None,
    ) -> Response:
        """Proxy an incoming FastAPI request to a downstream service endpoint."""
        url = f"{target_base_url.rstrip('/')}/{target_path.lstrip('/')}"
        headers: dict[str, str] = {"X-Request-ID": get_request_id()}

        for name in _FORWARDED_REQUEST_HEADERS:
            if val := request.headers.get(name):
                headers[name] = val

        if extra_headers:
            headers.update(extra_headers)

        body = await request.body()
        params = list(request.query_params.multi_items())

        try:
            upstream_resp = await self.client.request(
                method=request.method,
                url=url,
                params=params,
                content=body if body else None,
                headers=headers,
            )
        except httpx.TimeoutException as exc:
            raise ServiceTimeoutError(f"Downstream request to '{target_path}' timed out") from exc
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError(f"Downstream service unavailable for '{target_path}'") from exc

        resp_headers = {
            k: v
            for k, v in upstream_resp.headers.items()
            if k.lower() not in _STRIPPED_RESPONSE_HEADERS
        }
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    async def fetch_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[bool, Any, str | None]:
        """Fetch JSON from a downstream service, returning (ok, data, error_reason)."""
        req_headers = {"X-Request-ID": get_request_id()}
        if headers:
            req_headers.update(headers)
        try:
            resp = await self.client.get(url, headers=req_headers)
        except httpx.TimeoutException:
            return False, None, "timeout"
        except httpx.HTTPError:
            return False, None, "unavailable"

        if resp.status_code >= 500:
            return False, None, f"http_{resp.status_code}"
        if resp.status_code >= 400:
            return False, None, f"http_{resp.status_code}"
        return True, resp.json(), None
