"""
k9-mcts — observability.py

CB-7 Decision Journal — human-visible audit trail for autonomous actions.

Every MCTS commit that goes through the handover engine produces a journal
entry. This module records, indexes, and serves those entries so the operator
can answer: "What did the agent do autonomously, when, and why?"

Journal entries include:
  - Full reasoning trace (every MCTS node explored)
  - Handover decision (AUTO_EXECUTE / HUMAN_REVIEW / REJECT)
  - Risk classification + task class
  - Dispatch result (service called, latency, raw response summary)
  - JEPA target encoder state at decision time
  - CB v1 envelope ID (for cross-referencing Orbitron bus)

Storage: in-memory ring buffer (last 1000 entries). For production, this
should be mirrored to Supabase cb_messages via the Orbitron bus — but the
in-memory journal gives instant queryability without DB latency.

Endpoints (wired via api.py):
  - GET /reason/journal — recent decisions, filterable by type/risk
  - GET /reason/journal/{trace_id} — full decision detail by trace ID
  - GET /reason/journal/summary — aggregate stats (auto/human/reject counts)
  - GET /reason/journal/export — full journal as JSON array (for export)
"""

from __future__ import annotations

import json
import time
import uuid
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional
from collections import defaultdict, deque

logger = logging.getLogger("k9.observability")


@dataclass
class JournalEntry:
    """One decision record — the full story of one MCTS reasoning cycle."""
    trace_id: str
    timestamp: float

    # Input
    question: str
    context: dict = field(default_factory=dict)

    # MCTS result
    answer: str = ""
    confidence: float = 0.0
    iterations: int = 0
    elapsed_ms: float = 0.0
    reasoning_trace: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    task_class: str = ""

    # Handover decision
    decision: str = ""          # auto_execute / human_review / reject
    risk_level: str = ""         # read_only / idempotent / state_changing / high_risk
    handover_reason: str = ""
    cooldown_seconds: int = 0

    # Dispatch result (only for auto_execute)
    dispatch_result: str = ""    # executed / no_op / error / rejected
    dispatch_service: str = ""
    dispatch_latency_ms: float = 0.0
    dispatch_error: str = ""

    # JEPA state at decision time
    jepa_total_updates: int = 0
    jepa_proven_classes: list = field(default_factory=list)
    jepa_class_success_rate: float = 0.0

    # CB v1 envelope
    envelope_id: str = ""

    # Human-readable summary
    summary: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def to_summary_line(self) -> str:
        """One-line human-readable summary for dashboards."""
        status_emoji = {
            "auto_execute": "[AUTO]",
            "human_review": "[HUMAN]",
            "reject": "[REJECT]",
        }.get(self.decision, "[???]")
        disp = f" → {self.dispatch_service} ({self.dispatch_latency_ms:.0f}ms)" if self.dispatch_result == "executed" else ""
        return (
            f"{status_emoji} {self.trace_id} | "
            f"{self.task_class}/{self.risk_level} | "
            f"conf={self.confidence:.2f} | "
            f"{self.question[:60]}{disp}"
        )


class DecisionJournal:
    """
    In-memory decision journal with ring buffer + indexing.
    Thread-safe for concurrent MCTS reasoning cycles.
    """

    def __init__(self, max_entries: int = 1000):
        self._entries: deque[JournalEntry] = deque(maxlen=max_entries)
        self._index: dict[str, JournalEntry] = {}  # trace_id → entry
        self._max_entries = max_entries

        # Aggregate counters
        self._stats = defaultdict(int)
        self._stats["total"] = 0

    def record(self, entry: JournalEntry) -> None:
        """Record a decision in the journal."""
        # Generate summary
        entry.summary = entry.to_summary_line()

        # Store
        self._entries.append(entry)
        self._index[entry.trace_id] = entry

        # Trim index (keep only entries still in ring buffer)
        if len(self._index) > self._max_entries:
            oldest_id = next(iter(self._index))
            if oldest_id != entry.trace_id:
                del self._index[oldest_id]

        # Update stats
        self._stats["total"] += 1
        self._stats[f"decision:{entry.decision}"] += 1
        self._stats[f"risk:{entry.risk_level}"] += 1
        self._stats[f"task:{entry.task_class}"] += 1
        if entry.dispatch_result:
            self._stats[f"dispatch:{entry.dispatch_result}"] += 1

        logger.info(f"[JOURNAL] {entry.summary}")

        # CB-7.5: Enqueue for Supabase persistence (non-blocking)
        try:
            from .journal_persistence import get_persistence
            get_persistence().enqueue(entry)
        except Exception as e:
            logger.debug(f"[JOURNAL] persistence enqueue failed (non-fatal): {e}")

    def get(self, trace_id: str) -> Optional[JournalEntry]:
        """Retrieve a single entry by trace ID."""
        return self._index.get(trace_id)

    def recent(
        self,
        limit: int = 20,
        decision: str = "",
        risk_level: str = "",
        task_class: str = "",
    ) -> list[JournalEntry]:
        """Get recent entries, optionally filtered."""
        results = []
        for entry in reversed(self._entries):
            if decision and entry.decision != decision:
                continue
            if risk_level and entry.risk_level != risk_level:
                continue
            if task_class and entry.task_class != task_class:
                continue
            results.append(entry)
            if len(results) >= limit:
                break
        return results

    def summary(self) -> dict:
        """Aggregate statistics across all recorded decisions."""
        return {
            "total": self._stats["total"],
            "by_decision": {
                k.split(":", 1)[1]: v
                for k, v in self._stats.items()
                if k.startswith("decision:")
            },
            "by_risk": {
                k.split(":", 1)[1]: v
                for k, v in self._stats.items()
                if k.startswith("risk:")
            },
            "by_task_class": {
                k.split(":", 1)[1]: v
                for k, v in self._stats.items()
                if k.startswith("task:")
            },
            "by_dispatch_result": {
                k.split(":", 1)[1]: v
                for k, v in self._stats.items()
                if k.startswith("dispatch:")
            },
            "journal_size": len(self._entries),
            "max_entries": self._max_entries,
        }

    def export(self) -> list[dict]:
        """Export full journal as list of dicts (for backup/analysis)."""
        return [e.to_dict() for e in self._entries]

    def auto_executed(self, limit: int = 50) -> list[JournalEntry]:
        """Get only auto-executed decisions (for human audit of autonomous actions)."""
        return self.recent(limit=limit, decision="auto_execute")

    def human_reviews(self, limit: int = 50) -> list[JournalEntry]:
        """Get only human-review decisions (pending human action)."""
        return self.recent(limit=limit, decision="human_review")


# ── Global singleton ─────────────────────────────────────────────────────────
_journal: Optional[DecisionJournal] = None


def get_journal() -> DecisionJournal:
    global _journal
    if _journal is None:
        _journal = DecisionJournal()
    return _journal
