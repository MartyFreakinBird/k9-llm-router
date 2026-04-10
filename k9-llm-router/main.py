"""
k9-llm-router / main.py
─────────────────────────────────────────────────────────────────────────────
K-9 LLM ROUTER — Sprint 3
Giant Steps Framework · Phase 1 → Phase 2 bridge

Routes inference requests to the correct model backend based on task type.
Supports local Ollama/VLLM endpoints AND cloud fallback (Anthropic/OpenAI).

Model assignment:
  SCOUT tutoring / UI      → GLM-5    (human-preference ranked, vibe coding)
  Financial coach insights → DeepSeek V4  (cost-efficient, 1M+ token context)
  Agent swarms / desktop   → Qwen 3.5   (visual agents, 100+ concurrent)
  Long context / multimodal→ Kimi K2.5  (10M token Scout variant)
  General / fallback        → Llama 4   (industry standard, broad compat)
  Reasoning / coding hybrid → Mistral   (efficient, high throughput)

Env vars:
  LOCAL_MODEL_URL   — Ollama/VLLM base URL (e.g. http://localhost:11434)
  ANTHROPIC_API_KEY — cloud fallback
  OPENAI_API_KEY    — cloud fallback
  HEADSCALE_URL     — Headscale control plane
  ROUTER_PORT       — default 8765
  ROUTER_MODE       — "local" | "cloud" | "hybrid" (default: hybrid)
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Import swarm agent base (lives at workspace root or same dir)
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [k9-llm-router] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("k9-llm-router")


# ── CONFIGURATION ─────────────────────────────────────────────────────────────

LOCAL_MODEL_URL  = os.getenv("LOCAL_MODEL_URL", "http://localhost:11434")
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_KEY       = os.getenv("OPENAI_API_KEY", "")
ROUTER_PORT      = int(os.getenv("ROUTER_PORT", "8765"))
ROUTER_MODE      = os.getenv("ROUTER_MODE", "hybrid")   # local | cloud | hybrid
HEADSCALE_URL    = os.getenv("HEADSCALE_URL", "http://localhost:8080")


# ── MODEL REGISTRY ────────────────────────────────────────────────────────────

class ModelBackend:
    """Represents a model endpoint (local or cloud)."""
    def __init__(
        self,
        name: str,
        provider: str,           # "ollama" | "vllm" | "anthropic" | "openai"
        model_id: str,
        base_url: str,
        priority: int = 0,       # lower = preferred
        max_tokens: int = 4096,
        context_window: int = 128_000,
    ) -> None:
        self.name           = name
        self.provider       = provider
        self.model_id       = model_id
        self.base_url       = base_url
        self.priority       = priority
        self.max_tokens     = max_tokens
        self.context_window = context_window
        self._healthy       = True
        self._last_check    = 0.0
        self._latency_ms    = 0.0

    @property
    def healthy(self) -> bool:
        return self._healthy

    def __repr__(self) -> str:
        status = "✓" if self._healthy else "✗"
        return f"[{status}] {self.name} ({self.provider}:{self.model_id})"


# Task type → model family mapping
TASK_MODEL_MAP: dict[str, str] = {
    # PackAI SCOUT — Socratic tutor, UI rendering
    "scout_tutor":      "glm5",
    "ui_codegen":       "glm5",
    "vibe_coding":      "glm5",

    # Financial coach — cost-sensitive, long context
    "finance_coach":    "deepseek_v4",
    "financial_analysis": "deepseek_v4",
    "code_review":      "deepseek_v4",

    # Agent swarms, desktop/browser control, math
    "agent_swarm":      "qwen35",
    "desktop_control":  "qwen35",
    "browser_control":  "qwen35",
    "math_reasoning":   "qwen35",

    # Long context, multimodal
    "long_context":     "kimi_k25",
    "multimodal":       "kimi_k25",
    "document_analysis":"kimi_k25",

    # Reasoning + coding hybrid
    "reasoning":        "mistral",
    "coding":           "mistral",
    "hybrid_tasks":     "mistral",

    # Orbitron-specific
    "trading_signal":   "deepseek_v4",
    "quant_analysis":   "deepseek_v4",
    "auto_diagnostics": "qwen35",

    # Fallback
    "general":          "llama4",
    "default":          "llama4",
}


def build_model_registry(mode: str) -> dict[str, ModelBackend]:
    """
    Build model registry based on ROUTER_MODE.
    hybrid: try local first, fall back to cloud
    local:  local only (Ollama/VLLM)
    cloud:  cloud only (Anthropic/OpenAI)
    """
    registry: dict[str, ModelBackend] = {}

    if mode in ("local", "hybrid"):
        # Local Ollama endpoints
        registry["glm5"] = ModelBackend(
            name="GLM-5 (local)", provider="ollama",
            model_id="glm4",   # Ollama model tag — update when GLM-5 lands
            base_url=LOCAL_MODEL_URL, priority=0, context_window=128_000
        )
        registry["deepseek_v4"] = ModelBackend(
            name="DeepSeek V4 (local)", provider="ollama",
            model_id="deepseek-coder-v2", base_url=LOCAL_MODEL_URL,
            priority=0, context_window=1_000_000
        )
        registry["qwen35"] = ModelBackend(
            name="Qwen 3.5 (local)", provider="ollama",
            model_id="qwen2.5:72b", base_url=LOCAL_MODEL_URL,
            priority=0, context_window=128_000
        )
        registry["kimi_k25"] = ModelBackend(
            name="Kimi K2.5 (local)", provider="ollama",
            model_id="qwen2.5:72b",  # swap when Kimi lands in Ollama
            base_url=LOCAL_MODEL_URL, priority=0, context_window=10_000_000
        )
        registry["llama4"] = ModelBackend(
            name="Llama 4 (local)", provider="ollama",
            model_id="llama3.3:70b", base_url=LOCAL_MODEL_URL,
            priority=0, context_window=128_000
        )
        registry["mistral"] = ModelBackend(
            name="Mistral (local)", provider="ollama",
            model_id="mistral:latest", base_url=LOCAL_MODEL_URL,
            priority=0, context_window=32_000
        )

    if mode in ("cloud", "hybrid") and ANTHROPIC_KEY:
        # Cloud Anthropic — fallback (higher priority number = lower preference)
        registry["glm5_cloud"] = ModelBackend(
            name="Claude (cloud, SCOUT fallback)", provider="anthropic",
            model_id="claude-sonnet-4-20250514",
            base_url="https://api.anthropic.com", priority=10,
            context_window=200_000
        )
        registry["deepseek_v4_cloud"] = ModelBackend(
            name="Claude (cloud, finance fallback)", provider="anthropic",
            model_id="claude-sonnet-4-20250514",
            base_url="https://api.anthropic.com", priority=10,
            context_window=200_000
        )

    return registry


# ── REQUEST / RESPONSE MODELS ─────────────────────────────────────────────────

class RouterRequest(BaseModel):
    task_type: str = Field(
        default="default",
        description="Task classification. Maps to model family.",
        examples=["scout_tutor", "finance_coach", "agent_swarm", "trading_signal"]
    )
    messages: list[dict[str, Any]] = Field(
        ..., description="Message array (OpenAI-compatible format)"
    )
    system: str | None = Field(None, description="System prompt override")
    max_tokens: int = Field(default=1000, le=32768)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    stream: bool = Field(default=False)
    # Caller context
    component: str = Field(
        default="unknown",
        description="Calling K-9 component (e.g. packai-scout, orbitron-diag)"
    )
    force_model: str | None = Field(
        None,
        description="Override routing and force a specific model key"
    )


class RouterResponse(BaseModel):
    content: str
    model_used: str
    task_type: str
    backend: str          # "local" | "cloud"
    latency_ms: float
    tokens_used: int | None = None


class HealthResponse(BaseModel):
    status: str
    mode: str
    models_available: list[str]
    models_healthy: list[str]
    uptime_s: float
    sprint: int = 3


# ── INFERENCE BACKENDS ────────────────────────────────────────────────────────

async def call_ollama(
    backend: ModelBackend,
    messages: list[dict],
    system: str | None,
    max_tokens: int,
    temperature: float,
) -> tuple[str, int]:
    """Call local Ollama endpoint (OpenAI-compatible /v1/chat/completions)."""
    payload: dict[str, Any] = {
        "model": backend.model_id,
        "messages": messages if not system else [{"role": "system", "content": system}, *messages],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{backend.base_url}/v1/chat/completions", json=payload)
        r.raise_for_status()
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        tokens  = data.get("usage", {}).get("total_tokens")
        return content, tokens or 0


async def call_anthropic(
    backend: ModelBackend,
    messages: list[dict],
    system: str | None,
    max_tokens: int,
    temperature: float,
    api_key: str,
) -> tuple[str, int]:
    """Call Anthropic Messages API."""
    payload: dict[str, Any] = {
        "model": backend.model_id,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": messages,
    }
    if system:
        payload["system"] = system
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{backend.base_url}/v1/messages", json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
        content = data["content"][0]["text"]
        tokens  = data.get("usage", {}).get("input_tokens", 0) + data.get("usage", {}).get("output_tokens", 0)
        return content, tokens


async def call_backend(
    backend: ModelBackend,
    messages: list[dict],
    system: str | None,
    max_tokens: int,
    temperature: float,
) -> tuple[str, int]:
    """Dispatch to correct inference backend."""
    if backend.provider == "ollama":
        return await call_ollama(backend, messages, system, max_tokens, temperature)
    elif backend.provider == "anthropic":
        if not ANTHROPIC_KEY:
            raise RuntimeError("Anthropic key not set — cannot use cloud fallback")
        return await call_anthropic(backend, messages, system, max_tokens, temperature, ANTHROPIC_KEY)
    else:
        raise ValueError(f"Unknown provider: {backend.provider}")


# ── HEALTH CHECK ──────────────────────────────────────────────────────────────

async def check_backend_health(backend: ModelBackend) -> bool:
    """Ping local Ollama /api/tags or cloud endpoint."""
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            if backend.provider == "ollama":
                r = await c.get(f"{backend.base_url}/api/tags")
                backend._healthy = r.status_code == 200
            else:
                backend._healthy = bool(ANTHROPIC_KEY or OPENAI_KEY)
        backend._last_check = time.time()
        return backend._healthy
    except Exception:
        backend._healthy = False
        backend._last_check = time.time()
        return False


# ── ROUTER CORE ───────────────────────────────────────────────────────────────

class LLMRouter:
    """Core routing logic for K-9 LLM Router."""

    def __init__(self, mode: str = "hybrid") -> None:
        self.mode     = mode
        self.registry = build_model_registry(mode)
        self._start   = time.time()
        self._routed  = 0
        self._failed  = 0

    def resolve_model(self, task_type: str, force: str | None = None) -> ModelBackend:
        """Resolve task type to a healthy model backend."""
        if force and force in self.registry:
            return self.registry[force]

        model_key = TASK_MODEL_MAP.get(task_type, TASK_MODEL_MAP["default"])

        # Try local first in hybrid mode
        candidates = [
            b for k, b in self.registry.items()
            if k == model_key or k.startswith(model_key)
        ]
        candidates.sort(key=lambda b: (b.priority, not b.healthy))

        if not candidates:
            # Fall back to llama4
            return self.registry.get("llama4") or next(iter(self.registry.values()))

        return candidates[0]

    async def route(self, req: RouterRequest) -> RouterResponse:
        """Route a request to the correct model."""
        t0 = time.time()
        backend = self.resolve_model(req.task_type, req.force_model)

        log.info(
            "ROUTE %s → %s [%s] (component=%s)",
            req.task_type, backend.name, backend.provider, req.component
        )

        try:
            content, tokens = await call_backend(
                backend, req.messages, req.system, req.max_tokens, req.temperature
            )
            self._routed += 1
            latency = (time.time() - t0) * 1000
            backend._latency_ms = latency
            return RouterResponse(
                content=content,
                model_used=backend.name,
                task_type=req.task_type,
                backend=backend.provider,
                latency_ms=round(latency, 1),
                tokens_used=tokens or None,
            )
        except Exception as e:
            self._failed += 1
            backend._healthy = False
            log.error("Backend %s failed: %s — attempting fallback", backend.name, e)

            # Cloud fallback if in hybrid mode and local failed
            if self.mode == "hybrid" and backend.provider == "ollama":
                fallback_key = f"{TASK_MODEL_MAP.get(req.task_type, 'default')}_cloud"
                fallback = self.registry.get(fallback_key) or self.registry.get("glm5_cloud")
                if fallback and ANTHROPIC_KEY:
                    log.info("Falling back to cloud: %s", fallback.name)
                    content, tokens = await call_backend(
                        fallback, req.messages, req.system, req.max_tokens, req.temperature
                    )
                    latency = (time.time() - t0) * 1000
                    return RouterResponse(
                        content=content,
                        model_used=f"{fallback.name} [FALLBACK]",
                        task_type=req.task_type,
                        backend="cloud",
                        latency_ms=round(latency, 1),
                        tokens_used=tokens or None,
                    )
            raise HTTPException(status_code=503, detail=f"All backends failed: {e}")

    def health(self) -> dict:
        return {
            "status": "online",
            "mode": self.mode,
            "models_available": list(self.registry.keys()),
            "models_healthy": [k for k, b in self.registry.items() if b.healthy],
            "uptime_s": round(time.time() - self._start, 1),
            "routed_total": self._routed,
            "failed_total": self._failed,
            "sprint": 3,
        }


# ── FASTAPI APP ───────────────────────────────────────────────────────────────

router_instance: LLMRouter | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global router_instance
    router_instance = LLMRouter(mode=ROUTER_MODE)
    log.info("LLM Router starting | mode=%s | local=%s", ROUTER_MODE, LOCAL_MODEL_URL)
    # Background health checks every 30s
    async def _health_loop():
        while True:
            await asyncio.sleep(30)
            for backend in router_instance.registry.values():
                await check_backend_health(backend)
    asyncio.create_task(_health_loop(), name="health-checker")
    yield
    log.info("LLM Router shutting down.")


app = FastAPI(
    title="K-9 LLM Router",
    version="0.3.0",
    description="Sprint 3 — routes inference to local Ollama/VLLM or cloud fallback",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


@app.get("/")
async def root():
    return {"service": "k9-llm-router", "sprint": 3, "mode": ROUTER_MODE}


@app.post("/route", response_model=RouterResponse)
async def route_request(req: RouterRequest):
    """Main routing endpoint. Accepts task_type + messages, returns model response."""
    return await router_instance.route(req)


@app.get("/health")
async def health():
    return router_instance.health()


# ── Swarm API contract (required on every K-9 component) ──────────────────────

@app.get("/swarm/health")
async def swarm_health():
    h = router_instance.health()
    return {
        "agent_id": "k9-llm-router",
        "status": h["status"],
        "sprint": 3,
        "phase": 1,
        "router_mode": ROUTER_MODE,
        "models_healthy": len(h["models_healthy"]),
        "models_total": len(h["models_available"]),
    }


@app.get("/swarm/identity")
async def swarm_identity():
    return {
        "role": "llm-router",
        "capabilities": list(TASK_MODEL_MAP.keys()),
        "mode": ROUTER_MODE,
        "local_url": LOCAL_MODEL_URL,
        "sprint": 3,
    }


@app.get("/swarm/peers")
async def swarm_peers():
    return {"peers": [], "note": "k9-llm-router is a service node — peer list managed by k9-orchestrator"}


@app.post("/swarm/message")
async def swarm_message(payload: dict):
    """Accept FIPA-lite ACL messages from the swarm."""
    action = payload.get("content", {}).get("action")
    if action == "route":
        req = RouterRequest(**payload["content"].get("request", {}))
        result = await router_instance.route(req)
        return {"ok": True, "result": result.dict()}
    return {"ok": True, "ack": payload.get("msg_id", "unknown")}


@app.post("/swarm/peer/register")
async def swarm_peer_register(payload: dict):
    return {"ok": True, "note": "Peer registry delegated to k9-orchestrator"}


@app.get("/swarm/stats")
async def swarm_stats():
    h = router_instance.health()
    return {
        "routed_total": h["routed_total"],
        "failed_total": h["failed_total"],
        "uptime_s": h["uptime_s"],
        "sprint": 3,
    }


# ── Models introspection ──────────────────────────────────────────────────────

@app.get("/models")
async def list_models():
    """List all registered models and their health status."""
    return {
        k: {
            "name": b.name,
            "provider": b.provider,
            "model_id": b.model_id,
            "healthy": b.healthy,
            "latency_ms": b._latency_ms,
            "context_window": b.context_window,
            "priority": b.priority,
        }
        for k, b in router_instance.registry.items()
    }


@app.get("/task-map")
async def task_map():
    """Return full task_type → model mapping."""
    return TASK_MODEL_MAP


if __name__ == "__main__":
    import socket

    def get_local_ip() -> str:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"

    ip = get_local_ip()
    print(f"""
╔══════════════════════════════════════════════════════╗
║        K-9 LLM ROUTER  ·  Sprint 3                  ║
║   Giant Steps Framework — Phase 1 → 2 bridge        ║
╚══════════════════════════════════════════════════════╝
  Mode       : {ROUTER_MODE}
  Port       : {ROUTER_PORT}
  Local IP   : {ip}
  Ollama     : {LOCAL_MODEL_URL}

  Route API  : http://{ip}:{ROUTER_PORT}/route
  Health     : http://{ip}:{ROUTER_PORT}/swarm/health
  Models     : http://{ip}:{ROUTER_PORT}/models
  API docs   : http://{ip}:{ROUTER_PORT}/docs

  K-9 Wall + PWA will connect to: http://{ip}:{ROUTER_PORT}
  Press Ctrl+C to stop.
""")
    uvicorn.run("main:app", host="0.0.0.0", port=ROUTER_PORT, reload=False)
