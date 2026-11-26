"""Domain logic for verified-purchase verification, anti-spam moderation, and rating summaries."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from review_service.config import ReviewSettings
from review_service.models import Review, ReviewVote
from review_service.schemas import (
    ProductRatingSummaryResponse,
    ReviewCreateRequest,
)
from shared.errors import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ServiceTimeoutError,
    ServiceUnavailableError,
)
from shared.logging import get_request_id

_SPAM_MARKERS: tuple[str, ...] = (
    "buy followers",
    "free crypto",
    "scam link",
    "http://",
    "https://",
    "spam",
)
_QUALIFYING_ORDER_STATUSES = {"paid", "shipped", "delivered"}


class ReviewDomainService:
    """Handles verified-purchase review creation, moderation, helpfulness votes, and summaries."""

    def __init__(self, settings: ReviewSettings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger

    def _service_headers(self, user_id: str, role: str) -> dict[str, str]:
        return {
            "X-Request-ID": get_request_id(),
            "X-User-Id": user_id,
            "X-User-Role": role,
            "X-Internal-Token": self.settings.internal_service_token,
        }

    async def _fetch_product(self, client: httpx.AsyncClient, product_id: str, headers: dict[str, str]) -> dict[str, Any]:
        url = f"{self.settings.product_service_url}/products/{product_id}"
        try:
            resp = await client.get(url, headers=headers)
        except httpx.TimeoutException as exc:
            raise ServiceTimeoutError("product-service timed out") from exc
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("product-service is unavailable") from exc

        if resp.status_code == 404:
            raise NotFoundError(f"Product '{product_id}' not found", error_code="PRODUCT_NOT_FOUND")
        if resp.status_code >= 500:
            raise ServiceUnavailableError("product-service returned an internal error")
        return resp.json()

    async def _verify_purchase_order(
        self,
        client: httpx.AsyncClient,
        *,
        product_id: str,
        user_id: str,
        role: str,
    ) -> str:
        url = f"{self.settings.order_service_url}/orders"
        try:
            resp = await client.get(
                url,
                params={"limit": 200},
                headers=self._service_headers(user_id, role),
            )
        except httpx.TimeoutException as exc:
            raise ServiceTimeoutError("order-service timed out verifying purchase") from exc
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("order-service is unavailable") from exc

        if resp.status_code >= 500:
            raise ServiceUnavailableError("order-service returned an internal error")

        orders = resp.json() if resp.status_code == 200 else []
        for order in orders:
            if order.get("status") not in _QUALIFYING_ORDER_STATUSES:
                continue
            for item in order.get("items", []):
                if item.get("product_id") == product_id:
                    return str(order["id"])

        raise ForbiddenError(
            "Verified purchase required: only buyers who paid for this product can leave a review",
            error_code="PURCHASE_REQUIRED",
        )

    @staticmethod
    def _moderate_content(title: str, comment: str) -> str:
        combined = f"{title} {comment}".lower()
        if any(marker in combined for marker in _SPAM_MARKERS):
            return "flagged"
        return "approved"

    async def create_review(
        self,
        session: Session,
        client: httpx.AsyncClient,
        *,
        user_id: str,
        role: str,
        payload: ReviewCreateRequest,
    ) -> Review:
        existing = session.scalar(
            select(Review).where(Review.product_id == payload.product_id, Review.user_id == user_id)
        )
        if existing is not None:
            raise ConflictError("You have already reviewed this product", error_code="REVIEW_EXISTS")

        headers = self._service_headers(user_id, role)
        product = await self._fetch_product(client, payload.product_id, headers)
        order_id = await self._verify_purchase_order(
            client,
            product_id=payload.product_id,
            user_id=user_id,
            role=role,
        )

        moderation_status = self._moderate_content(payload.title, payload.comment)
        review = Review(
            product_id=payload.product_id,
            seller_id=str(product.get("seller_id", "")),
            user_id=user_id,
            order_id=order_id,
            rating=payload.rating,
            title=payload.title.strip(),
            comment=payload.comment.strip(),
            verified_purchase=True,
            moderation_status=moderation_status,
        )
        session.add(review)
        session.commit()
        session.refresh(review)
        return review

    def list_reviews(
        self,
        session: Session,
        *,
        product_id: str | None = None,
        rating: int | None = None,
        include_flagged: bool = False,
        skip: int = 0,
        limit: int = 50,
    ) -> list[Review]:
        stmt = select(Review)
        if not include_flagged:
            stmt = stmt.where(Review.moderation_status == "approved")
        if product_id:
            stmt = stmt.where(Review.product_id == product_id)
        if rating is not None:
            stmt = stmt.where(Review.rating == rating)
        stmt = stmt.order_by(Review.helpful_count.desc(), Review.created_at.desc()).offset(skip).limit(limit)
        return list(session.scalars(stmt).all())

    def get_review(self, session: Session, review_id: str) -> Review:
        review = session.get(Review, review_id)
        if review is None:
            raise NotFoundError(f"Review '{review_id}' not found", error_code="REVIEW_NOT_FOUND")
        return review

    def compute_product_summary(self, session: Session, product_id: str) -> ProductRatingSummaryResponse:
        approved = list(
            session.scalars(
                select(Review).where(
                    Review.product_id == product_id,
                    Review.moderation_status == "approved",
                )
            ).all()
        )
        dist = {str(star): 0 for star in range(1, 6)}
        for rev in approved:
            key = str(rev.rating)
            if key in dist:
                dist[key] += 1

        count = len(approved)
        avg = round(sum(r.rating for r in approved) / count, 2) if count > 0 else 0.0
        return ProductRatingSummaryResponse(
            product_id=product_id,
            average_rating=avg,
            review_count=count,
            rating_distribution=dist,
        )

    def vote_helpful(self, session: Session, review_id: str, *, user_id: str) -> Review:
        review = self.get_review(session, review_id)
        if review.user_id == user_id:
            raise BadRequestError("Cannot vote on your own review", error_code="SELF_VOTE_FORBIDDEN")
        existing = session.scalar(
            select(ReviewVote).where(ReviewVote.review_id == review_id, ReviewVote.user_id == user_id)
        )
        if existing is not None:
            raise ConflictError("You have already marked this review helpful", error_code="ALREADY_VOTED")

        session.add(ReviewVote(review_id=review_id, user_id=user_id))
        review.helpful_count += 1
        review.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(review)
        return review

    def add_seller_reply(
        self,
        session: Session,
        review_id: str,
        *,
        seller_reply: str,
        actor_id: str,
        actor_role: str,
    ) -> Review:
        review = self.get_review(session, review_id)
        if actor_role != "admin" and review.seller_id != actor_id:
            raise ForbiddenError("Only the product seller or an admin can reply to this review")
        review.seller_reply = seller_reply.strip()
        review.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(review)
        return review

    def moderate_review(self, session: Session, review_id: str, *, status_value: str) -> Review:
        review = self.get_review(session, review_id)
        review.moderation_status = status_value
        review.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(review)
        return review

    def delete_review(self, session: Session, review_id: str, *, actor_id: str, actor_role: str) -> Review:
        review = self.get_review(session, review_id)
        if actor_role != "admin" and review.user_id != actor_id:
            raise ForbiddenError("Only the review author or an admin can delete this review")
        session.delete(review)
        session.commit()
        return review
