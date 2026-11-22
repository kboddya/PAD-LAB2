"""FastAPI application entrypoint for product-service."""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, Query, Request, status
from sqlalchemy.orm import Session

from product_service.config import ProductSettings
from product_service.models import ProductBase
from product_service.schemas import (
    InventoryActionResponse,
    InventoryAdjustRequest,
    ProductCreateRequest,
    ProductResponse,
    ProductUpdateRequest,
)
from product_service.service import ProductDomainService
from shared.app_factory import build_service_app
from shared.database import DatabaseManager
from shared.messaging import EventBus, EventEnvelope, create_event_bus
from shared.security import (
    UserContext,
    extract_user_context,
    is_valid_internal_token,
    require_roles,
)


def create_app(
    settings: ProductSettings | None = None,
    event_bus: EventBus | None = None,
) -> FastAPI:
    """Create and configure the product-service FastAPI application."""
    cfg = settings or ProductSettings()
    db = DatabaseManager(cfg.database_url, ProductBase)
    bus = event_bus or create_event_bus(cfg.rabbitmq_url, cfg.service_name)
    domain = ProductDomainService()

    app, logger = build_service_app(
        title="ShopFlow Product Service",
        description="Product catalog, pricing, and Saga-safe inventory reservations.",
        settings=cfg,
        db=db,
        event_bus=bus,
    )

    def _on_payment_succeeded(envelope: EventEnvelope) -> None:
        order_id = str(envelope.payload.get("order_id", ""))
        if not order_id:
            return
        with db.session_scope() as session:
            count = domain.commit_order_reservations(session, order_id)
        logger.info("Committed stock reservations for paid order", extra={"order_id": order_id, "count": count})

    def _on_saga_compensation(envelope: EventEnvelope) -> None:
        order_id = str(envelope.payload.get("order_id", ""))
        if not order_id:
            return
        with db.session_scope() as session:
            count = domain.release_order_reservations(session, order_id)
        logger.info(
            "Released stock reservations via Saga compensation",
            extra={"order_id": order_id, "event_type": envelope.event_type, "count": count},
        )

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

    def _require_internal_or_privileged(request: Request) -> None:
        if not is_valid_internal_token(request, cfg.internal_service_token):
            caller = _get_caller(request)
            require_roles(caller, {"seller", "admin"})

    router = APIRouter(prefix="/products", tags=["Products"])

    @router.post(
        "",
        response_model=ProductResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Create a new catalog product (seller/admin)",
    )
    async def create_product(
        payload: ProductCreateRequest,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> ProductResponse:
        caller = _get_caller(request)
        require_roles(caller, {"seller", "admin"})
        product = domain.create_product(session, payload, seller_id=caller.user_id)
        logger.info("Product created", extra={"product_id": product.id, "sku": product.sku})
        return ProductResponse.model_validate(product)

    @router.get("", response_model=list[ProductResponse], summary="List and filter catalog products")
    async def list_products(
        category: str | None = Query(default=None),
        search: str | None = Query(default=None),
        in_stock: bool = Query(default=False),
        skip: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
        session: Session = Depends(db.get_db),
    ) -> list[ProductResponse]:
        products = domain.list_products(
            session,
            category=category,
            search=search,
            in_stock=in_stock,
            skip=skip,
            limit=limit,
        )
        return [ProductResponse.model_validate(p) for p in products]

    @router.get("/{product_id}", response_model=ProductResponse, summary="Get product details by ID")
    async def get_product(
        product_id: str,
        session: Session = Depends(db.get_db),
    ) -> ProductResponse:
        product = domain.get_product(session, product_id)
        return ProductResponse.model_validate(product)

    @router.put("/{product_id}", response_model=ProductResponse, summary="Update product details (seller/admin)")
    async def update_product(
        product_id: str,
        payload: ProductUpdateRequest,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> ProductResponse:
        caller = _get_caller(request)
        require_roles(caller, {"seller", "admin"})
        product = domain.update_product(
            session,
            product_id,
            payload,
            actor_id=caller.user_id,
            actor_role=caller.role,
        )
        return ProductResponse.model_validate(product)

    @router.delete(
        "/{product_id}",
        status_code=status.HTTP_200_OK,
        summary="Soft-delete a catalog product (seller/admin)",
    )
    async def delete_product(
        product_id: str,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> dict[str, str]:
        caller = _get_caller(request)
        require_roles(caller, {"seller", "admin"})
        domain.delete_product(session, product_id, actor_id=caller.user_id, actor_role=caller.role)
        return {"status": "deleted", "product_id": product_id}

    @router.post(
        "/{product_id}/reserve",
        response_model=InventoryActionResponse,
        summary="Reserve product inventory for an order (internal/seller/admin)",
    )
    async def reserve_stock(
        product_id: str,
        payload: InventoryAdjustRequest,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> InventoryActionResponse:
        _require_internal_or_privileged(request)
        return domain.reserve_stock(session, product_id, payload)

    @router.post(
        "/{product_id}/release",
        response_model=InventoryActionResponse,
        summary="Release previously reserved product inventory (internal/seller/admin)",
    )
    async def release_stock(
        product_id: str,
        payload: InventoryAdjustRequest,
        request: Request,
        session: Session = Depends(db.get_db),
    ) -> InventoryActionResponse:
        _require_internal_or_privileged(request)
        return domain.release_stock(session, product_id, payload)

    app.include_router(router)
    return app
