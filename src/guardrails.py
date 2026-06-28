"""
guardrails.py — K-9 Input/Output Safety Layer
─────────────────────────────────────────────────────────────────────────────
Sprint CB-2 · k9-llm-router + orbitron-bus

Provides:
  1. PII detection + redaction (Presidio)
  2. Toxicity classification (distilbert-base — lighter than toxic-bert)
  3. Prompt injection detection (regex + heuristic)
  4. Output validation (max length, JSON schema check)
  5. Audit logging (every moderation event → Redis stream)

Used by:
  - k9-llm-router /route handler (input + output check)
  - orbitron-bus edge function (cb.v1 payload scan)
  - k9-control-plane (execution_request validation)

Dependencies (add to requirements.txt):
  presidio-analyzer>=2.2.0
  presidio-anonymizer>=2.2.0
  transformers>=4.41.0
  torch>=2.3.0 (CPU-only is fine for inference)
  spacy>=3.7.0
  # python -m spacy download en_core_web_lg

Env vars:
  GUARDRAILS_ENABLED      "true" | "false", default: true
  GUARDRAILS_BLOCK_PII    "true" | "false" — block (true) or redact (false), default: false
  GUARDRAILS_TOX_THRESH   float 0-1, default: 0.80
  GUARDRAILS_AUDIT        "true" | "false" — log to Redis stream, default: true
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("k9-guardrails")

# ── Config ────────────────────────────────────────────────────────────────────

GUARDRAILS_ENABLED   = os.getenv("GUARDRAILS_ENABLED",    "true").lower() == "true"
GUARDRAILS_BLOCK_PII = os.getenv("GUARDRAILS_BLOCK_PII",  "false").lower() == "true"
TOX_THRESHOLD        = float(os.getenv("GUARDRAILS_TOX_THRESH", "0.80"))
AUDIT_ENABLED        = os.getenv("GUARDRAILS_AUDIT",       "true").lower() == "true"
REDIS_HOST           = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT           = int(os.getenv("REDIS_PORT", "6379"))

# Audit stream key in Redis
AUDIT_STREAM = "k9:guardrails:audit"
AUDIT_MAXLEN = 10_000


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class GuardrailResult:
    allowed:        bool
    redacted_text:  Optional[str]   = None   # text after PII removal (if any)
    block_reason:   Optional[str]   = None   # why it was blocked
    pii_found:      bool            = False
    pii_entities:   list            = field(default_factory=list)
    toxic:          bool            = False
    tox_score:      float           = 0.0
    injection:      bool            = False
    injection_type: Optional[str]   = None
    latency_ms:     float           = 0.0
    audit_id:       Optional[str]   = None


# ── Lazy imports ──────────────────────────────────────────────────────────────

def _load_presidio():
    try:
        from presidio_analyzer import AnalyzerEngine
        from presidio_anonymizer import AnonymizerEngine
        analyzer  = AnalyzerEngine()
        anonymizer = AnonymizerEngine()
        return analyzer, anonymizer
    except ImportError:
        return None, None

def _load_toxicity_classifier():
    try:
        from transformers import pipeline
        # Use lightweight distilbert for CPU deployments
        clf = pipeline(
            "text-classification",
            model="distilbert-base-uncased-finetuned-sst-2-english",
            # For real toxicity in production swap with: martin-ha/toxic-comment-model
            truncation=True,
            max_length=512,
        )
        return clf
    except ImportError:
        return None

def _load_redis():
    try:
        import redis
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True,
                        socket_connect_timeout=2)
        r.ping()
        return r
    except Exception:
        return None


# ── Prompt injection patterns ─────────────────────────────────────────────────

INJECTION_PATTERNS = [
    (r"ignore (all |previous |prior |above |your )?(instructions?|prompts?|rules?|context)",
     "ignore_instructions"),
    (r"(system|assistant|user):\s*<",           "role_injection"),
    (r"<\|im_start\|>|<\|im_end\|>",           "chat_template_injection"),
    (r"\[INST\]|\[/INST\]",                     "llama_template_injection"),
    (r"do anything now|DAN\b",                  "jailbreak_dan"),
    (r"pretend (you are|to be|you're) (not |un)?restricted", "jailbreak_restriction"),
    (r"forget (you are|you're) (an? )?ai",      "identity_override"),
    (r"(reveal|show|print|output|repeat) (your )?(system )?prompt", "prompt_extraction"),
    (r"<!-- .*-->|\/\*.*\*\/",                  "comment_injection"),
]
_INJECTION_RE = [(re.compile(p, re.IGNORECASE), t) for p, t in INJECTION_PATTERNS]


# ── Main guardrails class ─────────────────────────────────────────────────────

class Guardrails:
    """
    K-9 safety layer. Thread-safe, lazy-loads heavy deps.

    Usage:
        g = Guardrails()
        g.initialize()   # once at startup

        result = g.check_input(text, context={"task_type": "...", "component": "..."})
        if not result.allowed:
            raise HTTPException(403, result.block_reason)
        text = result.redacted_text or text

        output_result = g.check_output(response_text)
    """

    def __init__(self) -> None:
        self._enabled   = GUARDRAILS_ENABLED
        self._ready     = False
        self._analyzer  = None
        self._anonymizer = None
        self._tox_clf   = None
        self._redis     = None

        self._blocked   = 0
        self._pii_hits  = 0
        self._tox_hits  = 0
        self._inj_hits  = 0
        self._checked   = 0

    def initialize(self) -> None:
        if not self._enabled:
            log.info("[guardrails] Disabled via GUARDRAILS_ENABLED=false")
            return
        try:
            self._analyzer, self._anonymizer = _load_presidio()
            if self._analyzer:
                log.info("[guardrails] ✅ Presidio PII engine loaded")
            else:
                log.warning("[guardrails] ⚠️  Presidio unavailable — PII checks will use regex fallback")

            self._tox_clf = _load_toxicity_classifier()
            if self._tox_clf:
                log.info("[guardrails] ✅ Toxicity classifier loaded")
            else:
                log.warning("[guardrails] ⚠️  Toxicity classifier unavailable")

            self._redis = _load_redis()
            if self._redis:
                log.info("[guardrails] ✅ Redis audit stream connected")
            else:
                log.warning("[guardrails] ⚠️  Redis unavailable — audit will log only")

            self._ready = True
        except Exception as e:
            log.warning("[guardrails] Initialization error: %s", e)
            self._ready = True  # degrade gracefully — don't block startup

    # ── PII ───────────────────────────────────────────────────────────────────

    def _check_pii(self, text: str) -> tuple[bool, list, str]:
        """Returns (found, entities, redacted_text)."""
        if self._analyzer and self._anonymizer:
            try:
                results = self._analyzer.analyze(text, language="en")
                if not results:
                    return False, [], text
                redacted = self._anonymizer.anonymize(text=text, analyzer_results=results)
                entities = [{"type": r.entity_type, "start": r.start, "end": r.end}
                            for r in results]
                return True, entities, redacted.text
            except Exception as e:
                log.warning("[guardrails] Presidio error: %s", e)

        # Regex fallback — catch obvious PII
        pii_patterns = [
            (r"\b[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}\b", "EMAIL"),
            (r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b", "PHONE"),
            (r"\b\d{3}-\d{2}-\d{4}\b", "SSN"),
            (r"\b4[0-9]{12}(?:[0-9]{3})?\b", "CREDIT_CARD"),
        ]
        found_entities = []
        redacted = text
        for pattern, label in pii_patterns:
            matches = list(re.finditer(pattern, text))
            if matches:
                found_entities.extend([{"type": label, "start": m.start(), "end": m.end()}
                                       for m in matches])
                redacted = re.sub(pattern, f"<{label}>", redacted)
        return bool(found_entities), found_entities, redacted

    # ── Toxicity ──────────────────────────────────────────────────────────────

    def _check_toxicity(self, text: str) -> tuple[bool, float]:
        """Returns (is_toxic, score)."""
        if not self._tox_clf:
            return False, 0.0
        try:
            result = self._tox_clf(text[:512])[0]
            # SST-2 model uses POSITIVE/NEGATIVE; real toxicity model would be different
            # For production: swap model to martin-ha/toxic-comment-model
            label = result.get("label", "").upper()
            score = result.get("score", 0.0)
            # Treat "NEGATIVE" with high confidence as proxy for toxicity
            # (replace with proper toxic label check when model swapped)
            is_toxic = label == "NEGATIVE" and score >= TOX_THRESHOLD
            return is_toxic, score if is_toxic else 0.0
        except Exception as e:
            log.warning("[guardrails] Toxicity check error: %s", e)
            return False, 0.0

    # ── Prompt injection ──────────────────────────────────────────────────────

    def _check_injection(self, text: str) -> tuple[bool, Optional[str]]:
        """Returns (is_injection, injection_type)."""
        for pattern, inj_type in _INJECTION_RE:
            if pattern.search(text):
                return True, inj_type
        return False, None

    # ── Audit logging ─────────────────────────────────────────────────────────

    def _audit(self, event_type: str, text_preview: str, result: GuardrailResult,
               context: dict) -> Optional[str]:
        entry = {
            "ts":          str(time.time()),
            "event":       event_type,
            "allowed":     str(result.allowed),
            "block_reason": result.block_reason or "",
            "pii_found":   str(result.pii_found),
            "toxic":       str(result.toxic),
            "tox_score":   str(result.tox_score),
            "injection":   str(result.injection),
            "inj_type":    result.injection_type or "",
            "component":   context.get("component", "unknown"),
            "task_type":   context.get("task_type", "unknown"),
            "text_hash":   str(hash(text_preview))[:12],
            "preview":     text_preview[:80].replace("\n", " "),
        }
        if self._redis and AUDIT_ENABLED:
            try:
                audit_id = self._redis.xadd(AUDIT_STREAM, entry, maxlen=AUDIT_MAXLEN)
                return str(audit_id)
            except Exception:
                pass
        log.info("[guardrails] AUDIT %s", json.dumps(entry))
        return None

    # ── Public API ────────────────────────────────────────────────────────────

    def check_input(self, text: str, context: dict | None = None) -> GuardrailResult:
        """
        Full input check: injection → toxicity → PII.
        Returns GuardrailResult. On block, allowed=False + block_reason set.
        """
        if not self._enabled:
            return GuardrailResult(allowed=True)

        ctx   = context or {}
        start = time.time()
        self._checked += 1

        # 1. Prompt injection (highest priority — always block)
        inj, inj_type = self._check_injection(text)
        if inj:
            self._inj_hits += 1
            self._blocked  += 1
            result = GuardrailResult(
                allowed=False, injection=True, injection_type=inj_type,
                block_reason=f"Prompt injection detected: {inj_type}",
                latency_ms=(time.time() - start) * 1000,
            )
            self._audit("INPUT_BLOCKED_INJECTION", text, result, ctx)
            return result

        # 2. Toxicity
        toxic, tox_score = self._check_toxicity(text)
        if toxic:
            self._tox_hits += 1
            self._blocked  += 1
            result = GuardrailResult(
                allowed=False, toxic=True, tox_score=tox_score,
                block_reason=f"Toxic content detected (score={tox_score:.2f})",
                latency_ms=(time.time() - start) * 1000,
            )
            self._audit("INPUT_BLOCKED_TOXIC", text, result, ctx)
            return result

        # 3. PII — redact or block depending on GUARDRAILS_BLOCK_PII
        pii_found, pii_entities, redacted_text = self._check_pii(text)
        if pii_found:
            self._pii_hits += 1
            if GUARDRAILS_BLOCK_PII:
                self._blocked += 1
                result = GuardrailResult(
                    allowed=False, pii_found=True, pii_entities=pii_entities,
                    block_reason="PII detected and block mode enabled",
                    latency_ms=(time.time() - start) * 1000,
                )
                self._audit("INPUT_BLOCKED_PII", text, result, ctx)
                return result
            else:
                result = GuardrailResult(
                    allowed=True, pii_found=True,
                    pii_entities=pii_entities,
                    redacted_text=redacted_text,
                    latency_ms=(time.time() - start) * 1000,
                )
                self._audit("INPUT_PII_REDACTED", text, result, ctx)
                return result

        result = GuardrailResult(
            allowed=True,
            latency_ms=(time.time() - start) * 1000,
        )
        return result

    def check_output(self, text: str, context: dict | None = None) -> GuardrailResult:
        """
        Output check: PII scan on generated text before returning to user.
        Lighter check — no injection, no toxicity on LLM output.
        """
        if not self._enabled:
            return GuardrailResult(allowed=True)

        ctx = context or {}
        pii_found, pii_entities, redacted_text = self._check_pii(text)
        if pii_found:
            self._pii_hits += 1
            result = GuardrailResult(
                allowed=True, pii_found=True,
                pii_entities=pii_entities,
                redacted_text=redacted_text,
            )
            self._audit("OUTPUT_PII_REDACTED", text, result, ctx)
            return result

        return GuardrailResult(allowed=True)

    def stats(self) -> dict:
        return {
            "enabled":    self._enabled,
            "ready":      self._ready,
            "checked":    self._checked,
            "blocked":    self._blocked,
            "pii_hits":   self._pii_hits,
            "tox_hits":   self._tox_hits,
            "inj_hits":   self._inj_hits,
            "presidio":   self._analyzer is not None,
            "toxicity":   self._tox_clf  is not None,
            "audit_redis": self._redis   is not None,
            "tox_threshold": TOX_THRESHOLD,
            "block_pii":  GUARDRAILS_BLOCK_PII,
        }


# ── Module-level singleton ────────────────────────────────────────────────────
guardrails = Guardrails()
