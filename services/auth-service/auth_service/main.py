"""FastAPI application entrypoint for auth-service."""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, Request, status
from sqlalchemy.orm import Session

from auth_service.config import AuthSettings
from auth_service.models import AuthBase
from auth_service.schemas import (
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserPublicResponse,
    VerifyRequest,
    VerifyResponse,
)
from auth_service.service import AuthDomainService
from shared.app_factory import build_service_app
from shared.database import DatabaseManager
from shared.messaging import EventBus, create_event_bus
from shared.security import extract_bearer_token


def create_app(
    settings: AuthSettings | None = None,
    event_bus: EventBus | None = None,
) -> FastAPI:
    """Create and configure the auth-service FastAPI application."""
    cfg = settings or AuthSettings()
    db = DatabaseManager(cfg.database_url, AuthBase)
    bus = event_bus or create_event_bus(cfg.rabbitmq_url, cfg.service_name)
    domain = AuthDomainService(cfg)

    app, logger = build_service_app(
        title="ShopFlow Auth Service",
        description="Authentication, JWT issuance, token revocation, and RBAC service.",
        settings=cfg,
        db=db,
        event_bus=bus,
    )

    router = APIRouter(prefix="/auth", tags=["Auth"])

    @router.post(
        "/register",
        response_model=TokenResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Register a new user account",
    )
    async def register(
        payload: RegisterRequest,
        session: Session = Depends(db.get_db),
    ) -> TokenResponse:
        user, token_response = domain.register(session, payload)
        await bus.publish(
            event_type="user.registered",
            payload={
                "user_id": user.id,
                "email": user.email,
                "username": user.username,
                "role": user.role,
                "full_name": payload.full_name or user.username,
            },
            source_service=cfg.service_name,
        )
        logger.info("User registered", extra={"user_id": user.id, "role": user.role})
        return token_response

    @router.post("/login", response_model=TokenResponse, summary="Authenticate and issue JWT pair")
    async def login(
        payload: LoginRequest,
        session: Session = Depends(db.get_db),
    ) -> TokenResponse:
        result = domain.login(session, payload)
        logger.info("User logged in", extra={"user_id": result.user.id})
        return result

    @router.post("/refresh", response_model=TokenResponse, summary="Rotate refresh token and issue new JWTs")
    async def refresh(
        payload: RefreshRequest,
        session: Session = Depends(db.get_db),
    ) -> TokenResponse:
        return domain.refresh(session, payload.refresh_token)

    @router.post("/logout", status_code=status.HTTP_200_OK, summary="Revoke access (and optional refresh) token")
    async def logout(
        request: Request,
        payload: LogoutRequest | None = None,
        session: Session = Depends(db.get_db),
    ) -> dict[str, str]:
        token = extract_bearer_token(request)
        domain.logout(session, token, payload.refresh_token if payload else None)
        logger.info("User logged out")
        return {"status": "logged_out"}

    @router.post("/verify", response_model=VerifyResponse, summary="Verify JWT validity and revocation status")
    async def verify(
        payload: VerifyRequest,
        session: Session = Depends(db.get_db),
    ) -> VerifyResponse:
        return domain.verify_token(session, payload.token)

    @router.get("/me", response_model=UserPublicResponse, summary="Get current authenticated user")
    async def me(
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> UserPublicResponse:
        token = extract_bearer_token(request)
        verified = domain.verify_token(session, token)
        user = domain.get_user(session, verified.user_id)
        return UserPublicResponse.model_validate(user)

    app.include_router(router)
    return app
