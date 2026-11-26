"""Domain business logic for promotions, rule evaluation, and Saga redemptions."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from promotion_service.models import CouponRedemption, Promotion
from promotion_service.schemas import (
    PromotionCreateRequest,
    PromotionRedeemRequest,
    PromotionValidationResponse,
)
from shared.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError

_TIER_RANKS = {"standard": 0, "silver": 1, "gold": 2}


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class PromotionDomainService:
    """Manages promotional rules, validation, idempotent redemptions, and Saga rollbacks."""

    def create_promotion(self, session: Session, payload: PromotionCreateRequest, *, created_by: str) -> Promotion:
        normalized_code = payload.code.strip().upper()
        existing = session.scalar(select(Promotion).where(Promotion.code == normalized_code))
        if existing is not None:
            raise ConflictError(f"Promotion code '{normalized_code}' already exists", error_code="PROMOTION_EXISTS")

        promo = Promotion(
            code=normalized_code,
            description=payload.description.strip(),
            discount_type=payload.discount_type,
            discount_value=round(float(payload.discount_value), 2),
            min_order_amount=round(float(payload.min_order_amount), 2),
            max_discount_amount=(
                round(float(payload.max_discount_amount), 2) if payload.max_discount_amount is not None else None
            ),
            required_tier=payload.required_tier,
            category_filter=payload.category_filter.strip().lower() if payload.category_filter else None,
            max_uses=payload.max_uses,
            current_uses=0,
            per_user_limit=payload.per_user_limit,
            target_user_id=payload.target_user_id,
            valid_from=_as_utc(payload.valid_from) if payload.valid_from else None,
            valid_until=_as_utc(payload.valid_until) if payload.valid_until else None,
            is_active=True,
            created_by=created_by,
        )
        session.add(promo)
        session.commit()
        session.refresh(promo)
        return promo

    def create_welcome_coupon_idempotent(self, session: Session, *, user_id: str, username: str) -> Promotion:
        """Create a personal 10% welcome coupon once per newly registered user."""
        existing = session.scalar(
            select(Promotion).where(
                Promotion.target_user_id == user_id,
                Promotion.code.like("WELCOME-%"),
            )
        )
        if existing is not None:
            return existing

        short_suffix = user_id.replace("-", "")[:8].upper()
        code = f"WELCOME-{short_suffix}"
        if session.scalar(select(Promotion).where(Promotion.code == code)) is not None:
            code = f"WELCOME-{user_id[:12].upper()}"

        promo = Promotion(
            code=code,
            description=f"Personal 10% welcome discount for {username}",
            discount_type="percentage",
            discount_value=10.0,
            min_order_amount=20.0,
            max_discount_amount=100.0,
            required_tier="standard",
            max_uses=1,
            current_uses=0,
            per_user_limit=1,
            target_user_id=user_id,
            is_active=True,
            created_by="system",
        )
        session.add(promo)
        session.commit()
        session.refresh(promo)
        return promo

    def list_promotions(
        self,
        session: Session,
        *,
        viewer_id: str | None = None,
        viewer_role: str | None = None,
        active_only: bool = True,
    ) -> list[Promotion]:
        stmt = select(Promotion)
        if viewer_role != "admin":
            if active_only:
                stmt = stmt.where(Promotion.is_active.is_(True))
            if viewer_id:
                stmt = stmt.where(
                    or_(
                        Promotion.target_user_id.is_(None),
                        Promotion.target_user_id == viewer_id,
                        Promotion.created_by == viewer_id,
                    )
                )
            else:
                stmt = stmt.where(Promotion.target_user_id.is_(None))
        elif active_only:
            stmt = stmt.where(Promotion.is_active.is_(True))

        stmt = stmt.order_by(Promotion.created_at.desc())
        return list(session.scalars(stmt).all())

    def deactivate_promotion(
        self,
        session: Session,
        promotion_id: str,
        *,
        actor_id: str,
        actor_role: str,
    ) -> Promotion:
        promo = session.get(Promotion, promotion_id)
        if promo is None:
            raise NotFoundError(f"Promotion '{promotion_id}' not found", error_code="PROMOTION_NOT_FOUND")
        if actor_role != "admin" and promo.created_by != actor_id:
            raise ForbiddenError("Only the promotion creator or an admin can deactivate it")
        promo.is_active = False
        session.commit()
        session.refresh(promo)
        return promo

    def _evaluate_rules(
        self,
        session: Session,
        *,
        code: str,
        user_id: str,
        order_subtotal: float,
        loyalty_tier: str,
        categories: list[str],
    ) -> tuple[Promotion | None, float, str | None, str | None]:
        """Return (promotion, discount_amount, failure_code, failure_reason)."""
        normalized_code = code.strip().upper()
        promo = session.scalar(select(Promotion).where(Promotion.code == normalized_code))
        if promo is None:
            return None, 0.0, "PROMOTION_NOT_FOUND", f"Promotion code '{normalized_code}' was not found"

        now = datetime.now(timezone.utc)
        if not promo.is_active:
            return promo, 0.0, "PROMOTION_INACTIVE", f"Promotion '{normalized_code}' is no longer active"
        if promo.valid_from and now < _as_utc(promo.valid_from):
            return promo, 0.0, "PROMOTION_NOT_STARTED", f"Promotion '{normalized_code}' is not active yet"
        if promo.valid_until and now > _as_utc(promo.valid_until):
            return promo, 0.0, "PROMOTION_EXPIRED", f"Promotion '{normalized_code}' has expired"
        if promo.target_user_id and promo.target_user_id != user_id:
            return promo, 0.0, "PERSONAL_COUPON_MISMATCH", "This personal coupon belongs to another account"

        user_rank = _TIER_RANKS.get(loyalty_tier, 0)
        req_rank = _TIER_RANKS.get(promo.required_tier, 0)
        if user_rank < req_rank:
            return (
                promo,
                0.0,
                "TIER_TOO_LOW",
                f"Promotion '{normalized_code}' requires loyalty tier '{promo.required_tier}' or higher",
            )

        if order_subtotal < promo.min_order_amount:
            return (
                promo,
                0.0,
                "MIN_ORDER_NOT_MET",
                f"Minimum order amount of {promo.min_order_amount:.2f} required for '{normalized_code}'",
            )

        if promo.category_filter:
            normalized_cats = {c.strip().lower() for c in categories if c.strip()}
            if promo.category_filter not in normalized_cats:
                return (
                    promo,
                    0.0,
                    "CATEGORY_MISMATCH",
                    f"Promotion '{normalized_code}' applies only to category '{promo.category_filter}'",
                )

        if promo.max_uses is not None and promo.current_uses >= promo.max_uses:
            return promo, 0.0, "PROMOTION_LIMIT_EXCEEDED", f"Promotion '{normalized_code}' usage limit reached"

        user_uses = int(
            session.scalar(
                select(func.count(CouponRedemption.id)).where(
                    CouponRedemption.promotion_id == promo.id,
                    CouponRedemption.user_id == user_id,
                    CouponRedemption.status.in_(["reserved", "committed"]),
                )
            )
            or 0
        )
        if user_uses >= promo.per_user_limit:
            return (
                promo,
                0.0,
                "USER_LIMIT_EXCEEDED",
                f"You have already used promotion '{normalized_code}' the maximum number of times",
            )

        if promo.discount_type == "percentage":
            discount = round(order_subtotal * (promo.discount_value / 100.0), 2)
        else:
            discount = round(promo.discount_value, 2)

        if promo.max_discount_amount is not None:
            discount = min(discount, promo.max_discount_amount)
        discount = round(min(discount, max(0.0, order_subtotal - 0.01)), 2) if order_subtotal > 0 else 0.0

        return promo, max(0.0, discount), None, None

    def validate_coupon(
        self,
        session: Session,
        *,
        code: str,
        user_id: str,
        order_subtotal: float,
        loyalty_tier: str,
        categories: list[str],
    ) -> PromotionValidationResponse:
        normalized_code = code.strip().upper()
        promo, discount, _, reason = self._evaluate_rules(
            session,
            code=normalized_code,
            user_id=user_id,
            order_subtotal=order_subtotal,
            loyalty_tier=loyalty_tier,
            categories=categories,
        )
        if reason is not None or promo is None:
            return PromotionValidationResponse(
                valid=False,
                code=normalized_code,
                discount_type=promo.discount_type if promo else None,
                discount_amount=0.0,
                final_amount=round(order_subtotal, 2),
                reason=reason,
            )

        return PromotionValidationResponse(
            valid=True,
            code=promo.code,
            discount_type=promo.discount_type,
            discount_amount=discount,
            final_amount=round(max(0.01, order_subtotal - discount), 2),
            reason=None,
        )

    def redeem_coupon(self, session: Session, payload: PromotionRedeemRequest) -> tuple[CouponRedemption, bool]:
        """Idempotently reserve a promotion redemption for an order_id."""
        existing = session.scalar(select(CouponRedemption).where(CouponRedemption.order_id == payload.order_id))
        if existing is not None:
            return existing, False

        promo, discount, err_code, reason = self._evaluate_rules(
            session,
            code=payload.code,
            user_id=payload.user_id,
            order_subtotal=payload.order_subtotal,
            loyalty_tier=payload.loyalty_tier,
            categories=payload.categories,
        )
        if reason is not None or promo is None:
            if err_code == "PROMOTION_NOT_FOUND":
                raise NotFoundError(reason or "Promotion not found", error_code=err_code)
            if err_code in {"PROMOTION_LIMIT_EXCEEDED", "USER_LIMIT_EXCEEDED"}:
                raise ConflictError(reason or "Promotion limit reached", error_code=err_code)
            raise BadRequestError(reason or "Coupon cannot be redeemed", error_code=err_code or "INVALID_COUPON")

        promo.current_uses += 1
        redemption = CouponRedemption(
            promotion_id=promo.id,
            code=promo.code,
            order_id=payload.order_id,
            user_id=payload.user_id,
            discount_amount=discount,
            status="reserved",
        )
        session.add(redemption)
        session.commit()
        session.refresh(redemption)
        return redemption, True

    def commit_redemption(self, session: Session, order_id: str) -> CouponRedemption | None:
        redemption = session.scalar(select(CouponRedemption).where(CouponRedemption.order_id == order_id))
        if redemption is None or redemption.status != "reserved":
            return None
        redemption.status = "committed"
        redemption.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(redemption)
        return redemption

    def release_redemption(self, session: Session, order_id: str) -> CouponRedemption | None:
        redemption = session.scalar(select(CouponRedemption).where(CouponRedemption.order_id == order_id))
        if redemption is None or redemption.status == "released":
            return redemption
        promo = session.get(Promotion, redemption.promotion_id)
        if promo is not None:
            promo.current_uses = max(0, promo.current_uses - 1)
        redemption.status = "released"
        redemption.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(redemption)
        return redemption
