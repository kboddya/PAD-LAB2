"""Domain business logic for authentication, JWT issuance, and token revocation."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from auth_service.config import AuthSettings
from auth_service.models import RevokedToken, UserCredential
from auth_service.schemas import (
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserPublicResponse,
    VerifyResponse,
)
from shared.errors import ConflictError, ForbiddenError, NotFoundError, UnauthorizedError
from shared.security import create_token, decode_token, hash_password, verify_password


class AuthDomainService:
    """Encapsulates user credential management and JWT lifecycle."""

    def __init__(self, settings: AuthSettings) -> None:
        self.settings = settings

    def register(self, session: Session, payload: RegisterRequest) -> tuple[UserCredential, TokenResponse]:
        existing_email = session.scalar(
            select(UserCredential).where(UserCredential.email == payload.email)
        )
        if existing_email is not None:
            raise ConflictError("User with this email already exists", error_code="EMAIL_ALREADY_EXISTS")

        existing_username = session.scalar(
            select(UserCredential).where(UserCredential.username == payload.username)
        )
        if existing_username is not None:
            raise ConflictError(
                "User with this username already exists",
                error_code="USERNAME_ALREADY_EXISTS",
            )

        user = UserCredential(
            email=payload.email,
            username=payload.username,
            password_hash=hash_password(payload.password),
            role=payload.role,
            is_active=True,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user, self._issue_token_pair(user)

    def login(self, session: Session, payload: LoginRequest) -> TokenResponse:
        user = session.scalar(select(UserCredential).where(UserCredential.email == payload.email))
        if user is None or not verify_password(payload.password, user.password_hash):
            raise UnauthorizedError("Invalid email or password", error_code="INVALID_CREDENTIALS")
        if not user.is_active:
            raise ForbiddenError("User account is deactivated", error_code="ACCOUNT_INACTIVE")
        return self._issue_token_pair(user)

    def refresh(self, session: Session, refresh_token: str) -> TokenResponse:
        claims = decode_token(
            refresh_token,
            secret=self.settings.jwt_secret,
            algorithm=self.settings.jwt_algorithm,
            expected_type="refresh",
        )
        if self._is_revoked(session, claims["jti"]):
            raise UnauthorizedError("Refresh token has been revoked", error_code="TOKEN_REVOKED")

        user = session.get(UserCredential, str(claims["sub"]))
        if user is None:
            raise UnauthorizedError("User no longer exists", error_code="USER_NOT_FOUND")
        if not user.is_active:
            raise ForbiddenError("User account is deactivated", error_code="ACCOUNT_INACTIVE")

        # Rotate refresh token by revoking the consumed jti
        self._revoke_jti(session, str(claims["jti"]))
        session.commit()
        return self._issue_token_pair(user)

    def logout(self, session: Session, access_token: str, refresh_token: str | None = None) -> None:
        access_claims = decode_token(
            access_token,
            secret=self.settings.jwt_secret,
            algorithm=self.settings.jwt_algorithm,
            expected_type="access",
        )
        self._revoke_jti(session, str(access_claims["jti"]))

        if refresh_token:
            refresh_claims = decode_token(
                refresh_token,
                secret=self.settings.jwt_secret,
                algorithm=self.settings.jwt_algorithm,
                expected_type="refresh",
            )
            self._revoke_jti(session, str(refresh_claims["jti"]))

        session.commit()

    def verify_token(self, session: Session, token: str, expected_type: str = "access") -> VerifyResponse:
        claims = decode_token(
            token,
            secret=self.settings.jwt_secret,
            algorithm=self.settings.jwt_algorithm,
            expected_type=expected_type,
        )
        jti = str(claims["jti"])
        if self._is_revoked(session, jti):
            raise UnauthorizedError("Token has been revoked", error_code="TOKEN_REVOKED")

        user = session.get(UserCredential, str(claims["sub"]))
        if user is None or not user.is_active:
            raise UnauthorizedError("Account is unavailable", error_code="ACCOUNT_UNAVAILABLE")

        return VerifyResponse(
            valid=True,
            user_id=user.id,
            role=user.role,
            jti=jti,
            token_type=str(claims["type"]),
        )

    def get_user(self, session: Session, user_id: str) -> UserCredential:
        user = session.get(UserCredential, user_id)
        if user is None:
            raise NotFoundError(f"User '{user_id}' not found")
        return user

    def _is_revoked(self, session: Session, jti: str) -> bool:
        return session.get(RevokedToken, jti) is not None

    def _revoke_jti(self, session: Session, jti: str) -> None:
        if session.get(RevokedToken, jti) is None:
            session.add(RevokedToken(jti=jti))

    def _issue_token_pair(self, user: UserCredential) -> TokenResponse:
        access_ttl = timedelta(minutes=self.settings.access_token_expire_minutes)
        refresh_ttl = timedelta(days=self.settings.refresh_token_expire_days)

        access_token, _ = create_token(
            user_id=user.id,
            role=user.role,
            token_type="access",
            secret=self.settings.jwt_secret,
            algorithm=self.settings.jwt_algorithm,
            expires_delta=access_ttl,
        )
        refresh_token, _ = create_token(
            user_id=user.id,
            role=user.role,
            token_type="refresh",
            secret=self.settings.jwt_secret,
            algorithm=self.settings.jwt_algorithm,
            expires_delta=refresh_ttl,
        )
        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=int(access_ttl.total_seconds()),
            user=UserPublicResponse.model_validate(user),
        )
