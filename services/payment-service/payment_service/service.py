"""Domain business logic for idempotent payment processing and refunds."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from payment_service.config import PaymentSettings
from payment_service.models import Payment
from payment_service.schemas import PaymentCreateRequest
from shared.errors import ConflictError, ForbiddenError, NotFoundError


class PaymentDomainService:
    """Processes order payments idempotently and handles refunds."""

    def __init__(self, settings: PaymentSettings) -> None:
        self.settings = settings

    def process_payment(self, session: Session, payload: PaymentCreateRequest) -> tuple[Payment, bool]:
        idem_key = (payload.idempotency_key or f"order-{payload.order_id}").strip()

        existing = session.scalar(
            select(Payment).where(
                (Payment.idempotency_key == idem_key) | (Payment.order_id == payload.order_id)
            )
        )
        if existing is not None:
            return existing, False

        status = "succeeded"
        failure_reason: str | None = None
        if payload.simulate_failure:
            status = "failed"
            failure_reason = "Payment declined by simulated gateway"
        elif payload.amount > self.settings.max_single_payment_amount:
            status = "failed"
            failure_reason = (
                f"Amount {payload.amount:.2f} exceeds single transaction limit "
                f"{self.settings.max_single_payment_amount:.2f}"
            )

        payment = Payment(
            order_id=payload.order_id,
            user_id=payload.user_id,
            amount=round(float(payload.amount), 2),
            currency=payload.currency.strip().upper(),
            payment_method=payload.payment_method.strip().lower(),
            status=status,
            idempotency_key=idem_key,
            failure_reason=failure_reason,
        )
        session.add(payment)
        session.commit()
        session.refresh(payment)
        return payment, True

    def list_payments(
        self,
        session: Session,
        *,
        user_id: str | None = None,
        status: str | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> list[Payment]:
        stmt = select(Payment)
        if user_id:
            stmt = stmt.where(Payment.user_id == user_id)
        if status:
            stmt = stmt.where(Payment.status == status)
        stmt = stmt.order_by(Payment.created_at.desc()).offset(skip).limit(limit)
        return list(session.scalars(stmt).all())

    def get_payment(self, session: Session, payment_id: str) -> Payment:
        payment = session.get(Payment, payment_id)
        if payment is None:
            raise NotFoundError(f"Payment '{payment_id}' not found", error_code="PAYMENT_NOT_FOUND")
        return payment

    def get_by_order(self, session: Session, order_id: str) -> Payment:
        payment = session.scalar(select(Payment).where(Payment.order_id == order_id))
        if payment is None:
            raise NotFoundError(
                f"Payment for order '{order_id}' not found",
                error_code="PAYMENT_NOT_FOUND",
            )
        return payment

    def refund_payment(
        self,
        session: Session,
        payment_id: str,
        *,
        actor_id: str,
        actor_role: str,
    ) -> Payment:
        payment = self.get_payment(session, payment_id)
        if actor_role != "admin" and payment.user_id != actor_id:
            raise ForbiddenError("Only the payer or an admin can refund this payment")
        if payment.status != "succeeded":
            raise ConflictError(
                f"Cannot refund payment in status '{payment.status}'",
                error_code="INVALID_PAYMENT_STATE",
            )
        payment.status = "refunded"
        payment.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(payment)
        return payment
