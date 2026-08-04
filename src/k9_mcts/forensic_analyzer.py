"""
k9-mcts — forensic_analyzer.py

LLM-powered forensic analysis of captured suspicious activity.
Uses a LOCAL Ollama model (GLM-5, Mistral, etc.) to analyze attack patterns.

Why local? The Hugging Face incident proved commercial models (Claude, GPT)
REFUSE to analyze attack payloads due to safety filters. The HF team had
to pivot to open-source GLM-5.2 for forensic analysis. We keep a local
Ollama instance ready for exactly this scenario.

Architecture:
  1. ForensicCapture ring buffer → export records
  2. ForensicAnalyzer formats records into a forensic prompt
  3. Local Ollama model analyzes patterns, classifies attack type
  4. Returns structured threat assessment
  5. Optional: auto-activate kill switch on confirmed attack patterns

Analysis output:
  - attack_classification: none | prompt_injection | lateral_movement |
    data_exfiltration | credential_theft | sandbox_escape | rate_flood |
    suspicious_but_inconclusive
  - confidence: 0.0–1.0
  - recommended_action: monitor | investigate | halt | kill_switch
  - pattern_summary: human-readable analysis
  - indicators: list of specific suspicious indicators found
  - mitre_mapping: optional MITRE ATT&CK technique IDs

Endpoints (added to api.py):
  POST /security/forensics/analyze  — run analysis on captured records
  GET  /security/forensics/analysis — get last analysis result
  POST /security/forensics/auto-analyze — toggle auto-analysis mode
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import httpx

logger = logging.getLogger("k9.forensics")

# ── Configuration ──────────────────────────────────────────────────────────────

OLLAMA_URL = os.getenv("LOCAL_MODEL_URL", "http://localhost:11434")
FORENSIC_MODEL = os.getenv("K9_FORENSIC_MODEL", "mistral")  # GLM-5 or mistral
FORENSIC_TIMEOUT = int(os.getenv("K9_FORENSIC_TIMEOUT", "60"))
FORENSIC_MAX_RECORDS = int(os.getenv("K9_FORENSIC_MAX_RECORDS", "50"))
AUTO_ANALYZE = os.getenv("K9_FORENSIC_AUTO", "false").lower() == "true"
AUTO_ANALYZE_INTERVAL = int(os.getenv("K9_FORENSIC_INTERVAL", "300"))  # 5 min
ANALYSIS_TRIGGER_THRESHOLD = int(os.getenv("K9_FORENSIC_TRIGGER", "3"))  # min records


class AttackClassification(str, Enum):
    NONE = "none"
    PROMPT_INJECTION = "prompt_injection"
    LATERAL_MOVEMENT = "lateral_movement"
    DATA_EXFILTRATION = "data_exfiltration"
    CREDENTIAL_THEFT = "credential_theft"
    SANDBOX_ESCAPE = "sandbox_escape"
    RATE_FLOOD = "rate_flood"
    SUSPICIOUS_INCONCLUSIVE = "suspicious_but_inconclusive"


class RecommendedAction(str, Enum):
    MONITOR = "monitor"
    INVESTIGATE = "investigate"
    HALT = "halt"
    KILL_SWITCH = "kill_switch"


# ── Data Structures ────────────────────────────────────────────────────────────

@dataclass
class ThreatAssessment:
    """Structured output from LLM forensic analysis."""
    timestamp: float
    records_analyzed: int
    attack_classification: str
    confidence: float
    recommended_action: str
    pattern_summary: str
    indicators: list[str] = field(default_factory=list)
    mitre_mapping: list[str] = field(default_factory=list)
    model_used: str = ""
    analysis_latency_ms: float = 0.0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "records_analyzed": self.records_analyzed,
            "attack_classification": self.attack_classification,
            "confidence": self.confidence,
            "recommended_action": self.recommended_action,
            "pattern_summary": self.pattern_summary,
            "indicators": self.indicators,
            "mitre_mapping": self.mitre_mapping,
            "model_used": self.model_used,
            "analysis_latency_ms": round(self.analysis_latency_ms, 0),
            "error": self.error,
        }


# ── Forensic Analyzer ──────────────────────────────────────────────────────────

class ForensicAnalyzer:
    """
    Analyzes captured forensic records using a local LLM.
    Designed to work when commercial models refuse to process attack data.
    """

    def __init__(self):
        self._ollama_url = OLLAMA_URL
        self._model = FORENSIC_MODEL
        self._last_analysis: Optional[ThreatAssessment] = None
        self._analysis_count = 0
        self._auto_mode = AUTO_ANALYZE
        self._auto_task: Optional[asyncio.Task] = None

    async def analyze(self, records: list[dict]) -> ThreatAssessment:
        """
        Run LLM forensic analysis on captured records.
        Returns structured threat assessment.
        """
        start = time.monotonic()

        if not records:
            return ThreatAssessment(
                timestamp=time.time(),
                records_analyzed=0,
                attack_classification=AttackClassification.NONE.value,
                confidence=1.0,
                recommended_action=RecommendedAction.MONITOR.value,
                pattern_summary="No forensic records to analyze — system clean.",
                model_used=self._model,
            )

        # Build the forensic analysis prompt
        prompt = self._build_prompt(records)

        try:
            # Call local Ollama (OpenAI-compatible endpoint)
            async with httpx.AsyncClient(timeout=FORENSIC_TIMEOUT) as client:
                resp = await client.post(
                    f"{self._ollama_url}/v1/chat/completions",
                    json={
                        "model": self._model,
                        "messages": [
                            {
                                "role": "system",
                                "content": self._system_prompt(),
                            },
                            {
                                "role": "user",
                                "content": prompt,
                            },
                        ],
                        "temperature": 0.1,  # low temp for analytical accuracy
                        "max_tokens": 2000,
                        "stream": False,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]

            # Parse LLM response into structured assessment
            assessment = self._parse_response(content, len(records))
            assessment.analysis_latency_ms = (time.monotonic() - start) * 1000
            assessment.model_used = self._model

            self._last_analysis = assessment
            self._analysis_count += 1

            logger.info(
                f"[FORENSIC] analysis complete: "
                f"classification={assessment.attack_classification} "
                f"confidence={assessment.confidence:.2f} "
                f"action={assessment.recommended_action} "
                f"latency={assessment.analysis_latency_ms:.0f}ms"
            )

            return assessment

        except httpx.ConnectError:
            logger.warning(f"[FORENSIC] Ollama not reachable at {self._ollama_url}")
            return ThreatAssessment(
                timestamp=time.time(),
                records_analyzed=len(records),
                attack_classification=AttackClassification.SUSPICIOUS_INCONCLUSIVE.value,
                confidence=0.0,
                recommended_action=RecommendedAction.INVESTIGATE.value,
                pattern_summary=f"Ollama not reachable at {self._ollama_url}. Cannot perform LLM analysis. Manual review required.",
                indicators=["llm_analysis_unavailable"],
                error=f"connection_failed: {self._ollama_url}",
                analysis_latency_ms=(time.monotonic() - start) * 1000,
            )
        except Exception as e:
            logger.error(f"[FORENSIC] analysis failed: {e}")
            return ThreatAssessment(
                timestamp=time.time(),
                records_analyzed=len(records),
                attack_classification=AttackClassification.SUSPICIOUS_INCONCLUSIVE.value,
                confidence=0.0,
                recommended_action=RecommendedAction.INVESTIGATE.value,
                pattern_summary=f"Analysis failed: {str(e)[:200]}",
                indicators=["analysis_error"],
                error=str(e)[:500],
                analysis_latency_ms=(time.monotonic() - start) * 1000,
            )

    def _system_prompt(self) -> str:
        """System prompt for the forensic analysis LLM."""
        return """You are a cybersecurity forensic analyst AI. You analyze captured security events from an autonomous AI agent system and classify attack patterns.

You MUST respond in strict JSON format with these fields:
{
  "attack_classification": "none|prompt_injection|lateral_movement|data_exfiltration|credential_theft|sandbox_escape|rate_flood|suspicious_but_inconclusive",
  "confidence": 0.0-1.0,
  "recommended_action": "monitor|investigate|halt|kill_switch",
  "pattern_summary": "human-readable 2-3 sentence analysis",
  "indicators": ["list of specific suspicious indicators found"],
  "mitre_mapping": ["MITRE ATT&CK technique IDs if applicable, empty if none"]
}

Classification guide:
- none: Normal operational noise, no attack pattern
- prompt_injection: Attempts to manipulate the agent via crafted inputs
- lateral_movement: Agent trying to access services outside its scope
- data_exfiltration: Unusual data volume in responses, accessing sensitive data
- credential_theft: Attempts to read/steal credentials or secrets
- sandbox_escape: Attempts to bypass egress controls or reach unauthorized URLs
- rate_flood: Abnormal dispatch volume suggesting automated attack
- suspicious_but_inconclusive: Something wrong but can't classify definitively

Action guide:
- monitor: No threat, continue normal operations
- investigate: Suspicious activity, human should review
- halt: Stop auto-execution until reviewed
- kill_switch: Activate global kill switch immediately

Respond with ONLY the JSON object. No markdown, no explanation outside JSON."""

    def _build_prompt(self, records: list[dict]) -> str:
        """Format forensic records into an analysis prompt."""
        # Summarize records for the LLM
        formatted = []
        for i, r in enumerate(records[-FORENSIC_MAX_RECORDS:], 1):
            formatted.append(
                f"Record {i}:\n"
                f"  type: {r.get('event_type', 'unknown')}\n"
                f"  service: {r.get('service', 'unknown')}\n"
                f"  task_class: {r.get('task_class', 'unknown')}\n"
                f"  confidence: {r.get('confidence', 0)}\n"
                f"  reason: {r.get('reason', '')}\n"
                f"  question: {r.get('question', '')[:200]}\n"
                f"  answer: {r.get('answer', '')[:200]}\n"
                f"  context: {json.dumps(r.get('context', {}))[:300]}"
            )

        return f"""Analyze these {len(records)} security events captured from an autonomous AI agent system. This system has:
- An execution dispatcher that routes auto-executed decisions to downstream services
- An egress allowlist limiting which services can be called
- Behavioral anomaly detection with EMA baselines
- A kill switch for emergency halt
- HMAC-SHA256 signature verification on dispatches

Security events captured:
{chr(10).join(formatted)}

Classify the attack pattern (if any), assess confidence, and recommend an action. Respond in JSON only."""

    def _parse_response(self, content: str, record_count: int) -> ThreatAssessment:
        """Parse LLM JSON response into ThreatAssessment."""
        # Clean up the response — LLMs sometimes wrap in markdown
        content = content.strip()
        if content.startswith("```"):
            # Remove markdown code fences
            lines = content.split("\n")
            content = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

        try:
            data = json.loads(content)
            return ThreatAssessment(
                timestamp=time.time(),
                records_analyzed=record_count,
                attack_classification=data.get("attack_classification", AttackClassification.SUSPICIOUS_INCONCLUSIVE.value),
                confidence=float(data.get("confidence", 0.5)),
                recommended_action=data.get("recommended_action", RecommendedAction.INVESTIGATE.value),
                pattern_summary=data.get("pattern_summary", "No summary provided."),
                indicators=data.get("indicators", []),
                mitre_mapping=data.get("mitre_mapping", []),
            )
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"[FORENSIC] failed to parse LLM response: {e}")
            # Fallback: extract what we can
            return ThreatAssessment(
                timestamp=time.time(),
                records_analyzed=record_count,
                attack_classification=AttackClassification.SUSPICIOUS_INCONCLUSIVE.value,
                confidence=0.3,
                recommended_action=RecommendedAction.INVESTIGATE.value,
                pattern_summary=f"LLM response could not be parsed. Raw output: {content[:500]}",
                indicators=["parse_failure"],
                error=f"json_parse_error: {e}",
            )

    # ── Auto-analysis mode ─────────────────────────────────────────────────────

    async def start_auto_analysis(self, get_records_fn, on_assessment_fn=None):
        """Start background auto-analysis loop."""
        if self._auto_task and not self._auto_task.done():
            logger.info("[FORENSIC] auto-analysis already running")
            return

        self._auto_mode = True
        self._auto_task = asyncio.create_task(
            self._auto_loop(get_records_fn, on_assessment_fn)
        )
        logger.info(f"[FORENSIC] auto-analysis started (interval={AUTO_ANALYZE_INTERVAL}s)")

    async def stop_auto_analysis(self):
        """Stop background auto-analysis."""
        self._auto_mode = False
        if self._auto_task and not self._auto_task.done():
            self._auto_task.cancel()
            try:
                await self._auto_task
            except asyncio.CancelledError:
                pass
        logger.info("[FORENSIC] auto-analysis stopped")

    async def _auto_loop(self, get_records_fn, on_assessment_fn):
        """Background loop: analyze forensic records periodically."""
        while self._auto_mode:
            try:
                records = get_records_fn()
                if records and len(records) >= ANALYSIS_TRIGGER_THRESHOLD:
                    assessment = await self.analyze(records)
                    if on_assessment_fn:
                        await on_assessment_fn(assessment)
                    # Auto-activate kill switch on high-confidence attack
                    if (
                        assessment.confidence > 0.8
                        and assessment.recommended_action == RecommendedAction.KILL_SWITCH.value
                    ):
                        logger.critical(
                            f"[FORENSIC] AUTO KILL SWITCH: "
                            f"{assessment.attack_classification} "
                            f"(confidence={assessment.confidence:.2f})"
                        )
                        from .security_reinforcement import get_security_gate
                        get_security_gate().kill_switch.activate(
                            f"forensic LLM auto-detection: {assessment.attack_classification}",
                            "forensic_analyzer",
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[FORENSIC] auto-analysis error: {e}")

            await asyncio.sleep(AUTO_ANALYZE_INTERVAL)

    # ── Status ──────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "model": self._model,
            "ollama_url": self._ollama_url,
            "auto_mode": self._auto_mode,
            "analysis_count": self._analysis_count,
            "last_analysis": self._last_analysis.to_dict() if self._last_analysis else None,
            "trigger_threshold": ANALYSIS_TRIGGER_THRESHOLD,
            "auto_interval": AUTO_ANALYZE_INTERVAL,
        }


# ── Global singleton ─────────────────────────────────────────────────────────

_analyzer: Optional[ForensicAnalyzer] = None


def get_forensic_analyzer() -> ForensicAnalyzer:
    global _analyzer
    if _analyzer is None:
        _analyzer = ForensicAnalyzer()
    return _analyzer
