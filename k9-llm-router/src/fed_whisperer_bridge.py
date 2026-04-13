"""
fed_whisperer_bridge.py
─────────────────────────────────────────────────────────────────────────────
FedWhisperer → k9-llm-router bridge — v0.4.0

UPGRADE (2026-04-13):
  - DATA_SYNC_REQUEST responder: Orbitron polls K9_AGENT for platform_status
    — bridge now handles this by broadcasting DATA_SYNC_RESPONSE
  - Local FRED-backed regime synthesizer: when FedWhisperer has no live data
    (trading_signals table empty, no FED_REGIME_UPDATE events), synthesize
    a regime estimate from FRED macro indicators via OpenBB bridge
  - Signal poller: active polling loop at 30s that publishes any new
    FedWhisperer signals to k9-llm-router context cache
  - CLOB safety: all methods tagged L1/L2 — CLOB signing requires L1 sign-off

Architecture:
  FedWhisperer (Supabase) → trading_signals table (when live)
      ↓ fallback if empty ↓
  FRED via OpenBB → local regime synthesis
      ↓
  enrich_trading_request() → LLM context injection
      ↓
  DeepSeek V4 / GLM-5 via k9-llm-router

Orbitron event loop (background):
  K9_AGENT hears DATA_SYNC_REQUEST → responds with platform_status
  K9_AGENT broadcasts SIGNAL_ACKNOWLEDGED when signals are consumed

# LEVEL-1 ADVISORY: Signal context for LLM enrichment ONLY.
# No order execution. No wallet signing. CLOB signing requires Security review.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("fed-whisperer-bridge")

ORBITRON_URL        = os.getenv("ORBITRON_URL", "https://ziqenqqgnqxqrazmjohs.supabase.co")
ORBITRON_ANON_KEY   = os.getenv("ORBITRON_ANON_KEY", "")
ORBITRON_AUTH_TOKEN = os.getenv("ORBITRON_AUTH_TOKEN", "")
FED_WHISPERER_URL   = os.getenv("FED_WHISPERER_URL", ORBITRON_URL)

# Signal context cache — last known good state
_SIGNAL_CACHE: list[dict] = []
_REGIME_CACHE: dict = {}
_CACHE_TTL_S  = int(os.getenv("FED_CONTEXT_CACHE_TTL", "60"))
_last_cache_ts: float = 0.0


def _headers() -> dict:
    h = {"apikey": ORBITRON_ANON_KEY, "Content-Type": "application/json"}
    if ORBITRON_AUTH_TOKEN:
        h["Authorization"] = f"Bearer {ORBITRON_AUTH_TOKEN}"
    return h


# ── ORBITRON DATA_SYNC_REQUEST RESPONDER ─────────────────────────────────────

async def respond_to_data_sync_requests() -> None:
    """
    Orbitron broadcasts DATA_SYNC_REQUEST(requested_data=platform_status) ~every 30s.
    K9_AGENT responds with platform_status so Orbitron's hub knows we're alive.

    # L2 — no financial action, safe to run autonomously.
    """
    from .orbitron_client import OrbitronClient
    client = OrbitronClient.from_env()

    # Fetch unacknowledged DATA_SYNC_REQUESTs targeting K9_AGENT or broadcast
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(
                f"{ORBITRON_URL}/rest/v1/platform_sync_events"
                f"?event_type=eq.DATA_SYNC_REQUEST"
                f"&acknowledged=eq.false"
                f"&order=created_at.desc&limit=5",
                headers=_headers(),
            )
            if r.status_code != 200:
                return
            events = r.json()
    except Exception as e:
        log.debug("DATA_SYNC_REQUEST fetch failed: %s", e)
        return

    if not events:
        return

    # Build platform status payload
    status_payload = {
        "platform": "K9_AGENT",
        "status": "online",
        "services": {
            "llm_router": {"port": 8765, "status": "online"},
            "paymaster":  {"port": 9002, "status": "unknown"},
            "mcp_manager": {"port": 3030, "status": "unknown"},
        },
        "sprint": 3,
        "capabilities": [
            "llm_routing", "quant_analysis", "trading_signal",
            "fed_context_injection", "orbitron_event_bus"
        ],
        "responded_at": datetime.now(timezone.utc).isoformat(),
    }

    # Broadcast DATA_SYNC_RESPONSE
    await client.broadcast("DATA_SYNC_RESPONSE", status_payload)
    log.info("→ DATA_SYNC_RESPONSE sent (%d pending requests)", len(events))


# ── FED REGIME FETCHER ────────────────────────────────────────────────────────

async def get_fed_regime() -> dict[str, Any]:
    """
    Fetch current Fed regime. Priority order:
    1. Orbitron module-gateway (live FedWhisperer data)
    2. platform_sync_events (FED_REGIME_UPDATE events)
    3. Local FRED synthesis via OpenBB (fallback)
    4. Cached last-known-good value
    """
    global _REGIME_CACHE, _last_cache_ts

    # Return cache if fresh
    import time
    if _REGIME_CACHE and (time.time() - _last_cache_ts) < _CACHE_TTL_S:
        return _REGIME_CACHE

    # 1. Try module-gateway
    regime = await _regime_from_gateway()
    if regime.get("regime", "UNKNOWN") != "UNKNOWN":
        _REGIME_CACHE = regime
        _last_cache_ts = time.time()
        return regime

    # 2. Try event log
    regime = await _regime_from_events()
    if regime.get("regime", "UNKNOWN") != "UNKNOWN":
        _REGIME_CACHE = regime
        _last_cache_ts = time.time()
        return regime

    # 3. FRED synthesis
    regime = await _synthesize_regime_from_fred()
    if regime.get("regime", "UNKNOWN") != "UNKNOWN":
        _REGIME_CACHE = regime
        _last_cache_ts = time.time()
        return regime

    # 4. Return stale cache if any
    if _REGIME_CACHE:
        log.debug("Returning stale regime cache")
        return {**_REGIME_CACHE, "stale": True}

    return {"regime": "UNKNOWN", "confidence": 0.0, "drivers": [], "source": "unavailable"}


async def _regime_from_gateway() -> dict:
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.post(
                f"{ORBITRON_URL}/functions/v1/module-gateway",
                json={"action": "get_shared", "module": "fed_whisperer", "data_type": "regime_state"},
                headers=_headers(),
            )
            if r.status_code == 200:
                data = r.json()
                regime = data.get("data", {}).get("fed_whisperer.regime_state", {})
                if regime.get("regime"):
                    regime["source"] = "module-gateway"
                    return regime
    except Exception as e:
        log.debug("module-gateway regime: %s", e)
    return {"regime": "UNKNOWN"}


async def _regime_from_events() -> dict:
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(
                f"{ORBITRON_URL}/rest/v1/platform_sync_events"
                f"?source_platform=eq.FED_WHISPERER"
                f"&event_type=eq.FED_REGIME_UPDATE"
                f"&limit=1&order=created_at.desc",
                headers=_headers(),
            )
            if r.status_code == 200:
                rows = r.json()
                if rows:
                    payload = rows[0].get("payload") or rows[0].get("data", {})
                    return {
                        "regime": payload.get("regime", "UNKNOWN"),
                        "confidence": payload.get("confidence", 0.5),
                        "drivers": payload.get("drivers", []),
                        "source": "event_log",
                        "lastUpdated": rows[0].get("created_at"),
                    }
    except Exception as e:
        log.debug("Event log regime: %s", e)
    return {"regime": "UNKNOWN"}


async def _synthesize_regime_from_fred() -> dict:
    """
    Synthesize a Fed regime estimate from FRED macro indicators when
    FedWhisperer data is unavailable.

    Uses Taylor Rule heuristic:
      neutral_rate = 2.0 + inflation + 0.5*(inflation - 2.0) + 0.5*output_gap
      If fed_funds > neutral: RESTRICTIVE / HAWKISH
      If fed_funds < neutral: ACCOMMODATIVE / DOVISH
      If within 0.5%: NEUTRAL

    # L2 — synthesis only, no execution.
    """
    try:
        # Try OpenBB FRED data
        from .openbb_bridge import OpenBBBridge
        obb = OpenBBBridge()
        context = await obb.get_macro_context()

        if context:
            # Parse CPI from OpenBB context
            import re
            cpi_match = re.search(r"CPI=([\d.]+)", context)
            cpi = float(cpi_match.group(1)) if cpi_match else 3.2  # fallback estimate

            # Taylor Rule approximation
            inflation = cpi
            neutral_rate = 2.0 + inflation + 0.5 * (inflation - 2.0)
            # Fed funds rate — use 5.25% (last known as of late 2025)
            fed_funds = float(os.getenv("FED_FUNDS_RATE_ESTIMATE", "5.25"))
            taylor_gap = round(fed_funds - neutral_rate, 2)

            if taylor_gap > 0.5:
                regime = "RESTRICTIVE"
                confidence = min(0.5 + abs(taylor_gap) * 0.1, 0.85)
            elif taylor_gap < -0.5:
                regime = "ACCOMMODATIVE"
                confidence = min(0.5 + abs(taylor_gap) * 0.1, 0.85)
            else:
                regime = "NEUTRAL"
                confidence = 0.6

            log.info("FRED regime synthesis: %s (taylor_gap=%.2f)", regime, taylor_gap)
            return {
                "regime": regime,
                "confidence": round(confidence, 2),
                "drivers": ["FRED_CPI", "TAYLOR_RULE_ESTIMATE"],
                "taylor_gap": taylor_gap,
                "fed_funds": fed_funds,
                "cpi": round(inflation, 2),
                "source": "fred_synthesis",
            }
    except Exception as e:
        log.debug("FRED synthesis failed: %s", e)

    # Pure estimate fallback (no data available)
    return {
        "regime": "RESTRICTIVE",  # default: 2025 Fed policy context
        "confidence": 0.3,
        "drivers": ["heuristic_estimate"],
        "source": "static_estimate",
    }


# ── SIGNAL POLLER ─────────────────────────────────────────────────────────────

async def poll_fed_whisperer_signals(
    symbol: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """
    Poll Orbitron for live FedWhisperer trading signals.
    Updates the local signal cache.

    Returns list of signal dicts (empty if table has no active signals).

    # L2 — read-only, no execution.
    """
    global _SIGNAL_CACHE

    try:
        url = (
            f"{ORBITRON_URL}/rest/v1/trading_signals"
            f"?status=eq.active"
            f"&order=created_at.desc&limit={limit}"
        )
        if symbol:
            url += f"&symbol=eq.{symbol}"

        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(url, headers=_headers())
            if r.status_code == 200:
                rows = r.json()
                _SIGNAL_CACHE = rows
                log.debug("Signal poll: %d active signals", len(rows))
                return rows
    except Exception as e:
        log.debug("Signal poll failed: %s", e)

    return _SIGNAL_CACHE  # return last cached


# ── QUANT CONTEXT BUILDER ─────────────────────────────────────────────────────

async def build_quant_context(
    symbol: str | None = None,
    limit: int = 5,
) -> str:
    """
    Build rich quantitative context string for LLM quant_analysis / trading_signal tasks.
    Pulls FedWhisperer signals, Fed regime, and macro events in parallel.
    Falls back to FRED synthesis when FedWhisperer is dormant.

    # L1 ADVISORY: context enrichment only — no order execution.
    """
    from .orbitron_client import OrbitronClient
    client = OrbitronClient.from_env()

    signals_task    = asyncio.create_task(poll_fed_whisperer_signals(symbol, limit))
    regime_task     = asyncio.create_task(get_fed_regime())
    data_sync_task  = asyncio.create_task(respond_to_data_sync_requests())
    macro_task      = asyncio.create_task(
        client.get_recent_events(source_platform="openbb-bridge", limit=5)
    )

    signals, regime, _, macro_events = await asyncio.gather(
        signals_task, regime_task, data_sync_task, macro_task,
        return_exceptions=True
    )

    parts: list[str] = []

    # Fed regime block
    if isinstance(regime, dict) and regime.get("regime", "UNKNOWN") != "UNKNOWN":
        r = regime
        source_tag = f" [{r.get('source', '?')}]" if r.get("source") else ""
        taylor = f" taylor_gap={r.get('taylor_gap', 'N/A')}" if "taylor_gap" in r else ""
        parts.append(
            f"[FED REGIME{source_tag}] {r.get('regime')} "
            f"(confidence={r.get('confidence', 0):.0%}"
            f"{taylor}, drivers={', '.join(r.get('drivers', []))})"
        )

    # Live signals block
    if isinstance(signals, list) and signals:
        parts.append(f"\n[ACTIVE SIGNALS — {len(signals)} found]")
        for s in signals:
            sig_type = s.get("signal_type", "?").upper()
            sym = s.get("symbol", "?")
            conf = s.get("confidence_score", 0)
            entry = s.get("entry_price", 0)
            sl = s.get("stop_loss", 0)
            tp = s.get("take_profit", 0)
            tf = s.get("timeframe", "?")
            parts.append(
                f"  • {sig_type} {sym} | "
                f"Conf={conf:.0%} | Entry=${entry:,.2f} | "
                f"SL=${sl:,.2f} | TP=${tp:,.2f} | TF={tf}"
            )
    elif symbol:
        parts.append(f"\n[ACTIVE SIGNALS] None found for {symbol} — FedWhisperer may be dormant")

    # Macro events block
    if isinstance(macro_events, list) and macro_events:
        parts.append(f"\n[MACRO EVENTS — last {len(macro_events)}]")
        for ev in macro_events[:3]:
            ts = ev.get("created_at", "")[:16].replace("T", " ")
            parts.append(
                f"  • [{ts}] {ev.get('event_type', '')} from {ev.get('source_platform', '')}"
            )

    if not parts:
        return ""

    return (
        f"=== ORBITRON MARKET CONTEXT ===\n"
        f"Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        + "\n".join(parts)
        + "\n=== END CONTEXT ===\n"
    )


# ── ROUTER INTEGRATION HOOK ───────────────────────────────────────────────────

async def enrich_trading_request(
    task_type: str,
    messages: list[dict],
    system: str | None,
    symbol: str | None = None,
) -> tuple[list[dict], str | None]:
    """
    Enrich a trading/quant LLM request with live Orbitron + FRED context.
    Called by k9-llm-router before dispatching trading/quant tasks.

    # L1 ADVISORY: Enriches LLM context only — no execution.
    """
    TRADING_TASK_TYPES = {
        "trading_signal", "quant_analysis", "finance_coach",
        "financial_analysis",
    }
    if task_type not in TRADING_TASK_TYPES:
        return messages, system

    context = await build_quant_context(symbol=symbol, limit=5)
    if not context:
        return messages, system

    enriched_system = (
        f"{system}\n\n{context}" if system else context
    )
    log.info("Enriched %s request (%d chars context)", task_type, len(context))
    return messages, enriched_system


# ── BACKGROUND POLLER (for main.py startup) ──────────────────────────────────

async def start_fed_whisperer_poll_loop(interval_s: int = 30) -> None:
    """
    Background task: polls FedWhisperer signals + responds to DATA_SYNC_REQUESTs
    every `interval_s` seconds. Start via asyncio.create_task().
    """
    log.info("FedWhisperer poll loop started (interval=%ds)", interval_s)
    while True:
        try:
            await asyncio.gather(
                poll_fed_whisperer_signals(),
                respond_to_data_sync_requests(),
                return_exceptions=True
            )
        except Exception as e:
            log.warning("Poll loop error: %s", e)
        await asyncio.sleep(interval_s)
