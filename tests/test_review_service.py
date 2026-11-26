"""Tests for review-service (verified purchase gate, anti-spam, helpful votes, product rating sync)."""

from __future__ import annotations

import pytest

from tests.conftest import internal_headers, register_user_via_gateway


@pytest.mark.asyncio
async def test_review_verified_purchase_moderation_and_product_rating_sync(cluster) -> None:
    gw = cluster.gateway_client
    reviews_client = cluster.service_clients["review-service"]
    assert gw is not None

    seller = await register_user_via_gateway(gw, email="rsell@shop.io", username="rsell", role="seller")
    buyer = await register_user_via_gateway(gw, email="rbuy@shop.io", username="rbuy", role="customer")
    other = await register_user_via_gateway(gw, email="rother@shop.io", username="rother", role="customer")

    s_hdr = {"Authorization": f"Bearer {seller['access_token']}"}
    b_hdr = {"Authorization": f"Bearer {buyer['access_token']}"}
    o_hdr = {"Authorization": f"Bearer {other['access_token']}"}

    prod = (
        await gw.post(
            "/api/products",
            headers=s_hdr,
            json={"sku": "REV-1", "name": "Ergonomic Chair", "category": "office", "price": 250.0, "stock_quantity": 5},
        )
    ).json()

    # Unpurchased user cannot leave a review
    unpurchased = await gw.post(
        "/api/reviews",
        headers=o_hdr,
        json={"product_id": prod["id"], "rating": 5, "title": "Fake review", "comment": "Never bought it"},
    )
    assert unpurchased.status_code == 403
    assert unpurchased.json()["error"]["code"] == "PURCHASE_REQUIRED"

    # Buyer purchases the product
    order = await gw.post(
        "/api/orders",
        headers=b_hdr,
        json={"items": [{"product_id": prod["id"], "quantity": 1}], "shipping_address": "10 Oak St"},
    )
    assert order.status_code == 201

    # Verified purchase review succeeds & projects rating onto product-service
    rev = await gw.post(
        "/api/reviews",
        headers=b_hdr,
        json={
            "product_id": prod["id"],
            "rating": 5,
            "title": "Solid support",
            "comment": "Very comfortable for 8-hour coding sessions.",
        },
    )
    assert rev.status_code == 201
    rev_data = rev.json()
    assert rev_data["verified_purchase"] is True
    assert rev_data["moderation_status"] == "approved"

    prod_after = (await gw.get(f"/api/products/{prod['id']}")).json()
    assert prod_after["average_rating"] == 5.0
    assert prod_after["review_count"] == 1

    # Duplicate review rejected
    dup = await gw.post(
        "/api/reviews",
        headers=b_hdr,
        json={"product_id": prod["id"], "rating": 4, "title": "Again", "comment": "Second attempt"},
    )
    assert dup.status_code == 409

    # Self-vote blocked, other user vote succeeds once
    self_vote = await gw.post(f"/api/reviews/{rev_data['id']}/helpful", headers=b_hdr)
    assert self_vote.status_code == 400
    other_vote = await gw.post(f"/api/reviews/{rev_data['id']}/helpful", headers=o_hdr)
    assert other_vote.status_code == 200
    assert other_vote.json()["helpful_count"] == 1

    # Seller replies to verified review
    replied = await gw.post(
        f"/api/reviews/{rev_data['id']}/reply",
        headers=s_hdr,
        json={"seller_reply": "Thank you for the detailed feedback!"},
    )
    assert replied.status_code == 200
    assert "Thank you" in replied.json()["seller_reply"]

    # Spam review is automatically flagged & excluded until admin approval
    await gw.post(
        "/api/orders",
        headers=o_hdr,
        json={"items": [{"product_id": prod["id"], "quantity": 1}], "shipping_address": "12 Pine St"},
    )
    spam_rev = await gw.post(
        "/api/reviews",
        headers=o_hdr,
        json={
            "product_id": prod["id"],
            "rating": 1,
            "title": "Free crypto giveaway",
            "comment": "Visit http://spam.example to claim reward",
        },
    )
    assert spam_rev.status_code == 201
    assert spam_rev.json()["moderation_status"] == "flagged"

    public_list = (await gw.get(f"/api/reviews?product_id={prod['id']}")).json()
    assert len(public_list) == 1  # flagged spam hidden publicly

    # Admin rejects spam and deletes buyer review -> product rating resets to 0
    await reviews_client.patch(
        f"/reviews/{spam_rev.json()['id']}/moderate",
        headers=internal_headers("admin-1", "admin"),
        json={"moderation_status": "rejected"},
    )
    await gw.delete(f"/api/reviews/{rev_data['id']}", headers=b_hdr)
    prod_reset = (await gw.get(f"/api/products/{prod['id']}")).json()
    assert prod_reset["average_rating"] == 0.0
    assert prod_reset["review_count"] == 0
