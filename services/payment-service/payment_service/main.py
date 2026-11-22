"""FastAPI application entrypoint for payment-service."""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response, status
from sqlalchemy.orm import Session

from payment_service.config import PaymentSettings
from payment_service.models import PaymentBase
from payment_service.schemas import PaymentCreateRequest, PaymentResponse
from payment_service.service import PaymentDomainService
from shared.app_factory import build_service_app
from shared.database import DatabaseManager
from shared.errors import ForbiddenError
from shared.messaging import EventBus, create_event_bus
from shared.security import UserContext, extract_user_context, is_valid_internal_token


def create_app(
    settings: PaymentSettings | None = None,
    event_bus: EventBus | None = None,
) -> FastAPI:
    """Create and configure the payment-service FastAPI application."""
    cfg = settings or PaymentSettings()
    db = DatabaseManager(cfg.database_url, PaymentBase)
    bus = event_bus or create_event_bus(cfg.rabbitmq_url, cfg.service_name)
    domain = PaymentDomainService(cfg)

    app, logger = build_service_app(
        title="ShopFlow Payment Service",
        description="Idempotent payment processing, refunds, and Saga event publishing.",
        settings=cfg,
        db=db,
        event_bus=bus,
    )

    def _get_caller(request: Request) -> UserContext:
        return extract_user_context(
            request,
            jwt_secret=cfg.jwt_secret,
            jwt_algorithm=cfg.jwt_algorithm,
            internal_service_token=cfg.internal_service_token,
        )

    router = APIRouter(prefix="/payments", tags=["Payments"])

    @router.post(
        "",
        response_model=PaymentResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Process a payment idempotently",
    )
    async def create_payment(
        payload: PaymentCreateRequest,
        request: Request,
        response: Response,
        session: Session = Depends(db.get_db),
    ) -> PaymentResponse:
        if not is_valid_internal_token(request, cfg.internal_service_token):
            caller = _get_caller(request)
            if caller.role != "admin" and caller.user_id != payload.user_id:
                raise ForbiddenError("Cannot initiate payment on behalf of another user")

        payment, is_new = domain.process_payment(session, payload)
        if not is_new:
            response.status_code = status.HTTP_200_OK
            return PaymentResponse.model_validate(payment)

        if payment.status == "succeeded":
            await bus.publish(
                event_type="payment.succeeded",
                payload={
                    "payment_id": payment.id,
                    "order_id": payment.order_id,
                    "user_id": payment.user_id,
                    "amount": payment.amount,
                    "currency": payment.currency,
                },
                source_service=cfg.service_name,
            )
            logger.info("Payment succeeded", extra={"payment_id": payment.id, "order_id": payment.order_id})
        else:
            await bus.publish(
                event_type="payment.failed",
                payload={
                    "payment_id": payment.id,
                    "order_id": payment.order_id,
                    "user_id": payment.user_id,
                    "amount": payment.amount,
                    "currency": payment.currency,
                    "reason": payment.failure_reason or "Payment declined",
                },
                source_service=cfg.service_name,
            )
            logger.warning(
                "Payment failed",
                extra={"payment_id": payment.id, "order_id": payment.order_id, "reason": payment.failure_reason},
            )

        return PaymentResponse.model_validate(payment)

    @router.get("", response_model=list[PaymentResponse], summary="List payments (admin or own)")
    async def list_payments(
        request: Request,
        status_filter: str | None = Query(default=None, alias="status"),
        user_id: str | None = Query(default=None),
        skip: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
        session: Session = Depends(db.get_db),
    ) -> list[PaymentResponse]:
        caller = _get_caller(request)
        effective_user_id = user_id if caller.role == "admin" else caller.user_id
        if user_id and caller.role != "admin" and user_id != caller.user_id:
            raise ForbiddenError("Cannot view other users' payments")
        payments = domain.list_payments(
            session,
            user_id=effective_user_id,
            status=status_filter,
            skip=skip,
            limit=limit,
        )
        return [PaymentResponse.model_validate(p) for p in payments]

    @router.get("/by-order/{order_id}", response_model=PaymentResponse, summary="Get payment by order ID")
    async def get_payment_by_order(
        order_id: str,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> PaymentResponse:
        caller = _get_caller(request)
        payment = domain.get_by_order(session, order_id)
        if caller.role != "admin" and payment.user_id != caller.user_id:
            raise ForbiddenError("Access denied to another user's payment")
        return PaymentResponse.model_validate(payment)

    @router.get("/{payment_id}", response_model=PaymentResponse, summary="Get payment by ID")
    async def get_payment(
        payment_id: str,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> PaymentResponse:
        caller = _get_caller(request)
        payment = domain.get_payment(session, payment_id)
        if caller.role != "admin" and payment.user_id != caller.user_id:
            raise ForbiddenError("Access denied to another user's payment")
        return PaymentResponse.model_validate(payment)

    @router.post("/{payment_id}/refund", response_model=PaymentResponse, summary="Refund a succeeded payment")
    async def refund_payment(
        payment_id: str,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> PaymentResponse:
        caller = _get_caller(request)
        payment = domain.refund_payment(
            session,
            payment_id,
            actor_id=caller.user_id,
            actor_role=caller.role,
        )
        await bus.publish(
            event_type="payment.refunded",
            payload={
                "payment_id": payment.id,
                "order_id": payment.order_id,
                "user_id": payment.user_id,
                "amount": payment.amount,
                "currency": payment.currency,
            },
            source_service=cfg.service_name,
        )
        logger.info("Payment refunded", extra={"payment_id": payment.id, "order_id": payment.order_id})
        return PaymentResponse.model_validate(payment)

    app.include_router(router)
    return app
