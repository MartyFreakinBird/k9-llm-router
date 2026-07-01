"""
PackAI Compute Node Agent
─────────────────────────────────────────────────────────────────────────────
Windows 11 / WSL2 compute node that registers with Orbitron and
routes PackAI tasks through k9-llm-router.

Port: 8766  (k9-llm-router: 8765, k9-orchestrator: 8744)

Start:
  uvicorn agent:app --host 0.0.0.0 --port 8766
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
import socket
import time
import asyncio
import logging

import psutil
import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("packai-node")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [packai-node] %(levelname)s %(message)s")

LLM_ROUTER_URL  = os.getenv("LLM_ROUTER_URL", "http://localhost:8765")
ORBITRON_URL    = os.getenv("ORBITRON_URL", "https://ziqenqqgnqxqrazmjohs.supabase.co")
ORBITRON_ANON_KEY = os.getenv("ORBITRON_ANON_KEY", "")
ORBITRON_AUTH_TOKEN = os.getenv("ORBITRON_AUTH_TOKEN", "")
NODE_PORT       = int(os.getenv("NODE_PORT", "8766"))

_start = time.time()


def _orbitron_headers():
    h = {"apikey": ORBITRON_ANON_KEY, "Content-Type": "application/json"}
    if ORBITRON_AUTH_TOKEN:
        h["Authorization"] = f"Bearer {ORBITRON_AUTH_TOKEN}"
    return h


async def orbitron_broadcast(event_type: str, data: dict):
    """Fire-and-forget event to Orbitron platform-sync."""
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(
                f"{ORBITRON_URL}/functions/v1/platform-sync",
                json={"action": "broadcast_event", "source_platform": "K9_AGENT",
                      "event_type": event_type, "data": data},
                headers=_orbitron_headers(),
            )
    except Exception as e:
        log.warning("Orbitron broadcast failed: %s", e)


app = FastAPI(
    title="PackAI Compute Node",
    version="0.3.0",
    description="Windows 11 PackAI node — routes tasks through k9-llm-router, reports to Orbitron",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
async def startup():
    asyncio.create_task(orbitron_broadcast("PLATFORM_ONLINE", {
        "component": "packai-node",
        "hostname": socket.gethostname(),
        "port": NODE_PORT,
        "router_url": LLM_ROUTER_URL,
        "sprint": 3,
    }))
    # Heartbeat every 60s
    async def _hb():
        while True:
            await asyncio.sleep(60)
            await orbitron_broadcast("AGENT_HEARTBEAT", {
                "component": "packai-node",
                "status": "online",
                "cpu_pct": psutil.cpu_percent(),
                "mem_pct": psutil.virtual_memory().percent,
                "uptime_s": round(time.time() - _start, 1),
            })
    asyncio.create_task(_hb())


@app.get("/health")
def health():
    return {
        "status": "online",
        "component": "packai-node",
        "hostname": socket.gethostname(),
        "cpu_pct": psutil.cpu_percent(),
        "mem_pct": psutil.virtual_memory().percent,
        "uptime_s": round(time.time() - _start, 1),
        "router_url": LLM_ROUTER_URL,
        "sprint": 3,
    }


@app.post("/task")
async def execute_task(task: dict):
    """
    Receive a PackAI task and route it through k9-llm-router.
    
    Expected task format:
    {
        "id": "task-uuid",
        "task_type": "scout_tutor" | "finance_coach" | ...,
        "messages": [...],
        "system": "optional system prompt",
        "component": "packai-scout"
    }
    """
    task_id   = task.get("id", "unknown")
    task_type = task.get("task_type", "default")
    messages  = task.get("messages", [])
    component = task.get("component", "packai-node")

    log.info("Task received: id=%s type=%s", task_id, task_type)

    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                f"{LLM_ROUTER_URL}/route",
                json={
                    "task_type": task_type,
                    "messages": messages,
                    "system": task.get("system"),
                    "max_tokens": task.get("max_tokens", 1000),
                    "component": component,
                },
            )
            r.raise_for_status()
            result = r.json()

        await orbitron_broadcast("COMMAND_EXECUTED", {
            "task_id": task_id,
            "task_type": task_type,
            "model_used": result.get("model_used"),
            "latency_ms": result.get("latency_ms"),
            "component": component,
        })

        return {
            "status": "completed",
            "task_id": task_id,
            "result": result.get("content"),
            "model_used": result.get("model_used"),
            "latency_ms": result.get("latency_ms"),
        }

    except Exception as e:
        log.error("Task %s failed: %s", task_id, e)
        await orbitron_broadcast("COMMAND_FAILED", {
            "task_id": task_id,
            "error": str(e),
            "component": component,
        })
        return {"status": "failed", "task_id": task_id, "error": str(e)}


# ── Swarm API contract ────────────────────────────────────────────────────────

@app.get("/swarm/health")
def swarm_health():
    return {
        "agent_id": "packai-node",
        "status": "online",
        "sprint": 3,
        "phase": 1,
        "cpu_pct": psutil.cpu_percent(),
        "mem_pct": psutil.virtual_memory().percent,
    }


@app.get("/swarm/identity")
def swarm_identity():
    return {
        "role": "packai-node",
        "hostname": socket.gethostname(),
        "port": NODE_PORT,
        "capabilities": ["task_execution", "packai_scout", "packai_finance"],
        "router_url": LLM_ROUTER_URL,
    }


@app.get("/swarm/peers")
def swarm_peers():
    return {"peers": [{"role": "k9-llm-router", "url": LLM_ROUTER_URL}]}


@app.post("/swarm/message")
async def swarm_message(payload: dict):
    if payload.get("content", {}).get("action") == "execute_task":
        result = await execute_task(payload["content"].get("task", {}))
        return {"ok": True, "result": result}
    return {"ok": True, "ack": payload.get("msg_id", "unknown")}


@app.post("/swarm/peer/register")
def swarm_peer_register(payload: dict):
    return {"ok": True, "note": "Peer registry delegated to k9-orchestrator"}


@app.get("/swarm/stats")
def swarm_stats():
    return {
        "uptime_s": round(time.time() - _start, 1),
        "sprint": 3,
    }


if __name__ == "__main__":
    ip = socket.gethostbyname(socket.gethostname())
    print(f"""
╔══════════════════════════════════════════════════════╗
║       PackAI COMPUTE NODE  ·  Sprint 3              ║
╚══════════════════════════════════════════════════════╝
  Host       : {socket.gethostname()} ({ip})
  Port       : {NODE_PORT}
  Router     : {LLM_ROUTER_URL}
  Orbitron   : {ORBITRON_URL}

  Health     : http://{ip}:{NODE_PORT}/health
  Swarm API  : http://{ip}:{NODE_PORT}/swarm/health
  Task API   : http://{ip}:{NODE_PORT}/task

  Press Ctrl+C to stop.
""")
    uvicorn.run("agent:app", host="0.0.0.0", port=NODE_PORT, reload=False)
