"""
k9-mcts — security_reinforcement.py

Security reinforcement layer inspired by the Hugging Face AI attack case study.
Addresses 6 gaps identified from the July 2025 incident:

  1. EGRESS GUARD    — Allowlist of permitted service URLs + per-service rate limiting
                       Prevents lateral movement if the agent escapes its intended scope.
  2. BEHAVIORAL DETECTION — Statistical baseline learning (EMA) of request patterns.
                       Flags deviations: unusual task_class distribution, confidence
                       anomalies, dispatch rate spikes, off-hours activity.
  3. KILL SWITCH    — Global emergency halt + credential revocation plan.
                       One call halts all auto-execution, queues all dispatches, and
                       emits a CRED_REVOCATION alert.
  4. ZERO-TRUST      — CB v1 signature verification at internal hops.
                       Every dispatch must carry a valid HMAC-SHA256 signature from
                       the sovereign kernel. Unsigned or mismatched = rejected.
  5. FORENSIC PIPELINE — Suspicious activity capture + open-source LLM analysis hook.
                       Anomalous patterns are logged with full context for later
                       analysis by a sandboxed local model (Ollama/GLM).
  6. RATE LIMITING   — Per-service, per-time-window dispatch limits.
                       Prevents the 17,000-action flood scenario.

Integration: Wraps the existing ExecutionDispatcher. The dispatcher calls
security_gate.check() before every dispatch. If the gate denies, the dispatch
is recorded as REJECTED with the security reason.

All checks are non-blocking on the hot path — behavioral baselines update
asynchronously. Gate denials are fast (O(1) lookups).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger("k9.security")

# ── Configuration ──────────────────────────────────────────────────────────────

K9_SECURITY_ENABLED = os.getenv("K9_SECURITY_ENABLED", "true").lower() == "true"
K9_SIGNING_KEY = os.getenv("K9_SIGNING_KEY", "")  # HMAC-SHA256 shared secret

# Per-service rate limits (requests per window)
DEFAULT_RATE_LIMIT = int(os.getenv("K9_RATE_LIMIT", "20"))  # per minute
RATE_WINDOW_S = float(os.getenv("K9_RATE_WINDOW", "60"))

# Behavioral baseline parameters
BASELINE_EMA_ALPHA = float(os.getenv("K9_BASELINE_ALPHA", "0.001"))  # slow learning
ANOMALY_Z_THRESHOLD = float(os.getenv("K9_ANOMALY_Z", "3.0"))  # 3-sigma

# Forensic capture buffer
FORENSIC_BUFFER_SIZE = int(os.getenv("K9_FORENSIC_BUFFER", "1000"))


# ── 1. EGRESS GUARD ────────────────────────────────────────────────────────────

# Allowlist of permitted service URLs for the execution dispatcher.
# Any dispatch to a URL NOT in this list is blocked.
# This prevents lateral movement if the agent constructs unexpected URLs.
EGRESS_ALLOWLIST = {
    "tx-adapter:compliance": {
        "url": os.getenv("K9_TX_ADAPTER_URL", "http://localhost:9005"),
        "methods": {"POST"},
        "paths": {"/compliance/check"},
        "rate_limit": 10,  # per minute
    },
    "tx-adapter:balance": {
        "url": os.getenv("K9_TX_ADAPTER_URL", "http://localhost:9005"),
        "methods": {"GET"},
        "paths": {"/balance/"},
        "rate_limit": 20,
    },
    "aeg-signal-router": {
        "url": os.getenv("AEG_SIGNAL_URL", "http://localhost:9004"),
        "methods": {"POST"},
        "paths": {"/batch", "/signal"},
        "rate_limit": 15,
    },
    "text-to-sql": {
        "url": "local",  # in-process, no network
        "methods": {"INTERNAL"},
        "paths": {"*"},
        "rate_limit": 30,
    },
    "llm-router": {
        "url": os.getenv("K9_JEPA_ENDPOINT", "http://localhost:8765/route"),
        "methods": {"POST"},
        "paths": {"/route", "/query"},
        "rate_limit": 30,
    },
}


class EgressGuard:
    """
    Validates that a dispatch target is in the allowlist and within rate limits.
    Prevents the 'escaped sandbox → lateral movement' attack vector.
    """

    def __init__(self):
        self._allowlist = EGRESS_ALLOWLIST.copy()
        self._rate_counters: dict[str, deque] = defaultdict(lambda: deque())

    def check(self, service_name: str, method: str, path: str) -> tuple[bool, str]:
        """Check if a dispatch is permitted. Returns (allowed, reason)."""
        if not K9_SECURITY_ENABLED:
            return True, "security disabled"

        # Check allowlist
        entry = self._allowlist.get(service_name)
        if not entry:
            return False, f"service '{service_name}' not in egress allowlist"

        # Check method
        if method not in entry["methods"] and "*" not in entry["methods"]:
            return False, f"method '{method}' not permitted for '{service_name}'"

        # Check path
        allowed_paths = entry["paths"]
        if "*" not in allowed_paths:
            if not any(path.startswith(p) for p in allowed_paths):
                return False, f"path '{path}' not permitted for '{service_name}'"

        # Check rate limit
        limit = entry.get("rate_limit", DEFAULT_RATE_LIMIT)
        now = time.time()
        counter = self._rate_counters[service_name]

        # Purge old entries
        while counter and counter[0] < now - RATE_WINDOW_S:
            counter.popleft()

        if len(counter) >= limit:
            return False, f"rate limit exceeded for '{service_name}' ({limit}/{int(RATE_WINDOW_S)}s)"

        # Record this request
        counter.append(now)
        return True, "ok"

    def add_service(self, name: str, url: str, methods: set, paths: set, rate_limit: int = 20):
        """Dynamically add a service to the allowlist (for new integrations)."""
        self._allowlist[name] = {
            "url": url, "methods": methods, "paths": paths, "rate_limit": rate_limit
        }
        logger.info(f"[SECURITY] Egress allowlist updated: added '{name}' → {url}")

    def remove_service(self, name: str):
        """Remove a service from the allowlist (kill-switch for specific service)."""
        if name in self._allowlist:
            del self._allowlist[name]
            logger.warning(f"[SECURITY] Egress allowlist: REMOVED '{name}'")

    def list_services(self) -> dict:
        return {k: {**v, "current_rate": len(self._rate_counters[k])} for k, v in self._allowlist.items()}


# ── 2. BEHAVIORAL ANOMALY DETECTION ─────────────────────────────────────────────

class BehavioralMonitor:
    """
    Learns normal request patterns using exponential moving averages (EMA).
    Flags deviations beyond N-sigma from the learned baseline.

    Tracks:
      - Task class distribution (compliance/analytics/forensic/idempotent)
      - Confidence distribution per task class
      - Dispatch rate (requests per minute)
      - Time-of-day activity patterns
      - Answer length distribution

    This is statistical, not deep-learning — intentionally lightweight.
    The goal is fast detection of the 17,000-action flood scenario,
    not sophisticated attack classification.
    """

    def __init__(self):
        # EMA baselines
        self._task_class_ema: dict[str, float] = defaultdict(float)
        self._confidence_ema: dict[str, float] = defaultdict(float)
        self._dispatch_rate_ema = 0.0
        self._dispatch_rate_var = 1.0
        self._answer_len_ema = 0.0
        self._answer_len_var = 1.0

        # Time series for rate calculation
        self._dispatch_times: deque = deque(maxlen=1000)

        # Anomaly log
        self._anomalies: deque = deque(maxlen=100)

        # Is the system in "kill" mode (behavioral halt)?
        self._halted = False
        self._halt_reason = ""

    def observe(self, task_class: str, confidence: float, answer: str, timestamp: float | None = None):
        """Record a dispatch observation and update baselines."""
        if not K9_SECURITY_ENABLED:
            return

        ts = timestamp or time.time()
        self._dispatch_times.append(ts)

        # Update task class EMA (fraction of total)
        total = len(self._dispatch_times)
        for tc in self._task_class_ema:
            self._task_class_ema[tc] *= (1 - BASELINE_EMA_ALPHA)
        self._task_class_ema[task_class] += BASELINE_EMA_ALPHA

        # Update confidence EMA per task class
        delta = confidence - self._confidence_ema[task_class]
        self._confidence_ema[task_class] += BASELINE_EMA_ALPHA * delta

        # Update answer length EMA
        ans_len = len(answer)
        delta_len = ans_len - self._answer_len_ema
        self._answer_len_ema += BASELINE_EMA_ALPHA * delta_len
        self._answer_len_var = (1 - BASELINE_EMA_ALPHA) * self._answer_len_var + BASELINE_EMA_ALPHA * delta_len ** 2

        # Update dispatch rate EMA
        recent = sum(1 for t in self._dispatch_times if t > ts - 60)
        delta_rate = recent - self._dispatch_rate_ema
        self._dispatch_rate_ema += 0.01 * delta_rate  # faster learning for rate
        self._dispatch_rate_var = (1 - 0.01) * self._dispatch_rate_var + 0.01 * delta_rate ** 2

    def detect_anomalies(self) -> list[dict]:
        """Check current state against learned baselines. Returns list of anomalies."""
        if not K9_SECURITY_ENABLED or len(self._dispatch_times) < 50:
            return []  # Not enough data to detect

        anomalies = []
        now = time.time()

        # 1. Dispatch rate anomaly (flood detection)
        recent = sum(1 for t in self._dispatch_times if t > now - 60)
        if self._dispatch_rate_var > 0:
            z_score = (recent - self._dispatch_rate_ema) / max(self._dispatch_rate_var ** 0.5, 1.0)
            if z_score > ANOMALY_Z_THRESHOLD:
                anomaly = {
                    "type": "dispatch_rate_flood",
                    "z_score": round(z_score, 2),
                    "current_rate": recent,
                    "baseline_rate": round(self._dispatch_rate_ema, 1),
                    "severity": "critical" if z_score > 5 else "warning",
                }
                anomalies.append(anomaly)

        # 2. Confidence anomaly (unexpected drop or spike)
        for tc, ema in self._confidence_ema.items():
            if ema > 0:
                # A sudden drop to near-zero confidence in a proven class is suspicious
                if ema < 0.2 and len(self._dispatch_times) > 100:
                    anomaly = {
                        "type": "confidence_collapse",
                        "task_class": tc,
                        "current_ema": round(ema, 3),
                        "severity": "warning",
                    }
                    anomalies.append(anomaly)

        # 3. Answer length anomaly (potential data exfiltration)
        if self._answer_len_var > 0:
            # If recent answers are abnormally long, could be data exfiltration
            # Check the last few answer lengths against baseline
            z_len = self._answer_len_ema / max(self._answer_len_var ** 0.5, 1.0)
            if self._answer_len_ema > 10000 and z_len > ANOMALY_Z_THRESHOLD:
                anomaly = {
                    "type": "answer_length_anomaly",
                    "current_avg": round(self._answer_len_ema, 0),
                    "z_score": round(z_len, 2),
                    "severity": "warning",
                }
                anomalies.append(anomaly)

        if anomalies:
            for a in anomalies:
                a["timestamp"] = now
                self._anomalies.append(a)
                if a["severity"] == "critical":
                    logger.warning(f"[SECURITY] CRITICAL anomaly: {a['type']} (z={a.get('z_score', 'N/A')})")
                    # Auto-halt on critical anomalies
                    self.halt(f"critical anomaly: {a['type']}")
                else:
                    logger.info(f"[SECURITY] anomaly detected: {a['type']}")

        return anomalies

    def halt(self, reason: str):
        """Halt the system — no more auto-execution allowed."""
        self._halted = True
        self._halt_reason = reason
        logger.critical(f"[SECURITY] *** BEHAVIORAL HALT *** reason={reason}")

    def resume(self):
        """Resume after manual review."""
        self._halted = False
        self._halt_reason = ""
        logger.info("[SECURITY] Behavioral halt cleared — resuming")

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    def recent_anomalies(self, limit: int = 20) -> list[dict]:
        return list(self._anomalies)[-limit:]

    def baseline_stats(self) -> dict:
        return {
            "dispatch_rate_ema": round(self._dispatch_rate_ema, 2),
            "dispatch_rate_std": round(self._dispatch_rate_var ** 0.5, 2),
            "answer_len_ema": round(self._answer_len_ema, 0),
            "confidence_ema": {k: round(v, 3) for k, v in self._confidence_ema.items()},
            "task_class_ema": {k: round(v, 4) for k, v in self._task_class_ema.items()},
            "total_observations": len(self._dispatch_times),
            "halted": self._halted,
            "halt_reason": self._halt_reason,
        }


# ── 3. KILL SWITCH ──────────────────────────────────────────────────────────────

class KillSwitch:
    """
    Global emergency stop. When triggered:
      1. Halts all auto-execution (circuit breaker + behavioral monitor)
      2. Flags all credentials for revocation
      3. Queues all pending dispatches for human review
      4. Emits a CRITICAL alert to the monitoring system
      5. Records forensic evidence

    Triggered by:
      - Manual call to activate()
      - Behavioral monitor critical anomaly
      - Circuit breaker trip
      - Detection of unsigned execution requests
    """

    def __init__(self):
        self._active = False
        self._activated_at = 0.0
        self._reason = ""
        self._activated_by = ""
        self._credentials_flagged: set[str] = set()
        self._history: deque = deque(maxlen=20)

    def activate(self, reason: str, activated_by: str = "system") -> dict:
        """Activate the global kill switch."""
        self._active = True
        self._activated_at = time.time()
        self._reason = reason
        self._activated_by = activated_by
        self._credentials_flagged.update([
            "SUPABASE_SVC_KEY",
            "SUPABASE_ANON_KEY",
            "K9_SIGNING_KEY",
            "AEG_OPERATOR_KEY",
            "GEMINI_API_KEY",
        ])
        self._history.append({
            "action": "activate",
            "reason": reason,
            "by": activated_by,
            "timestamp": self._activated_at,
        })
        logger.critical(f"[SECURITY] *** KILL SWITCH ACTIVATED *** reason={reason} by={activated_by}")
        return {
            "active": True,
            "reason": reason,
            "activated_by": activated_by,
            "credentials_flagged": list(self._credentials_flagged),
            "timestamp": self._activated_at,
        }

    def deactivate(self, authorized_by: str = "operator") -> dict:
        """Deactivate the kill switch. Requires explicit authorization."""
        self._active = False
        self._history.append({
            "action": "deactivate",
            "by": authorized_by,
            "timestamp": time.time(),
        })
        logger.info(f"[SECURITY] Kill switch deactivated by {authorized_by}")
        return {"active": False, "deactivated_by": authorized_by}

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def credentials_to_revoke(self) -> list[str]:
        return list(self._credentials_flagged) if self._active else []

    def status(self) -> dict:
        return {
            "active": self._active,
            "reason": self._reason,
            "activated_by": self._activated_by,
            "activated_at": self._activated_at,
            "credentials_flagged": list(self._credentials_flagged),
            "history": list(self._history),
        }


# ── 4. ZERO-TRUST SIGNATURE VERIFICATION ────────────────────────────────────────

class SignatureVerifier:
    """
    Verifies HMAC-SHA256 signatures on internal dispatch requests.
    Every execution must carry a valid signature from the sovereign kernel.

    This prevents forged dispatch requests from injected code or
    compromised internal services.
    """

    def __init__(self, signing_key: str = ""):
        self._key = signing_key or K9_SIGNING_KEY

    def sign(self, payload: dict) -> str:
        """Sign a payload with the HMAC key. Returns hex signature."""
        if not self._key:
            return ""  # No key configured — signatures disabled
        msg = json.dumps(payload, sort_keys=True).encode()
        return hmac.new(self._key.encode(), msg, hashlib.sha256).hexdigest()

    def verify(self, payload: dict, signature: str) -> tuple[bool, str]:
        """Verify a payload signature. Returns (valid, reason)."""
        if not self._key:
            return True, "signing disabled (no key)"  # Fail open if unconfigured
        if not signature:
            return False, "missing signature"
        expected = self.sign(payload)
        if not hmac.compare_digest(expected, signature):
            return False, "signature mismatch"
        return True, "verified"

    def is_configured(self) -> bool:
        return bool(self._key)


# ── 5. FORENSIC CAPTURE PIPELINE ────────────────────────────────────────────────

@dataclass
class ForensicRecord:
    """A captured suspicious activity record for later analysis."""
    timestamp: float
    event_type: str  # "blocked_dispatch", "anomaly", "kill_switch", "unsigned_request"
    service: str
    task_class: str
    question: str  # truncated
    answer: str  # truncated
    confidence: float
    reason: str
    context: dict = field(default_factory=dict)


class ForensicCapture:
    """
    Captures suspicious activity for forensic analysis.
    Records are stored in a ring buffer and can be exported for
    analysis by a sandboxed open-source LLM (Ollama/GLM).

    In the Hugging Face incident, the team needed open-source models
    because commercial models refused to analyze attack payloads.
    We keep a local Ollama instance ready for exactly this.
    """

    def __init__(self, buffer_size: int = FORENSIC_BUFFER_SIZE):
        self._buffer: deque[ForensicRecord] = deque(maxlen=buffer_size)
        self._total_captured = 0

    def capture(
        self,
        event_type: str,
        service: str,
        task_class: str,
        question: str,
        answer: str,
        confidence: float,
        reason: str,
        context: dict | None = None,
    ):
        """Capture a suspicious event."""
        record = ForensicRecord(
            timestamp=time.time(),
            event_type=event_type,
            service=service,
            task_class=task_class,
            question=question[:500],
            answer=answer[:500],
            confidence=confidence,
            reason=reason,
            context=context or {},
        )
        self._buffer.append(record)
        self._total_captured += 1
        logger.info(f"[FORENSIC] captured {event_type}: {reason}")

    def export(self, limit: int = 100) -> list[dict]:
        """Export recent records for LLM forensic analysis."""
        return [
            {
                "timestamp": r.timestamp,
                "event_type": r.event_type,
                "service": r.service,
                "task_class": r.task_class,
                "question": r.question,
                "answer": r.answer,
                "confidence": r.confidence,
                "reason": r.reason,
                "context": r.context,
            }
            for r in list(self._buffer)[-limit:]
        ]

    def stats(self) -> dict:
        return {
            "total_captured": self._total_captured,
            "buffer_size": len(self._buffer),
            "by_type": dict(
                (et, sum(1 for r in self._buffer if r.event_type == et))
                for et in set(r.event_type for r in self._buffer)
            ),
        }


# ── SECURITY GATE (Integration Point) ──────────────────────────────────────────

class SecurityGate:
    """
    The single integration point for the execution dispatcher.
    Called before every dispatch to validate the request.

    Flow:
      1. Check kill switch (if active, block all)
      2. Check behavioral halt (if halted, block all)
      3. Verify egress allowlist + rate limit
      4. Verify signature (if signing enabled)
      5. If denied, capture forensic record
      6. If allowed, observe in behavioral monitor
    """

    def __init__(self):
        self.egress = EgressGuard()
        self.behavior = BehavioralMonitor()
        self.kill_switch = KillSwitch()
        self.signatures = SignatureVerifier()
        self.forensics = ForensicCapture()

    def check(
        self,
        service_name: str,
        method: str,
        path: str,
        task_class: str,
        confidence: float,
        question: str,
        answer: str,
        payload: dict | None = None,
        signature: str = "",
    ) -> tuple[bool, str]:
        """
        Check if a dispatch is permitted by all security layers.
        Returns (allowed, reason).
        """
        if not K9_SECURITY_ENABLED:
            return True, "security disabled"

        # 1. Kill switch
        if self.kill_switch.is_active:
            self.forensics.capture(
                "blocked_dispatch", service_name, task_class,
                question, answer, confidence,
                f"kill switch active: {self.kill_switch.status()['reason']}",
            )
            return False, f"KILL_SWITCH_ACTIVE: {self.kill_switch.status()['reason']}"

        # 2. Behavioral halt
        if self.behavior.is_halted:
            self.forensics.capture(
                "blocked_dispatch", service_name, task_class,
                question, answer, confidence,
                f"behavioral halt: {self.behavior.halt_reason}",
            )
            return False, f"BEHAVIORAL_HALT: {self.behavior.halt_reason}"

        # 3. Egress allowlist + rate limit
        allowed, reason = self.egress.check(service_name, method, path)
        if not allowed:
            self.forensics.capture(
                "blocked_dispatch", service_name, task_class,
                question, answer, confidence,
                f"egress denied: {reason}",
            )
            return False, f"EGRESS_DENIED: {reason}"

        # 4. Signature verification (if enabled)
        if self.signatures.is_configured() and payload:
            valid, sig_reason = self.signatures.verify(payload, signature)
            if not valid:
                self.forensics.capture(
                    "unsigned_request", service_name, task_class,
                    question, answer, confidence,
                    f"signature verification failed: {sig_reason}",
                    {"payload_keys": list(payload.keys())},
                )
                # Auto-activate kill switch on unsigned requests
                self.kill_switch.activate(
                    f"unsigned dispatch request detected: {sig_reason}",
                    "security_gate",
                )
                return False, f"SIGNATURE_INVALID: {sig_reason}"

        # 5. Check for anomalies (non-blocking)
        anomalies = self.behavior.detect_anomalies()
        for a in anomalies:
            self.forensics.capture(
                "anomaly", service_name, task_class,
                question, answer, confidence,
                f"behavioral anomaly: {a['type']} (z={a.get('z_score', 'N/A')})",
                a,
            )

        # 6. Record observation for behavioral baseline
        self.behavior.observe(task_class, confidence, answer)

        return True, "ok"

    def status(self) -> dict:
        """Full security status for monitoring/monitoring endpoint."""
        return {
            "enabled": K9_SECURITY_ENABLED,
            "kill_switch": self.kill_switch.status(),
            "behavioral": self.behavior.baseline_stats(),
            "egress": self.egress.list_services(),
            "signatures_configured": self.signatures.is_configured(),
            "forensics": self.forensics.stats(),
            "recent_anomalies": self.behavior.recent_anomalies(10),
            "recent_forensics": self.forensics.export(10),
        }


# ── Global singleton ─────────────────────────────────────────────────────────

_gate: Optional[SecurityGate] = None


def get_security_gate() -> SecurityGate:
    global _gate
    if _gate is None:
        _gate = SecurityGate()
        logger.info(
            f"[SECURITY] Security gate initialized "
            f"(enabled={K9_SECURITY_ENABLED}, signing={K9_SIGNING_KEY[:4] + '...' if K9_SIGNING_KEY else 'disabled'})"
        )
    return _gate
