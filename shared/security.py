"""Password hashing (PBKDF2-SHA256), JWT utilities, and request user context helpers."""

from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import Request

from shared.errors import ForbiddenError, UnauthorizedError


@dataclass(frozen=True)
class UserContext:
    """Authenticated caller identity extracted from gateway headers or JWT."""

    user_id: str
    role: str


def hash_password(password: str, iterations: int = 120_000) -> str:
    """Hash a plaintext password using PBKDF2-HMAC-SHA256 with a random 16-byte salt."""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a plaintext password against a stored PBKDF2-SHA256 hash."""
    try:
        algorithm, iter_str, salt_hex, hash_hex = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iter_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False

    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)


def create_token(
    *,
    user_id: str,
    role: str,
    token_type: str,
    secret: str,
    algorithm: str,
    expires_delta: timedelta,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Create a signed JWT and return ``(encoded_token, jti)``."""
    now = datetime.now(timezone.utc)
    jti = uuid.uuid4().hex
    payload: dict[str, Any] = {
        "sub": user_id,
        "role": role,
        "type": token_type,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
    }
    if extra_claims:
        payload.update(extra_claims)

    encoded = jwt.encode(payload, secret, algorithm=algorithm)
    return encoded, jti


def decode_token(
    token: str,
    *,
    secret: str,
    algorithm: str,
    expected_type: str | None = None,
) -> dict[str, Any]:
    """Decode and validate a JWT, raising UnauthorizedError on invalid or expired tokens."""
    try:
        payload = jwt.decode(token, secret, algorithms=[algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("Token has expired", error_code="TOKEN_EXPIRED") from exc
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError("Invalid authentication token", error_code="INVALID_TOKEN") from exc

    if expected_type is not None and payload.get("type") != expected_type:
        raise UnauthorizedError(
            f"Expected {expected_type} token",
            error_code="INVALID_TOKEN_TYPE",
        )
    if not payload.get("sub") or not payload.get("jti"):
        raise UnauthorizedError("Token payload missing required claims", error_code="INVALID_TOKEN")
    return payload


def extract_bearer_token(request: Request) -> str:
    """Extract a Bearer token from the Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise UnauthorizedError("Missing Bearer authentication token", error_code="MISSING_TOKEN")
    token = auth_header[7:].strip()
    if not token:
        raise UnauthorizedError("Empty Bearer authentication token", error_code="MISSING_TOKEN")
    return token


def extract_user_context(
    request: Request,
    *,
    jwt_secret: str | None = None,
    jwt_algorithm: str = "HS256",
) -> UserContext:
    """Resolve user identity from API Gateway headers or fallback Bearer JWT."""
    header_user_id = request.headers.get("X-User-Id")
    header_role = request.headers.get("X-User-Role")
    if header_user_id and header_role:
        return UserContext(user_id=header_user_id, role=header_role)

    if jwt_secret:
        token = extract_bearer_token(request)
        payload = decode_token(token, secret=jwt_secret, algorithm=jwt_algorithm, expected_type="access")
        return UserContext(user_id=str(payload["sub"]), role=str(payload.get("role", "customer")))

    raise UnauthorizedError("Authentication context is required")


def require_roles(user: UserContext, allowed_roles: set[str]) -> None:
    """Ensure the authenticated user has one of the allowed RBAC roles."""
    if user.role not in allowed_roles:
        raise ForbiddenError(
            f"Role '{user.role}' is not authorized to perform this action",
            error_code="INSUFFICIENT_ROLE",
        )
