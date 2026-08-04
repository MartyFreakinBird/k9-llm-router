"""
k9-mcts — execution_dispatcher.py

CB-6 Execution Dispatcher — wires the handover engine's AUTO_EXECUTE decisions
to actual downstream service calls. When the handover engine approves an
auto-execute, this module routes the query to the appropriate service and
returns the real result.

Routing matrix:
  task_class=compliance + risk=read_only      → TX adapter :9005/compliance/check
  task_class=analytics  + risk=read_only      → text_to_sql engine (local)
  task_class=forensic   + risk=read_only      → TX adapter :9005/balance/{addr}
  task_class=execution  + risk=idempotent     → aeg_signal_router :9004/batch
  Any other combination                      → NO_OP (shouldn't happen — handover gates this)

The dispatcher is the last stop before external action. It:
  1. Validates the handover decision is AUTO_EXECUTE (double-check)
  2. Extracts execution parameters from the MCTS answer
  3. Routes to the correct downstream service
  4. Returns the raw result + a success flag
  5. Feeds the outcome back to the target encoder

This module NEVER executes state-changing or high-risk operations —
the handover engine guarantees those are HUMAN_REVIEW before reaching here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

import httpx

from .handover_engine import HandoverDecision, RiskLevel, HandoverResult
from .jepa_target_encoder import get_target_encoder, OutcomeRecord
from .security_reinforcement import get_security_gate

logger = logging.getLogger("k9.dispatcher")

# ── Service endpoints ────────────────────────────────────────────────────────
TX_ADAPTER_URL = os.getenv("K9_TX_ADAPTER_URL", "http://localhost:9005")
AEG_SIGNAL_URL = os.getenv("AEG_SIGNAL_URL", "http://localhost:9004")
LLM_ROUTER_URL = os.getenv("K9_JEPA_ENDPOINT", "http://localhost:8765/route")
DISPATCHER_TIMEOUT = float(os.getenv("DISPATCHER_TIMEOUT", "15.0"))


class DispatchResult(str, Enum):
    EXECUTED = "executed"
    NO_OP = "no_op"
    ERROR = "error"
    REJECTED = "rejected"


@dataclass
class ExecutionOutcome:
    dispatch_result: DispatchResult
    service_called: str
    raw_response: dict = field(default_factory=dict)
    latency_ms: float = 0.0
    error: str = ""
    timestamp: float = field(default_factory=time.time)


class ExecutionDispatcher:
    """
    Routes AUTO_EXECUTE decisions to downstream services.
    Enforces: only read_only and idempotent tasks reach here.
    """

    def __init__(self):
        self.timeout = httpx.Timeout(DISPATCHER_TIMEOUT)
        self.dispatch_count = 0
        self.error_count = 0
        self.noop_count = 0
        self.total_latency_ms = 0.0
        self._encoder = get_target_encoder()

    async def dispatch(
        self,
        handover: HandoverResult,
        question: str,
        answer: str,
        trace_id: str = "",
    ) -> ExecutionOutcome:
        """
        Execute the downstream service call for an AUTO_EXECUTE decision.
        """
        # Safety gate — NEVER execute non-auto decisions
        if handover.decision != HandoverDecision.AUTO_EXECUTE:
            self.noop_count += 1
            return ExecutionOutcome(
                dispatch_result=DispatchResult.REJECTED,
                service_called="none",
                error=f"dispatch called with non-AUTO_EXECUTE decision: {handover.decision.value}",
            )

        start = time.monotonic()
        task_class = handover.task_class
        risk = handover.risk_level
        self.dispatch_count += 1

        # ── Security Gate: check before every dispatch ──────────────────────────
        sec = get_security_gate()
        allowed, sec_reason = sec.check(
            service_name=task_class,
            method="POST" if task_class != "forensic" else "GET",
            path="/" + task_class,
            task_class=task_class,
            confidence=handover.confidence,
            question=question,
            answer=answer,
        )
        if not allowed:
            self.error_count += 1
            logger.warning(f"[DISPATCHER] SECURITY GATE BLOCKED: {sec_reason}")
            return ExecutionOutcome(
                dispatch_result=DispatchResult.REJECTED,
                service_called=task_class,
                error=f"security_gate: {sec_reason}",
            )

        try:
            if task_class == "compliance" and risk == RiskLevel.READ_ONLY:
                result = await self._dispatch_compliance(question, answer)
            elif task_class == "analytics" and risk == RiskLevel.READ_ONLY:
                result = await self._dispatch_analytics(question, answer)
            elif task_class == "forensic" and risk == RiskLevel.READ_ONLY:
                result = await self._dispatch_forensic(question, answer)
            elif task_class == "execution" and risk == RiskLevel.IDEMPOTENT:
                result = await self._dispatch_idempotent(question, answer)
            else:
                result = ExecutionOutcome(
                    dispatch_result=DispatchResult.NO_OP,
                    service_called="none",
                    error=f"no dispatcher for task_class={task_class} risk={risk.value}",
                )
                self.noop_count += 1

        except Exception as e:
            self.error_count += 1
            result = ExecutionOutcome(
                dispatch_result=DispatchResult.ERROR,
                service_called=task_class,
                error=str(e)[:200],
            )
            logger.error(f"[DISPATCHER] error in {task_class}: {e}")

        # Record latency
        result.latency_ms = (time.monotonic() - start) * 1000
        self.total_latency_ms += result.latency_ms

        # Feed outcome back to target encoder
        self._record_outcome(handover, result, answer, trace_id)

        logger.info(
            f"[DISPATCHER] {result.dispatch_result.value} "
            f"service={result.service_called} "
            f"latency={result.latency_ms:.0f}ms"
        )

        return result

    # ── Compliance: TX adapter :9005 ──────────────────────────────────────────

    async def _dispatch_compliance(self, question: str, answer: str) -> ExecutionOutcome:
        """Route compliance queries to the TX adapter's compliance check."""
        # Extract address from the question/answer
        addr = self._extract_address(question + " " + answer)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{TX_ADAPTER_URL}/compliance/check",
                json={
                    "account_address": addr or "",
                    "denom": "utx",
                    "forward": True,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        return ExecutionOutcome(
            dispatch_result=DispatchResult.EXECUTED,
            service_called="tx-adapter:compliance",
            raw_response=data,
        )

    # ── Analytics: text-to-SQL (local) ────────────────────────────────────────

    async def _dispatch_analytics(self, question: str, answer: str) -> ExecutionOutcome:
        """Route analytics queries to the text-to-SQL engine."""
        from src.text_to_sql import text_to_sql_engine

        result = await text_to_sql_engine.query(question, user_id="mcts-auto")

        return ExecutionOutcome(
            dispatch_result=DispatchResult.EXECUTED if result.success else DispatchResult.ERROR,
            service_called="text-to-sql",
            raw_response={
                "success": result.success,
                "sql": result.query or "",
                "rows": result.rows[:100] if result.rows else [],
                "columns": result.columns or [],
                "row_count": len(result.rows) if result.rows else 0,
                "error": result.error or "",
            },
            error=result.error or "",
        )

    # ── Forensic: TX adapter balance lookup ────────────────────────────────────

    async def _dispatch_forensic(self, question: str, answer: str) -> ExecutionOutcome:
        """Route forensic queries to TX adapter balance lookup."""
        addr = self._extract_address(question + " " + answer)
        if not addr:
            return ExecutionOutcome(
                dispatch_result=DispatchResult.ERROR,
                service_called="tx-adapter:balance",
                error="no address found in question/answer to query",
            )

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{TX_ADAPTER_URL}/balance/{addr}")
            resp.raise_for_status()
            data = resp.json()

        return ExecutionOutcome(
            dispatch_result=DispatchResult.EXECUTED,
            service_called="tx-adapter:balance",
            raw_response=data,
        )

    # ── Idempotent: AEG signal router ──────────────────────────────────────────

    async def _dispatch_idempotent(self, question: str, answer: str) -> ExecutionOutcome:
        """Route idempotent operations to the AEG signal router."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{AEG_SIGNAL_URL}/batch",
                json={
                    "signals": [{
                        "source": "k9-mcts-auto",
                        "signal_type": "idempotent",
                        "payload": {"question": question[:200], "answer": answer[:200]},
                        "confidence": 0.92,
                    }],
                },
            )
            resp.raise_for_status()
            data = resp.json()

        return ExecutionOutcome(
            dispatch_result=DispatchResult.EXECUTED,
            service_called="aeg-signal-router:batch",
            raw_response=data,
        )

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_address(text: str) -> str:
        """Extract a blockchain address from text (hex or bech32)."""
        match = re.search(r'(0x[a-fA-F0-9]{40}|core1[a-z0-9]{38,})', text)
        return match.group(1) if match else ""

    def _record_outcome(
        self, handover: HandoverResult, result: ExecutionOutcome,
        answer: str, trace_id: str,
    ):
        """Feed the execution outcome back to the target encoder."""
        # Success = executed without error
        success = result.dispatch_result == DispatchResult.EXECUTED
        # Map to confidence for the encoder: success → high, error → low
        confidence = handover.confidence if success else 0.3

        outcome = OutcomeRecord(
            trace_id=trace_id or str(uuid.uuid4()),
            hypothesis=answer,
            task_class=handover.task_class,
            confidence=confidence,
            critique_score=0.8 if success else 0.2,
            iterations=0,  # dispatch doesn't iterate
            elapsed_ms=result.latency_ms,
            evidence_count=len(result.raw_response),
            auto_executed=True,
        )
        self._encoder.observe(outcome)

    def stats(self) -> dict:
        avg_latency = (
            self.total_latency_ms / self.dispatch_count
            if self.dispatch_count > 0 else 0.0
        )
        return {
            "dispatch_count": self.dispatch_count,
            "error_count": self.error_count,
            "noop_count": self.noop_count,
            "avg_latency_ms": round(avg_latency, 1),
            "total_latency_ms": round(self.total_latency_ms, 1),
        }


# ── Global singleton ─────────────────────────────────────────────────────────
_dispatcher: Optional[ExecutionDispatcher] = None


def get_dispatcher() -> ExecutionDispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = ExecutionDispatcher()
    return _dispatcher
