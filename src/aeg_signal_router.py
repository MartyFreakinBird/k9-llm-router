"""
aeg_signal_router.py — AEG Signal Formatter + Router
─────────────────────────────────────────────────────────────────────────────
Base44 Superagent Vectors — Sprint AEG-5
Alignment: BASE44_SUPERAGENT_ALIGNMENT_PROMPT.md v1.0

Responsibilities:
  - Accept raw signals from k9-quant-engine (:9001), k9-llm-router (:8765),
    and AEG token model (:9003)
  - Format to Orbitron signal-aggregator schema exactly
  - Enforce ≥65% consensus gate (do not submit low-confidence signals solo)
  - Route to /signal-aggregator with source: "base44-aeg-vector"
  - Log prefix: 🐕 AEG:

Endpoints:
  POST /route          — format + route a raw signal
  POST /batch          — batch route multiple signals
  GET  /health         — liveness
  GET  /stats          — routing statistics

Run:
  uvicorn aeg_signal_router:app --host 0.0.0.0 --port 9004
─────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()
log = logging.getLogger("aeg-signal-router")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [🐕 AEG] %(levelname)s %(message)s")

# ── Config ────────────────────────────────────────────────────────────────────

SUPABASE_URL    = os.getenv("SUPABASE_URL", "")
INTEGRATION_KEY = os.getenv("AEG_INTEGRATION_KEY", "")
AGENT_ID        = "base44-aeg-vector"
MODEL_VERSION   = "1.0.0"
PORT            = int(os.getenv("AEG_SIGNAL_ROUTER_PORT", "9004"))

# Minimum confidence to route — signals below this are held until consensus
MIN_CONFIDENCE  = 0.65

_start            = time.time()
_routed           = 0
_blocked          = 0
_pending_signals: list[dict] = []

# ── Types ─────────────────────────────────────────────────────────────────────

SignalType  = Literal["token_utility", "risk_alert", "opportunity"]
Direction   = Literal["bullish", "bearish", "neutral"]
Timeframe   = Literal["1h", "4h", "1d", "1w"]
EntropyState = Literal["low", "medium", "high"]


@dataclass
class RawSignal:
    """Input from k9-quant-engine or AEG token model."""
    signal_type:  SignalType
    asset:        str
    direction:    Direction
    confidence:   float              # 0.0–1.0
    timeframe:    Timeframe = "1d"
    entropy_state: EntropyState = "medium"
    fed_alignment: bool = False
    source_system: str = "k9-quant-engine"
    metadata:     dict = field(default_factory=dict)


@dataclass
class OrbitronSignalPayload:
    """Exact schema required by /signal-aggregator."""
    source:      str               # always "base44-aeg-vector"
    signal_type: SignalType
    asset:       str
    direction:   Direction
    confidence:  float
    timeframe:   Timeframe
    metadata:    dict


def format_signal(raw: RawSignal) -> OrbitronSignalPayload:
    """
    Format a raw signal into the exact Orbitron signal-aggregator schema.
    Enforces AGENT_ID as source — never pass through caller's identity.
    """
    return OrbitronSignalPayload(
        source=AGENT_ID,
        signal_type=raw.signal_type,
        asset=raw.asset,
        direction=raw.direction,
        confidence=round(raw.confidence, 4),
        timeframe=raw.timeframe,
        metadata={
            "model_version":  MODEL_VERSION,
            "entropy_state":  raw.entropy_state,
            "fed_alignment":  raw.fed_alignment,
            "source_system":  raw.source_system,
            **raw.metadata,
        },
    )

# ── Orbitron Client ───────────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "Content-Type":      "application/json",
        "x-integration-key": INTEGRATION_KEY,
    }


async def submit_to_aggregator(
    client: httpx.AsyncClient,
    payload: OrbitronSignalPayload,
) -> dict:
    """POST formatted signal to /signal-aggregator."""
    url = f"{SUPABASE_URL}/functions/v1/signal-aggregator"
    body = {
        "source":      payload.source,
        "signal_type": payload.signal_type,
        "asset":       payload.asset,
        "direction":   payload.direction,
        "confidence":  payload.confidence,
        "timeframe":   payload.timeframe,
        "metadata":    payload.metadata,
    }
    try:
        r = await client.post(url, json=body, headers=_headers(), timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("🐕 AEG: signal-aggregator POST failed: %s", e)
        return {"error": str(e)}


async def push_module_gateway(
    client: httpx.AsyncClient,
    payload: OrbitronSignalPayload,
) -> dict:
    """Set shared state via /module-gateway for cross-module consumers."""
    url = f"{SUPABASE_URL}/functions/v1/module-gateway"
    body = {
        "action":        "set_shared",
        "source_module": "external",
        "target_module": "ai_yield",
        "data_type":     "aeg_signal",
        "data": {
            "signal_type": payload.signal_type,
            "asset":       payload.asset,
            "direction":   payload.direction,
            "confidence":  payload.confidence,
            "agent_id":    AGENT_ID,
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        },
    }
    try:
        r = await client.post(url, json=body, headers=_headers(), timeout=10)
        return r.json() if r.is_success else {"error": f"HTTP {r.status_code}"}
    except Exception as e:
        log.warning("🐕 AEG: module-gateway failed: %s", e)
        return {"error": str(e)}

# ── Router Logic ──────────────────────────────────────────────────────────────

async def route_signal(raw: RawSignal) -> dict:
    """
    Main routing pipeline:
    1. Confidence gate (≥65% — Orbitron consensus threshold)
    2. Format to Orbitron schema
    3. POST /signal-aggregator
    4. SET via /module-gateway
    Returns routing result.
    """
    global _routed, _blocked

    if raw.confidence < MIN_CONFIDENCE:
        _blocked += 1
        log.info(
            "🐕 AEG: Signal HELD — confidence %.3f < %.2f (asset=%s dir=%s)",
            raw.confidence, MIN_CONFIDENCE, raw.asset, raw.direction
        )
        # Hold in pending — could be surfaced if multi-source consensus arrives
        _pending_signals.append({
            "asset":      raw.asset,
            "direction":  raw.direction,
            "confidence": raw.confidence,
            "held_at":    datetime.now(timezone.utc).isoformat(),
        })
        return {
            "routed":  False,
            "reason":  f"confidence {raw.confidence:.3f} below threshold {MIN_CONFIDENCE}",
            "pending": True,
        }

    payload = format_signal(raw)

    async with httpx.AsyncClient() as client:
        agg_result = await submit_to_aggregator(client, payload)
        gw_result  = await push_module_gateway(client, payload)

    _routed += 1
    log.info(
        "🐕 AEG: Signal ROUTED — %s %s %s conf=%.3f",
        payload.asset, payload.direction, payload.signal_type, payload.confidence
    )

    return {
        "routed":            True,
        "signal":            {
            "source":      payload.source,
            "asset":       payload.asset,
            "direction":   payload.direction,
            "confidence":  payload.confidence,
            "signal_type": payload.signal_type,
        },
        "aggregator_result": agg_result,
        "gateway_result":    gw_result,
    }

# ── FastAPI App ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="AEG Signal Router",
    version=MODEL_VERSION,
    description="Base44 Superagent Vectors — signal formatter + Orbitron router [AEG-5]",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
def health():
    return {
        "status":    "online",
        "component": f"{AGENT_ID}-signal-router",
        "version":   MODEL_VERSION,
        "sprint":    "AEG-5",
        "uptime_s":  round(time.time() - _start, 1),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/stats")
def stats():
    return {
        "routed":          _routed,
        "blocked":         _blocked,
        "pending":         len(_pending_signals),
        "min_confidence":  MIN_CONFIDENCE,
        "agent_id":        AGENT_ID,
    }


@app.post("/route")
async def route_endpoint(body: dict):
    """
    Route a single signal.

    Expected body:
    {
      "signal_type": "token_utility|risk_alert|opportunity",
      "asset": "AEG",
      "direction": "bullish|bearish|neutral",
      "confidence": 0.0-1.0,
      "timeframe": "1h|4h|1d|1w",          (optional, default: 1d)
      "entropy_state": "low|medium|high",   (optional, default: medium)
      "fed_alignment": true/false,          (optional)
      "source_system": "k9-quant-engine",   (optional)
      "metadata": {}                        (optional)
    }
    """
    try:
        raw = RawSignal(
            signal_type   = body["signal_type"],
            asset         = body["asset"],
            direction     = body["direction"],
            confidence    = float(body["confidence"]),
            timeframe     = body.get("timeframe", "1d"),
            entropy_state = body.get("entropy_state", "medium"),
            fed_alignment = body.get("fed_alignment", False),
            source_system = body.get("source_system", "k9-quant-engine"),
            metadata      = body.get("metadata", {}),
        )
    except KeyError as e:
        raise HTTPException(status_code=422, detail=f"Missing required field: {e}")

    return await route_signal(raw)


@app.post("/batch")
async def batch_endpoint(body: dict):
    """
    Route multiple signals.
    Body: { "signals": [ { ...signal... }, ... ] }
    """
    signals = body.get("signals", [])
    if not signals:
        raise HTTPException(status_code=422, detail="signals array required")

    results = []
    for sig in signals:
        try:
            raw = RawSignal(
                signal_type   = sig["signal_type"],
                asset         = sig["asset"],
                direction     = sig["direction"],
                confidence    = float(sig["confidence"]),
                timeframe     = sig.get("timeframe", "1d"),
                entropy_state = sig.get("entropy_state", "medium"),
                fed_alignment = sig.get("fed_alignment", False),
                source_system = sig.get("source_system", "k9-quant-engine"),
                metadata      = sig.get("metadata", {}),
            )
            results.append(await route_signal(raw))
        except Exception as e:
            results.append({"routed": False, "error": str(e)})

    return {
        "total":   len(results),
        "routed":  sum(1 for r in results if r.get("routed")),
        "blocked": sum(1 for r in results if not r.get("routed")),
        "results": results,
    }


@app.get("/pending")
def pending_signals():
    """Return signals held below confidence threshold."""
    return {"pending": _pending_signals, "count": len(_pending_signals)}


if __name__ == "__main__":
    log.info("🐕 AEG: Signal Router starting — port %d", PORT)
    uvicorn.run("aeg_signal_router:app", host="0.0.0.0", port=PORT, reload=False)
