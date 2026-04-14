"""
k9_orchestrator.py
─────────────────────────────────────────────────────────────────────────────
K-9 Orchestrator — Sprint 3 (Giant Steps Phase 2 bridge)

Port: 8744

This is the coordination layer of the K-9 swarm. It:
  - Accepts commands from n8n (/orchestrator/command)
  - Dispatches tasks to LLM Router, Paymaster, MCP Manager
  - Maintains a lightweight in-memory task queue (Sprint 4: Celery/NATS)
  - Broadcasts task outcomes to Orbitron event bus
  - Heartbeats to Orbitron as K9_ORCHESTRATOR

K-9 Layer: L3 Coordination (sits above L2 Discovery, below L4 Governance)

Start:
  python3 k9_orchestrator.py
  # or via launch-economic-stack.sh start

Usage (from n8n or any HTTP client):
  POST http://localhost:8744/orchestrator/command
    {"command": "run_quant_analysis", "params": {"symbol": "BTCUSD"}}

  GET  http://localhost:8744/swarm/health
  GET  http://localhost:8744/orchestrator/tasks
  POST http://localhost:8744/orchestrator/dispatch

# L2 — orchestration only. L1 Advisory: DeFi execution requires sign-off.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations
from contextlib import asynccontextmanager

import asyncio
import logging
import os
import time
import uuid
from collections import deque
from k9_task_queue import get_task_queue, BaseTaskQueue

# -- Sprint 4b: Celery worker offload -----------------------------------------
# Heavy commands (LLM/MCP, 15-30s) fire via Celery and return 202 + task_id.
# Light commands (<5s) remain synchronous and return 200 + inline result.
_HEAVY_COMMANDS = {"run_quant_analysis", "run_trading_signal", "call_mcp_tool"}

def _fire_celery(command: str, params: dict, task_id: str) -> bool:
    """
    Fire a Celery task. Returns True on success, False if Celery/Redis
    is unavailable -- caller falls back to in-line execution.
    """
    try:
        from k9_worker import run_heavy_command
        run_heavy_command.apply_async(
            args=[command, params, task_id],
            queue="k9-heavy",
            countdown=0,
        )
        log.info("[celery] fired command=%s task_id=%s", command, task_id)
        return True
    except Exception as e:
        log.warning("[celery] unavailable (%s) -- falling back to inline", e)
        return False

from datetime import datetime, timezone
from typing import Any

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [k9-orchestrator] %(levelname)s %(message)s",
)
log = logging.getLogger("k9-orchestrator")

# ── CONFIG ─────────────────────────────────────────────────────────────────────
HOST              = os.getenv("K9_ORCHESTRATOR_HOST", "0.0.0.0")
PORT              = int(os.getenv("K9_ORCHESTRATOR_PORT", "8744"))
LLM_ROUTER_URL    = os.getenv("LLM_ROUTER_URL",   "http://localhost:8765")
PAYMASTER_URL     = os.getenv("K9_PAYMASTER_URL",  "http://localhost:9002")
MCP_MANAGER_URL   = os.getenv("K9_MCP_MANAGER_URL","http://localhost:3030")
ORBITRON_URL      = os.getenv("ORBITRON_URL",      "https://ziqenqqgnqxqrazmjohs.supabase.co")
ORBITRON_ANON_KEY = os.getenv("ORBITRON_ANON_KEY", "")
ORBITRON_AUTH_TOKEN = os.getenv("ORBITRON_AUTH_TOKEN", "")

# ── TASK QUEUE (Sprint 4: Redis Streams — falls back to in-memory) ─────────────
_queue: BaseTaskQueue | None = None
_start_time = time.time()


# ── MODELS ────────────────────────────────────────────────────────────────────
class CommandRequest(BaseModel):
    command: str
    params: dict = Field(default_factory=dict)
    source: str = "api"
    priority: int = 5  # 1=high, 10=low

class DispatchRequest(BaseModel):
    task_type: str
    payload: dict = Field(default_factory=dict)
    target: str = "llm_router"  # "llm_router" | "mcp_manager" | "paymaster"
    component: str = "k9-orchestrator"


# ── ORBITRON ──────────────────────────────────────────────────────────────────
def _orb_headers():
    h = {"apikey": ORBITRON_ANON_KEY, "Content-Type": "application/json"}
    if ORBITRON_AUTH_TOKEN:
        h["Authorization"] = f"Bearer {ORBITRON_AUTH_TOKEN}"
    return h

async def orbitron_broadcast(event_type: str, data: dict) -> None:
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(
                f"{ORBITRON_URL}/functions/v1/platform-sync",
                json={"action": "broadcast_event", "source_platform": "K9_AGENT",
                      "event_type": event_type, "data": data},
                headers=_orb_headers(),
            )
    except Exception as e:
        log.debug("Orbitron broadcast failed: %s", e)


# ── COMMAND DISPATCH TABLE ────────────────────────────────────────────────────
# Maps command name → handler coroutine
COMMAND_REGISTRY: dict[str, Any] = {}

def command(name: str):
    """Decorator to register a command handler."""
    def decorator(fn):
        COMMAND_REGISTRY[name] = fn
        return fn
    return decorator


@command("run_quant_analysis")
async def cmd_quant_analysis(params: dict) -> dict:
    """Route a quant analysis task through the LLM router."""
    symbol  = params.get("symbol", "BTCUSD")
    prompt  = params.get("prompt", f"Provide a concise quant regime analysis for {symbol}. Include trend, momentum, and key levels.")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{LLM_ROUTER_URL}/route", json={
            "task_type":  "quant_analysis",
            "messages":   [{"role": "user", "content": prompt}],
            "component":  "k9-orchestrator",
            "symbol":     symbol,
        })
        r.raise_for_status()
        return r.json()


@command("run_trading_signal")
async def cmd_trading_signal(params: dict) -> dict:
    """Generate a trading signal for a symbol."""
    symbol = params.get("symbol", "BTCUSD")
    tf     = params.get("timeframe", "4H")
    prompt = params.get("prompt", (
        f"Generate a trading signal for {symbol} on {tf} timeframe. "
        "Include direction (buy/sell/hold), confidence, key S/R levels, and reasoning."
    ))
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{LLM_ROUTER_URL}/route", json={
            "task_type": "trading_signal",
            "messages":  [{"role": "user", "content": prompt}],
            "component": "k9-orchestrator",
            "symbol":    symbol,
        })
        r.raise_for_status()
        return r.json()


@command("paymaster_summary")
async def cmd_paymaster_summary(params: dict) -> dict:
    """Fetch current Paymaster budget summary."""
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.get(f"{PAYMASTER_URL}/paymaster/summary")
        r.raise_for_status()
        return r.json()


@command("list_mcp_tools")
async def cmd_list_mcp_tools(params: dict) -> dict:
    """List all available MCP tools from the manager."""
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.get(f"{MCP_MANAGER_URL}/tools")
        r.raise_for_status()
        return r.json()


@command("call_mcp_tool")
async def cmd_call_mcp_tool(params: dict) -> dict:
    """Execute an MCP tool via the manager. L2 — Paymaster-gated."""
    tool_id = params.get("tool_id", "")
    method  = params.get("method", "")
    if not tool_id or not method:
        raise ValueError("tool_id and method required")
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{MCP_MANAGER_URL}/tools/call", json={
            "tool_id":  tool_id,
            "method":   method,
            "params":   params.get("params", {}),
            "agent_id": "k9-orchestrator",
        })
        r.raise_for_status()
        return r.json()


@command("health_check")
async def cmd_health_check(params: dict) -> dict:
    """Check health of all K-9 stack components."""
    services = {
        "llm_router":  f"{LLM_ROUTER_URL}/swarm/health",
        "paymaster":   f"{PAYMASTER_URL}/paymaster/summary",
        "mcp_manager": f"{MCP_MANAGER_URL}/",
    }
    results = {}
    async with httpx.AsyncClient(timeout=3) as c:
        for name, url in services.items():
            try:
                r = await c.get(url)
                results[name] = {"status": "online" if r.status_code < 400 else "degraded"}
            except Exception:
                results[name] = {"status": "offline"}
    return results


@command("broadcast_signal")
async def cmd_broadcast_signal(params: dict) -> dict:
    """Broadcast a SIGNAL_GENERATED event to Orbitron. L1 Advisory."""
    signal_data = {
        "signal_id":     f"orch_{int(time.time())}",
        "symbol":        params.get("symbol", "UNKNOWN"),
        "signal_type":   params.get("signal_type", "neutral"),
        "confidence":    params.get("confidence", 0.5),
        "source":        "k9-orchestrator",
        "advisory_note": "L1: signal for analysis — no order execution",
        "timestamp":     datetime.now(timezone.utc).isoformat(),
    }
    await orbitron_broadcast("SIGNAL_GENERATED", signal_data)
    return {"broadcast": True, "signal": signal_data}



@command("clob_request")
async def cmd_clob_request(params: dict) -> dict:
    """
    Submit a CLOB order request to Paymaster L1 queue for human review.

    # L1 ADVISORY: Creates a pending order — does NOT execute.
    # Human must approve via POST /paymaster/clob/{rid}/sign.
    """
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.post(f"{PAYMASTER_URL}/paymaster/clob/request", json=params)
        r.raise_for_status()
        return r.json()


@command("clob_pending")
async def cmd_clob_pending(params: dict) -> dict:
    """List all CLOB orders pending L1 human review."""
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.get(f"{PAYMASTER_URL}/paymaster/clob/pending")
        r.raise_for_status()
        return r.json()

# ── FASTAPI APP ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="K-9 Orchestrator",
    version="0.1.0",
    description="Sprint 3 — L3 Coordination layer",
    lifespan=lifespan
)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


@app.get("/")
async def root():
    return {
        "service": "k9-orchestrator",
        "version": "0.1.0",
        "sprint":  3,
        "layer":   "L3-Coordination",
        "commands": list(COMMAND_REGISTRY.keys()),
        "docs":    "/docs",
    }


@app.get("/swarm/health")
async def health():
    uptime = round(time.time() - _start_time, 1)
    return {
        "status":       "online",
        "service":      "k9-orchestrator",
        "port":         PORT,
        "layer":        "L3-Coordination",
        "uptime_s":     uptime,
        "tasks_queued": (await _queue.stats()).get("queued", 0) if _queue else 0,
        "tasks_done":   0,
        "commands":     list(COMMAND_REGISTRY.keys()),
        "timestamp":    datetime.now(timezone.utc).isoformat(),
    }


@app.post("/orchestrator/command")
async def execute_command(req: CommandRequest):
    """
    Execute a named command via the orchestrator.

    HEAVY commands (run_quant_analysis, run_trading_signal, call_mcp_tool):
      Returns 202 immediately with task_id.
      Celery worker (k9_worker.py) executes in background.
      Poll: GET /orchestrator/tasks/{task_id}
      Falls back to inline execution if Celery/Redis is unavailable.

    LIGHT commands (all others):
      Returns 200 synchronously with inline result.

    n8n: POST http://localhost:8744/orchestrator/command
      {"command": "run_quant_analysis", "params": {"symbol": "BTCUSD"}}
    """
    handler = COMMAND_REGISTRY.get(req.command)
    if not handler:
        raise HTTPException(
            404,
            "Unknown command: '{}'. Available: {}".format(req.command, list(COMMAND_REGISTRY.keys()))
        )

    task_id = await _queue.enqueue(req.command, req.params, req.source, req.priority)
    log.info("COMMAND %s (task=%s source=%s heavy=%s)",
             req.command, task_id, req.source, req.command in _HEAVY_COMMANDS)

    # Heavy path: offload to Celery, return 202 immediately
    if req.command in _HEAVY_COMMANDS:
        fired = _fire_celery(req.command, req.params, task_id)
        if fired:
            await _queue.mark_running(task_id)
            return JSONResponse(status_code=202, content={
                "task_id":  task_id,
                "command":  req.command,
                "status":   "queued",
                "poll_url": "/orchestrator/tasks/{}".format(task_id),
                "note":     "Heavy command offloaded to Celery worker. Poll poll_url for result.",
            })
        log.warning("[celery-fallback] command=%s executing inline (degraded mode)", req.command)

    # Light path (or Celery fallback): execute inline
    try:
        await _queue.mark_running(task_id)
        t0 = time.time()
        result = await handler(req.params)
        latency = round((time.time() - t0) * 1000, 1)
        await _queue.mark_done(task_id, result, latency)

        asyncio.create_task(orbitron_broadcast("COMMAND_EXECUTED", {
            "task_id":    task_id,
            "command":    req.command,
            "source":     req.source,
            "latency_ms": latency,
        }))

        return {
            "task_id":    task_id,
            "command":    req.command,
            "status":     "done",
            "latency_ms": latency,
            "result":     result,
        }
    except Exception as e:
        await _queue.mark_failed(task_id, str(e))
        log.error("Command %s failed: %s", req.command, e)
        asyncio.create_task(orbitron_broadcast("COMMAND_FAILED", {
            "task_id": task_id, "command": req.command, "error": str(e),
        }))
        raise HTTPException(500, "Command failed: {}".format(e))


@app.post("/orchestrator/dispatch")
async def dispatch_task(req: DispatchRequest):
    """
    Raw task dispatch to a specific K-9 layer service.
    Lower-level than /orchestrator/command — for direct routing.
    """
    targets = {
        "llm_router":  (f"{LLM_ROUTER_URL}/route",          30),
        "mcp_manager": (f"{MCP_MANAGER_URL}/tools/call",     15),
        "paymaster":   (f"{PAYMASTER_URL}/paymaster/gate",    5),
    }
    if req.target not in targets:
        raise HTTPException(400, f"Unknown target: {req.target}. Choose: {list(targets)}")

    url, timeout = targets[req.target]
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(url, json={"task_type": req.task_type, **req.payload})
            r.raise_for_status()
            return {"target": req.target, "task_type": req.task_type, "result": r.json()}
    except Exception as e:
        raise HTTPException(503, f"Dispatch to {req.target} failed: {e}")


@app.get("/orchestrator/tasks")
async def list_tasks(limit: int = 20):
    """List recent tasks from the in-memory queue."""
    tasks = await _queue.list_tasks(limit)
    queue_stats = await _queue.stats()
    return {
        "tasks": tasks,
        **queue_stats,
    }


@app.get("/orchestrator/tasks/{task_id}")
async def get_task(task_id: str):
    """Get result for a specific task."""
    result = await _queue.get_result(task_id)
    if not result:
        raise HTTPException(404, f"Task {task_id} not found")
    return result


# ── BACKGROUND HEARTBEAT ──────────────────────────────────────────────────────
async def _heartbeat_loop():
    await asyncio.sleep(10)  # initial delay
    while True:
        await orbitron_broadcast("AGENT_HEARTBEAT", {
            "component":     "k9-orchestrator",
            "status":        "online",
            "layer":         "L3-Coordination",
            "tasks_queued":  (await _queue.stats()).get("queued", 0) if _queue else 0,
            "tasks_done":    0,  # see /orchestrator/tasks for full history
            "uptime_s":      round(time.time() - _start_time, 1),
            "sprint":        3,
            "timestamp":     datetime.now(timezone.utc).isoformat(),
        })
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _queue
    _queue = get_task_queue()
    asyncio.create_task(_heartbeat_loop(), name="orchestrator-heartbeat")
    log.info("K-9 Orchestrator online — port %d — %d commands registered",
             PORT, len(COMMAND_REGISTRY))
    await orbitron_broadcast("PLATFORM_ONLINE", {
        "platform":     "k9-orchestrator",
        "type":         "coordination_layer",
        "layer":        "L3",
        "port":         PORT,
        "commands":     list(COMMAND_REGISTRY.keys()),
        "sprint":       3,
    })
    yield
    log.info("K-9 Orchestrator shutting down.")


if __name__ == "__main__":
    uvicorn.run(
        "k9_orchestrator:app",
        host=HOST,
        port=PORT,
        reload=False,
        log_level="info",
    )
