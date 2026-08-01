"""
k9-mcts — handover_engine.py

Autonomous Handover Engine (CB-6).
Decides whether an MCTS-committed result should be auto-executed or
escalated to human review.

Risk classification:
  - read_only:     queries, analytics, lookups — always auto-eligible
  - idempotent:    retryable writes, cache invalidation — auto if proven
  - state_changing: fund moves, contract calls — human fallback always
  - high_risk:     cross-chain, large value, new contract — human + cooldown

Decision matrix:
  proven class + read_only + confidence >= threshold  → AUTO_EXECUTE
  proven class + idempotent + confidence >= threshold  → AUTO_EXECUTE
  unproven + any risk + confidence >= threshold        → HUMAN_REVIEW
  any class + state_changing                          → HUMAN_REVIEW
  any class + high_risk                               → HUMAN_REVIEW + COOLDOWN
  confidence < threshold                              → REJECT

Auto-executed tasks emit a CB v1 "execution_request" envelope with signature.
Human-review tasks emit a CB v1 "alert" envelope.
"""

from __future__ import annotations

import enum
import time
import uuid
import logging
from dataclasses import dataclass, field
from typing import Optional

from .jepa_target_encoder import get_target_encoder, OutcomeRecord

logger = logging.getLogger("k9.handover")


class RiskLevel(str, enum.Enum):
    READ_ONLY = "read_only"
    IDEMPOTENT = "idempotent"
    STATE_CHANGING = "state_changing"
    HIGH_RISK = "high_risk"


class HandoverDecision(str, enum.Enum):
    AUTO_EXECUTE = "auto_execute"
    HUMAN_REVIEW = "human_review"
    REJECT = "reject"


@dataclass
class HandoverResult:
    decision: HandoverDecision
    risk_level: RiskLevel
    task_class: str
    confidence: float
    reason: str
    cooldown_seconds: int = 0
    execution_envelope: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


# ── Risk classification keywords ─────────────────────────────────────────────
# Maps question/hypothesis text to risk levels.
READ_ONLY_KEYWORDS = [
    "query", "show", "list", "count", "find", "check", "get", "lookup",
    "analyze", "inspect", "monitor", "scan", "detect", "report", "status",
    "balance", "compliance", "trace", "audit",
]

IDEMPOTENT_KEYWORDS = [
    "cache", "invalidate", "refresh", "reindex", "rebuild", "sync",
    "heartbeat", "register", "update_params",
]

HIGH_RISK_KEYWORDS = [
    "transfer", "send", "swap", "execute", "deploy", "approve", "permit",
    "sign", "broadcast", "withdraw", "deposit", "bridge", "cross-chain",
    "large", "max", "all funds",
]

STATE_CHANGING_KEYWORDS = [
    "write", "set", "update", "create", "delete", "remove", "modify",
    "insert", "grant", "revoke", "pause", "resume",
]


def classify_risk(text: str) -> RiskLevel:
    """Classify a question/hypothesis into a risk level based on keywords."""
    text_lower = text.lower()

    # High risk takes priority
    for kw in HIGH_RISK_KEYWORDS:
        if kw in text_lower:
            return RiskLevel.HIGH_RISK

    # State changing
    for kw in STATE_CHANGING_KEYWORDS:
        if kw in text_lower:
            return RiskLevel.STATE_CHANGING

    # Idempotent
    for kw in IDEMPOTENT_KEYWORDS:
        if kw in text_lower:
            return RiskLevel.IDEMPOTENT

    # Read-only (default for analytical queries)
    for kw in READ_ONLY_KEYWORDS:
        if kw in text_lower:
            return RiskLevel.READ_ONLY

    # Default: read-only (safe assumption for forensic MCTS)
    return RiskLevel.READ_ONLY


def classify_task(text: str) -> str:
    """Classify the task type for the target encoder."""
    text_lower = text.lower()
    if any(kw in text_lower for kw in ["sanction", "mixer", "frozen", "kyc", "compliance", "whitelist"]):
        return "compliance"
    if any(kw in text_lower for kw in ["transfer", "swap", "trade", "position", "drawdown", "risk"]):
        return "execution"
    if any(kw in text_lower for kw in ["count", "stats", "summary", "report", "aggregate", "trend"]):
        return "analytics"
    return "forensic"


class HandoverEngine:
    """
    The gate between MCTS reasoning and autonomous action.
    Uses the JEPA target encoder's proven-class set + risk classification.
    """

    def __init__(self, confidence_threshold: float = 0.92):
        self.threshold = confidence_threshold
        self.encoder = get_target_encoder()
        self.encoder.threshold = confidence_threshold  # sync
        self.cooldown_seconds = 300  # 5 min cooldown for high-risk human review
        self.auto_execute_count = 0
        self.human_review_count = 0
        self.reject_count = 0
        self.last_decision_ts = 0.0

    def evaluate(
        self,
        question: str,
        answer: str,
        confidence: float,
        iterations: int,
        elapsed_ms: float,
        evidence_count: int,
        critique_score: float = 0.5,
        trace_id: str = "",
    ) -> HandoverResult:
        """
        Evaluate whether the MCTS-committed result should auto-execute or
        require human review.
        """
        self.last_decision_ts = time.time()
        task_class = classify_task(question)
        risk = classify_risk(question)

        # Confidence gate
        if confidence < self.threshold:
            self.reject_count += 1
            return HandoverResult(
                decision=HandoverDecision.REJECT,
                risk_level=risk,
                task_class=task_class,
                confidence=confidence,
                reason=f"confidence {confidence:.3f} below threshold {self.threshold}",
            )

        # Risk-based decision
        if risk == RiskLevel.HIGH_RISK:
            self.human_review_count += 1
            return self._human_review(
                question, answer, confidence, task_class, risk,
                iterations, elapsed_ms, evidence_count, critique_score, trace_id,
                cooldown=self.cooldown_seconds,
                reason="high_risk classification — human approval required",
            )

        if risk == RiskLevel.STATE_CHANGING:
            self.human_review_count += 1
            return self._human_review(
                question, answer, confidence, task_class, risk,
                iterations, elapsed_ms, evidence_count, critique_score, trace_id,
                reason="state_changing operation — human approval required",
            )

        # Read-only and idempotent: check proven class
        is_proven = self.encoder.is_proven(task_class)
        if not is_proven:
            self.human_review_count += 1
            return self._human_review(
                question, answer, confidence, task_class, risk,
                iterations, elapsed_ms, evidence_count, critique_score, trace_id,
                reason=f"task class '{task_class}' not yet proven (need {self.encoder.min_proven_samples} successes)",
            )

        # Proven + low-risk + high confidence → AUTO-EXECUTE
        self.auto_execute_count += 1
        return self._auto_execute(
            question, answer, confidence, task_class, risk,
            iterations, elapsed_ms, evidence_count, critique_score, trace_id,
        )

    def _auto_execute(
        self, question, answer, confidence, task_class, risk,
        iterations, elapsed_ms, evidence_count, critique_score, trace_id,
    ) -> HandoverResult:
        envelope = self._build_envelope(
            "execution_request", question, answer, confidence, trace_id,
            task_class, risk, iterations, elapsed_ms,
        )
        # Record outcome for target encoder
        outcome = OutcomeRecord(
            trace_id=trace_id or str(uuid.uuid4()),
            hypothesis=answer,
            task_class=task_class,
            confidence=confidence,
            critique_score=critique_score,
            iterations=iterations,
            elapsed_ms=elapsed_ms,
            evidence_count=evidence_count,
            auto_executed=True,
        )
        self.encoder.observe(outcome)

        logger.info(f"[HANDOVER] AUTO_EXECUTE task_class={task_class} conf={confidence:.3f}")
        return HandoverResult(
            decision=HandoverDecision.AUTO_EXECUTE,
            risk_level=risk,
            task_class=task_class,
            confidence=confidence,
            reason=f"proven class '{task_class}' + {risk.value} + confidence >= {self.threshold}",
            execution_envelope=envelope,
        )

    def _human_review(
        self, question, answer, confidence, task_class, risk,
        iterations, elapsed_ms, evidence_count, critique_score, trace_id,
        cooldown=0, reason="",
    ) -> HandoverResult:
        envelope = self._build_envelope(
            "alert", question, answer, confidence, trace_id,
            task_class, risk, iterations, elapsed_ms,
        )
        outcome = OutcomeRecord(
            trace_id=trace_id or str(uuid.uuid4()),
            hypothesis=answer,
            task_class=task_class,
            confidence=confidence,
            critique_score=critique_score,
            iterations=iterations,
            elapsed_ms=elapsed_ms,
            evidence_count=evidence_count,
            auto_executed=False,
            human_overridden=True,
        )
        self.encoder.observe(outcome)

        logger.info(f"[HANDOVER] HUMAN_REVIEW task_class={task_class} risk={risk.value} reason={reason}")
        return HandoverResult(
            decision=HandoverDecision.HUMAN_REVIEW,
            risk_level=risk,
            task_class=task_class,
            confidence=confidence,
            reason=reason,
            cooldown_seconds=cooldown,
            execution_envelope=envelope,
        )

    def _build_envelope(
        self, msg_type, question, answer, confidence, trace_id,
        task_class, risk, iterations, elapsed_ms,
    ) -> dict:
        return {
            "spec": "cb.v1",
            "message_id": str(uuid.uuid4()),
            "trace_id": trace_id or str(uuid.uuid4()),
            "source": "k9-handover",
            "target": "orbitron-bus",
            "type": msg_type,
            "ontology_tags": [task_class, risk.value, "cb-6"],
            "confidence": confidence,
            "payload": {
                "question": question,
                "answer": answer,
                "iterations": iterations,
                "elapsed_ms": elapsed_ms,
                "risk_level": risk.value,
                "task_class": task_class,
                "auto_eligible": msg_type == "execution_request",
            },
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def stats(self) -> dict:
        return {
            "threshold": self.threshold,
            "cooldown_seconds": self.cooldown_seconds,
            "auto_execute_count": self.auto_execute_count,
            "human_review_count": self.human_review_count,
            "reject_count": self.reject_count,
            "last_decision_ts": self.last_decision_ts,
            "target_encoder": self.encoder.stats(),
        }


# ── Global singleton ─────────────────────────────────────────────────────────
_handover: Optional[HandoverEngine] = None

def get_handover_engine() -> HandoverEngine:
    global _handover
    if _handover is None:
        _handover = HandoverEngine()
    return _handover
