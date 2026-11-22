"""FastAPI application entrypoint for user-service."""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, Query, Request, status
from sqlalchemy.orm import Session

from shared.app_factory import build_service_app
from shared.database import DatabaseManager
from shared.errors import ForbiddenError
from shared.messaging import EventBus, EventEnvelope, create_event_bus
from shared.security import UserContext, extract_user_context, require_roles
from user_service.config import UserSettings
from user_service.models import UserBase
from user_service.schemas import (
    AddressCreateRequest,
    AddressResponse,
    UserCreateRequest,
    UserProfileResponse,
    UserUpdateRequest,
)
from user_service.service import UserDomainService


def create_app(
    settings: UserSettings | None = None,
    event_bus: EventBus | None = None,
) -> FastAPI:
    """Create and configure the user-service FastAPI application."""
    cfg = settings or UserSettings()
    db = DatabaseManager(cfg.database_url, UserBase)
    bus = event_bus or create_event_bus(cfg.rabbitmq_url, cfg.service_name)
    domain = UserDomainService()

    app, logger = build_service_app(
        title="ShopFlow User Service",
        description="User profiles, loyalty tiers, and shipping addresses microservice.",
        settings=cfg,
        db=db,
        event_bus=bus,
    )

    def _on_user_registered(envelope: EventEnvelope) -> None:
        data = envelope.payload
        with db.session_scope() as session:
            domain.create_profile_idempotent(
                session,
                UserCreateRequest(
                    user_id=str(data["user_id"]),
                    email=str(data["email"]),
                    username=str(data["username"]),
                    full_name=str(data.get("full_name") or data["username"]),
                    role=str(data.get("role", "customer")),
                ),
            )
        logger.info("Created profile from user.registered event", extra={"user_id": data.get("user_id")})

    bus.on("user.registered", _on_user_registered)

    def _get_caller(request: Request) -> UserContext:
        return extract_user_context(
            request,
            jwt_secret=cfg.jwt_secret,
            jwt_algorithm=cfg.jwt_algorithm,
        )

    def _ensure_self_or_admin(caller: UserContext, target_user_id: str) -> None:
        if caller.user_id != target_user_id and caller.role != "admin":
            raise ForbiddenError("Access denied to another user's profile")

    router = APIRouter(prefix="/users", tags=["Users"])

    @router.post(
        "",
        response_model=UserProfileResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Create user profile (idempotent fallback)",
    )
    async def create_user(
        payload: UserCreateRequest,
        session: Session = Depends(db.get_db),
    ) -> UserProfileResponse:
        profile = domain.create_profile_idempotent(session, payload)
        return UserProfileResponse.model_validate(profile)

    @router.get("", response_model=list[UserProfileResponse], summary="List all user profiles (admin)")
    async def list_users(
        request: Request,
        skip: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
        session: Session = Depends(db.get_db),
    ) -> list[UserProfileResponse]:
        caller = _get_caller(request)
        require_roles(caller, {"admin"})
        profiles = domain.list_profiles(session, skip=skip, limit=limit)
        return [UserProfileResponse.model_validate(p) for p in profiles]

    @router.get("/{user_id}", response_model=UserProfileResponse, summary="Get user profile by ID")
    async def get_user(
        user_id: str,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> UserProfileResponse:
        caller = _get_caller(request)
        _ensure_self_or_admin(caller, user_id)
        profile = domain.get_profile(session, user_id)
        return UserProfileResponse.model_validate(profile)

    @router.put("/{user_id}", response_model=UserProfileResponse, summary="Update user profile")
    async def update_user(
        user_id: str,
        payload: UserUpdateRequest,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> UserProfileResponse:
        caller = _get_caller(request)
        _ensure_self_or_admin(caller, user_id)
        profile = domain.update_profile(session, user_id, payload, actor_role=caller.role)
        return UserProfileResponse.model_validate(profile)

    @router.post(
        "/{user_id}/addresses",
        response_model=AddressResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Add shipping address to user profile",
    )
    async def add_address(
        user_id: str,
        payload: AddressCreateRequest,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> AddressResponse:
        caller = _get_caller(request)
        _ensure_self_or_admin(caller, user_id)
        addr = domain.add_address(session, user_id, payload)
        return AddressResponse.model_validate(addr)

    @router.get("/{user_id}/addresses", response_model=list[AddressResponse], summary="List shipping addresses")
    async def list_addresses(
        user_id: str,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> list[AddressResponse]:
        caller = _get_caller(request)
        _ensure_self_or_admin(caller, user_id)
        addresses = domain.list_addresses(session, user_id)
        return [AddressResponse.model_validate(a) for a in addresses]

    app.include_router(router)
    return app
