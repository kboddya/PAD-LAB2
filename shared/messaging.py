"""Asynchronous EventBus abstraction with RabbitMQ (aio-pika) and InMemory backends."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import Any

import aio_pika
from pydantic import BaseModel, Field

from shared.logging import get_request_id, request_id_var

EventHandler = Callable[["EventEnvelope"], Awaitable[None] | None]


class EventEnvelope(BaseModel):
    """Standard event message payload published between microservices."""

    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    event_type: str
    source_service: str
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    request_id: str = "-"
    payload: dict[str, Any] = Field(default_factory=dict)


class EventBus(ABC):
    """Abstract publish/subscribe bus for event-driven choreography."""

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection and declare required exchanges/queues."""

    @abstractmethod
    async def close(self) -> None:
        """Gracefully close connections and background consumers."""

    @abstractmethod
    def on(self, routing_key: str, handler: EventHandler) -> None:
        """Synchronously register an event handler before connect()."""

    @abstractmethod
    async def subscribe(self, routing_key: str, handler: EventHandler) -> None:
        """Register an event handler for a topic routing key (supports wildcards)."""

    @abstractmethod
    async def publish(
        self,
        event_type: str,
        payload: dict[str, Any],
        source_service: str,
        request_id: str | None = None,
    ) -> EventEnvelope:
        """Publish an event envelope to interested subscribers."""


def _topic_matches(pattern: str, routing_key: str) -> bool:
    """Match AMQP-style topic patterns (* = one word, # = zero or more words)."""
    if pattern == routing_key or pattern == "#":
        return True
    p_parts = pattern.split(".")
    r_parts = routing_key.split(".")

    def _match(pi: int, ri: int) -> bool:
        if pi == len(p_parts) and ri == len(r_parts):
            return True
        if pi == len(p_parts):
            return False
        if p_parts[pi] == "#":
            if pi == len(p_parts) - 1:
                return True
            for skip in range(ri, len(r_parts) + 1):
                if _match(pi + 1, skip):
                    return True
            return False
        if ri == len(r_parts):
            return False
        if p_parts[pi] == "*" or fnmatchcase(r_parts[ri], p_parts[pi]):
            return _match(pi + 1, ri + 1)
        return False

    return _match(0, 0)


class InMemoryEventBus(EventBus):
    """In-process EventBus ideal for deterministic tests and zero-broker local dev."""

    def __init__(
        self,
        logger: logging.Logger | None = None,
        *,
        raise_on_handler_error: bool = False,
    ) -> None:
        self.logger = logger or logging.getLogger("shopflow.eventbus")
        self.raise_on_handler_error = raise_on_handler_error
        self._subscriptions: list[tuple[str, EventHandler]] = []
        self.published_events: list[EventEnvelope] = []
        self.connected: bool = False

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    def on(self, routing_key: str, handler: EventHandler) -> None:
        self._subscriptions.append((routing_key, handler))

    async def subscribe(self, routing_key: str, handler: EventHandler) -> None:
        self.on(routing_key, handler)

    async def publish(
        self,
        event_type: str,
        payload: dict[str, Any],
        source_service: str,
        request_id: str | None = None,
    ) -> EventEnvelope:
        req_id = request_id or get_request_id()
        envelope = EventEnvelope(
            event_type=event_type,
            source_service=source_service,
            request_id=req_id,
            payload=payload,
        )
        self.published_events.append(envelope)

        matching_handlers = [
            handler for pattern, handler in self._subscriptions if _topic_matches(pattern, event_type)
        ]
        token = request_id_var.set(req_id)
        try:
            for handler in matching_handlers:
                try:
                    result = handler(envelope)
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:
                    self.logger.exception(
                        "Event handler failed for %s",
                        event_type,
                        exc_info=exc,
                        extra={"event_type": event_type, "request_id": req_id},
                    )
                    if self.raise_on_handler_error:
                        raise
        finally:
            request_id_var.reset(token)

        return envelope


class RabbitMQEventBus(EventBus):
    """Production RabbitMQ topic-exchange EventBus built on aio-pika."""

    def __init__(
        self,
        amqp_url: str,
        service_name: str,
        exchange_name: str = "shopflow.events",
        logger: logging.Logger | None = None,
    ) -> None:
        self.amqp_url = amqp_url
        self.service_name = service_name
        self.exchange_name = exchange_name
        self.logger = logger or logging.getLogger(f"shopflow.{service_name}")
        self._connection: aio_pika.abc.AbstractRobustConnection | None = None
        self._channel: aio_pika.abc.AbstractChannel | None = None
        self._exchange: aio_pika.abc.AbstractExchange | None = None
        self._pending_subscriptions: list[tuple[str, EventHandler]] = []
        self._consumer_tags: list[tuple[aio_pika.abc.AbstractQueue, str]] = []

    async def connect(self) -> None:
        self._connection = await aio_pika.connect_robust(self.amqp_url)
        self._channel = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=10)
        self._exchange = await self._channel.declare_exchange(
            self.exchange_name,
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )
        pending = list(self._pending_subscriptions)
        self._pending_subscriptions.clear()
        for routing_key, handler in pending:
            await self._bind_subscription(routing_key, handler)

    async def close(self) -> None:
        for queue, tag in self._consumer_tags:
            try:
                await queue.cancel(tag)
            except Exception:
                pass
        self._consumer_tags.clear()
        if self._channel and not self._channel.is_closed:
            await self._channel.close()
        if self._connection and not self._connection.is_closed:
            await self._connection.close()

    def on(self, routing_key: str, handler: EventHandler) -> None:
        self._pending_subscriptions.append((routing_key, handler))

    async def subscribe(self, routing_key: str, handler: EventHandler) -> None:
        if self._channel is None or self._exchange is None:
            self.on(routing_key, handler)
            return
        await self._bind_subscription(routing_key, handler)

    async def _bind_subscription(self, routing_key: str, handler: EventHandler) -> None:
        assert self._channel is not None and self._exchange is not None
        safe_key = routing_key.replace("*", "star").replace("#", "hash")
        queue_name = f"{self.service_name}.{safe_key}"
        queue = await self._channel.declare_queue(queue_name, durable=True)
        await queue.bind(self._exchange, routing_key=routing_key)

        async def _on_message(message: aio_pika.abc.AbstractIncomingMessage) -> None:
            async with message.process(requeue=False):
                data = json.loads(message.body.decode("utf-8"))
                envelope = EventEnvelope.model_validate(data)
                token = request_id_var.set(envelope.request_id)
                try:
                    result = handler(envelope)
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:
                    self.logger.exception(
                        "Failed handling RabbitMQ event %s",
                        envelope.event_type,
                        exc_info=exc,
                        extra={"event_type": envelope.event_type},
                    )
                finally:
                    request_id_var.reset(token)

        tag = await queue.consume(_on_message)
        self._consumer_tags.append((queue, tag))

    async def publish(
        self,
        event_type: str,
        payload: dict[str, Any],
        source_service: str,
        request_id: str | None = None,
    ) -> EventEnvelope:
        req_id = request_id or get_request_id()
        envelope = EventEnvelope(
            event_type=event_type,
            source_service=source_service,
            request_id=req_id,
            payload=payload,
        )
        if self._exchange is None:
            raise RuntimeError("RabbitMQEventBus is not connected")

        body = envelope.model_dump_json().encode("utf-8")
        message = aio_pika.Message(
            body=body,
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            correlation_id=req_id,
        )
        await self._exchange.publish(message, routing_key=event_type)
        return envelope


def create_event_bus(
    rabbitmq_url: str,
    service_name: str,
    logger: logging.Logger | None = None,
) -> EventBus:
    """Create either InMemoryEventBus or RabbitMQEventBus based on configuration URL."""
    normalized = (rabbitmq_url or "").strip()
    if not normalized or normalized.startswith("memory://"):
        return InMemoryEventBus(logger=logger)
    return RabbitMQEventBus(amqp_url=normalized, service_name=service_name, logger=logger)
