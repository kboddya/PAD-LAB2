"""FastAPI application entrypoint for promotion-service."""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response, status
from sqlalchemy.orm import Session

from promotion_service.config import PromotionSettings
from promotion_service.models import PromotionBase
from promotion_service.schemas import (
    CouponRedemptionResponse,
    PromotionCreateRequest,
    PromotionRedeemRequest,
    PromotionReleaseRequest,
    PromotionResponse,
    PromotionValidateRequest,
    PromotionValidationResponse,
)
from promotion_service.service import PromotionDomainService
from shared.app_factory import build_service_app
from shared.database import DatabaseManager
from shared.errors import UnauthorizedError
from shared.messaging import EventBus, EventEnvelope, create_event_bus
from shared.security import (
    UserContext,
    extract_user_context,
    is_valid_internal_token,
    require_roles,
)


def create_app(
    settings: PromotionSettings | None = None,
    event_bus: EventBus | None = None,
) -> FastAPI:
    """Create and configure the promotion-service FastAPI application."""
    cfg = settings or PromotionSettings()
    db = DatabaseManager(cfg.database_url, PromotionBase)
    bus = event_bus or create_event_bus(cfg.rabbitmq_url, cfg.service_name)
    domain = PromotionDomainService()

    app, logger = build_service_app(
        title="ShopFlow Promotion Service",
        description="Coupon campaigns, discount rule evaluation, and Saga-safe redemptions.",
        settings=cfg,
        db=db,
        event_bus=bus,
    )

    def _on_user_registered(envelope: EventEnvelope) -> None:
        data = envelope.payload
        user_id = str(data.get("user_id", ""))
        if not user_id:
            return
        with db.session_scope() as session:
            promo = domain.create_welcome_coupon_idempotent(
                session,
                user_id=user_id,
                username=str(data.get("username", "shopper")),
            )
        logger.info("Provisioned welcome coupon for new user", extra={"user_id": user_id, "code": promo.code})

    async def _on_payment_succeeded(envelope: EventEnvelope) -> None:
        order_id = str(envelope.payload.get("order_id", ""))
        if not order_id:
            return
        with db.session_scope() as session:
            redemption = domain.commit_redemption(session, order_id)
        if redemption is not None:
            await bus.publish(
                event_type="promotion.redeemed",
                payload={
                    "order_id": redemption.order_id,
                    "user_id": redemption.user_id,
                    "code": redemption.code,
                    "discount_amount": redemption.discount_amount,
                },
                source_service=cfg.service_name,
                request_id=envelope.request_id,
            )
            logger.info("Committed coupon redemption", extra={"order_id": order_id, "code": redemption.code})

    def _on_saga_compensation(envelope: EventEnvelope) -> None:
        order_id = str(envelope.payload.get("order_id", ""))
        if not order_id:
            return
        with db.session_scope() as session:
            released = domain.release_redemption(session, order_id)
        if released is not None:
            logger.info(
                "Released coupon redemption via Saga compensation",
                extra={"order_id": order_id, "code": released.code, "event_type": envelope.event_type},
            )

    bus.on("user.registered", _on_user_registered)
    bus.on("payment.succeeded", _on_payment_succeeded)
    bus.on("payment.failed", _on_saga_compensation)
    bus.on("order.cancelled", _on_saga_compensation)

    def _get_caller(request: Request) -> UserContext:
        return extract_user_context(
            request,
            jwt_secret=cfg.jwt_secret,
            jwt_algorithm=cfg.jwt_algorithm,
            internal_service_token=cfg.internal_service_token,
        )

    def _get_optional_caller(request: Request) -> UserContext | None:
        try:
            return _get_caller(request)
        except UnauthorizedError:
            return None

    def _require_internal_or_admin(request: Request) -> None:
        if not is_valid_internal_token(request, cfg.internal_service_token):
            caller = _get_caller(request)
            require_roles(caller, {"admin"})

    router = APIRouter(prefix="/promotions", tags=["Promotions"])

    @router.post(
        "",
        response_model=PromotionResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Create a new promotional campaign (seller/admin)",
    )
    async def create_promotion(
        payload: PromotionCreateRequest,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> PromotionResponse:
        caller = _get_caller(request)
        require_roles(caller, {"seller", "admin"})
        promo = domain.create_promotion(session, payload, created_by=caller.user_id)
        logger.info("Promotion created", extra={"code": promo.code, "created_by": caller.user_id})
        return PromotionResponse.model_validate(promo)

    @router.get("", response_model=list[PromotionResponse], summary="List active public and personal promotions")
    async def list_promotions(
        request: Request,
        active_only: bool = Query(default=True),
        session: Session = Depends(db.get_db),
    ) -> list[PromotionResponse]:
        caller = _get_optional_caller(request)
        rows = domain.list_promotions(
            session,
            viewer_id=caller.user_id if caller else None,
            viewer_role=caller.role if caller else None,
            active_only=active_only,
        )
        return [PromotionResponse.model_validate(r) for r in rows]

    @router.post(
        "/validate",
        response_model=PromotionValidationResponse,
        summary="Validate a coupon code and preview calculated discount",
    )
    async def validate_promotion(
        payload: PromotionValidateRequest,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> PromotionValidationResponse:
        caller = _get_caller(request)
        target_user = payload.user_id if (payload.user_id and is_valid_internal_token(request, cfg.internal_service_token)) else caller.user_id
        return domain.validate_coupon(
            session,
            code=payload.code,
            user_id=target_user,
            order_subtotal=payload.order_subtotal,
            loyalty_tier=payload.loyalty_tier,
            categories=payload.categories,
        )

    @router.post(
        "/redeem",
        response_model=CouponRedemptionResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Reserve/redeem a coupon idempotently for an order (internal/admin)",
    )
    async def redeem_promotion(
        payload: PromotionRedeemRequest,
        request: Request,
        response: Response,
        session: Session = Depends(db.get_db),
    ) -> CouponRedemptionResponse:
        _require_internal_or_admin(request)
        redemption, is_new = domain.redeem_coupon(session, payload)
        if not is_new:
            response.status_code = status.HTTP_200_OK
        return CouponRedemptionResponse.model_validate(redemption)

    @router.post(
        "/release",
        summary="Release a reserved coupon redemption on order failure (internal/admin)",
    )
    async def release_promotion(
        payload: PromotionReleaseRequest,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> dict[str, str]:
        _require_internal_or_admin(request)
        released = domain.release_redemption(session, payload.order_id)
        return {
            "order_id": payload.order_id,
            "status": released.status if released else "not_found",
        }

    @router.patch(
        "/{promotion_id}/deactivate",
        response_model=PromotionResponse,
        summary="Deactivate a promotional campaign (seller/admin)",
    )
    async def deactivate_promotion(
        promotion_id: str,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> PromotionResponse:
        caller = _get_caller(request)
        require_roles(caller, {"seller", "admin"})
        promo = domain.deactivate_promotion(
            session,
            promotion_id,
            actor_id=caller.user_id,
            actor_role=caller.role,
        )
        return PromotionResponse.model_validate(promo)

    app.include_router(router)
    return app
