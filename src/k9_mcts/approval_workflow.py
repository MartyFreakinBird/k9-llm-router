"""
k9-mcts — approval_workflow.py

CB-9: Human-in-the-Loop Approval Workflow

When the handover engine routes a decision to HUMAN_REVIEW, the approval
workflow creates a pending approval task. The operator can then:

  1. View pending approvals via /reason/approvals (or the wallpaper UI)
  2. Approve → system dispatches the action via the execution dispatcher
  3. Reject → system records a negative outcome in the JEPA target encoder

This closes the loop: human_review → approval → execution → feedback.
Without this, human_review items just sit in the journal with no action path.

Key design decisions:
  - Approvals have a TTL (default 1 hour) — stale approvals auto-expire
  - Approval records the operator's identity and timestamp
  - Approved items go through the SAME dispatcher as auto-execute
  - Rejected items feed back to the target encoder as negative outcomes
  - Circuit breaker still gates approved dispatches (safety first)
  - All approval state changes emit CB v1 envelopes to Orbitron bus

Storage: in-memory with Supabase persistence via journal_persistence.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional
from collections import deque

import httpx

from .observability import get_journal, JournalEntry

logger = logging.getLogger("k9.approval")

ORBITRON_URL = os.getenv("ORBITRON_BUS_URL", "http://localhost:8769")
APPROVAL_TTL_SECONDS = float(os.getenv("APPROVAL_TTL_S", "3600"))  # 1 hour


class ApprovalState(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"
    EXECUTION_FAILED = "execution_failed"


@dataclass
class ApprovalTask:
    """A human-review decision awaiting operator action."""
    approval_id: str
    trace_id: str
    timestamp: float

    # The decision context
    question: str
    answer: str
    confidence: float
    task_class: str
    risk_level: str
    handover_reason: str
    reasoning_trace: list = field(default_factory=list)
    evidence: list = field(default_factory=list)

    # Approval state
    state: ApprovalState = ApprovalState.PENDING

    # Approval metadata (filled when operator acts)
    approved_by: str = ""
    approved_at: float = 0.0
    approval_note: str = ""

    # Dispatch result (filled after execution)
    dispatch_result: str = ""
    dispatch_service: str = ""
    dispatch_latency_ms: float = 0.0
    dispatch_error: str = ""

    # Expiry
    expires_at: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    def to_summary(self) -> str:
        age = int(time.time() - self.timestamp)
        return (
            f"[{self.state.value.upper()}] {self.approval_id} | "
            f"{self.task_class}/{self.risk_level} | "
            f"conf={self.confidence:.2f} | "
            f"age={age}s | "
            f"{self.question[:50]}"
        )

    @property
    def is_expired(self) -> bool:
        return self.state == ApprovalState.PENDING and time.time() > self.expires_at


class ApprovalWorkflow:
    """
    Manages the lifecycle of human-review approval tasks.
    Thread-safe for concurrent MCTS reasoning cycles.
    """

    def __init__(self, max_tasks: int = 500):
        self._tasks: dict[str, ApprovalTask] = {}  # approval_id → task
        self._pending: deque[str] = deque()  # approval_ids in pending order
        self._max_tasks = max_tasks
        self._supabase_poller: Optional[asyncio.Task] = None

        # Stats
        self._stats = {
            "total_created": 0,
            "total_approved": 0,
            "total_rejected": 0,
            "total_expired": 0,
            "total_executed": 0,
            "total_execution_failed": 0,
        }

    def create_task(
        self,
        trace_id: str,
        question: str,
        answer: str,
        confidence: float,
        task_class: str,
        risk_level: str,
        handover_reason: str,
        reasoning_trace: list = None,
        evidence: list = None,
    ) -> ApprovalTask:
        """Create a pending approval task for a human-review decision."""
        approval_id = str(uuid.uuid4())
        now = time.time()
        task = ApprovalTask(
            approval_id=approval_id,
            trace_id=trace_id,
            timestamp=now,
            question=question,
            answer=answer,
            confidence=confidence,
            task_class=task_class,
            risk_level=risk_level,
            handover_reason=handover_reason,
            reasoning_trace=reasoning_trace or [],
            evidence=evidence or [],
            expires_at=now + APPROVAL_TTL_SECONDS,
        )

        self._tasks[approval_id] = task
        self._pending.append(approval_id)
        self._stats["total_created"] += 1

        # Trim if too many
        if len(self._tasks) > self._max_tasks:
            oldest_id = self._pending.popleft()
            if oldest_id in self._tasks:
                del self._tasks[oldest_id]

        logger.info(f"[APPROVAL] Created {approval_id}: {task.to_summary()}")
        return task

    def approve(
        self, approval_id: str, approved_by: str = "operator", note: str = ""
    ) -> Optional[ApprovalTask]:
        """Mark a pending approval as approved."""
        task = self._tasks.get(approval_id)
        if not task:
            return None
        if task.state != ApprovalState.PENDING:
            return None
        if task.is_expired:
            self._expire_task(task)
            return None

        task.state = ApprovalState.APPROVED
        task.approved_by = approved_by
        task.approved_at = time.time()
        task.approval_note = note
        self._stats["total_approved"] += 1

        logger.info(f"[APPROVAL] Approved {approval_id} by {approved_by}")
        try:
            asyncio.create_task(self._emit_envelope(task, "approval.approved"))
        except RuntimeError:
            coro = self._emit_envelope(task, "approval.approved")
            coro.close()
        return task

    def reject(
        self, approval_id: str, approved_by: str = "operator", note: str = ""
    ) -> Optional[ApprovalTask]:
        """Mark a pending approval as rejected."""
        task = self._tasks.get(approval_id)
        if not task:
            return None
        if task.state != ApprovalState.PENDING:
            return None

        task.state = ApprovalState.REJECTED
        task.approved_by = approved_by
        task.approved_at = time.time()
        task.approval_note = note
        self._stats["total_rejected"] += 1

        # Feed negative outcome to JEPA target encoder
        try:
            from .jepa_target_encoder import get_target_encoder, OutcomeRecord
            encoder = get_target_encoder()
            outcome = OutcomeRecord(
                trace_id=task.trace_id,
                hypothesis=task.answer,
                task_class=task.task_class,
                confidence=task.confidence,
                critique_score=0.0,
                iterations=0,
                elapsed_ms=0.0,
                evidence_count=0,
                auto_executed=False,
                human_overridden=True,
            )
            encoder.observe(outcome)
            logger.info(f"[APPROVAL] Rejected {approval_id} — negative outcome fed to encoder")
        except Exception as e:
            logger.debug(f"[APPROVAL] encoder feedback failed (non-fatal): {e}")

        try:
            asyncio.create_task(self._emit_envelope(task, "approval.rejected"))
        except RuntimeError:
            coro = self._emit_envelope(task, "approval.rejected")
            coro.close()
        return task

    def mark_executed(
        self,
        approval_id: str,
        dispatch_result: str,
        dispatch_service: str,
        dispatch_latency_ms: float,
        dispatch_error: str = "",
    ) -> Optional[ApprovalTask]:
        """Mark an approved task as executed (or execution failed)."""
        task = self._tasks.get(approval_id)
        if not task:
            return None

        if dispatch_result == "executed":
            task.state = ApprovalState.EXECUTED
            self._stats["total_executed"] += 1
        else:
            task.state = ApprovalState.EXECUTION_FAILED
            self._stats["total_execution_failed"] += 1

        task.dispatch_result = dispatch_result
        task.dispatch_service = dispatch_service
        task.dispatch_latency_ms = dispatch_latency_ms
        task.dispatch_error = dispatch_error

        logger.info(f"[APPROVAL] {task.state.value} {approval_id} → {dispatch_service}")
        try:
            asyncio.create_task(self._emit_envelope(task, f"approval.{task.state.value}"))
        except RuntimeError:
            coro = self._emit_envelope(task, f"approval.{task.state.value}")
            coro.close()
        return task

    def get(self, approval_id: str) -> Optional[ApprovalTask]:
        """Get a single approval task by ID."""
        return self._tasks.get(approval_id)

    def pending(self, limit: int = 20) -> list[ApprovalTask]:
        """Get pending approval tasks, oldest first."""
        # Auto-expire stale tasks
        self._expire_stale()
        results = []
        for aid in self._pending:
            task = self._tasks.get(aid)
            if task and task.state == ApprovalState.PENDING:
                results.append(task)
                if len(results) >= limit:
                    break
        return results

    def recent(self, limit: int = 50, state: str = "") -> list[ApprovalTask]:
        """Get recent approval tasks, optionally filtered by state."""
        results = []
        for task in reversed(list(self._tasks.values())):
            if state and task.state.value != state:
                continue
            results.append(task)
            if len(results) >= limit:
                break
        return results

    def _expire_stale(self) -> None:
        """Expire pending tasks that have exceeded TTL."""
        for task in list(self._tasks.values()):
            if task.state == ApprovalState.PENDING and task.is_expired:
                self._expire_task(task)

    def _expire_task(self, task: ApprovalTask) -> None:
        task.state = ApprovalState.EXPIRED
        self._stats["total_expired"] += 1
        logger.info(f"[APPROVAL] Expired {task.approval_id} (TTL {APPROVAL_TTL_SECONDS:.0f}s)")

    def stats(self) -> dict:
        """Aggregate approval statistics."""
        pending_count = sum(1 for t in self._tasks.values() if t.state == ApprovalState.PENDING)
        return {
            "total_tasks": len(self._tasks),
            "pending": pending_count,
            "stats": self._stats,
            "ttl_seconds": APPROVAL_TTL_SECONDS,
        }

    def start_supabase_poller(self, interval: float = 3.0) -> None:
        """Start a background task that polls Supabase for approval decisions every N seconds."""
        if self._supabase_poller is not None:
            return  # Already running
        
        async def _poll_loop():
            while True:
                try:
                    await asyncio.sleep(interval)
                    await self.poll_supabase_decisions()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.debug(f"[APPROVAL] Supabase poller error: {e}")
                    await asyncio.sleep(interval)
        
        try:
            self._supabase_poller = asyncio.create_task(_poll_loop())
            logger.info(f"[APPROVAL] Supabase decision poller started (interval={interval}s)")
        except RuntimeError:
            logger.debug("[APPROVAL] No event loop — Supabase poller not started")

    async def poll_supabase_decisions(self) -> int:
        """
        CB-9: Poll Supabase cb_messages for approval decisions made from Lovable UI.
        
        Looks for messages where source='lovable-approval-ui' and 
        payload contains an approval_id matching one of our pending tasks.
        Processes the decision (approve/reject) and removes it from the queue.
        
        Returns the number of decisions processed.
        """
        if not self._pending:
            return 0
        
        supabase_url = os.getenv("SUPABASE_URL", "https://ziqenqqgnqxqrazmjohs.supabase.co")
        supabase_key = os.getenv("SUPABASE_SVC_KEY") or os.getenv("SUPABASE_ANON_KEY", "")
        
        if not supabase_key:
            return 0  # No Supabase configured — skip polling
        
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                # Query for recent lovable-approval-ui decisions
                res = await client.get(
                    f"{supabase_url}/rest/v1/cb_messages",
                    params={
                        "select": "*",
                        "source": "eq.lovable-approval-ui",
                        "order": "created_at.desc",
                        "limit": "10",
                    },
                    headers={
                        "apikey": supabase_key,
                        "Authorization": f"Bearer {supabase_key}",
                    },
                )
                if not res.is_success:
                    return 0
                
                rows = res.json()
                if not rows:
                    return 0
                
                processed = 0
                for row in rows:
                    payload = row.get("payload", {})
                    approval_id = payload.get("approval_id")
                    action = payload.get("action")
                    approved_by = payload.get("approved_by", "lovable-ui")
                    note = payload.get("note", "")
                    
                    if not approval_id or not action:
                        continue
                    
                    # Check if we have this task and it's pending
                    task = self._tasks.get(approval_id)
                    if not task or task.state != ApprovalState.PENDING:
                        continue  # Already processed or unknown
                    
                    if action == "approve":
                        self.approve(approval_id, approved_by=approved_by, note=note)
                        logger.info(f"[APPROVAL] Supabase poll: approved {approval_id} by {approved_by}")
                        processed += 1
                    elif action == "reject":
                        self.reject(approval_id, approved_by=approved_by, note=note)
                        logger.info(f"[APPROVAL] Supabase poll: rejected {approval_id} by {approved_by}")
                        processed += 1
                
                return processed
                
        except Exception as e:
            logger.debug(f"[APPROVAL] Supabase poll failed (non-fatal): {e}")
            return 0

    async def _emit_envelope(self, task: ApprovalTask, event_type: str) -> None:
        """Emit a CB v1 envelope for approval state changes to Orbitron bus."""
        envelope = {
            "spec": "cb.v1",
            "message_id": str(uuid.uuid4()),
            "trace_id": task.trace_id,
            "source": "k9-approval",
            "target": "orbitron-bus",
            "type": event_type,
            "ontology_tags": [task.task_class, task.risk_level, "cb-9"],
            "confidence": task.confidence,
            "payload": task.to_dict(),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                await client.post(
                    f"{ORBITRON_URL}/cb",
                    json=envelope,
                    headers={"Content-Type": "application/json"},
                )
        except Exception as e:
            logger.debug(f"[APPROVAL] envelope publish failed (non-fatal): {e}")


# ── Global singleton ─────────────────────────────────────────────────────────
_workflow: Optional[ApprovalWorkflow] = None


def get_approval_workflow() -> ApprovalWorkflow:
    global _workflow
    if _workflow is None:
        _workflow = ApprovalWorkflow()
    return _workflow
