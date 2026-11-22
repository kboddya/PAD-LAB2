"""Unified domain/infrastructure exceptions and safe FastAPI error handlers."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from shared.logging import get_request_id


class ErrorDetail(BaseModel):
    """Canonical error body returned by every ShopFlow service."""

    code: str
    message: str
    details: Any = None
    request_id: str
    service: str
    timestamp: str


class ErrorResponse(BaseModel):
    """Top-level JSON envelope for API errors."""

    error: ErrorDetail


class AppError(Exception):
    """Base application exception translated into a safe HTTP JSON response."""

    status_code: int = 500
    error_code: str = "APP_ERROR"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if error_code is not None:
            self.error_code = error_code
        self.details = details


class BadRequestError(AppError):
    status_code = 400
    error_code = "BAD_REQUEST"


class UnauthorizedError(AppError):
    status_code = 401
    error_code = "UNAUTHORIZED"


class ForbiddenError(AppError):
    status_code = 403
    error_code = "FORBIDDEN"


class NotFoundError(AppError):
    status_code = 404
    error_code = "NOT_FOUND"


class ConflictError(AppError):
    status_code = 409
    error_code = "CONFLICT"


class RateLimitError(AppError):
    status_code = 429
    error_code = "RATE_LIMIT_EXCEEDED"


class ExternalServiceError(AppError):
    status_code = 502
    error_code = "EXTERNAL_SERVICE_ERROR"


class DatabaseError(AppError):
    status_code = 503
    error_code = "DATABASE_ERROR"


class ServiceUnavailableError(AppError):
    status_code = 503
    error_code = "SERVICE_UNAVAILABLE"


class ServiceTimeoutError(AppError):
    status_code = 504
    error_code = "GATEWAY_TIMEOUT"


def _resolve_request_id(request: Request) -> str:
    state_req_id = getattr(request.state, "request_id", None)
    if state_req_id:
        return str(state_req_id)
    ctx_req_id = get_request_id()
    if ctx_req_id and ctx_req_id != "-":
        return ctx_req_id
    header_req_id = request.headers.get("X-Request-ID")
    return header_req_id if header_req_id else "-"


def _build_error_response(
    *,
    request: Request,
    service_name: str,
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    req_id = _resolve_request_id(request)
    body = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message,
            details=details,
            request_id=req_id,
            service=service_name,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    )
    response_headers = {"X-Request-ID": req_id}
    if headers:
        response_headers.update(headers)
    return JSONResponse(status_code=status_code, content=body.model_dump(), headers=response_headers)


def register_exception_handlers(app: FastAPI, service_name: str, logger: logging.Logger) -> None:
    """Register uniform exception handlers that never leak stack traces."""

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        req_id = _resolve_request_id(request)
        log_fn = logger.warning if exc.status_code < 500 else logger.error
        log_fn(
            "Application error handled: %s",
            exc.message,
            extra={
                "request_id": req_id,
                "error_code": exc.error_code,
                "status_code": exc.status_code,
                "path": request.url.path,
            },
        )
        return _build_error_response(
            request=request,
            service_name=service_name,
            status_code=exc.status_code,
            code=exc.error_code,
            message=exc.message,
            details=exc.details,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        req_id = _resolve_request_id(request)
        safe_errors = [
            {
                "field": ".".join(str(part) for part in err.get("loc", ())),
                "message": err.get("msg", "Invalid value"),
                "type": err.get("type", "value_error"),
            }
            for err in exc.errors()
        ]
        logger.warning(
            "Request validation failed",
            extra={"request_id": req_id, "status_code": 422, "path": request.url.path},
        )
        return _build_error_response(
            request=request,
            service_name=service_name,
            status_code=422,
            code="VALIDATION_ERROR",
            message="Request input validation failed",
            details=safe_errors,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        req_id = _resolve_request_id(request)
        logger.warning(
            "HTTP exception: %s",
            exc.detail,
            extra={"request_id": req_id, "status_code": exc.status_code, "path": request.url.path},
        )
        return _build_error_response(
            request=request,
            service_name=service_name,
            status_code=exc.status_code,
            code="HTTP_ERROR",
            message=str(exc.detail) if exc.detail else "HTTP error",
            headers=dict(exc.headers) if exc.headers else None,
        )

    @app.exception_handler(SQLAlchemyError)
    async def handle_sqlalchemy_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        req_id = _resolve_request_id(request)
        logger.error(
            "Database error occurred",
            exc_info=exc,
            extra={"request_id": req_id, "status_code": 503, "path": request.url.path},
        )
        return _build_error_response(
            request=request,
            service_name=service_name,
            status_code=503,
            code="DATABASE_ERROR",
            message="Database operation failed",
        )

    @app.exception_handler(httpx.TimeoutException)
    async def handle_httpx_timeout(request: Request, exc: httpx.TimeoutException) -> JSONResponse:
        req_id = _resolve_request_id(request)
        logger.error(
            "Downstream service request timed out",
            exc_info=exc,
            extra={"request_id": req_id, "status_code": 504, "path": request.url.path},
        )
        return _build_error_response(
            request=request,
            service_name=service_name,
            status_code=504,
            code="GATEWAY_TIMEOUT",
            message="Downstream service timed out",
        )

    @app.exception_handler(httpx.HTTPError)
    async def handle_httpx_error(request: Request, exc: httpx.HTTPError) -> JSONResponse:
        req_id = _resolve_request_id(request)
        logger.error(
            "Downstream service communication error",
            exc_info=exc,
            extra={"request_id": req_id, "status_code": 502, "path": request.url.path},
        )
        return _build_error_response(
            request=request,
            service_name=service_name,
            status_code=502,
            code="EXTERNAL_SERVICE_ERROR",
            message="Downstream service communication failed",
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        req_id = _resolve_request_id(request)
        logger.exception(
            "Unhandled internal error",
            exc_info=exc,
            extra={"request_id": req_id, "status_code": 500, "path": request.url.path},
        )
        return _build_error_response(
            request=request,
            service_name=service_name,
            status_code=500,
            code="INTERNAL_SERVER_ERROR",
            message="An unexpected internal error occurred",
        )
