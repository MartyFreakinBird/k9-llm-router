"""
orbitron_client.py
─────────────────────────────────────────────────────────────────────────────
Orbitron API Client for K-9 LLM Router

Wraps the Orbitron Supabase backend:
  - platform-sync edge function (event bus)
  - trading_signals table (live signals)
  - platform_sync_events table (ecosystem event log)

Auth: Bearer JWT (from $ORBITRON_AUTH_TOKEN) + anon apikey header.
Source identity: K9_AGENT (registered in Orbitron's valid_platforms list).

Usage:
  client = OrbitronClient.from_env()
  signals = await client.get_latest_signals(symbol="BTCUSD", limit=10)
  await client.broadcast(event_type="AGENT_HEARTBEAT", data={...})
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("orbitron-client")

# ── CONFIG ────────────────────────────────────────────────────────────────────

ORBITRON_URL      = os.getenv("ORBITRON_URL", "https://ziqenqqgnqxqrazmjohs.supabase.co")
ORBITRON_ANON_KEY = os.getenv("ORBITRON_ANON_KEY", "")
ORBITRON_AUTH_TOKEN = os.getenv("ORBITRON_AUTH_TOKEN", "")

SOURCE_PLATFORM = "K9_AGENT"

# Valid Orbitron event types (confirmed from API)
class OrbitronEvent:
    PLATFORM_ONLINE          = "PLATFORM_ONLINE"
    PLATFORM_OFFLINE         = "PLATFORM_OFFLINE"
    PLATFORM_HEARTBEAT       = "PLATFORM_HEARTBEAT"
    AGENT_HEARTBEAT          = "AGENT_HEARTBEAT"
    AGENT_DECISION           = "AGENT_DECISION"
    AGENT_FEEDBACK           = "AGENT_FEEDBACK"
    SIGNAL_GENERATED         = "SIGNAL_GENERATED"
    SIGNAL_ACKNOWLEDGED      = "SIGNAL_ACKNOWLEDGED"
    SIGNAL_PUBLISHED         = "SIGNAL_PUBLISHED"
    TRADING_SIGNAL           = "SIGNAL_GENERATED"
    NEURAL_PREDICTION        = "NEURAL_PREDICTION"
    TOPOLOGY_ALPHA_SIGNAL    = "TOPOLOGY_ALPHA_SIGNAL"
    TOPOLOGY_METRICS_UPDATE  = "TOPOLOGY_METRICS_UPDATE"
    TOPOLOGY_REGIME_CHANGE   = "TOPOLOGY_REGIME_CHANGE"
    BLENDED_SCORE_UPDATE     = "BLENDED_SCORE_UPDATE"
    FED_REGIME_UPDATE        = "FED_REGIME_UPDATE"
    FED_AMD_PHASE            = "FED_AMD_PHASE"
    FED_ALIGNMENT_SIGNAL     = "FED_ALIGNMENT_SIGNAL"
    MACRO_ANALYSIS_UPDATE    = "MACRO_ANALYSIS_UPDATE"
    YIELD_OPPORTUNITY        = "YIELD_OPPORTUNITY_DETECTED"
    DEFI_RISK_ALERT          = "DEFI_RISK_ALERT"
    PORTFOLIO_INSIGHT        = "PORTFOLIO_INSIGHT"
    MARKET_SENTIMENT         = "MARKET_SENTIMENT_UPDATE"
    ORCHESTRATION_COMMAND    = "ORCHESTRATION_COMMAND"
    ECOSYSTEM_ALERT          = "ECOSYSTEM_ALERT"
    DATA_SYNC_REQUEST        = "DATA_SYNC_REQUEST"
    DATA_SYNC_RESPONSE       = "DATA_SYNC_RESPONSE"
    SHARED_STATE_UPDATE      = "SHARED_STATE_UPDATE"
    COMMAND_EXECUTED         = "COMMAND_EXECUTED"
    COMMAND_FAILED           = "COMMAND_FAILED"
    ERROR_REPORT             = "ERROR_REPORT"
    CIRCUIT_BREAKER_TRIPPED  = "CIRCUIT_BREAKER_TRIPPED"
    RECOVERY_COMPLETE        = "RECOVERY_COMPLETE"


@dataclass
class TradingSignal:
    """Parsed Orbitron trading signal."""
    id: str
    signal_id: str
    symbol: str
    signal_type: str          # "buy" | "sell" | "hold"
    strategy_name: str
    confidence_score: float
    entry_price: float
    stop_loss: float
    take_profit: float
    timeframe: str
    indicators: list[str]
    signal_strength: str      # "weak" | "moderate" | "strong"
    market_condition: str
    status: str
    created_at: str

    @classmethod
    def from_row(cls, row: dict) -> "TradingSignal":
        return cls(
            id=row["id"],
            signal_id=row.get("signal_id", ""),
            symbol=row.get("symbol", ""),
            signal_type=row.get("signal_type", ""),
            strategy_name=row.get("strategy_name", ""),
            confidence_score=row.get("confidence_score", 0.0),
            entry_price=row.get("entry_price", 0.0),
            stop_loss=row.get("stop_loss", 0.0),
            take_profit=row.get("take_profit", 0.0),
            timeframe=row.get("timeframe", ""),
            indicators=row.get("indicators_used", {}).get("indicators", []),
            signal_strength=row.get("signal_strength", ""),
            market_condition=row.get("market_condition", ""),
            status=row.get("status", ""),
            created_at=row.get("created_at", ""),
        )

    def to_prompt_context(self) -> str:
        """Format signal as context for LLM routing (quant_analysis tasks)."""
        return (
            f"Signal: {self.signal_type.upper()} {self.symbol} | "
            f"Confidence: {self.confidence_score:.1%} | "
            f"Entry: ${self.entry_price:,.2f} | "
            f"SL: ${self.stop_loss:,.2f} | TP: ${self.take_profit:,.2f} | "
            f"Timeframe: {self.timeframe} | "
            f"Indicators: {', '.join(self.indicators)} | "
            f"Strength: {self.signal_strength} | "
            f"Market: {self.market_condition}"
        )


class OrbitronClient:
    """
    Async client for the Orbitron platform API.

    All public methods are async — use in FastAPI route handlers or
    background tasks via asyncio.create_task().
    """

    def __init__(
        self,
        base_url: str = ORBITRON_URL,
        anon_key: str = ORBITRON_ANON_KEY,
        auth_token: str = ORBITRON_AUTH_TOKEN,
        timeout: float = 10.0,
    ) -> None:
        self._base     = base_url.rstrip("/")
        self._anon_key = anon_key
        self._token    = auth_token
        self._timeout  = timeout
        self._sent     = 0
        self._errors   = 0
        self._start    = time.time()

    @classmethod
    def from_env(cls) -> "OrbitronClient":
        """Construct from environment variables."""
        return cls(
            base_url=ORBITRON_URL,
            anon_key=ORBITRON_ANON_KEY,
            auth_token=ORBITRON_AUTH_TOKEN,
        )

    def _headers(self) -> dict[str, str]:
        h = {
            "apikey": self._anon_key,
            "Content-Type": "application/json",
        }
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    # ── EVENT BUS (platform-sync) ─────────────────────────────────────────────

    async def broadcast(
        self,
        event_type: str,
        data: dict[str, Any],
        action: str = "broadcast_event",
    ) -> dict:
        """
        Broadcast an event to the Orbitron ecosystem via platform-sync.

        Args:
            event_type: One of OrbitronEvent.* constants
            data: Event payload
            action: platform-sync action (default: broadcast_event)

        Returns:
            {"success": True, "event_id": "...", "message": "..."}
        """
        payload = {
            "action": action,
            "source_platform": SOURCE_PLATFORM,
            "event_type": event_type,
            "data": data,
        }
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            try:
                r = await c.post(
                    f"{self._base}/functions/v1/platform-sync",
                    json=payload,
                    headers=self._headers(),
                )
                r.raise_for_status()
                self._sent += 1
                result = r.json()
                log.info(
                    "→ Orbitron [%s] event_id=%s",
                    event_type,
                    result.get("event_id", "?")[:8],
                )
                return result
            except Exception as e:
                self._errors += 1
                log.warning("Orbitron broadcast failed [%s]: %s", event_type, e)
                return {"success": False, "error": str(e)}

    async def heartbeat(self, component: str, status: str = "online", extra: dict | None = None) -> dict:
        """Send AGENT_HEARTBEAT to Orbitron."""
        return await self.broadcast(
            event_type=OrbitronEvent.AGENT_HEARTBEAT,
            data={
                "component": component,
                "status": status,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "sprint": 3,
                **(extra or {}),
            },
        )

    async def report_routing_decision(
        self,
        task_type: str,
        model_used: str,
        backend: str,
        latency_ms: float,
        component: str,
    ) -> dict:
        """Report an LLM routing decision to Orbitron as AGENT_DECISION."""
        return await self.broadcast(
            event_type=OrbitronEvent.AGENT_DECISION,
            data={
                "decision_type": "llm_route",
                "task_type": task_type,
                "model_used": model_used,
                "backend": backend,
                "latency_ms": latency_ms,
                "requesting_component": component,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def report_signal_acknowledged(
        self,
        signal_id: str,
        symbol: str,
        action_taken: str,
    ) -> dict:
        """Acknowledge a trading signal was received and routed."""
        return await self.broadcast(
            event_type=OrbitronEvent.SIGNAL_ACKNOWLEDGED,
            data={
                "signal_id": signal_id,
                "symbol": symbol,
                "action_taken": action_taken,
                "acknowledged_by": "k9-llm-router",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def report_error(self, component: str, error: str, severity: str = "warning") -> dict:
        """Report an error to Orbitron."""
        return await self.broadcast(
            event_type=OrbitronEvent.ERROR_REPORT,
            data={
                "component": component,
                "error": error,
                "severity": severity,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    # ── DATABASE (REST) ───────────────────────────────────────────────────────

    async def get_latest_signals(
        self,
        symbol: str | None = None,
        signal_type: str | None = None,
        limit: int = 10,
        status: str = "active",
    ) -> list[TradingSignal]:
        """
        Fetch latest trading signals from Orbitron.

        Args:
            symbol: Filter by symbol (e.g. "BTCUSD")
            signal_type: "buy" | "sell" | "hold"
            limit: Max records to return
            status: "active" | "expired" | "triggered"

        Returns:
            List of TradingSignal objects
        """
        params: dict[str, str] = {
            "limit": str(limit),
            "order": "created_at.desc",
            "status": f"eq.{status}",
        }
        if symbol:
            params["symbol"] = f"eq.{symbol}"
        if signal_type:
            params["signal_type"] = f"eq.{signal_type}"

        async with httpx.AsyncClient(timeout=self._timeout) as c:
            try:
                r = await c.get(
                    f"{self._base}/rest/v1/trading_signals",
                    params=params,
                    headers=self._headers(),
                )
                r.raise_for_status()
                rows = r.json()
                signals = [TradingSignal.from_row(row) for row in rows]
                log.debug("Fetched %d signals from Orbitron", len(signals))
                return signals
            except Exception as e:
                self._errors += 1
                log.warning("Failed to fetch signals: %s", e)
                return []

    async def get_recent_events(
        self,
        source_platform: str | None = None,
        event_type: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Fetch recent ecosystem events from platform_sync_events."""
        params: dict[str, str] = {
            "limit": str(limit),
            "order": "created_at.desc",
            "select": "source_platform,event_type,created_at,data",
        }
        if source_platform:
            params["source_platform"] = f"eq.{source_platform}"
        if event_type:
            params["event_type"] = f"eq.{event_type}"

        async with httpx.AsyncClient(timeout=self._timeout) as c:
            try:
                r = await c.get(
                    f"{self._base}/rest/v1/platform_sync_events",
                    params=params,
                    headers=self._headers(),
                )
                r.raise_for_status()
                return r.json()
            except Exception as e:
                self._errors += 1
                log.warning("Failed to fetch events: %s", e)
                return []

    # ── STATS ─────────────────────────────────────────────────────────────────

    @property
    def stats(self) -> dict:
        return {
            "sent": self._sent,
            "errors": self._errors,
            "uptime_s": round(time.time() - self._start, 1),
            "orbitron_url": self._base,
        }
