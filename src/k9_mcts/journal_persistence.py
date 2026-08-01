"""
k9-mcts — journal_persistence.py

CB-7.5: Persist decision journal entries to Supabase cb_messages table.
Survives restarts — in-memory ring buffer is fast query, Supabase is durable.

Writes to cb_messages via Supabase REST API (POST /rest/v1/cb_messages).
The cb_messages table was created by the CB-1 migration (migration_cb_messages.sql).

Schema mapping (JournalEntry → cb_messages row):
  trace_id              → trace_id
  timestamp             → created_at (ISO 8601)
  decision              → type (auto_execute / human_review / reject)
  question              → payload.question
  answer                → payload.answer
  confidence            → confidence
  risk_level            → payload.risk_level
  task_class            → ontology_tags[0]
  reasoning_trace       → payload.reasoning_trace
  evidence              → payload.evidence
  dispatch_result       → payload.dispatch.result
  dispatch_service      → payload.dispatch.service
  handover_reason       → payload.handover_reason
  envelope_id           → message_id (on cb_messages)

Writes are async and non-blocking — failure to persist does NOT block the
MCTS reasoning loop. A background flusher batches writes every 5 seconds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Optional
from collections import deque

import httpx

from .observability import JournalEntry, get_journal

logger = logging.getLogger("k9.journal.persist")

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://ziqenqqgnqxqrazmjohs.supabase.co")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_SVC_KEY = os.getenv("SUPABASE_SVC_KEY", "")
FLUSH_INTERVAL = float(os.getenv("JOURNAL_FLUSH_INTERVAL", "5.0"))
BATCH_SIZE = 50


class JournalPersistence:
    """
    Batched async persistence of journal entries to Supabase cb_messages.
    Non-blocking: entries queue in memory, flushed every FLUSH_INTERVAL seconds.
    """

    def __init__(self):
        self._queue: deque[JournalEntry] = deque()
        self._flusher_task: Optional[asyncio.Task] = None
        self._running = False
        self.total_persisted = 0
        self.total_errors = 0
        self.last_flush_ts = 0.0

        # Use service key if available, else anon key
        self._api_key = SUPABASE_SVC_KEY or SUPABASE_ANON_KEY
        self._headers = {
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }
        if self._api_key:
            self._headers["Authorization"] = f"Bearer {self._api_key}"
            self._headers["apikey"] = self._api_key

    def enqueue(self, entry: JournalEntry) -> None:
        """Queue a journal entry for persistence (non-blocking)."""
        self._queue.append(entry)

    async def start(self) -> None:
        """Start the background flusher."""
        if self._running:
            return
        if not self._api_key:
            logger.warning("[JOURNAL-PERSIST] No Supabase API key — persistence disabled")
            return
        self._running = True
        self._flusher_task = asyncio.create_task(self._flush_loop())
        logger.info("[JOURNAL-PERSIST] Background flusher started (interval=%ss)", FLUSH_INTERVAL)

    async def stop(self) -> None:
        """Stop the flusher and flush remaining entries."""
        self._running = False
        if self._flusher_task:
            self._flusher_task.cancel()
            try:
                await self._flusher_task
            except asyncio.CancelledError:
                pass
        # Final flush
        await self._flush()

    async def _flush_loop(self) -> None:
        """Background loop that flushes queued entries periodically."""
        while self._running:
            await asyncio.sleep(FLUSH_INTERVAL)
            try:
                await self._flush()
            except Exception as e:
                logger.error(f"[JOURNAL-PERSIST] flush error: {e}")

    async def _flush(self) -> None:
        """Flush queued entries to Supabase in batches."""
        if not self._queue:
            return

        batch = []
        while self._queue and len(batch) < BATCH_SIZE:
            entry = self._queue.popleft()
            row = self._entry_to_row(entry)
            if row:
                batch.append(row)

        if not batch:
            return

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                resp = await client.post(
                    f"{SUPABASE_URL}/rest/v1/cb_messages",
                    headers=self._headers,
                    json=batch,
                )
                if resp.status_code in (200, 201):
                    self.total_persisted += len(batch)
                    self.last_flush_ts = time.time()
                    logger.info(
                        "[JOURNAL-PERSIST] Flushed %d entries (total=%d)",
                        len(batch), self.total_persisted
                    )
                else:
                    self.total_errors += 1
                    logger.error(
                        "[JOURNAL-PERSIST] Supabase returned %d: %s",
                        resp.status_code, resp.text[:200]
                    )
                    # Re-queue on error (but cap to prevent infinite growth)
                    if len(self._queue) < 500:
                        self._queue.extendleft(reversed(batch))
        except Exception as e:
            self.total_errors += 1
            logger.error(f"[JOURNAL-PERSIST] flush exception: {e}")
            if len(self._queue) < 500:
                self._queue.extendleft(reversed(batch))

    def _entry_to_row(self, entry: JournalEntry) -> Optional[dict]:
        """Map a JournalEntry to a cb_messages row."""
        if not entry.trace_id:
            return None

        payload = {
            "question": entry.question,
            "answer": entry.answer,
            "iterations": entry.iterations,
            "elapsed_ms": entry.elapsed_ms,
            "reasoning_trace": entry.reasoning_trace,
            "evidence": entry.evidence,
            "risk_level": entry.risk_level,
            "handover_reason": entry.handover_reason,
            "cooldown_seconds": entry.cooldown_seconds,
            "dispatch": {
                "result": entry.dispatch_result,
                "service": entry.dispatch_service,
                "latency_ms": entry.dispatch_latency_ms,
                "error": entry.dispatch_error,
            },
            "jepa": {
                "total_updates": entry.jepa_total_updates,
                "proven_classes": entry.jepa_proven_classes,
                "class_success_rate": entry.jepa_class_success_rate,
            },
            "summary": entry.summary,
        }

        return {
            "message_id": entry.envelope_id or entry.trace_id,
            "trace_id": entry.trace_id,
            "source": "k9-mcts",
            "type": entry.decision,
            "ontology_tags": [entry.task_class, entry.risk_level, "cb-7"],
            "confidence": entry.confidence,
            "payload": payload,
            "created_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(entry.timestamp)
            ),
        }

    def stats(self) -> dict:
        return {
            "enabled": self._running and bool(self._api_key),
            "queue_size": len(self._queue),
            "total_persisted": self.total_persisted,
            "total_errors": self.total_errors,
            "last_flush_ts": self.last_flush_ts,
            "supabase_url": SUPABASE_URL if self._api_key else "not configured",
            "flush_interval": FLUSH_INTERVAL,
        }


# ── Global singleton ─────────────────────────────────────────────────────────
_persistence: Optional[JournalPersistence] = None


def get_persistence() -> JournalPersistence:
    global _persistence
    if _persistence is None:
        _persistence = JournalPersistence()
    return _persistence
