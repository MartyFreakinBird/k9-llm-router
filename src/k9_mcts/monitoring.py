"""
k9-mcts — monitoring.py

CB-8: Real-time monitoring & alerting for the autonomous decision pipeline.

Monitors the decision journal and JEPA target encoder for:
  1. Error rate spikes — dispatch error rate exceeding threshold
  2. Confidence drift — rolling average confidence dropping below baseline
  3. JEPA class degradation — proven class success rate falling
  4. Human-review backlog — pending items exceeding SLA timeout
  5. Auto-execute rate anomaly — sudden spike or drop in auto-execute ratio
  6. Circuit breaker — emergency stop on sustained failures

Alerts are emitted as CB v1 "alert" envelopes to the Orbitron bus and
recorded in the decision journal. When the circuit breaker trips,
auto-execution is halted until manual reset.

Endpoints (via api.py):
  - GET /reason/monitor/status — current monitoring state + all metrics
  - GET /reason/monitor/alerts — active alerts
  - POST /reason/monitor/circuit-breaker/reset — reset circuit breaker
  - GET /reason/monitor/circuit-breaker — circuit breaker state
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
from .jepa_target_encoder import get_target_encoder
from .handover_engine import get_handover_engine, HandoverDecision

logger = logging.getLogger("k9.monitor")

ORBITRON_URL = os.getenv("ORBITRON_BUS_URL", "http://localhost:8769")

# ── Thresholds (configurable via env) ────────────────────────────────────────
ERROR_RATE_THRESHOLD = float(os.getenv("MONITOR_ERROR_RATE_THRESHOLD", "0.30"))       # 30% dispatch errors
CONFIDENCE_DRIFT_THRESHOLD = float(os.getenv("MONITOR_CONFIDENCE_DRIFT", "0.15"))        # drop of 15% from baseline
HUMAN_REVIEW_SLA_SECONDS = float(os.getenv("MONITOR_HUMAN_SLA_S", "3600"))               # 1 hour SLA
AUTO_RATE_ANOMALY_DELTA = float(os.getenv("MONITOR_AUTO_RATE_DELTA", "0.40"))            # 40% change in ratio
CIRCUIT_BREAKER_ERROR_STREAK = int(os.getenv("MONITOR_CB_STREAK", "10"))                 # 10 consecutive errors
ROLLING_WINDOW = int(os.getenv("MONITOR_WINDOW", "50"))                                  # last 50 decisions


class AlertLevel(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    EMERGENCY = "emergency"


class AlertType(str, enum.Enum):
    ERROR_RATE_SPIKE = "error_rate_spike"
    CONFIDENCE_DRIFT = "confidence_drift"
    JEPA_CLASS_DEGRADATION = "jepa_class_degradation"
    HUMAN_REVIEW_BACKLOG = "human_review_backlog"
    AUTO_RATE_ANOMALY = "auto_rate_anomaly"
    CIRCUIT_BREAKER_TRIP = "circuit_breaker_trip"


@dataclass
class Alert:
    alert_id: str
    alert_type: AlertType
    level: AlertLevel
    message: str
    metric_value: float
    threshold: float
    timestamp: float = field(default_factory=time.time)
    acknowledged: bool = False
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class CircuitBreaker:
    """
    Emergency stop for auto-execution.
    Trips after CIRCUIT_BREAKER_ERROR_STREAK consecutive dispatch errors.
    Requires manual reset via API.
    """
    def __init__(self):
        self.tripped = False
        self.consecutive_errors = 0
        self.trip_reason = ""
        self.trip_timestamp = 0.0
        self.total_trips = 0

    def record_dispatch(self, success: bool) -> bool:
        """Record a dispatch result. Returns True if circuit is OK."""
        if self.tripped:
            return False
        if success:
            self.consecutive_errors = 0
        else:
            self.consecutive_errors += 1
            if self.consecutive_errors >= CIRCUIT_BREAKER_ERROR_STREAK:
                self.trip()
        return not self.tripped

    def trip(self, reason: str = "consecutive dispatch errors") -> None:
        self.tripped = True
        self.trip_reason = reason
        self.trip_timestamp = time.time()
        self.total_trips += 1
        logger.critical(f"[CIRCUIT-BREAKER] TRIPPED: {reason} (streak={self.consecutive_errors})")

    def reset(self) -> None:
        self.tripped = False
        self.consecutive_errors = 0
        self.trip_reason = ""
        logger.info("[CIRCUIT-BREAKER] Reset — auto-execution resumed")

    def to_dict(self) -> dict:
        return {
            "tripped": self.tripped,
            "consecutive_errors": self.consecutive_errors,
            "trip_reason": self.trip_reason,
            "trip_timestamp": self.trip_timestamp,
            "total_trips": self.total_trips,
            "threshold": CIRCUIT_BREAKER_ERROR_STREAK,
        }


class MonitoringEngine:
    """
    Real-time monitoring of the autonomous decision pipeline.
    Runs checks on every call to evaluate() — no background loop needed
    since the journal and handover engine are updated synchronously.
    """

    def __init__(self):
        self._alerts: deque[Alert] = deque(maxlen=200)
        self._circuit_breaker = CircuitBreaker()
        self._baseline_confidence = 0.0
        self._baseline_samples = 0
        self._auto_execute_count = 0
        self._human_review_count = 0
        self._reject_count = 0
        self._dispatch_success_count = 0
        self._dispatch_error_count = 0
        self._total_evaluations = 0
        self._last_evaluation_ts = 0.0
        self._monitoring_active = True

    def evaluate(self, entry: JournalEntry) -> list[Alert]:
        """
        Run all monitoring checks against a new journal entry.
        Returns list of any new alerts triggered.
        """
        if not self._monitoring_active:
            return []

        self._total_evaluations += 1
        self._last_evaluation_ts = time.time()

        # Update counters
        if entry.decision == "auto_execute":
            self._auto_execute_count += 1
            if entry.dispatch_result == "executed":
                self._dispatch_success_count += 1
                self._circuit_breaker.record_dispatch(True)
            elif entry.dispatch_result == "error":
                self._dispatch_error_count += 1
                self._circuit_breaker.record_dispatch(False)
        elif entry.decision == "human_review":
            self._human_review_count += 1
        elif entry.decision == "reject":
            self._reject_count += 1

        # Update confidence baseline (rolling average)
        if self._baseline_samples < ROLLING_WINDOW:
            self._baseline_samples += 1
            self._baseline_confidence = (
                (self._baseline_confidence * (self._baseline_samples - 1) + entry.confidence)
                / self._baseline_samples
            )
        else:
            self._baseline_confidence = (
                0.95 * self._baseline_confidence + 0.05 * entry.confidence
            )

        new_alerts = []

        # 1. Error rate check
        total_dispatches = self._dispatch_success_count + self._dispatch_error_count
        if total_dispatches >= 5:
            error_rate = self._dispatch_error_count / total_dispatches
            if error_rate >= ERROR_RATE_THRESHOLD:
                alert = Alert(
                    alert_id=str(uuid.uuid4()),
                    alert_type=AlertType.ERROR_RATE_SPIKE,
                    level=AlertLevel.WARNING if error_rate < 0.5 else AlertLevel.CRITICAL,
                    message=f"Dispatch error rate {error_rate:.0%} exceeds threshold {ERROR_RATE_THRESHOLD:.0%}",
                    metric_value=error_rate,
                    threshold=ERROR_RATE_THRESHOLD,
                    metadata={"errors": self._dispatch_error_count, "total": total_dispatches},
                )
                new_alerts.append(alert)

        # 2. Confidence drift check
        if self._baseline_samples >= 10 and entry.confidence < self._baseline_confidence - CONFIDENCE_DRIFT_THRESHOLD:
            drift = self._baseline_confidence - entry.confidence
            alert = Alert(
                alert_id=str(uuid.uuid4()),
                alert_type=AlertType.CONFIDENCE_DRIFT,
                level=AlertLevel.WARNING,
                message=f"Confidence {entry.confidence:.3f} drifted {drift:.3f} below baseline {self._baseline_confidence:.3f}",
                metric_value=entry.confidence,
                threshold=self._baseline_confidence - CONFIDENCE_DRIFT_THRESHOLD,
                metadata={"baseline": self._baseline_confidence, "drift": drift},
            )
            new_alerts.append(alert)

        # 3. Human review backlog check
        journal = get_journal()
        pending_human = journal.recent(limit=100, decision="human_review")
        stale_count = 0
        now = time.time()
        for e in pending_human:
            if now - e.timestamp > HUMAN_REVIEW_SLA_SECONDS:
                stale_count += 1
        if stale_count > 0:
            alert = Alert(
                alert_id=str(uuid.uuid4()),
                alert_type=AlertType.HUMAN_REVIEW_BACKLOG,
                level=AlertLevel.WARNING if stale_count < 5 else AlertLevel.CRITICAL,
                message=f"{stale_count} human-review items past SLA ({HUMAN_REVIEW_SLA_SECONDS:.0f}s)",
                metric_value=stale_count,
                threshold=0,
                metadata={"sla_seconds": HUMAN_REVIEW_SLA_SECONDS},
            )
            new_alerts.append(alert)

        # 4. Auto-execute rate anomaly
        total_decisions = self._auto_execute_count + self._human_review_count + self._reject_count
        if total_decisions >= 20:
            auto_ratio = self._auto_execute_count / total_decisions
            # Check for sudden drop (was auto-executing, now stopped)
            if auto_ratio < 0.05 and self._auto_execute_count > 5:
                alert = Alert(
                    alert_id=str(uuid.uuid4()),
                    alert_type=AlertType.AUTO_RATE_ANOMALY,
                    level=AlertLevel.WARNING,
                    message=f"Auto-execute ratio dropped to {auto_ratio:.1%} — possible class degradation",
                    metric_value=auto_ratio,
                    threshold=0.05,
                    metadata={"auto": self._auto_execute_count, "total": total_decisions},
                )
                new_alerts.append(alert)

        # 5. Circuit breaker trip
        if self._circuit_breaker.tripped:
            alert = Alert(
                alert_id=str(uuid.uuid4()),
                alert_type=AlertType.CIRCUIT_BREAKER_TRIP,
                level=AlertLevel.EMERGENCY,
                message=f"CIRCUIT BREAKER TRIPPED: {self._circuit_breaker.trip_reason}. Auto-execution halted.",
                metric_value=self._circuit_breaker.consecutive_errors,
                threshold=CIRCUIT_BREAKER_ERROR_STREAK,
                metadata=self._circuit_breaker.to_dict(),
            )
            new_alerts.append(alert)

        # 6. JEPA class degradation
        encoder = get_target_encoder()
        for task_class, stats in encoder.get_class_stats().items():
            if stats.get("total", 0) >= 20:
                success_rate = stats.get("successes", 0) / stats["total"]
                ema_conf = stats.get("ema_confidence", 0)
                if ema_conf > 0 and success_rate < 0.5 and encoder.is_proven(task_class):
                    alert = Alert(
                        alert_id=str(uuid.uuid4()),
                        alert_type=AlertType.JEPA_CLASS_DEGRADATION,
                        level=AlertLevel.CRITICAL,
                        message=f"Proven class '{task_class}' degrading: success_rate={success_rate:.1%}, ema_conf={ema_conf:.3f}",
                        metric_value=success_rate,
                        threshold=0.5,
                        metadata={"task_class": task_class, "ema_confidence": ema_conf, "total": stats["total"]},
                    )
                    new_alerts.append(alert)

        # Record new alerts
        for alert in new_alerts:
            self._alerts.append(alert)
            logger.warning(f"[MONITOR] ALERT {alert.level.value.upper()}: {alert.message}")
            # Emit CB v1 alert envelope (fire-and-forget, safe if no event loop)
            try:
                asyncio.create_task(self._emit_alert_envelope(alert))
            except RuntimeError:
                # No running event loop — close the coroutine to avoid warnings
                coro = self._emit_alert_envelope(alert)
                coro.close()

        return new_alerts

    async def _emit_alert_envelope(self, alert: Alert) -> None:
        """Emit a CB v1 alert envelope to the Orbitron bus."""
        envelope = {
            "spec": "cb.v1",
            "message_id": str(uuid.uuid4()),
            "trace_id": alert.alert_id,
            "source": "k9-monitor",
            "target": "orbitron-bus",
            "type": "alert",
            "ontology_tags": [alert.alert_type.value, alert.level.value, "cb-8"],
            "confidence": 1.0,
            "payload": alert.to_dict(),
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
            logger.debug(f"[MONITOR] alert envelope publish failed (non-fatal): {e}")

    def can_auto_execute(self) -> bool:
        """Gate: should auto-execution be allowed right now?"""
        return self._monitoring_active and not self._circuit_breaker.tripped

    def reset_circuit_breaker(self) -> dict:
        """Manually reset the circuit breaker."""
        self._circuit_breaker.reset()
        return self._circuit_breaker.to_dict()

    def alerts(self, level: str = "", limit: int = 50) -> list[dict]:
        """Get recent alerts, optionally filtered by level."""
        results = []
        for alert in reversed(self._alerts):
            if level and alert.level.value != level:
                continue
            results.append(alert.to_dict())
            if len(results) >= limit:
                break
        return results

    def status(self) -> dict:
        """Full monitoring status."""
        total_decisions = self._auto_execute_count + self._human_review_count + self._reject_count
        total_dispatches = self._dispatch_success_count + self._dispatch_error_count

        return {
            "monitoring_active": self._monitoring_active,
            "total_evaluations": self._total_evaluations,
            "last_evaluation_ts": self._last_evaluation_ts,
            "decision_counts": {
                "auto_execute": self._auto_execute_count,
                "human_review": self._human_review_count,
                "reject": self._reject_count,
                "total": total_decisions,
            },
            "dispatch_stats": {
                "success": self._dispatch_success_count,
                "error": self._dispatch_error_count,
                "error_rate": round(self._dispatch_error_count / max(1, total_dispatches), 4),
            },
            "confidence": {
                "baseline": round(self._baseline_confidence, 4),
                "baseline_samples": self._baseline_samples,
                "drift_threshold": CONFIDENCE_DRIFT_THRESHOLD,
            },
            "circuit_breaker": self._circuit_breaker.to_dict(),
            "active_alerts": len([a for a in self._alerts if not a.acknowledged]),
            "total_alerts": len(self._alerts),
            "thresholds": {
                "error_rate": ERROR_RATE_THRESHOLD,
                "confidence_drift": CONFIDENCE_DRIFT_THRESHOLD,
                "human_review_sla_s": HUMAN_REVIEW_SLA_SECONDS,
                "auto_rate_anomaly_delta": AUTO_RATE_ANOMALY_DELTA,
                "circuit_breaker_streak": CIRCUIT_BREAKER_ERROR_STREAK,
                "rolling_window": ROLLING_WINDOW,
            },
        }


# ── Global singleton ─────────────────────────────────────────────────────────
_monitor: Optional[MonitoringEngine] = None


def get_monitor() -> MonitoringEngine:
    global _monitor
    if _monitor is None:
        _monitor = MonitoringEngine()
    return _monitor
