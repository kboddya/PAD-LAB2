"""SQLAlchemy 2.0 database manager supporting isolated per-service SQLite and PostgreSQL."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class DatabaseManager:
    """Encapsulates engine, session factory, table lifecycle, and health checks for one service."""

    def __init__(self, database_url: str, base: type[DeclarativeBase]) -> None:
        self.database_url = database_url
        self.base = base

        engine_kwargs: dict[str, Any] = {"future": True}
        if database_url.startswith("sqlite"):
            engine_kwargs["connect_args"] = {"check_same_thread": False}
            if ":memory:" in database_url:
                engine_kwargs["poolclass"] = StaticPool
        else:
            engine_kwargs["pool_pre_ping"] = True

        self.engine = create_engine(database_url, **engine_kwargs)
        self.SessionLocal = sessionmaker(
            bind=self.engine,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )

    def create_all(self) -> None:
        """Create all tables registered on this service's declarative base."""
        self.base.metadata.create_all(bind=self.engine)

    @contextmanager
    def session_scope(self) -> Generator[Session, None, None]:
        """Provide a transactional database session scope."""
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_db(self) -> Generator[Session, None, None]:
        """FastAPI dependency yielding a scoped SQLAlchemy session."""
        session = self.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    def check_health(self) -> bool:
        """Verify database connectivity using a lightweight SELECT 1 query."""
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    def dispose(self) -> None:
        """Dispose underlying SQLAlchemy connection pool."""
        self.engine.dispose()
