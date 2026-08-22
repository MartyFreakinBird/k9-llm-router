"""
k9_3commas_bridge.py — 3Commas Signal Bridge for K-9 Ecosystem
─────────────────────────────────────────────────────────────────────────────
Translates K-9 macro/micro/GEX signals into 3Commas webhook payloads
for Gate.io spot and perpetual trading bot.

Signal Flow:
  K-9 Signal Suite (microflow, FOMC, GEX, Polymarket, fiscal dominance)
    → 3Commas Bridge (this module)
      → SUPPRESS_SHORT check (microflow divergence)
        → 3Commas Webhook (Gate.io bot)
          → Trade execution

Safety Gates (in order):
  1. Microflow SUPPRESS_SHORT — blocks short entries for high-catalyst instruments
  2. Rate limit — max 1 signal per 5 min per instrument
  3. Signal TTL — max_lag 300s (signals older than 5 min are rejected)
  4. Action allowlist — only approved actions dispatched
  5. Confidence threshold — K-9 signals below 0.65 confidence are held

Port: 9013
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger("k9.3commas")

# ── Config ────────────────────────────────────────────────────────────────────

THREECOMMAS_SECRET = os.getenv("THREECOMMAS-SECRET-SECRET", "")
THREECOMMAS_BOT_UUID = os.getenv("THREECOMMAS-BOT-UUID", "ee41599e-95dd-4b6e-8374-d0968cbff2de")
THREECOMMAS_WEBHOOK_URL = os.getenv("THREECOMMAS-WEBHOOK-URL", "https://app.3commas.io/trade_signal/trading_view")

MICROFLOW_URL = os.getenv("MICROFLOW_URL", "http://localhost:9012")
FISCAL_DOMINANCE_URL = os.getenv("FISCAL_DOMINANCE_URL", "http://localhost:9010")
LLM_ROUTER_URL = os.getenv("LLM_ROUTER_URL", "http://localhost:8765")
GEX_ENGINE_URL = os.getenv("GEX_ENGINE_URL", "http://localhost:9008")
POLYMARKET_URL = os.getenv("POLYMARKET_URL", "http://localhost:9007")
AEG_SIGNAL_ROUTER = os.getenv("AEG_SIGNAL_ROUTER_URL", "http://localhost:9004")

MAX_LAG = int(os.getenv("THREECOMMAS-MAX-LAG", "300"))  # 5 minutes
RATE_LIMIT_SECONDS = int(os.getenv("THREECOMMAS-RATE-LIMIT", "300"))  # 5 min per instrument
CONFIDENCE_THRESHOLD = float(os.getenv("THREECOMMAS-CONFIDENCE-THRESHOLD", "0.65"))

# Supported actions
VALID_ACTIONS = {
    "enter_long", "exit_long", "enter_short", "exit_short",
    "close_position", "add_position", "cancel_enter_order",
    "enter_long_both", "enter_short_both",
    "tpp", "slp",  # take profit / stop loss percentage
}

# Instruments that 3Commas bot trades on Gate.io
# Maps K-9 instrument names to 3Commas/TradingView ticker format
INSTRUMENT_MAP = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "HBAR": "HBARUSDT",
    "SPX": "SPY",
    "QQQ": "QQQ",
    "GLD": "GLD",
    "TLT": "TLT",
    "KRE": "KRE",
}

# ── State ─────────────────────────────────────────────────────────────────────

_signal_log: deque = deque(maxlen=500)  # audit trail
_rate_limits: dict[str, float] = {}  # instrument -> last signal timestamp
_suppress_short_cache: list[str] = []  # instruments with SUPPRESS_SHORT active
_suppress_cache_ts: float = 0
_stats: dict[str, int] = defaultdict(int)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_timestamp() -> str:
    """3Commas expects a unix timestamp string."""
    return str(int(time.time()))


# ── Microflow Divergence Check ────────────────────────────────────────────────

def _refresh_suppress_cache() -> None:
    """Fetch current SUPPRESS_SHORT instruments from microflow engine."""
    global _suppress_short_cache, _suppress_cache_ts
    if (time.time() - _suppress_cache_ts) < 60:  # 1 min cache
        return
    try:
        with httpx.Client(timeout=10) as c:
            resp = c.get(f"{MICROFLOW_URL}/microflow/divergence")
            if resp.status_code == 200:
                data = resp.json()
                _suppress_short_cache = data.get("suppress_short_instruments", [])
                _suppress_cache_ts = time.time()
    except Exception as e:
        log.debug(f"Microflow divergence fetch: {e}")


def is_suppressed(instrument: str) -> bool:
    """Check if SUPPRESS_SHORT is active for this instrument."""
    _refresh_suppress_cache()
    return instrument.upper() in _suppress_short_cache


# ── Rate Limiting ─────────────────────────────────────────────────────────────

def _check_rate_limit(instrument: str) -> bool:
    """Returns True if signal is allowed (not rate-limited)."""
    now = time.time()
    last = _rate_limits.get(instrument, 0)
    if (now - last) < RATE_LIMIT_SECONDS:
        return False
    _rate_limits[instrument] = now
    return True


def _rate_limit_remaining(instrument: str) -> float:
    """Seconds until rate limit expires for this instrument."""
    now = time.time()
    last = _rate_limits.get(instrument, 0)
    remaining = RATE_LIMIT_SECONDS - (now - last)
    return max(0, remaining)


# ── 3Commas Webhook ────────────────────────────────────────────────────────────

def _build_3commas_payload(
    action: str,
    instrument: str,
    trigger_price: float | None = None,
    extra: dict | None = None,
) -> dict:
    """Build the 3Commas webhook payload matching the user's signal key format."""
    tv_instrument = INSTRUMENT_MAP.get(instrument.upper(), instrument.upper())
    payload = {
        "secret": THREECOMMAS_SECRET,
        "max_lag": str(MAX_LAG),
        "timestamp": _now_timestamp(),
        "trigger_price": str(trigger_price) if trigger_price else "{{close}}",
        "tv_exchange": "gateio",
        "tv_instrument": tv_instrument,
        "action": action,
        "bot_uuid": THREECOMMAS_BOT_UUID,
    }
    if extra:
        payload.update(extra)
    return payload


def _send_to_3commas(payload: dict) -> dict:
    """POST signal to 3Commas webhook endpoint."""
    if not THREECOMMAS_SECRET:
        return {"success": False, "error": "THREECOMMAS-SECRET-SECRET not set in environment"}
    try:
        with httpx.Client(timeout=15) as c:
            resp = c.post(THREECOMMAS_WEBHOOK_URL, json=payload)
            return {
                "success": resp.status_code == 200,
                "status_code": resp.status_code,
                "response": resp.text[:500],
            }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Signal Dispatch ───────────────────────────────────────────────────────────

def dispatch_signal(
    action: str,
    instrument: str,
    source: str = "k9-manual",
    confidence: float = 0.80,
    trigger_price: float | None = None,
    reason: str = "",
    metadata: dict | None = None,
) -> dict:
    """
    Dispatch a trading signal to 3Commas with all safety gates.

    Gates (checked in order):
    1. Action validation
    2. Confidence threshold
    3. SUPPRESS_SHORT (blocks short entries for high-catalyst instruments)
    4. Rate limiting (1 signal per 5 min per instrument)
    5. 3Commas webhook delivery
    """
    result: dict[str, Any] = {
        "signal_id": str(uuid.uuid4()),
        "timestamp": _now_iso(),
        "action": action,
        "instrument": instrument.upper(),
        "source": source,
        "confidence": confidence,
        "reason": reason,
        "gates": {},
    }

    # Gate 1: Action validation
    if action not in VALID_ACTIONS:
        result["gates"]["action_validation"] = "REJECTED"
        result["error"] = f"Invalid action '{action}'. Valid: {', '.join(sorted(VALID_ACTIONS))}"
        _stats["rejected_action"] += 1
        _signal_log.appendleft({**result, "status": "rejected"})
        return result
    result["gates"]["action_validation"] = "PASSED"

    # Gate 2: Confidence threshold
    if confidence < CONFIDENCE_THRESHOLD:
        result["gates"]["confidence"] = "HELD"
        result["error"] = f"Confidence {confidence:.2f} below threshold {CONFIDENCE_THRESHOLD}"
        _stats["held_confidence"] += 1
        _signal_log.appendleft({**result, "status": "held"})
        return result
    result["gates"]["confidence"] = "PASSED"

    # Gate 3: SUPPRESS_SHORT check
    is_short = action in ("enter_short", "enter_short_both")
    if is_short and is_suppressed(instrument):
        result["gates"]["suppress_short"] = "BLOCKED"
        result["error"] = f"SUPPRESS_SHORT active for {instrument} — micro catalysts dominating macro. Short signal blocked."
        _stats["blocked_suppress"] += 1
        _signal_log.appendleft({**result, "status": "blocked"})
        # Route a CB v1 notification
        _route_suppress_notification(instrument, action, source, reason)
        return result
    result["gates"]["suppress_short"] = "PASSED"

    # Gate 4: Rate limiting
    if not _check_rate_limit(instrument.upper()):
        remaining = _rate_limit_remaining(instrument.upper())
        result["gates"]["rate_limit"] = "THROTTLED"
        result["error"] = f"Rate limited for {instrument}. {remaining:.0f}s until next signal allowed."
        _stats["throttled"] += 1
        _signal_log.appendleft({**result, "status": "throttled"})
        return result
    result["gates"]["rate_limit"] = "PASSED"

    # Gate 5: Send to 3Commas
    payload = _build_3commas_payload(action, instrument, trigger_price, metadata)
    send_result = _send_to_3commas(payload)
    result["gates"]["delivery"] = "DELIVERED" if send_result.get("success") else "FAILED"
    result["webhook"] = send_result
    result["payload_sent"] = {k: v for k, v in payload.items() if k != "secret"}

    if send_result.get("success"):
        _stats["delivered"] += 1
        _signal_log.appendleft({**result, "status": "delivered"})
    else:
        _stats["delivery_failed"] += 1
        _signal_log.appendleft({**result, "status": "failed"})

    # Route a CB v1 envelope to the signal router
    _route_cb_envelope(result)

    return result


def _route_cb_envelope(signal_result: dict) -> bool:
    """Route a K9-CB v1 envelope for the signal to aeg-signal-router."""
    envelope = {
        "timestamp": _now_iso(),
        "source": "k9-3commas-bridge",
        "type": "trade_signal_dispatched",
        "ontology_tags": ["3commas", "gateio", signal_result["action"], signal_result["instrument"].lower()],
        "confidence": signal_result["confidence"],
        "payload": {
            "signal_id": signal_result["signal_id"],
            "action": signal_result["action"],
            "instrument": signal_result["instrument"],
            "source": signal_result["source"],
            "reason": signal_result["reason"],
            "gates": signal_result["gates"],
            "webhook_status": signal_result.get("webhook", {}),
        },
        "trace_id": signal_result["signal_id"],
        "causation_id": str(uuid.uuid4()),
        "signature": "",
    }
    try:
        with httpx.Client(timeout=10) as c:
            resp = c.post(f"{AEG_SIGNAL_ROUTER}/route", json=envelope)
            return resp.status_code == 200
    except Exception:
        return False


def _route_suppress_notification(instrument: str, action: str, source: str, reason: str) -> bool:
    """Route a notification when SUPPRESS_SHORT blocks a signal."""
    envelope = {
        "timestamp": _now_iso(),
        "source": "k9-3commas-bridge",
        "type": "signal_blocked_suppress_short",
        "ontology_tags": ["3commas", "suppress_short", "blocked", instrument.lower()],
        "confidence": 0.95,
        "payload": {
            "instrument": instrument,
            "blocked_action": action,
            "source": source,
            "reason": reason,
            "message": f"Short signal for {instrument} blocked by SUPPRESS_SHORT — micro catalysts dominating macro",
        },
        "trace_id": str(uuid.uuid4()),
        "causation_id": str(uuid.uuid4()),
        "signature": "",
    }
    try:
        with httpx.Client(timeout=10) as c:
            resp = c.post(f"{AEG_SIGNAL_ROUTER}/route", json=envelope)
            return resp.status_code == 200
    except Exception:
        return False


# ── Automated Signal Generation ───────────────────────────────────────────────

def generate_from_microflow() -> list[dict]:
    """Check microflow divergence and generate signals."""
    signals = []
    try:
        with httpx.Client(timeout=10) as c:
            resp = c.get(f"{MICROFLOW_URL}/microflow/divergence")
            if resp.status_code != 200:
                return signals
            data = resp.json()

        # MACRO_DOMINANT instruments → potential short signals
        for flag in data.get("flags", []):
            if flag["divergence_signal"] == "MACRO_DOMINANT":
                inst = flag["instrument"]
                if inst in INSTRUMENT_MAP:
                    signals.append(dispatch_signal(
                        action="enter_short",
                        instrument=inst,
                        source="microflow-macro-dominant",
                        confidence=0.75,
                        reason=f"Catalyst score {flag['catalyst_score']:.0f} < 20, fiscal dominance {flag['fiscal_dominance_score']:.0f} > 70",
                        metadata={"catalyst_score": flag["catalyst_score"], "fiscal_score": flag["fiscal_dominance_score"]},
                    ))
    except Exception as e:
        log.warning(f"Microflow signal generation: {e}")
    return signals


def generate_from_gex() -> list[dict]:
    """Check GEX flip and generate entry signals."""
    signals = []
    try:
        with httpx.Client(timeout=10) as c:
            resp = c.get(f"{GEX_ENGINE_URL}/gex/analysis/BTC")
            if resp.status_code != 200:
                return signals
            data = resp.json()

        flip = data.get("gamma_flip")
        if flip and flip.get("flip_detected"):
            direction = flip.get("flip_direction", "unknown")
            if direction == "positive_to_negative":
                # Gamma flip positive→negative = bearish
                signals.append(dispatch_signal(
                    action="enter_short",
                    instrument="BTC",
                    source="gex-flip",
                    confidence=0.70,
                    reason=f"Gamma flip positive→negative at ${flip.get('flip_price', 0):.0f}",
                    metadata={"flip_price": flip.get("flip_price"), "net_gex": flip.get("net_gex")},
                ))
            elif direction == "negative_to_positive":
                # Gamma flip negative→positive = bullish
                signals.append(dispatch_signal(
                    action="enter_long",
                    instrument="BTC",
                    source="gex-flip",
                    confidence=0.70,
                    reason=f"Gamma flip negative→positive at ${flip.get('flip_price', 0):.0f}",
                    metadata={"flip_price": flip.get("flip_price"), "net_gex": flip.get("net_gex")},
                ))
    except Exception as e:
        log.warning(f"GEX signal generation: {e}")
    return signals


def generate_from_fomc() -> list[dict]:
    """Check FOMC cascade and generate signals based on hike probability."""
    signals = []
    try:
        with httpx.Client(timeout=10) as c:
            resp = c.get(f"{LLM_ROUTER_URL}/fomc/cascade")
            if resp.status_code != 200:
                return signals
            data = resp.json()

        agg = data.get("aggregate", {})
        hike_prob = agg.get("hike_probability", 0)
        cut_prob = agg.get("cut_probability", 0)

        # High hike probability → bearish for crypto
        if hike_prob > 0.65:
            signals.append(dispatch_signal(
                action="exit_long",
                instrument="BTC",
                source="fomc-hike-signal",
                confidence=0.75,
                reason=f"FOMC hike probability {hike_prob:.0%} — exit longs before hawkish surprise",
                metadata={"hike_probability": hike_prob, "cut_probability": cut_prob},
            ))
        # High cut probability → bullish
        elif cut_prob > 0.65:
            signals.append(dispatch_signal(
                action="enter_long",
                instrument="BTC",
                source="fomc-cut-signal",
                confidence=0.70,
                reason=f"FOMC cut probability {cut_prob:.0%} — dovish surprise incoming",
                metadata={"hike_probability": hike_prob, "cut_probability": cut_prob},
            ))
    except Exception as e:
        log.warning(f"FOMC signal generation: {e}")
    return signals


def generate_all_automated() -> dict:
    """Run all automated signal generators and return results."""
    all_signals = []
    all_signals.extend(generate_from_microflow())
    all_signals.extend(generate_from_gex())
    all_signals.extend(generate_from_fomc())

    return {
        "timestamp": _now_iso(),
        "total_generated": len(all_signals),
        "delivered": sum(1 for s in all_signals if s.get("gates", {}).get("delivery") == "DELIVERED"),
        "blocked": sum(1 for s in all_signals if s.get("gates", {}).get("suppress_short") == "BLOCKED"),
        "held": sum(1 for s in all_signals if "HELD" in str(s.get("gates", {}).values())),
        "throttled": sum(1 for s in all_signals if s.get("gates", {}).get("rate_limit") == "THROTTLED"),
        "signals": all_signals,
    }


# ── FastAPI ───────────────────────────────────────────────────────────────────

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="K-9 3Commas Signal Bridge", version="1.0")


class SignalRequest(BaseModel):
    action: str
    instrument: str
    source: str = "manual"
    confidence: float = 0.80
    trigger_price: float | None = None
    reason: str = ""
    metadata: dict | None = None


@app.get("/3commas/health")
async def health():
    return {
        "status": "ok",
        "port": 9013,
        "bot_uuid": THREECOMMAS_BOT_UUID[:8] + "..." if THREECOMMAS_BOT_UUID else "NOT_SET",
        "webhook_url": THREECOMMAS_WEBHOOK_URL,
        "secret_set": bool(THREECOMMAS_SECRET),
        "rate_limit_seconds": RATE_LIMIT_SECONDS,
        "confidence_threshold": CONFIDENCE_THRESHOLD,
        "max_lag": MAX_LAG,
        "suppress_short_active": _suppress_short_cache,
        "stats": dict(_stats),
    }


@app.post("/3commas/signal")
async def send_signal(req: SignalRequest):
    """Manually dispatch a signal to 3Commas."""
    result = dispatch_signal(
        action=req.action,
        instrument=req.instrument,
        source=req.source,
        confidence=req.confidence,
        trigger_price=req.trigger_price,
        reason=req.reason,
        metadata=req.metadata,
    )
    if "error" in result and result["gates"].get("action_validation") == "REJECTED":
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/3commas/auto")
async def auto_generate():
    """Run all automated signal generators (microflow, GEX, FOMC)."""
    return generate_all_automated()


@app.get("/3commas/log")
async def signal_log(limit: int = 50):
    """Recent signal log for audit."""
    logs = list(_signal_log)[:limit]
    return {"count": len(logs), "signals": logs}


@app.get("/3commas/stats")
async def stats():
    """Signal dispatch statistics."""
    return {
        "stats": dict(_stats),
        "rate_limits": {
            inst: {"last_signal": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                   "remaining_seconds": _rate_limit_remaining(inst)}
            for inst, ts in _rate_limits.items()
        },
        "suppress_short_active": _suppress_short_cache,
        "total_logged": len(_signal_log),
    }


@app.get("/3commas/suppress")
async def suppress_status():
    """Check which instruments have SUPPRESS_SHORT active."""
    _refresh_suppress_cache()
    return {
        "suppress_short_instruments": _suppress_short_cache,
        "cached_at": datetime.fromtimestamp(_suppress_cache_ts, timezone.utc).isoformat() if _suppress_cache_ts else None,
    }


@app.post("/3commas/enter_long/{instrument}")
async def quick_enter_long(instrument: str, confidence: float = 0.80, reason: str = ""):
    """Quick endpoint to enter long on an instrument."""
    return dispatch_signal(
        action="enter_long",
        instrument=instrument,
        source="api-quick",
        confidence=confidence,
        reason=reason or f"Manual enter_long for {instrument}",
    )


@app.post("/3commas/enter_short/{instrument}")
async def quick_enter_short(instrument: str, confidence: float = 0.80, reason: str = ""):
    """Quick endpoint to enter short on an instrument (respects SUPPRESS_SHORT)."""
    return dispatch_signal(
        action="enter_short",
        instrument=instrument,
        source="api-quick",
        confidence=confidence,
        reason=reason or f"Manual enter_short for {instrument}",
    )


@app.post("/3commas/exit/{instrument}")
async def quick_exit(instrument: str, reason: str = ""):
    """Quick endpoint to close/exit position on an instrument."""
    return dispatch_signal(
        action="close_position",
        instrument=instrument,
        source="api-quick",
        confidence=0.90,
        reason=reason or f"Manual close_position for {instrument}",
    )


# ── TradingView Webhook Compatible Endpoint ────────────────────────────────────

@app.post("/3commas/webhook")
async def tradingview_webhook(payload: dict):
    """
    Accept TradingView-format webhook and forward to 3Commas.
    This allows K-9 to act as a signal proxy with SUPPRESS_SHORT protection.
    """
    action = payload.get("action", "")
    instrument = payload.get("tv_instrument", payload.get("instrument", ""))
    trigger_price = payload.get("trigger_price")

    # Convert 3Commas instrument back to K-9 format
    reverse_map = {v: k for k, v in INSTRUMENT_MAP.items()}
    k9_instrument = reverse_map.get(instrument, instrument)

    result = dispatch_signal(
        action=action,
        instrument=k9_instrument,
        source="tradingview-webhook",
        confidence=0.75,
        trigger_price=float(trigger_price) if trigger_price and trigger_price != "{{close}}" else None,
        reason=f"TradingView webhook: {action} {instrument}",
        metadata=payload,
    )
    return result
