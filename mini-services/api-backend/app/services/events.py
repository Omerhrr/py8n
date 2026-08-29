"""Event bus — broadcast execution events to WebSocket subscribers.

Two interchangeable implementations selected by ``settings.execution_mode``:

* :class:`MemoryEventBus` — in-process pub/sub (sandbox / single-process mode).
* :class:`RedisEventBus`  — Redis pub/sub fan-out across API + Celery workers
  (production docker-compose mode).
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import AsyncIterator


class BaseEventBus:
    async def publish(self, execution_id: str, event: dict) -> None:  # pragma: no cover
        raise NotImplementedError

    async def subscribe(self, execution_id: str) -> AsyncIterator[dict]:  # pragma: no cover
        raise NotImplementedError

    async def wait_finished(self, execution_id: str, timeout: float) -> dict | None:  # pragma: no cover
        raise NotImplementedError


class MemoryEventBus(BaseEventBus):
    """Simple asyncio queue registry keyed by execution id."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)

    async def publish(self, execution_id: str, event: dict) -> None:
        for q in list(self._subscribers.get(execution_id, ())):
            await q.put(event)
        if event.get("event") == "execution_finished":
            for q in list(self._subscribers.get("__finished__", ())):
                if q is not None and event.get("execution_id") == getattr(q, "_execution_id", None):
                    await q.put(event)

    async def subscribe(self, execution_id: str) -> AsyncIterator[dict]:
        queue: asyncio.Queue = asyncio.Queue()
        queue._execution_id = execution_id  # type: ignore[attr-defined]
        self._subscribers[execution_id].add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers[execution_id].discard(queue)
            if not self._subscribers[execution_id]:
                self._subscribers.pop(execution_id, None)

    async def wait_finished(self, execution_id: str, timeout: float) -> dict | None:
        """Await the execution_finished event for one run (webhook last_node mode)."""
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers[execution_id].add(queue)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    return None
                if event.get("event") == "execution_finished":
                    return event
        finally:
            self._subscribers[execution_id].discard(queue)
            if not self._subscribers[execution_id]:
                self._subscribers.pop(execution_id, None)


class RedisEventBus(BaseEventBus):
    """Redis pub/sub implementation (production)."""

    CHANNEL_PREFIX = "py8n:events:"

    def __init__(self, redis_url: str) -> None:
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(redis_url, decode_responses=True)

    async def publish(self, execution_id: str, event: dict) -> None:
        await self._redis.publish(self.CHANNEL_PREFIX + execution_id, json.dumps(event, default=str))

    async def subscribe(self, execution_id: str) -> AsyncIterator[dict]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(self.CHANNEL_PREFIX + execution_id)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                yield json.loads(message["data"])
        finally:
            await pubsub.unsubscribe(self.CHANNEL_PREFIX + execution_id)
            await pubsub.aclose()

    async def wait_finished(self, execution_id: str, timeout: float) -> dict | None:
        async for event in self.subscribe(execution_id):
            if event.get("event") == "execution_finished":
                return event
        return None


_bus: BaseEventBus | None = None


def get_event_bus() -> BaseEventBus:
    global _bus
    if _bus is None:
        from ..config import settings

        if settings.execution_mode == "celery":
            _bus = RedisEventBus(settings.redis_url)
        else:
            _bus = MemoryEventBus()
    return _bus
