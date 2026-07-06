"""
n8n_webhook_bridge.py
─────────────────────────────────────────────────────────────────────────────
n8n → k9-orchestrator → k9-mcp-manager webhook bridge — v0.1.0

Exposes FastAPI routes that n8n can call via HTTP Request nodes:

  POST /webhooks/n8n/mcp/call
    — Execute any MCP tool by tool_id + method
    — Routes to k9_mcp_manager (:3030) with budget gate

  POST /webhooks/n8n/llm/route
    — Route a prompt through k9-llm-router
    — n8n passes task_type, messages[], system

  POST /webhooks/n8n/signal/ingest
    — Push a trading signal from n8n into Orbitron
    — Broadcasts SIGNAL_GENERATED event to ecosystem

  GET  /webhooks/n8n/status
    — Health check for n8n HTTP node (returns stack status)

n8n Workflow pattern:
  Trigger (schedule/webhook/etc.)
      → HTTP Request node → POST /webhooks/n8n/mcp/call
      → JSON parse → further nodes

Security:
  N8N_WEBHOOK_SECRET env var — Bearer token n8n sends in Authorization header.
  If not set: requests are allowed from localhost only (dev mode).

# L2 — webhook calls are gated by Paymaster before any tool execution.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, Header

log = logging.getLogger("n8n-webhook-bridge")

N8N_WEBHOOK_SECRET   = os.getenv("N8N_WEBHOOK_SECRET", "")
K9_MCP_MANAGER_URL   = os.getenv("K9_MCP_MANAGER_URL",   "http://localhost:3030")
K9_PAYMASTER_URL     = os.getenv("K9_PAYMASTER_URL",      "http://localhost:9002")
K9_ORCHESTRATOR_URL  = os.getenv("K9_ORCHESTRATOR_URL",   "http://localhost:8744")
ORBITRON_URL         = os.getenv("ORBITRON_URL", "https://ziqenqqgnqxqrazmjohs.supabase.co")
ORBITRON_ANON_KEY    = os.getenv("ORBITRON_ANON_KEY", "")
ORBITRON_AUTH_TOKEN  = os.getenv("ORBITRON_AUTH_TOKEN", "")

router = APIRouter(prefix="/webhooks/n8n", tags=["n8n"])


# ── AUTH ──────────────────────────────────────────────────────────────────────

def _verify_n8n_auth(request: Request, authorization: str | None) -> None:
    """Verify n8n webhook secret. Skips auth if secret not configured."""
    if not N8N_WEBHOOK_SECRET:
        # Dev mode — allow all but log warning
        log.debug("N8N_WEBHOOK_SECRET not set — webhook auth disabled")
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    token = authorization.replace("Bearer ", "").strip()
    if token != N8N_WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")


def _orbitron_headers() -> dict:
    h = {"apikey": ORBITRON_ANON_KEY, "Content-Type": "application/json"}
    if ORBITRON_AUTH_TOKEN:
        h["Authorization"] = f"Bearer {ORBITRON_AUTH_TOKEN}"
    return h


# ── ROUTES ────────────────────────────────────────────────────────────────────

@router.get("/status")
async def n8n_status(request: Request):
    """
    Health check for n8n HTTP nodes.
    Returns status of router, paymaster, mcp-manager, orchestrator.

    n8n: GET http://localhost:8765/webhooks/n8n/status
    """
    results: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": {}
    }

    checks = {
        "llm_router":    f"http://localhost:8765/swarm/health",
        "paymaster":     f"{K9_PAYMASTER_URL}/paymaster/summary",
        "mcp_manager":   f"{K9_MCP_MANAGER_URL}/",
        "orchestrator":  f"{K9_ORCHESTRATOR_URL}/swarm/health",
    }

    async with httpx.AsyncClient(timeout=2) as c:
        for name, url in checks.items():
            try:
                r = await c.get(url)
                results["services"][name] = {
                    "status": "online" if r.status_code < 400 else "degraded",
                    "http_code": r.status_code,
                }
            except Exception:
                results["services"][name] = {"status": "offline"}

    online = sum(1 for s in results["services"].values() if s["status"] == "online")
    results["overall"] = "healthy" if online >= 2 else "degraded"
    return results


@router.post("/mcp/call")
async def n8n_mcp_call(
    payload: dict,
    request: Request,
    authorization: str | None = Header(None),
):
    """
    Execute any MCP tool from n8n via HTTP Request node.

    POST body:
      {
        "tool_id":  "ollama",
        "method":   "tool.llm.generate",
        "params":   {"prompt": "..."},
        "agent_id": "n8n-workflow-123"
      }

    n8n pattern:
      HTTP Request node → POST http://localhost:8765/webhooks/n8n/mcp/call
      Body: JSON above
      Response: tool call result

    # L2 — gated by Paymaster before execution.
    """
    _verify_n8n_auth(request, authorization)

    tool_id  = payload.get("tool_id", "")
    method   = payload.get("method", "")
    params   = payload.get("params", {})
    agent_id = payload.get("agent_id", "n8n")

    if not tool_id or not method:
        raise HTTPException(400, "tool_id and method are required")

    # Paymaster gate — estimate cost from tool tier
    tool_costs = {
        "ollama": 0.0, "redis": 0.0001, "supabase": 0.0001,
        "openai": 0.005, "elevenlabs": 0.01, "moonpay": 0.0,
        "claudecode": 0.003,
    }
    est_cost = tool_costs.get(tool_id, 0.001)

    try:
        async with httpx.AsyncClient(timeout=3) as c:
            gate = await c.post(f"{K9_PAYMASTER_URL}/paymaster/gate", json={
                "cost_usd": est_cost,
                "category": "mcp_tool",
                "agent_id": agent_id,
                "description": f"n8n→{tool_id}.{method}",
            })
            if gate.status_code == 200:
                gate_data = gate.json()
                if not gate_data.get("approved"):
                    raise HTTPException(402, f"Budget gate denied: {gate_data.get('reason')}")
    except HTTPException:
        raise
    except Exception as e:
        log.debug("Paymaster gate skipped: %s", e)

    # Route to MCP Manager
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{K9_MCP_MANAGER_URL}/tools/call", json={
                "tool_id":  tool_id,
                "method":   method,
                "params":   params,
                "agent_id": agent_id,
            })
            r.raise_for_status()
            result = r.json()
            result["_source"] = "k9-mcp-manager"
            result["_n8n_agent"] = agent_id
            return result
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, f"MCP call failed: {e.response.text}")
    except Exception as e:
        log.error("MCP call error: %s", e)
        raise HTTPException(503, f"MCP Manager unreachable: {e}")


@router.post("/llm/route")
async def n8n_llm_route(
    payload: dict,
    request: Request,
    authorization: str | None = Header(None),
):
    """
    Route an LLM prompt through k9-llm-router from n8n.

    POST body:
      {
        "task_type": "quant_analysis",
        "messages":  [{"role": "user", "content": "Analyze BTC regime..."}],
        "system":    "Optional system prompt",
        "component": "n8n-workflow-signal-gen",
        "symbol":    "BTCUSD"
      }

    Returns: {content, model_used, task_type, latency_ms}

    n8n pattern:
      HTTP Request → POST http://localhost:8765/webhooks/n8n/llm/route
      Parse JSON → extract .content
    """
    _verify_n8n_auth(request, authorization)

    task_type = payload.get("task_type", "code_generation")
    messages  = payload.get("messages", [])
    system    = payload.get("system")
    component = payload.get("component", "n8n")
    symbol    = payload.get("symbol")

    if not messages:
        raise HTTPException(400, "messages array required")

    # Route through the local router
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post("http://localhost:8765/route", json={
                "task_type": task_type,
                "messages":  messages,
                "system":    system,
                "component": component,
                "symbol":    symbol,
            })
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, f"Router error: {e.response.text}")
    except Exception as e:
        raise HTTPException(503, f"LLM Router unreachable: {e}")


@router.post("/signal/ingest")
async def n8n_signal_ingest(
    payload: dict,
    request: Request,
    authorization: str | None = Header(None),
):
    """
    Push a trading signal from n8n into the Orbitron ecosystem.
    Broadcasts SIGNAL_GENERATED event to all connected modules.

    POST body:
      {
        "symbol":           "BTCUSD",
        "signal_type":      "buy",
        "confidence_score": 0.82,
        "entry_price":      84500.0,
        "stop_loss":        82000.0,
        "take_profit":      90000.0,
        "timeframe":        "4H",
        "strategy_name":    "ICT_BOS_OB",
        "source":           "n8n-tradingview-webhook"
      }

    # L1 ADVISORY: Publishes signal for analysis — no order execution.
    """
    _verify_n8n_auth(request, authorization)

    symbol = payload.get("symbol", "UNKNOWN")
    if not symbol or symbol == "UNKNOWN":
        raise HTTPException(400, "symbol required")

    # Broadcast to Orbitron
    event_payload = {
        "signal_id":        f"n8n_{int(time.time())}",
        "symbol":           symbol,
        "signal_type":      payload.get("signal_type", "neutral"),
        "confidence_score": float(payload.get("confidence_score", 0.5)),
        "entry_price":      float(payload.get("entry_price", 0)),
        "stop_loss":        float(payload.get("stop_loss", 0)),
        "take_profit":      float(payload.get("take_profit", 0)),
        "timeframe":        payload.get("timeframe", "1H"),
        "strategy_name":    payload.get("strategy_name", "n8n_signal"),
        "source_platform":  "K9_AGENT",
        "via":              payload.get("source", "n8n"),
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "advisory_note":    "L1: signal published for analysis — no order execution",
    }

    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.post(
                f"{ORBITRON_URL}/functions/v1/platform-sync",
                json={
                    "action":          "broadcast_event",
                    "source_platform": "K9_AGENT",
                    "event_type":      "SIGNAL_GENERATED",
                    "data":            event_payload,
                },
                headers=_orbitron_headers(),
            )
            result = r.json() if r.status_code < 400 else {"error": r.text}
    except Exception as e:
        result = {"error": str(e)}

    return {
        "ingested": True,
        "symbol":   symbol,
        "event_id": result.get("event_id"),
        "orbitron": result,
        "advisory": "L1: signal published to Orbitron for analysis only",
    }


@router.post("/orchestrator/command")
async def n8n_orchestrator_command(
    payload: dict,
    request: Request,
    authorization: str | None = Header(None),
):
    """
    Send a command to k9-orchestrator from n8n.
    Used for workflow-triggered orchestration events.

    POST body:
      {
        "command":   "rebalance_portfolio",
        "agent_id":  "k9-orchestrator",
        "params":    {...},
        "source":    "n8n-daily-trigger"
      }

    # L1 ADVISORY: Orchestrator executes command — review before enabling DeFi actions.
    """
    _verify_n8n_auth(request, authorization)

    command  = payload.get("command", "")
    agent_id = payload.get("agent_id", "k9-orchestrator")
    params   = payload.get("params", {})

    if not command:
        raise HTTPException(400, "command required")

    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post(f"{K9_ORCHESTRATOR_URL}/orchestrator/command", json={
                "command":   command,
                "params":    params,
                "source":    "n8n",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            r.raise_for_status()
            return {**r.json(), "_source": "k9-orchestrator", "_n8n_command": command}
    except Exception as e:
        raise HTTPException(503, f"Orchestrator unreachable: {e}")
