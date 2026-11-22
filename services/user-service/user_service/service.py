"""Domain business logic for user profiles and shipping addresses."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from shared.errors import ConflictError, ForbiddenError, NotFoundError
from user_service.models import ShippingAddress, UserProfile
from user_service.schemas import AddressCreateRequest, UserCreateRequest, UserUpdateRequest


class UserDomainService:
    """Manages user profile lifecycle and delivery addresses."""

    def create_profile_idempotent(self, session: Session, payload: UserCreateRequest) -> UserProfile:
        existing = session.get(UserProfile, payload.user_id)
        if existing is not None:
            return existing

        conflict = session.scalar(select(UserProfile).where(UserProfile.email == payload.email.lower()))
        if conflict is not None:
            raise ConflictError("Profile with this email already exists", error_code="PROFILE_EXISTS")

        profile = UserProfile(
            user_id=payload.user_id,
            email=payload.email.strip().lower(),
            username=payload.username.strip(),
            full_name=(payload.full_name or payload.username).strip(),
            phone=payload.phone,
            role=payload.role,
            loyalty_tier=payload.loyalty_tier,
        )
        session.add(profile)
        session.commit()
        return self.get_profile(session, profile.user_id)

    def list_profiles(self, session: Session, skip: int = 0, limit: int = 50) -> list[UserProfile]:
        stmt = (
            select(UserProfile)
            .options(selectinload(UserProfile.addresses))
            .order_by(UserProfile.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        return list(session.scalars(stmt).all())

    def get_profile(self, session: Session, user_id: str) -> UserProfile:
        stmt = (
            select(UserProfile)
            .options(selectinload(UserProfile.addresses))
            .where(UserProfile.user_id == user_id)
        )
        profile = session.scalar(stmt)
        if profile is None:
            raise NotFoundError(f"User profile '{user_id}' not found", error_code="USER_NOT_FOUND")
        return profile

    def update_profile(
        self,
        session: Session,
        user_id: str,
        payload: UserUpdateRequest,
        *,
        actor_role: str = "customer",
    ) -> UserProfile:
        profile = self.get_profile(session, user_id)
        if payload.loyalty_tier is not None and actor_role != "admin":
            raise ForbiddenError("Only admins can change loyalty tier", error_code="FORBIDDEN_FIELD")

        if payload.full_name is not None:
            profile.full_name = payload.full_name.strip()
        if payload.phone is not None:
            profile.phone = payload.phone.strip() if payload.phone else None
        if payload.loyalty_tier is not None:
            profile.loyalty_tier = payload.loyalty_tier
        profile.updated_at = datetime.now(timezone.utc)

        session.commit()
        return self.get_profile(session, user_id)

    def add_address(self, session: Session, user_id: str, payload: AddressCreateRequest) -> ShippingAddress:
        profile = self.get_profile(session, user_id)
        if payload.is_default:
            for existing in profile.addresses:
                existing.is_default = False

        address = ShippingAddress(
            user_id=user_id,
            label=payload.label.strip(),
            country=payload.country.strip(),
            city=payload.city.strip(),
            street=payload.street.strip(),
            postal_code=payload.postal_code.strip(),
            is_default=payload.is_default or len(profile.addresses) == 0,
        )
        session.add(address)
        session.commit()
        session.refresh(address)
        return address

    def list_addresses(self, session: Session, user_id: str) -> list[ShippingAddress]:
        self.get_profile(session, user_id)
        stmt = (
            select(ShippingAddress)
            .where(ShippingAddress.user_id == user_id)
            .order_by(ShippingAddress.is_default.desc(), ShippingAddress.created_at.asc())
        )
        return list(session.scalars(stmt).all())
