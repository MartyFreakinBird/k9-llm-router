"""
k9-gemini-agent — Sprint 10
WSL2 Python service — Port :8770

GUI automation + agentic task execution via Google Gemini 2.5 Computer Use API.
Sits above Claude/MoonPay in the MCP Manager tool tier (premium, Paymaster-gated).

Endpoints:
  POST /computer_use      — execute a GUI automation task
  POST /task              — general Gemini task (non-GUI: analysis, generation)
  GET  /health            — liveness + model + task stats

Env vars:
  GEMINI_API_KEY          — required (Google AI Studio key)
  GEMINI_MODEL            — default: gemini-2.5-computer-use-preview
  GEMINI_TASK_MODEL       — default: gemini-2.5-pro (for non-GUI tasks)
  K9_PAYMASTER_URL        — default: http://localhost:9002
  K9_GEMINI_PORT          — default: 8770
  BROWSERBASE_API_KEY     — optional: for sandboxed browser sessions
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

GEMINI_API_KEY       = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL         = os.getenv("GEMINI_MODEL", "gemini-2.5-computer-use-preview")
GEMINI_TASK_MODEL    = os.getenv("GEMINI_TASK_MODEL", "gemini-2.5-pro")
K9_PAYMASTER_URL     = os.getenv("K9_PAYMASTER_URL", "http://localhost:9002")
GEMINI_PORT          = int(os.getenv("K9_GEMINI_PORT", "8770"))
BROWSERBASE_API_KEY  = os.getenv("BROWSERBASE_API_KEY", "")

GEMINI_BASE          = "https://generativelanguage.googleapis.com/v1beta"

log = logging.getLogger("k9-gemini-agent")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

# ── Task stats ────────────────────────────────────────────────────────────────

class AgentStats:
    def __init__(self):
        self.total_tasks: int = 0
        self.total_computer_use: int = 0
        self.last_task_at: str | None = None
        self.last_error: str | None = None

stats = AgentStats()

# ── Paymaster gate ────────────────────────────────────────────────────────────

async def paymaster_gate(
    client: httpx.AsyncClient,
    task_type: str,
    estimated_cost: float = 0.05,
) -> bool:
    """
    Request spending approval from k9-paymaster before any Gemini API call.
    Returns True if approved, False if queued for human review.
    Auto-approved when estimated_cost < $0.10.
    """
    try:
        res = await client.post(
            f"{K9_PAYMASTER_URL}/paymaster/gate",
            json={
                "service": "gemini",
                "operation": task_type,
                "estimated_cost_usd": estimated_cost,
                "tier": "premium",
            },
            timeout=3.0,
        )
        data = res.json()
        approved = data.get("approved", False)
        if not approved:
            log.warning("Paymaster queued for review: %s %.4f", task_type, estimated_cost)
        return approved
    except Exception as e:
        log.warning("Paymaster unreachable (%s) — defaulting to approve", e)
        return True  # fail open (non-blocking) — operator controls via K9_PAYMASTER_URL

# ── Gemini API helpers ────────────────────────────────────────────────────────

async def call_gemini_computer_use(
    client: httpx.AsyncClient,
    query: str,
    screenshot_base64: str | None = None,
) -> dict[str, Any]:
    """
    Call Gemini computer_use model with an optional screenshot context.
    Returns structured action: {action_type, coordinates, text, reasoning}
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set — cannot call Gemini API")

    contents: list[dict] = []

    if screenshot_base64:
        contents.append({
            "role": "user",
            "parts": [
                {
                    "inline_data": {
                        "mime_type": "image/png",
                        "data": screenshot_base64,
                    }
                },
                {"text": query},
            ],
        })
    else:
        contents.append({
            "role": "user",
            "parts": [{"text": query}],
        })

    payload = {
        "contents": contents,
        "tools": [{"computer_use": {}}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 2048,
        },
    }

    url = f"{GEMINI_BASE}/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    res = await client.post(url, json=payload, timeout=30.0)
    res.raise_for_status()
    return res.json()

async def call_gemini_task(
    client: httpx.AsyncClient,
    prompt: str,
    context: str | None = None,
) -> dict[str, Any]:
    """
    General Gemini task (text-in, text-out).
    Used for analysis, signal reasoning, report generation.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set")

    text = f"{context}\n\n{prompt}" if context else prompt
    payload = {
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 4096},
    }

    url = f"{GEMINI_BASE}/models/{GEMINI_TASK_MODEL}:generateContent?key={GEMINI_API_KEY}"
    res = await client.post(url, json=payload, timeout=30.0)
    res.raise_for_status()
    return res.json()

def extract_text(gemini_response: dict) -> str:
    """Extract text from Gemini API response."""
    try:
        parts = gemini_response["candidates"][0]["content"]["parts"]
        return " ".join(p.get("text", "") for p in parts if "text" in p)
    except (KeyError, IndexError):
        return json.dumps(gemini_response)

# ── Request / Response models ─────────────────────────────────────────────────

class ComputerUseRequest(BaseModel):
    query: str
    screenshot_base64: str | None = None
    context: str | None = None
    request_id: str | None = None

class ComputerUseResponse(BaseModel):
    success: bool
    request_id: str | None
    query: str
    raw_response: dict | None = None
    action: dict | None = None
    text: str | None = None
    model: str
    elapsed_ms: int
    error: str | None = None

class TaskRequest(BaseModel):
    prompt: str
    context: str | None = None
    request_id: str | None = None

class TaskResponse(BaseModel):
    success: bool
    request_id: str | None
    prompt: str
    result: str
    model: str
    elapsed_ms: int
    error: str | None = None

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="k9-gemini-agent", version="0.1.0")

@app.post("/computer_use", response_model=ComputerUseResponse)
async def computer_use(req: ComputerUseRequest):
    """
    Execute a GUI automation task via Gemini computer_use model.
    Requires GEMINI_API_KEY. Gated by k9-paymaster (auto-approved < $0.10).

    The caller is responsible for:
      1. Taking a screenshot of the current UI state
      2. Passing it as screenshot_base64 (PNG, base64-encoded)
      3. Acting on the returned action (click, type, scroll, etc.)
      4. Repeating until the task completes
    """
    if not req.query.strip():
        raise HTTPException(400, "query must be non-empty")

    t0 = int(time.time() * 1000)
    request_id = req.request_id or f"cu-{t0}"

    async with httpx.AsyncClient() as client:
        # Paymaster gate — Gemini computer_use ~$0.003-0.010 per step
        approved = await paymaster_gate(client, "computer_use", estimated_cost=0.008)
        if not approved:
            raise HTTPException(402, "Paymaster queued for operator review — check /paymaster/pending")

        try:
            raw = await call_gemini_computer_use(
                client, req.query, req.screenshot_base64
            )

            # Parse the computer_use tool call from response
            action: dict | None = None
            text_output = extract_text(raw)

            try:
                parts = raw["candidates"][0]["content"]["parts"]
                for part in parts:
                    if "functionCall" in part:
                        fc = part["functionCall"]
                        if fc.get("name") == "computer_use":
                            action = fc.get("args", {})
                            break
            except (KeyError, IndexError):
                pass

            elapsed = int(time.time() * 1000) - t0
            stats.total_tasks += 1
            stats.total_computer_use += 1
            stats.last_task_at = datetime.now(timezone.utc).isoformat()
            stats.last_error = None

            log.info("computer_use %s → action=%s elapsed=%dms",
                     request_id, action.get("action") if action else "text", elapsed)

            return ComputerUseResponse(
                success=True,
                request_id=request_id,
                query=req.query,
                raw_response=raw,
                action=action,
                text=text_output,
                model=GEMINI_MODEL,
                elapsed_ms=elapsed,
            )

        except Exception as e:
            stats.last_error = str(e)
            log.error("computer_use error: %s", e, exc_info=True)
            return ComputerUseResponse(
                success=False,
                request_id=request_id,
                query=req.query,
                model=GEMINI_MODEL,
                elapsed_ms=int(time.time() * 1000) - t0,
                error=str(e),
            )

@app.post("/task", response_model=TaskResponse)
async def gemini_task(req: TaskRequest):
    """
    General Gemini task — text in, text out.
    Use for: signal analysis, report generation, reasoning, summarization.
    Routed to gemini-2.5-pro (not computer_use model).
    """
    if not req.prompt.strip():
        raise HTTPException(400, "prompt must be non-empty")

    t0 = int(time.time() * 1000)
    request_id = req.request_id or f"task-{t0}"

    async with httpx.AsyncClient() as client:
        approved = await paymaster_gate(client, "gemini_task", estimated_cost=0.02)
        if not approved:
            raise HTTPException(402, "Paymaster queued for operator review")

        try:
            raw = await call_gemini_task(client, req.prompt, req.context)
            result = extract_text(raw)
            elapsed = int(time.time() * 1000) - t0
            stats.total_tasks += 1
            stats.last_task_at = datetime.now(timezone.utc).isoformat()
            stats.last_error = None

            log.info("gemini_task %s elapsed=%dms", request_id, elapsed)

            return TaskResponse(
                success=True,
                request_id=request_id,
                prompt=req.prompt,
                result=result,
                model=GEMINI_TASK_MODEL,
                elapsed_ms=elapsed,
            )

        except Exception as e:
            stats.last_error = str(e)
            log.error("gemini_task error: %s", e, exc_info=True)
            return TaskResponse(
                success=False,
                request_id=request_id,
                prompt=req.prompt,
                result="",
                model=GEMINI_TASK_MODEL,
                elapsed_ms=int(time.time() * 1000) - t0,
                error=str(e),
            )

@app.get("/health")
def health():
    """Liveness + model config + task stats."""
    return {
        "status": "online",
        "service": "k9-gemini-agent",
        "port": GEMINI_PORT,
        "sprint": 10,
        "api_key_set": bool(GEMINI_API_KEY),
        "models": {
            "computer_use": GEMINI_MODEL,
            "task": GEMINI_TASK_MODEL,
        },
        "browserbase": bool(BROWSERBASE_API_KEY),
        "stats": {
            "total_tasks": stats.total_tasks,
            "total_computer_use": stats.total_computer_use,
            "last_task_at": stats.last_task_at,
            "last_error": stats.last_error,
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not GEMINI_API_KEY:
        log.warning("⚠️  GEMINI_API_KEY not set — /computer_use and /task will fail until configured")
    log.info("🤖 k9-gemini-agent starting on :%d", GEMINI_PORT)
    log.info("Models: computer_use=%s task=%s", GEMINI_MODEL, GEMINI_TASK_MODEL)
    uvicorn.run(app, host="0.0.0.0", port=GEMINI_PORT, log_level="warning")
