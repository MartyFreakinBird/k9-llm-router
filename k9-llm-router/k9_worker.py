"""
k9_worker.py — K-9 Celery Worker (Sprint 4b)
=============================================
Offloads CPU/LLM-heavy orchestrator commands to background workers so the
orchestrator request path never blocks.

Heavy commands (async via this worker):
  - run_quant_analysis   (LLM, 30s timeout)
  - run_trading_signal   (LLM, 30s timeout)
  - call_mcp_tool        (MCP, 15s timeout)

Light commands remain in-line in k9_orchestrator.py.

Architecture:
  Orchestrator /orchestrator/command
    → enqueues task_id in Redis Streams (k9_task_queue)
    → fires Celery task: k9_worker.run_command.delay(command, params, task_id)
    → returns {task_id, status: "queued"} immediately (202)
  Celery worker:
    → executes command via HTTP to correct service
    → writes result back to Redis via k9_task_queue
  Caller polls GET /orchestrator/tasks/{task_id} for completion

Run:
  celery -A k9_worker worker --loglevel=info --concurrency=4 -Q k9-heavy

Requirements (add to requirements.txt):
  celery[redis]>=5.3.0
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx
from celery import Celery
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("k9.worker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [worker] %(levelname)s %(message)s")

# ── Config ────────────────────────────────────────────────────────────────────
REDIS_URL         = os.getenv("REDIS_URL",          "redis://localhost:6379/0")
LLM_ROUTER_URL    = os.getenv("LLM_ROUTER_URL",     "http://localhost:8765")
MCP_MANAGER_URL   = os.getenv("K9_MCP_MANAGER_URL", "http://localhost:3030")
PAYMASTER_URL     = os.getenv("K9_PAYMASTER_URL",   "http://localhost:9002")
ORCHESTRATOR_URL  = os.getenv("K9_ORCHESTRATOR_URL","http://localhost:8744")

# ── Celery app ────────────────────────────────────────────────────────────────
celery_app = Celery(
    "k9_worker",
    broker=REDIS_URL,
    backend=REDIS_URL,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,                  # re-queue if worker dies mid-task
    worker_prefetch_multiplier=1,         # one task at a time per worker slot
    task_track_started=True,
    task_routes={
        "k9_worker.run_heavy_command": {"queue": "k9-heavy"},
    },
    broker_connection_retry_on_startup=True,
)

# ── Result writer (sync wrapper for Redis Streams) ────────────────────────────
def _write_result_to_redis(task_id: str, status: str, result: Any, latency_ms: float) -> None:
    """
    Write task result directly to Redis so the orchestrator's
    GET /orchestrator/tasks/{task_id} endpoint can return it.
    """
    import redis as _redis, json, time as _time

    r = _redis.from_url(REDIS_URL, decode_responses=True)
    key = f"k9:task:result:{task_id}"
    payload = {
        "task_id":    task_id,
        "status":     status,
        "result":     json.dumps(result),
        "latency_ms": str(round(latency_ms, 1)),
        "completed_at": str(_time.time()),
    }
    ttl = int(os.getenv("TASK_RESULT_TTL_S", "3600"))
    r.hset(key, mapping=payload)
    r.expire(key, ttl)
    log.info("[result] task=%s status=%s latency=%.0fms", task_id, status, latency_ms)


# ── HTTP helpers (sync — Celery tasks run synchronously) ─────────────────────
def _post_sync(url: str, payload: dict, timeout: float = 35.0) -> dict:
    import httpx as _httpx
    with _httpx.Client(timeout=timeout) as c:
        r = c.post(url, json=payload)
        r.raise_for_status()
        return r.json()


# ── Celery Task ───────────────────────────────────────────────────────────────
@celery_app.task(
    name="k9_worker.run_heavy_command",
    bind=True,
    max_retries=2,
    default_retry_delay=5,
    soft_time_limit=60,
    time_limit=90,
)
def run_heavy_command(self, command: str, params: dict, task_id: str) -> dict:
    """
    Execute a heavy K-9 command off the orchestrator request path.
    Writes result back to Redis; orchestrator polls via task_id.
    """
    t0 = time.time()
    log.info("[task] START command=%s task_id=%s", command, task_id)

    try:
        result = _dispatch(command, params)
        latency = (time.time() - t0) * 1000
        _write_result_to_redis(task_id, "done", result, latency)
        log.info("[task] DONE  command=%s task_id=%s latency=%.0fms", command, task_id, latency)
        return {"task_id": task_id, "status": "done", "result": result}

    except Exception as exc:
        latency = (time.time() - t0) * 1000
        err = str(exc)
        log.error("[task] FAIL  command=%s task_id=%s error=%s", command, task_id, err)
        _write_result_to_redis(task_id, "failed", {"error": err}, latency)
        # Retry transient errors
        if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
            raise self.retry(exc=exc)
        return {"task_id": task_id, "status": "failed", "error": err}


def _dispatch(command: str, params: dict) -> dict:
    """Route command to appropriate service."""
    if command == "run_quant_analysis":
        symbol = params.get("symbol", "BTCUSD")
        prompt = params.get(
            "prompt",
            f"Provide a concise quant regime analysis for {symbol}. Include trend, momentum, and key levels."
        )
        return _post_sync(f"{LLM_ROUTER_URL}/route", {
            "task_type": "quant_analysis",
            "messages":  [{"role": "user", "content": prompt}],
            "component": "k9-worker",
            "symbol":    symbol,
        }, timeout=35.0)

    elif command == "run_trading_signal":
        symbol = params.get("symbol", "BTCUSD")
        tf     = params.get("timeframe", "4H")
        prompt = params.get(
            "prompt",
            f"Generate a trading signal for {symbol} on {tf} timeframe. "
            "Include direction (buy/sell/hold), confidence, key S/R levels, and reasoning."
        )
        return _post_sync(f"{LLM_ROUTER_URL}/route", {
            "task_type": "trading_signal",
            "messages":  [{"role": "user", "content": prompt}],
            "component": "k9-worker",
            "symbol":    symbol,
        }, timeout=35.0)

    elif command == "call_mcp_tool":
        tool_id = params.get("tool_id", "")
        method  = params.get("method", "")
        if not tool_id or not method:
            raise ValueError("tool_id and method required")
        return _post_sync(f"{MCP_MANAGER_URL}/tools/call", {
            "tool_id":  tool_id,
            "method":   method,
            "params":   params.get("params", {}),
            "agent_id": "k9-worker",
        }, timeout=20.0)

    else:
        raise ValueError(f"Unknown heavy command: {command}")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    celery_app.worker_main(
        argv=["worker", "--loglevel=info", "--concurrency=4", "-Q", "k9-heavy"]
    )
