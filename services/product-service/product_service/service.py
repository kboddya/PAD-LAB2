"""Domain business logic for product catalog and Saga-safe inventory reservations."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from product_service.models import Product, StockReservation
from product_service.schemas import (
    InventoryActionResponse,
    InventoryAdjustRequest,
    ProductCreateRequest,
    ProductUpdateRequest,
)
from shared.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError


class ProductDomainService:
    """Handles product CRUD, search filtering, and atomic inventory reserve/release."""

    def create_product(self, session: Session, payload: ProductCreateRequest, seller_id: str) -> Product:
        normalized_sku = payload.sku.strip().upper()
        existing = session.scalar(select(Product).where(Product.sku == normalized_sku))
        if existing is not None:
            raise ConflictError(f"Product with SKU '{normalized_sku}' already exists", error_code="SKU_EXISTS")

        product = Product(
            sku=normalized_sku,
            name=payload.name.strip(),
            description=payload.description.strip(),
            category=payload.category.strip().lower(),
            price=round(float(payload.price), 2),
            stock_quantity=payload.stock_quantity,
            reserved_quantity=0,
            seller_id=seller_id,
            is_active=True,
        )
        session.add(product)
        session.commit()
        session.refresh(product)
        return product

    def list_products(
        self,
        session: Session,
        *,
        category: str | None = None,
        search: str | None = None,
        in_stock: bool = False,
        include_inactive: bool = False,
        skip: int = 0,
        limit: int = 50,
    ) -> list[Product]:
        stmt = select(Product)
        if not include_inactive:
            stmt = stmt.where(Product.is_active.is_(True))
        if category:
            stmt = stmt.where(Product.category == category.strip().lower())
        if search:
            pattern = f"%{search.strip()}%"
            stmt = stmt.where(Product.name.ilike(pattern) | Product.description.ilike(pattern))
        if in_stock:
            stmt = stmt.where((Product.stock_quantity - Product.reserved_quantity) > 0)

        stmt = stmt.order_by(Product.created_at.desc()).offset(skip).limit(limit)
        return list(session.scalars(stmt).all())

    def get_product(self, session: Session, product_id: str) -> Product:
        product = session.get(Product, product_id)
        if product is None:
            raise NotFoundError(f"Product '{product_id}' not found", error_code="PRODUCT_NOT_FOUND")
        return product

    def update_product(
        self,
        session: Session,
        product_id: str,
        payload: ProductUpdateRequest,
        *,
        actor_id: str,
        actor_role: str,
    ) -> Product:
        product = self.get_product(session, product_id)
        if actor_role != "admin" and product.seller_id != actor_id:
            raise ForbiddenError("Only the product seller or an admin can update this product")

        if payload.name is not None:
            product.name = payload.name.strip()
        if payload.description is not None:
            product.description = payload.description.strip()
        if payload.category is not None:
            product.category = payload.category.strip().lower()
        if payload.price is not None:
            product.price = round(float(payload.price), 2)
        if payload.stock_quantity is not None:
            if payload.stock_quantity < product.reserved_quantity:
                raise ConflictError(
                    "New stock quantity cannot be lower than currently reserved quantity",
                    error_code="STOCK_BELOW_RESERVED",
                )
            product.stock_quantity = payload.stock_quantity
        if payload.is_active is not None:
            product.is_active = payload.is_active

        product.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(product)
        return product

    def delete_product(self, session: Session, product_id: str, *, actor_id: str, actor_role: str) -> None:
        product = self.get_product(session, product_id)
        if actor_role != "admin" and product.seller_id != actor_id:
            raise ForbiddenError("Only the product seller or an admin can delete this product")
        if product.reserved_quantity > 0:
            raise ConflictError("Cannot delete product with active stock reservations")
        product.is_active = False
        product.updated_at = datetime.now(timezone.utc)
        session.commit()

    def reserve_stock(
        self,
        session: Session,
        product_id: str,
        payload: InventoryAdjustRequest,
    ) -> InventoryActionResponse:
        product = self.get_product(session, product_id)
        if not product.is_active:
            raise BadRequestError(f"Product '{product.name}' is no longer active", error_code="PRODUCT_INACTIVE")

        if payload.order_id:
            existing_res = session.scalar(
                select(StockReservation).where(
                    StockReservation.order_id == payload.order_id,
                    StockReservation.product_id == product_id,
                    StockReservation.status == "reserved",
                )
            )
            if existing_res is not None:
                return self._build_inventory_response(product, payload.order_id, "reserved", existing_res.quantity)

        if product.available_quantity < payload.quantity:
            raise ConflictError(
                f"Insufficient stock for '{product.name}': available {product.available_quantity}, requested {payload.quantity}",
                error_code="INSUFFICIENT_STOCK",
            )

        product.reserved_quantity += payload.quantity
        product.updated_at = datetime.now(timezone.utc)
        if payload.order_id:
            session.add(
                StockReservation(
                    order_id=payload.order_id,
                    product_id=product_id,
                    quantity=payload.quantity,
                    status="reserved",
                )
            )
        session.commit()
        session.refresh(product)
        return self._build_inventory_response(product, payload.order_id, "reserved", payload.quantity)

    def release_stock(
        self,
        session: Session,
        product_id: str,
        payload: InventoryAdjustRequest,
    ) -> InventoryActionResponse:
        product = self.get_product(session, product_id)
        released_qty = payload.quantity

        if payload.order_id:
            reservation = session.scalar(
                select(StockReservation).where(
                    StockReservation.order_id == payload.order_id,
                    StockReservation.product_id == product_id,
                )
            )
            if reservation is not None:
                if reservation.status == "released":
                    return self._build_inventory_response(product, payload.order_id, "released", 0)
                if reservation.status == "reserved":
                    product.reserved_quantity = max(0, product.reserved_quantity - reservation.quantity)
                    released_qty = reservation.quantity
                elif reservation.status == "committed":
                    product.stock_quantity += reservation.quantity
                    released_qty = reservation.quantity
                reservation.status = "released"
                product.updated_at = datetime.now(timezone.utc)
                session.commit()
                session.refresh(product)
                return self._build_inventory_response(product, payload.order_id, "released", released_qty)

        actual_release = min(product.reserved_quantity, payload.quantity)
        product.reserved_quantity -= actual_release
        product.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(product)
        return self._build_inventory_response(product, payload.order_id, "released", actual_release)

    def commit_order_reservations(self, session: Session, order_id: str) -> int:
        """Finalize reserved stock into deducted stock when payment succeeds."""
        reservations = list(
            session.scalars(
                select(StockReservation).where(
                    StockReservation.order_id == order_id,
                    StockReservation.status == "reserved",
                )
            ).all()
        )
        for res in reservations:
            product = session.get(Product, res.product_id)
            if product is not None:
                product.stock_quantity = max(0, product.stock_quantity - res.quantity)
                product.reserved_quantity = max(0, product.reserved_quantity - res.quantity)
                product.updated_at = datetime.now(timezone.utc)
            res.status = "committed"
        session.commit()
        return len(reservations)

    def release_order_reservations(self, session: Session, order_id: str) -> int:
        """Compensate reserved/committed stock when payment fails or order is cancelled."""
        reservations = list(
            session.scalars(
                select(StockReservation).where(
                    StockReservation.order_id == order_id,
                    StockReservation.status.in_(["reserved", "committed"]),
                )
            ).all()
        )
        for res in reservations:
            product = session.get(Product, res.product_id)
            if product is not None:
                if res.status == "reserved":
                    product.reserved_quantity = max(0, product.reserved_quantity - res.quantity)
                elif res.status == "committed":
                    product.stock_quantity += res.quantity
                product.updated_at = datetime.now(timezone.utc)
            res.status = "released"
        session.commit()
        return len(reservations)

    @staticmethod
    def _build_inventory_response(
        product: Product,
        order_id: str | None,
        action: str,
        quantity: int,
    ) -> InventoryActionResponse:
        return InventoryActionResponse(
            product_id=product.id,
            order_id=order_id,
            action=action,
            quantity=quantity,
            stock_quantity=product.stock_quantity,
            reserved_quantity=product.reserved_quantity,
            available_quantity=product.available_quantity,
        )
