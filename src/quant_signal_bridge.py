"""
quant_signal_bridge.py — K-9 Quantitative Signal Bridge
─────────────────────────────────────────────────────────────────────────────
Sprint CB-4 · k9-llm-router/src/

Packages QuantSignal output from JpyRepatriationEngine into CB v1 envelopes
and routes them through the existing aeg_signal_router → Orbitron pipeline.

Integration points:
  - JpyRepatriationEngine.analyze() → QuantSignal
  - build_cb1_envelope()            → CB v1 dict (matches K9-CB schema)
  - route_quant_signal()            → POST to aeg_signal_router :9004
  - enrich_llm_context()            → inject live signal into quant_analysis LLM calls

Wired into main.py: POST /quant/analyze + automated daily n8n trigger.

# LEVEL-1 ADVISORY: Signal routing only. No wallet signing here.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from .jpy_repatriation_model import (
    JpyRepatriationEngine,
    QuantSignal,
    MarketSnapshot,
    fetch_market_snapshot,
)

log = logging.getLogger("k9.quant-bridge")

AEG_SIGNAL_ROUTER_URL = os.getenv("AEG_SIGNAL_ROUTER_URL", "http://localhost:9004")
ORBITRON_URL          = os.getenv("ORBITRON_URL", "https://ziqenqqgnqxqrazmjohs.supabase.co")
ORBITRON_ANON_KEY     = os.getenv("ORBITRON_ANON_KEY", "")
K9_INTEGRATION_KEY    = os.getenv("K9_INTEGRATION_KEY", "")

# Singleton engine — calibrated once on module import (lazy)
_engine: JpyRepatriationEngine | None = None


def get_engine() -> JpyRepatriationEngine:
    global _engine
    if _engine is None:
        _engine = JpyRepatriationEngine()
        _engine.calibrate()
    return _engine


# ── CB v1 ENVELOPE BUILDER ────────────────────────────────────────────────────

def build_cb1_envelope(sig: QuantSignal, snap: MarketSnapshot) -> dict[str, Any]:
    """
    Package QuantSignal as a K9-CB v1 envelope.

    Schema matches cb-schema.ts / orbitron-bus validation:
      timestamp, source, type, ontology_tags, confidence,
      payload, trace_id, causation_id, signature
    """
    trace_id    = str(uuid.uuid4())
    payload_str = str(sig.signal_class) + str(sig.repatriation_confidence) + sig.timestamp

    return {
        "timestamp":      sig.timestamp,
        "source":         "k9-quant-engine",
        "type":           "quant_signal",
        "ontology_tags":  ["jpy_repatriation", "fx_macro", "regime_detection", "mof_flows"],
        "confidence":     round(sig.repatriation_confidence, 4),
        "payload": {
            # Core signal
            "signal_class":          sig.signal_class,
            "directional_bias":      sig.directional_bias,
            "conviction":            sig.conviction,
            "repatriation_confidence": round(sig.repatriation_confidence, 4),
            "narrative_data_divergence": sig.narrative_vs_data_divergence,

            # Model A
            "trigger_probability":   round(sig.trigger_probability, 4),
            "trigger_fired":         sig.trigger_fired,

            # Model B
            "flow_estimate_jpy_tn":  round(sig.flow_estimate_jpy_tn, 3),
            "flow_95ci_lower":       round(sig.flow_95ci_lower, 3),
            "flow_95ci_upper":       round(sig.flow_95ci_upper, 3),

            # Model C
            "repatriation_pressure":      round(sig.repatriation_pressure, 4),
            "gpif_optimal_domestic_share": round(sig.gpif_optimal_domestic_share, 4),
            "gpif_current_domestic_share": round(sig.gpif_current_domestic_share, 4),

            # Model D
            "regime":                sig.regime,
            "regime_probabilities":  {k: round(v, 4) for k, v in sig.regime_probability.items()},

            # Market context
            "carry_incentive_bps":   round(sig.carry_incentive_bps, 1),
            "usdjpy":                round(snap.usdjpy, 2),
            "yield_spread_bps":      round(snap.yield_spread_bps, 1),
            "vix":                   round(snap.vix, 2),

            # Human-readable notes
            "notes":                 sig.notes,
        },
        "trace_id":    trace_id,
        "causation_id": None,
        "signature":   hashlib.sha256(payload_str.encode()).hexdigest()[:16],
    }


# ── SIGNAL ROUTER ─────────────────────────────────────────────────────────────

async def route_quant_signal(envelope: dict[str, Any]) -> dict[str, Any]:
    """
    POST CB v1 envelope to aeg_signal_router :9004.
    Falls back to direct Orbitron /signal-aggregator if router unavailable.
    """
    confidence = envelope.get("confidence", 0.0)

    # Gate: CB-4 alignment — below 0.40 confidence goes nowhere
    if confidence < 0.40:
        log.info("quant_signal gated (confidence=%.2f < 0.40)", confidence)
        return {"status": "gated", "confidence": confidence}

    # Primary: aeg_signal_router
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post(
                f"{AEG_SIGNAL_ROUTER_URL}/route",
                json={
                    "source": envelope["source"],
                    "signal_type": "quant_signal",
                    "symbol":      "USDJPY",
                    "direction":   envelope["payload"]["directional_bias"],
                    "confidence":  confidence,
                    "metadata":    envelope,
                },
                headers={"Content-Type": "application/json"},
            )
            if r.status_code == 200:
                log.info("quant_signal routed via aeg_signal_router: confidence=%.2f", confidence)
                return r.json()
    except Exception as e:
        log.warning("aeg_signal_router unavailable (%s) — falling back to Orbitron direct", e)

    # Fallback: direct to Orbitron signal-aggregator
    try:
        headers = {
            "apikey": ORBITRON_ANON_KEY,
            "Content-Type": "application/json",
        }
        if K9_INTEGRATION_KEY:
            headers["x-integration-key"] = K9_INTEGRATION_KEY

        orbitron_payload = {
            "source":            "k9-quant-engine",
            "signal_type":       "quant_signal",
            "symbol":            "USDJPY",
            "direction":         envelope["payload"]["directional_bias"],
            "confidence_score":  confidence,
            "strategy_name":     "jpy_repatriation_model",
            "metadata":          envelope,
        }

        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post(
                f"{ORBITRON_URL}/functions/v1/signal-aggregator",
                json=orbitron_payload,
                headers=headers,
            )
            log.info("quant_signal → Orbitron direct: status=%d", r.status_code)
            return {"status": "orbitron_direct", "http_status": r.status_code}
    except Exception as e:
        log.error("quant_signal routing fully failed: %s", e)
        return {"status": "error", "error": str(e)}


# ── LLM CONTEXT ENRICHMENT ────────────────────────────────────────────────────

async def enrich_with_quant_context(
    task_type: str,
    messages: list[dict],
    system: str | None,
) -> tuple[list[dict], str | None]:
    """
    Inject live JPY repatriation signal into quant_analysis LLM context.
    Called by main.py route() before dispatching to DeepSeek V4.
    Complements existing fed_whisperer_bridge enrichment.
    """
    QUANT_TASK_TYPES = {"quant_analysis", "trading_signal", "financial_analysis", "finance_coach"}
    if task_type not in QUANT_TASK_TYPES:
        return messages, system

    try:
        engine = get_engine()
        snap   = fetch_market_snapshot(lookback_days=5)
        sig    = engine.analyze(snap)

        conviction_icon = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢", "NONE": "⚪"}.get(sig.conviction, "⚪")
        regime_icon     = {"carry_trade": "🔵", "repatriation": "🟠", "crisis": "🔴", "rebalancing": "🟢"}.get(sig.regime, "⚪")

        context_block = f"""
=== K-9 JPY REPATRIATION MODEL (LIVE) ===
Timestamp: {sig.timestamp}

Signal: {sig.signal_class} | Bias: {sig.directional_bias} {conviction_icon} [{sig.conviction}]
Regime: {regime_icon} {sig.regime.upper()} (P={sig.regime_probability.get(sig.regime, 0):.1%})
Confidence: {sig.repatriation_confidence:.1%}
Narrative/Data Divergence: {"⚠ YES — data contradicts repatriation narrative" if sig.narrative_vs_data_divergence else "✓ ALIGNED"}

Model A (Trigger):     P(repatriation) = {sig.trigger_probability:.1%} | Fired: {"YES" if sig.trigger_fired else "No"}
Model B (Flow):        Est. MoF flow = {sig.flow_estimate_jpy_tn:+.2f}¥T [95CI: {sig.flow_95ci_lower:+.2f} / {sig.flow_95ci_upper:+.2f}]
Model C (GPIF):        Repatriation pressure = {sig.repatriation_pressure:.1%} | Optimal domestic = {sig.gpif_optimal_domestic_share:.1%}
Model D (Regime):      carry_trade={sig.regime_probability.get('carry_trade',0):.1%} | repatriation={sig.regime_probability.get('repatriation',0):.1%} | crisis={sig.regime_probability.get('crisis',0):.1%}

Market: USDJPY={snap.usdjpy:.2f} | Spread={snap.yield_spread_bps:.0f}bps | VIX={snap.vix:.1f} | JGB10Y={snap.jgb_10y_yield:.2f}%

Notes:
{chr(10).join(f"  • {n}" for n in sig.notes) if sig.notes else "  • No flags"}
=== END QUANT CONTEXT ===
"""

        enriched_system = f"{system}\n\n{context_block}" if system else context_block
        log.info("quant_analysis enriched: %s [%s] confidence=%.2f",
                 sig.signal_class, sig.conviction, sig.repatriation_confidence)
        return messages, enriched_system

    except Exception as e:
        log.warning("quant_signal enrichment failed: %s — proceeding without context", e)
        return messages, system


# ── MAIN API HANDLER ──────────────────────────────────────────────────────────

async def run_full_analysis() -> dict[str, Any]:
    """
    Full pipeline: fetch → analyze → package → route.
    Called by POST /quant/analyze and n8n daily trigger.
    """
    t0 = time.time()
    engine = get_engine()
    snap   = fetch_market_snapshot()
    sig    = engine.analyze(snap)
    env    = build_cb1_envelope(sig, snap)
    route_result = await route_quant_signal(env)

    return {
        "signal":        engine.to_dict(sig),
        "cb1_envelope":  env,
        "route_result":  route_result,
        "elapsed_ms":    round((time.time() - t0) * 1000),
    }
