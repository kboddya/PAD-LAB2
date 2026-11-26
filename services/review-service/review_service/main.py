"""FastAPI application entrypoint for review-service."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, FastAPI, Query, Request, status
from sqlalchemy.orm import Session

from review_service.config import ReviewSettings
from review_service.models import ReviewBase
from review_service.schemas import (
    ProductRatingSummaryResponse,
    ReviewCreateRequest,
    ReviewModerateRequest,
    ReviewReplyRequest,
    ReviewResponse,
)
from review_service.service import ReviewDomainService
from shared.app_factory import build_service_app
from shared.database import DatabaseManager
from shared.errors import UnauthorizedError
from shared.messaging import EventBus, create_event_bus
from shared.security import UserContext, extract_user_context, require_roles


def create_app(
    settings: ReviewSettings | None = None,
    event_bus: EventBus | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Create and configure the review-service FastAPI application."""
    cfg = settings or ReviewSettings()
    db = DatabaseManager(cfg.database_url, ReviewBase)
    bus = event_bus or create_event_bus(cfg.rabbitmq_url, cfg.service_name)
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=cfg.downstream_timeout_seconds)

    async def _shutdown_client() -> None:
        if owns_client:
            await client.aclose()

    app, logger = build_service_app(
        title="ShopFlow Review Service",
        description="Verified-purchase product reviews, anti-spam moderation, and rating summaries.",
        settings=cfg,
        db=db,
        event_bus=bus,
        on_shutdown=_shutdown_client,
    )
    domain = ReviewDomainService(cfg, logger)

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

    async def _publish_summary_event(
        event_type: str,
        *,
        review_id: str,
        product_id: str,
        seller_id: str,
        reviewer_id: str,
        rating: int,
        moderation_status: str,
        session: Session,
    ) -> None:
        summary = domain.compute_product_summary(session, product_id)
        await bus.publish(
            event_type=event_type,
            payload={
                "review_id": review_id,
                "product_id": product_id,
                "seller_id": seller_id,
                "recipient_user_id": seller_id,
                "reviewer_id": reviewer_id,
                "rating": rating,
                "moderation_status": moderation_status,
                "average_rating": summary.average_rating,
                "review_count": summary.review_count,
            },
            source_service=cfg.service_name,
        )

    router = APIRouter(prefix="/reviews", tags=["Reviews"])

    @router.post(
        "",
        response_model=ReviewResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Publish a verified-purchase product review",
    )
    async def create_review(
        payload: ReviewCreateRequest,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> ReviewResponse:
        caller = _get_caller(request)
        review = await domain.create_review(
            session,
            client,
            user_id=caller.user_id,
            role=caller.role,
            payload=payload,
        )
        if review.moderation_status == "approved":
            await _publish_summary_event(
                "review.published",
                review_id=review.id,
                product_id=review.product_id,
                seller_id=review.seller_id,
                reviewer_id=review.user_id,
                rating=review.rating,
                moderation_status=review.moderation_status,
                session=session,
            )
        logger.info(
            "Review submitted",
            extra={"review_id": review.id, "product_id": review.product_id, "moderation": review.moderation_status},
        )
        return ReviewResponse.model_validate(review)

    @router.get("", response_model=list[ReviewResponse], summary="List product reviews")
    async def list_reviews(
        request: Request,
        product_id: str | None = Query(default=None),
        rating: int | None = Query(default=None, ge=1, le=5),
        include_flagged: bool = Query(default=False),
        skip: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
        session: Session = Depends(db.get_db),
    ) -> list[ReviewResponse]:
        allow_flagged = False
        if include_flagged:
            caller = _get_optional_caller(request)
            if caller and caller.role == "admin":
                allow_flagged = True
        rows = domain.list_reviews(
            session,
            product_id=product_id,
            rating=rating,
            include_flagged=allow_flagged,
            skip=skip,
            limit=limit,
        )
        return [ReviewResponse.model_validate(r) for r in rows]

    @router.get(
        "/product/{product_id}/summary",
        response_model=ProductRatingSummaryResponse,
        summary="Get rating summary for a product",
    )
    async def get_product_summary(
        product_id: str,
        session: Session = Depends(db.get_db),
    ) -> ProductRatingSummaryResponse:
        return domain.compute_product_summary(session, product_id)

    @router.post("/{review_id}/helpful", response_model=ReviewResponse, summary="Vote a review helpful")
    async def vote_helpful(
        review_id: str,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> ReviewResponse:
        caller = _get_caller(request)
        review = domain.vote_helpful(session, review_id, user_id=caller.user_id)
        return ReviewResponse.model_validate(review)

    @router.post("/{review_id}/reply", response_model=ReviewResponse, summary="Seller reply to review")
    async def reply_to_review(
        review_id: str,
        payload: ReviewReplyRequest,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> ReviewResponse:
        caller = _get_caller(request)
        require_roles(caller, {"seller", "admin"})
        review = domain.add_seller_reply(
            session,
            review_id,
            seller_reply=payload.seller_reply,
            actor_id=caller.user_id,
            actor_role=caller.role,
        )
        return ReviewResponse.model_validate(review)

    @router.patch("/{review_id}/moderate", response_model=ReviewResponse, summary="Moderate flagged review (admin)")
    async def moderate_review(
        review_id: str,
        payload: ReviewModerateRequest,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> ReviewResponse:
        caller = _get_caller(request)
        require_roles(caller, {"admin"})
        review = domain.moderate_review(session, review_id, status_value=payload.moderation_status)
        await _publish_summary_event(
            "review.updated",
            review_id=review.id,
            product_id=review.product_id,
            seller_id=review.seller_id,
            reviewer_id=review.user_id,
            rating=review.rating,
            moderation_status=review.moderation_status,
            session=session,
        )
        return ReviewResponse.model_validate(review)

    @router.delete("/{review_id}", summary="Delete a review (author or admin)")
    async def delete_review(
        review_id: str,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> dict[str, str]:
        caller = _get_caller(request)
        deleted = domain.delete_review(
            session,
            review_id,
            actor_id=caller.user_id,
            actor_role=caller.role,
        )
        await _publish_summary_event(
            "review.deleted",
            review_id=deleted.id,
            product_id=deleted.product_id,
            seller_id=deleted.seller_id,
            reviewer_id=deleted.user_id,
            rating=deleted.rating,
            moderation_status=deleted.moderation_status,
            session=session,
        )
        return {"status": "deleted", "review_id": review_id}

    app.include_router(router)
    return app
