"""
fed_whisperer_bridge.py
─────────────────────────────────────────────────────────────────────────────
FedWhisperer → k9-llm-router bridge.

Pulls FedWhisperer trading signals (Taylor Rule gap, Fed regime, macro data)
from Orbitron shared state and injects them as context into quant_analysis
and trading_signal LLM routing requests.

This is the QuantSignalFetcher → quant-engine seam.
Both components are already built — this wires them.

Architecture:
  FedWhisperer (Supabase) → generate-trading-signals fn
      → trading_signals table
          → OrbitronClient.get_latest_signals()
              → inject as context into LLM prompt
                  → DeepSeek V4 via k9-llm-router
                      → AGENT_DECISION → Orbitron event bus

Level-1 Advisory: Functions that prepare signal context for downstream
execution are tagged. Do NOT add wallet signing without Security Auditor review.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("fed-whisperer-bridge")

ORBITRON_URL       = os.getenv("ORBITRON_URL", "https://ziqenqqgnqxqrazmjohs.supabase.co")
ORBITRON_ANON_KEY  = os.getenv("ORBITRON_ANON_KEY", "")
ORBITRON_AUTH_TOKEN = os.getenv("ORBITRON_AUTH_TOKEN", "")

# FedWhisperer Supabase (same project — shared Orbitron backend)
FED_WHISPERER_URL  = os.getenv("FED_WHISPERER_URL", ORBITRON_URL)


def _headers() -> dict:
    h = {"apikey": ORBITRON_ANON_KEY, "Content-Type": "application/json"}
    if ORBITRON_AUTH_TOKEN:
        h["Authorization"] = f"Bearer {ORBITRON_AUTH_TOKEN}"
    return h


# ── FED REGIME CONTEXT ────────────────────────────────────────────────────────

async def get_fed_regime() -> dict[str, Any]:
    """
    Fetch current Fed regime state from Orbitron module-gateway.

    Returns:
        {
          "regime": "HAWKISH" | "DOVISH" | "NEUTRAL" | "RESTRICTIVE" | "ACCOMMODATIVE",
          "confidence": 0.85,
          "drivers": ["inflation", "employment"],
          "taylor_gap": 1.2,
          "lastUpdated": "2026-04-10T..."
        }
    """
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(
                f"{ORBITRON_URL}/functions/v1/module-gateway/get_shared"
                f"?module=fed_whisperer&data_type=regime_state",
                headers=_headers(),
            )
            if r.status_code == 200:
                data = r.json()
                regime = data.get("data", {}).get("fed_whisperer.regime_state", {})
                log.debug("Fed regime: %s (confidence=%.0f%%)",
                          regime.get("regime"), (regime.get("confidence", 0) * 100))
                return regime
    except Exception as e:
        log.warning("Fed regime fetch failed: %s", e)

    # Fallback: pull from recent platform_sync_events
    return await _get_regime_from_events()


async def _get_regime_from_events() -> dict[str, Any]:
    """Fallback — infer regime from recent FED_REGIME_UPDATE events."""
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
                    data = rows[0].get("data", {})
                    return {
                        "regime": data.get("regime", "UNKNOWN"),
                        "confidence": data.get("confidence", 0.5),
                        "drivers": data.get("drivers", []),
                        "source": "event_log_fallback",
                        "lastUpdated": rows[0].get("created_at"),
                    }
    except Exception as e:
        log.warning("Regime event fallback failed: %s", e)

    return {"regime": "UNKNOWN", "confidence": 0.0, "drivers": [], "source": "unavailable"}


# ── SIGNAL CONTEXT BUILDER ────────────────────────────────────────────────────

async def build_quant_context(
    symbol: str | None = None,
    limit: int = 5,
) -> str:
    """
    Build rich quantitative context string for LLM quant_analysis / trading_signal tasks.

    Pulls:
    - Latest trading signals from Orbitron (FedWhisperer generated)
    - Current Fed regime state
    - Recent macro events from event log

    Returns a formatted string ready to prepend to LLM system/user prompts.

    # LEVEL-1 ADVISORY: This context is used to inform LLM analysis only.
    # No order execution or wallet signing occurs here.
    """
    from .orbitron_client import OrbitronClient

    client = OrbitronClient.from_env()

    # Parallel fetch
    import asyncio
    signals_task = asyncio.create_task(
        client.get_latest_signals(symbol=symbol, limit=limit, status="active")
    )
    regime_task  = asyncio.create_task(get_fed_regime())
    macro_task   = asyncio.create_task(
        client.get_recent_events(source_platform="openbb-bridge", limit=5)
    )

    signals, regime, macro_events = await asyncio.gather(
        signals_task, regime_task, macro_task, return_exceptions=True
    )

    parts: list[str] = []

    # Fed regime block
    if isinstance(regime, dict) and regime.get("regime", "UNKNOWN") != "UNKNOWN":
        r = regime
        parts.append(
            f"[FED REGIME] {r.get('regime')} "
            f"(confidence={r.get('confidence', 0):.0%}, "
            f"drivers={', '.join(r.get('drivers', []))})"
        )

    # Live signals block
    if isinstance(signals, list) and signals:
        parts.append(f"\n[ACTIVE SIGNALS — {len(signals)} found]")
        for s in signals:
            parts.append(f"  • {s.to_prompt_context()}")
    elif symbol:
        parts.append(f"\n[ACTIVE SIGNALS] None found for {symbol}")

    # Macro events block
    if isinstance(macro_events, list) and macro_events:
        parts.append(f"\n[MACRO EVENTS — last {len(macro_events)}]")
        for ev in macro_events[:3]:
            ts = ev.get("created_at", "")[:16].replace("T", " ")
            parts.append(f"  • [{ts}] {ev.get('event_type', '')} from {ev.get('source_platform', '')}")

    if not parts:
        return ""

    header = (
        f"=== ORBITRON MARKET CONTEXT ==="
        f"\nTimestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
    )
    return header + "\n".join(parts) + "\n=== END CONTEXT ===\n"


# ── ROUTER INTEGRATION HOOK ───────────────────────────────────────────────────

async def enrich_trading_request(
    task_type: str,
    messages: list[dict],
    system: str | None,
    symbol: str | None = None,
) -> tuple[list[dict], str | None]:
    """
    Enrich a trading/quant LLM request with live Orbitron context.

    Called by k9-llm-router before dispatching trading_signal or quant_analysis tasks.
    Injects FedWhisperer signals + Fed regime into the request context.

    Args:
        task_type: Router task type
        messages: Original message array
        system: Original system prompt
        symbol: Optional symbol to filter signals (e.g. "BTCUSD")

    Returns:
        (enriched_messages, enriched_system)
    """
    TRADING_TASK_TYPES = {
        "trading_signal", "quant_analysis", "finance_coach",
        "financial_analysis", "trading_signal"
    }

    if task_type not in TRADING_TASK_TYPES:
        return messages, system

    context = await build_quant_context(symbol=symbol, limit=5)
    if not context:
        return messages, system

    # Inject context into system prompt
    enriched_system = (
        f"{system}\n\n{context}" if system
        else context
    )

    log.info(
        "Enriched %s request with Orbitron context (%d chars)",
        task_type, len(context)
    )
    return messages, enriched_system
