"""
k9_task_queue.py
─────────────────────────────────────────────────────────────────────────────
K-9 Task Queue — Sprint 4

Replaces the in-memory deque in k9_orchestrator.py with a Redis-backed
persistent async queue. Designed to be API-compatible with the in-memory
version so k9_orchestrator.py can switch with a one-line import change.

Architecture:
  - Redis Streams as the task queue (XADD / XREAD)
  - Redis Hashes for task result storage (keyed by task_id)
  - Falls back to in-memory deque if Redis is unavailable (graceful degradation)
  - Celery-compatible task signature format (name, args, kwargs, id)

Sprint 4 → Sprint 5 path:
  - Sprint 4: Redis Streams (this file)
  - Sprint 5: NATS JetStream (swap RedisTaskQueue for NATSTaskQueue, same interface)
  - Sprint 6+: Celery workers for CPU-heavy quant tasks

Usage in k9_orchestrator.py:
  from k9_task_queue import get_task_queue
  queue = get_task_queue()
  task_id = await queue.enqueue(command, params, source)
  result  = await queue.get_result(task_id)
  tasks   = await queue.list_tasks(limit=20)

# L2 — task queue infrastructure only. No financial execution.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("k9-task-queue")

REDIS_URL    = os.getenv("REDIS_URL", "redis://localhost:6379")
QUEUE_STREAM = "k9:tasks"
RESULT_TTL   = int(os.getenv("TASK_RESULT_TTL_S", "3600"))  # 1 hour


# ── INTERFACES ────────────────────────────────────────────────────────────────

class TaskRecord:
    __slots__ = ("task_id", "command", "params", "source", "priority",
                 "status", "result", "error", "queued_at", "latency_ms")

    def __init__(self, command: str, params: dict, source: str = "api", priority: int = 5):
        self.task_id    = str(uuid.uuid4())[:8]
        self.command    = command
        self.params     = params
        self.source     = source
        self.priority   = priority
        self.status     = "queued"
        self.result     = None
        self.error      = None
        self.queued_at  = datetime.now(timezone.utc).isoformat()
        self.latency_ms = None

    def to_dict(self) -> dict:
        return {
            "task_id":    self.task_id,
            "command":    self.command,
            "params":     self.params,
            "source":     self.source,
            "priority":   self.priority,
            "status":     self.status,
            "result":     self.result,
            "error":      self.error,
            "queued_at":  self.queued_at,
            "latency_ms": self.latency_ms,
        }


class BaseTaskQueue:
    """Interface that all queue backends implement."""

    async def enqueue(self, command: str, params: dict,
                      source: str = "api", priority: int = 5) -> str:
        raise NotImplementedError

    async def mark_running(self, task_id: str) -> None:
        raise NotImplementedError

    async def mark_done(self, task_id: str, result: Any, latency_ms: float) -> None:
        raise NotImplementedError

    async def mark_failed(self, task_id: str, error: str) -> None:
        raise NotImplementedError

    async def get_result(self, task_id: str) -> dict | None:
        raise NotImplementedError

    async def list_tasks(self, limit: int = 20) -> list[dict]:
        raise NotImplementedError

    async def stats(self) -> dict:
        raise NotImplementedError


# ── IN-MEMORY FALLBACK ────────────────────────────────────────────────────────

class InMemoryTaskQueue(BaseTaskQueue):
    """In-memory fallback — no persistence across restarts. Sprint 3 compatible."""

    def __init__(self):
        self._queue:   deque[TaskRecord] = deque(maxlen=500)
        self._results: dict[str, TaskRecord] = {}
        log.warning("Using in-memory task queue — tasks lost on restart. Set REDIS_URL for persistence.")

    async def enqueue(self, command, params, source="api", priority=5) -> str:
        rec = TaskRecord(command, params, source, priority)
        self._queue.append(rec)
        return rec.task_id

    async def mark_running(self, task_id: str) -> None:
        rec = self._find(task_id)
        if rec: rec.status = "running"

    async def mark_done(self, task_id: str, result: Any, latency_ms: float) -> None:
        rec = self._find(task_id)
        if rec:
            rec.status     = "done"
            rec.result     = result
            rec.latency_ms = latency_ms
            self._results[task_id] = rec

    async def mark_failed(self, task_id: str, error: str) -> None:
        rec = self._find(task_id)
        if rec:
            rec.status = "failed"
            rec.error  = error
            self._results[task_id] = rec

    async def get_result(self, task_id: str) -> dict | None:
        rec = self._results.get(task_id)
        return rec.to_dict() if rec else None

    async def list_tasks(self, limit: int = 20) -> list[dict]:
        tasks = list(self._queue)[-limit:]
        return [t.to_dict() for t in reversed(tasks)]

    async def stats(self) -> dict:
        return {
            "backend":   "in-memory",
            "queued":    len(self._queue),
            "completed": len(self._results),
        }

    def _find(self, task_id: str) -> TaskRecord | None:
        for t in self._queue:
            if t.task_id == task_id:
                return t
        return self._results.get(task_id)


# ── REDIS STREAMS BACKEND ─────────────────────────────────────────────────────

class RedisTaskQueue(BaseTaskQueue):
    """
    Redis Streams-backed task queue.
    - Tasks written to stream k9:tasks (XADD)
    - Results stored in Redis Hash k9:results:{task_id} with TTL
    - Lightweight: no Celery worker needed — orchestrator executes inline
    """

    def __init__(self, redis_url: str = REDIS_URL):
        self._url      = redis_url
        self._redis    = None
        self._ready    = False
        self._fallback = InMemoryTaskQueue()

    async def _connect(self):
        if self._ready:
            return True
        try:
            import redis.asyncio as aioredis
            self._redis = await aioredis.from_url(self._url, decode_responses=True)
            await self._redis.ping()
            self._ready = True
            log.info("Redis task queue connected: %s", self._url)
            return True
        except Exception as e:
            log.warning("Redis unavailable (%s) — using in-memory fallback", e)
            return False

    async def enqueue(self, command, params, source="api", priority=5) -> str:
        if not await self._connect():
            return await self._fallback.enqueue(command, params, source, priority)

        task_id = str(uuid.uuid4())[:8]
        payload = {
            "task_id":   task_id,
            "command":   command,
            "params":    json.dumps(params),
            "source":    source,
            "priority":  str(priority),
            "status":    "queued",
            "queued_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            await self._redis.xadd(QUEUE_STREAM, payload, maxlen=1000)
            # Store initial state in hash
            await self._redis.hset(f"k9:task:{task_id}", mapping=payload)
            await self._redis.expire(f"k9:task:{task_id}", RESULT_TTL)
            log.debug("Enqueued task %s → %s", task_id, command)
            return task_id
        except Exception as e:
            log.warning("Redis enqueue failed: %s — using fallback", e)
            self._ready = False
            return await self._fallback.enqueue(command, params, source, priority)

    async def _update(self, task_id: str, fields: dict) -> None:
        if not self._ready:
            return
        try:
            await self._redis.hset(f"k9:task:{task_id}", mapping={
                k: json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                for k, v in fields.items()
            })
        except Exception as e:
            log.debug("Redis update failed: %s", e)

    async def mark_running(self, task_id: str) -> None:
        await self._update(task_id, {"status": "running"})
        await self._fallback.mark_running(task_id)

    async def mark_done(self, task_id: str, result: Any, latency_ms: float) -> None:
        await self._update(task_id, {
            "status":     "done",
            "result":     json.dumps(result) if not isinstance(result, str) else result,
            "latency_ms": latency_ms,
        })
        await self._fallback.mark_done(task_id, result, latency_ms)

    async def mark_failed(self, task_id: str, error: str) -> None:
        await self._update(task_id, {"status": "failed", "error": error})
        await self._fallback.mark_failed(task_id, error)

    async def get_result(self, task_id: str) -> dict | None:
        if not await self._connect():
            return await self._fallback.get_result(task_id)
        try:
            data = await self._redis.hgetall(f"k9:task:{task_id}")
            if data:
                result_raw = data.get("result")
                if result_raw:
                    try:
                        data["result"] = json.loads(result_raw)
                    except Exception:
                        pass
                params_raw = data.get("params")
                if params_raw:
                    try:
                        data["params"] = json.loads(params_raw)
                    except Exception:
                        pass
                return data
        except Exception as e:
            log.debug("Redis get_result: %s", e)
        return await self._fallback.get_result(task_id)

    async def list_tasks(self, limit: int = 20) -> list[dict]:
        if not await self._connect():
            return await self._fallback.list_tasks(limit)
        try:
            # Read last N entries from the stream
            entries = await self._redis.xrevrange(QUEUE_STREAM, count=limit)
            tasks = []
            for entry_id, fields in entries:
                row = dict(fields)
                row["stream_id"] = entry_id
                for f in ("params", "result"):
                    if f in row:
                        try: row[f] = json.loads(row[f])
                        except Exception: pass
                tasks.append(row)
            return tasks
        except Exception as e:
            log.debug("Redis list_tasks: %s", e)
            return await self._fallback.list_tasks(limit)

    async def stats(self) -> dict:
        if not await self._connect():
            return {**await self._fallback.stats(), "backend": "in-memory-fallback"}
        try:
            length = await self._redis.xlen(QUEUE_STREAM)
            return {
                "backend":  "redis-streams",
                "url":      self._url,
                "stream":   QUEUE_STREAM,
                "queued":   length,
                "result_ttl_s": RESULT_TTL,
            }
        except Exception:
            return {"backend": "redis-streams", "status": "error"}


# ── SINGLETON FACTORY ─────────────────────────────────────────────────────────

_queue_instance: BaseTaskQueue | None = None


def get_task_queue() -> BaseTaskQueue:
    """
    Get the active task queue singleton.
    Prefers Redis if REDIS_URL is set and Redis is reachable.
    Falls back to in-memory automatically.
    """
    global _queue_instance
    if _queue_instance is None:
        if REDIS_URL:
            _queue_instance = RedisTaskQueue(REDIS_URL)
        else:
            _queue_instance = InMemoryTaskQueue()
    return _queue_instance
