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
from typing import Any, Optional

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

    # Gemini — computer_use + deep analysis (Sprint 10, Paymaster-gated)
    "computer_use":     "gemini",
    "gui_automation":   "gemini",
    "web_search":       "gemini",
    "deep_analysis":    "gemini",
    "multi_step":       "gemini",
}



# ── ORBITRON INTEGRATION ──────────────────────────────────────────────────────
try:
    from src.orbitron_client import OrbitronClient, OrbitronEvent
    from src.fed_whisperer_bridge import enrich_trading_request
    _orbitron = OrbitronClient.from_env()
    _orbitron_enabled = bool(os.getenv("ORBITRON_AUTH_TOKEN"))
    if _orbitron_enabled:
        log.info("Orbitron integration ENABLED (K9_AGENT)")
    else:
        log.info("Orbitron integration DISABLED (no ORBITRON_AUTH_TOKEN)")
except ImportError as e:
    log.warning("Orbitron modules not found: %s", e)
    _orbitron = None
    _orbitron_enabled = False

# ── CB-2: SEMANTIC CACHE + GUARDRAILS ─────────────────────────────────────────
try:
    from src.semantic_cache import semantic_cache
    from src.guardrails import guardrails
    _cb2_enabled = True
    log.info("CB-2 modules loaded: semantic_cache + guardrails")
except ImportError as e:
    log.warning("CB-2 modules not available: %s — cache/guardrails bypassed", e)
    semantic_cache = None
    guardrails = None
    _cb2_enabled = False

# ── CB-3: TEXT-TO-SQL QUERY ENGINE ───────────────────────────────────────────
try:
    from src.text_to_sql import text_to_sql_engine
    _cb3_enabled = True
    log.info("CB-3 module loaded: text-to-sql")
except ImportError as e:
    log.warning("CB-3 module not available: %s", e)
    text_to_sql_engine = None
    _cb3_enabled = False

# ── CB-4: JPY REPATRIATION QUANT ENGINE ──────────────────────────────────────
try:
    from src.quant_signal_bridge import enrich_with_quant_context, run_full_analysis

    # CB-Polymarket: Prediction market adapter (optional)
    _polymarket_enabled = False
    try:
        from src.k9_polymarket_adapter import app as polymarket_app, refresh_markets as poly_refresh
        _polymarket_enabled = True
        log.info("CB-Polymarket: adapter loaded")
    except ImportError as e:
        log.warning(f"CB-Polymarket: not available ({e})")

    # GEX Engine: Gamma Exposure (optional)
    _gex_enabled = False
    try:
        from src.k9_gex_engine import compute_gex as gex_calc, _cache as gex_cache
        _gex_enabled = True
        log.info("GEX Engine: loaded (Deribit options gamma exposure)")
    except ImportError as e:
        log.warning(f"GEX Engine: not available ({e})")

    # FOMC Fiscal Fragility Modifier (optional)
    _fomc_fiscal_enabled = False
    try:
        from src.k9_fomc_fiscal_modifier import compute_cascade, grid_bot_stress_test, run_full_analysis
        _fomc_fiscal_enabled = True
        log.info("FOMC Fiscal Modifier: loaded (fiscal fragility cascade)")
    except ImportError as e:
        log.warning(f"FOMC Fiscal Modifier: not available ({e})")

    # Microflow Ingestion Engine (optional)
    _microflow_enabled = False
    try:
        from src.k9_microflow import ingest_all, compute_all_scores, compute_divergence, compute_catalyst_score
        _microflow_enabled = True
        log.info("Microflow Engine: loaded (catalyst score + divergence)")
    except ImportError as e:
        log.warning(f"Microflow Engine: not available ({e})")

    # 3Commas Signal Bridge (optional)
    _threecommas_enabled = False
    try:
        from src.k9_3commas_bridge import dispatch_signal, generate_all_automated
        _threecommas_enabled = True
        log.info("3Commas Bridge: loaded (Gate.io signal dispatch with SUPPRESS_SHORT)")
    except ImportError as e:
        log.warning(f"3Commas Bridge: not available ({e})")
    _cb4_enabled = True
    log.info("CB-4 module loaded: jpy_repatriation_model + quant_signal_bridge")
except ImportError as e:
    log.warning("CB-4 module not available: %s — quant enrichment bypassed", e)
    enrich_with_quant_context = None
    run_full_analysis = None
    _cb4_enabled = False

# CB-5: MCTS Reasoning Engine
try:
    from src.k9_mcts.api import router as mcts_router
    _cb5_enabled = True
    log.info("CB-5 module loaded: k9-mcts reasoning engine")
except Exception as e:
    log.warning("CB-5 module not available: %s — MCTS reasoning bypassed", e)
    mcts_router = None
    _cb5_enabled = False

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
        # Gemini agent — HTTP passthrough to :8770 (Sprint 10)
        # Only register when K9_GEMINI_URL is set (avoids ghost backend in fallback chain)
        gemini_url = os.getenv("K9_GEMINI_URL", "")
        if gemini_url:
            registry["k9-gemini-agent"] = ModelBackend(
                name="k9-gemini-agent",
                provider="gemini",
                model_id="gemini-2.5-computer-use-preview",
                base_url=gemini_url,
                priority=8,               # above Claude; Paymaster-gated in the agent itself
                max_tokens=4096,
                context_window=1_000_000,
            )

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


class TextToSQLRequest(BaseModel):
    query: str = Field(..., description="Natural language query")
    user_id: str = Field(default="anonymous", description="User identifier for rate limiting")


class TextToSQLResponse(BaseModel):
    success: bool
    sql: Optional[str] = None
    rows: Optional[list] = None
    row_count: int
    columns: Optional[list] = None
    execution_time_ms: float
    error: Optional[str] = None


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


async def call_gemini_agent(
    backend: ModelBackend,
    messages: list[dict],
    system: str | None,
    task_type: str,
    max_tokens: int,
) -> tuple[str, int]:
    """
    HTTP passthrough to k9-gemini-agent (:8770).
    computer_use/gui_automation task types → /computer_use
    All others → /task (Gemini 2.5 Pro reasoning)
    Paymaster gating is handled inside k9-gemini-agent itself.
    """
    # Collapse messages into a single prompt string
    prompt_parts = []
    if system:
        prompt_parts.append(f"System: {system}")
    for msg in messages:
        role = msg.get("role", "user")
        txt  = msg.get("content", "")
        if isinstance(txt, list):
            txt = " ".join(p.get("text", "") for p in txt if isinstance(p, dict))
        prompt_parts.append(f"{role.capitalize()}: {txt}")
    prompt = "\n\n".join(prompt_parts)

    gui_types = {"computer_use", "gui_automation", "desktop_control", "browser_control"}
    endpoint  = "/computer_use" if task_type in gui_types else "/task"

    body: dict = (
        {"query": prompt, "request_id": f"router-{int(time.time()*1000)}"}
        if endpoint == "/computer_use"
        else {"prompt": prompt, "request_id": f"router-{int(time.time()*1000)}"}
    )

    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{backend.base_url}{endpoint}", json=body)
        r.raise_for_status()
        data = r.json()

    if not data.get("success"):
        raise RuntimeError(f"k9-gemini-agent returned error: {data.get('error', 'unknown')}")

    # Extract text — /task returns {result}, /computer_use returns {text, action}
    result_text = data.get("result") or data.get("text") or ""
    if data.get("action"):
        result_text = f"{result_text}\nACTION: {data['action']}"

    return result_text, 0  # token count not exposed by gemini agent


async def call_backend(
    backend: ModelBackend,
    messages: list[dict],
    system: str | None,
    max_tokens: int,
    temperature: float,
    task_type: str = "general",
) -> tuple[str, int]:
    """Dispatch to correct inference backend."""
    if backend.provider == "ollama":
        return await call_ollama(backend, messages, system, max_tokens, temperature)
    elif backend.provider == "anthropic":
        if not ANTHROPIC_KEY:
            raise RuntimeError("Anthropic key not set — cannot use cloud fallback")
        return await call_anthropic(backend, messages, system, max_tokens, temperature, ANTHROPIC_KEY)
    elif backend.provider == "gemini":
        return await call_gemini_agent(backend, messages, system, task_type, max_tokens)
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
                # For cloud backends: try a lightweight probe when base_url is set,
                # fall back to key presence check
                if backend.base_url and backend.base_url.startswith("http"):
                    try:
                        r = await c.get(backend.base_url, timeout=2.0)
                        backend._healthy = r.status_code < 500
                    except Exception:
                        backend._healthy = bool(ANTHROPIC_KEY or OPENAI_KEY)
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

        # ── Request-size guard (DoS mitigation) ─────────────────────────────
        total_chars = sum(len(str(m.get("content", ""))) for m in req.messages)
        if len(req.messages) > 200 or total_chars > 200_000:
            raise HTTPException(status_code=413, detail="Request too large")
        if req.max_tokens > 32768:
            raise HTTPException(status_code=413, detail="max_tokens exceeds limit")

        # ── CB-2: Extract prompt for cache/guardrail checks ────────────────────
        _prompt = " ".join(
            m.get("content", "") for m in req.messages if m.get("role") == "user"
        )[-2000:]  # cap at 2000 chars for embedding

        # ── CB-2: Guardrails — input check ────────────────────────────────────
        if guardrails and guardrails._ready:
            _gr = guardrails.check_input(
                _prompt,
                context={"task_type": req.task_type, "component": req.component}
            )
            if not _gr.allowed:
                raise HTTPException(status_code=403, detail=f"Guardrails blocked: {_gr.block_reason}")
            if _gr.redacted_text:
                # Replace last user message with redacted version
                for m in reversed(req.messages):
                    if m.get("role") == "user":
                        m["content"] = _gr.redacted_text
                        break
                _prompt = _gr.redacted_text

        # ── CB-2: Semantic cache check ─────────────────────────────────────────
        if semantic_cache and semantic_cache._ready:
            _cache_result = semantic_cache.get(_prompt, task_type=req.task_type)
            if _cache_result.hit:
                log.info(
                    "CACHE HIT %s similarity=%.4f saved~%.0fms",
                    req.task_type, _cache_result.similarity, _cache_result.saved_ms or 0
                )
                return RouterResponse(
                    content    = _cache_result.response,
                    model_used = f"{_cache_result.model_used} [CACHED]",
                    task_type  = req.task_type,
                    backend    = "cache",
                    latency_ms = round((time.time() - t0) * 1000, 1),
                    tokens_used= None,
                )

        backend = self.resolve_model(req.task_type, req.force_model)

        # Enrich trading/quant requests with Orbitron context
        if _orbitron_enabled and _orbitron:
            req.messages, req.system = await enrich_trading_request(
                req.task_type, req.messages, req.system
            )

        # CB-4: Enrich with live JPY repatriation quant signal
        if _cb4_enabled and enrich_with_quant_context:
            req.messages, req.system = await enrich_with_quant_context(
                req.task_type, req.messages, req.system
            )

        log.info(
            "ROUTE %s → %s [%s] (component=%s)",
            req.task_type, backend.name, backend.provider, req.component
        )

        try:
            content, tokens = await call_backend(
                backend, req.messages, req.system, req.max_tokens, req.temperature, req.task_type
            )
            self._routed += 1
            latency = (time.time() - t0) * 1000
            backend._latency_ms = latency
            response = RouterResponse(
                content=content,
                model_used=backend.name,
                task_type=req.task_type,
                backend=backend.provider,
                latency_ms=round(latency, 1),
                tokens_used=tokens or None,
            )
            # ── CB-2: Cache store + output guardrail ──────────────────────────
            if semantic_cache and semantic_cache._ready and _prompt:
                semantic_cache.set(
                    _prompt, content, backend.name,
                    task_type=req.task_type,
                    latency_ms=round(latency, 1),
                    tokens=tokens or 0,
                )
            if guardrails and guardrails._ready:
                _out_gr = guardrails.check_output(
                    content, context={"task_type": req.task_type, "component": req.component}
                )
                if _out_gr.redacted_text:
                    response = RouterResponse(
                        content=_out_gr.redacted_text,
                        model_used=backend.name,
                        task_type=req.task_type,
                        backend=backend.provider,
                        latency_ms=round(latency, 1),
                        tokens_used=tokens or None,
                    )
            # Report to Orbitron (fire-and-forget)
            if _orbitron_enabled and _orbitron:
                asyncio.create_task(_orbitron.report_routing_decision(
                    task_type=req.task_type,
                    model_used=backend.name,
                    backend=backend.provider,
                    latency_ms=round(latency, 1),
                    component=req.component,
                ))
            return response
        except Exception as e:
            self._failed += 1
            backend._healthy = False
            log.error("Backend %s failed: %s — attempting fallback", backend.name, e)

            # Fallback chain: Gemini agent → Cloud Anthropic → fail
            if self.mode in ("hybrid", "local") and backend.provider == "ollama":
                # 1st fallback: Gemini agent (if online)
                gemini_backend = self.registry.get("k9-gemini-agent")
                if gemini_backend and gemini_backend.healthy:
                    try:
                        log.info("Fallback → k9-gemini-agent (%s)", req.task_type)
                        content, tokens = await call_gemini_agent(
                            gemini_backend, req.messages, req.system, req.task_type, req.max_tokens
                        )
                        latency = (time.time() - t0) * 1000
                        gemini_backend._latency_ms = latency
                        return RouterResponse(
                            content=content,
                            model_used=f"{gemini_backend.name} [FALLBACK]",
                            task_type=req.task_type,
                            backend="gemini",
                            latency_ms=round(latency, 1),
                            tokens_used=tokens or None,
                        )
                    except Exception as ge:
                        log.warning("Gemini fallback failed: %s", ge)
                        gemini_backend._healthy = False

                # 2nd fallback: Cloud Anthropic
                if self.mode == "hybrid" and ANTHROPIC_KEY:
                    fallback_key = f"{TASK_MODEL_MAP.get(req.task_type, 'default')}_cloud"
                    fallback = self.registry.get(fallback_key) or self.registry.get("glm5_cloud")
                    if fallback:
                        log.info("Fallback → Anthropic cloud: %s", fallback.name)
                        content, tokens = await call_backend(
                            fallback, req.messages, req.system, req.max_tokens, req.temperature, req.task_type
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
    # ── CB-2: Initialize semantic cache + guardrails ────────────────────────
    if _cb2_enabled:
        if semantic_cache:
            semantic_cache.initialize()
        if guardrails:
            guardrails.initialize()
    # Background health checks every 30s
    async def _health_loop():
        while True:
            await asyncio.sleep(30)
            for backend in router_instance.registry.values():
                await check_backend_health(backend)
            # Orbitron heartbeat every 30s
            if _orbitron_enabled and _orbitron:
                h = router_instance.health()
                await _orbitron.heartbeat(
                    component="k9-llm-router",
                    extra={
                        "models_healthy": len(h["models_healthy"]),
                        "models_total": len(h["models_available"]),
                        "routed_total": h["routed_total"],
                        "mode": ROUTER_MODE,
                    }
                )
    asyncio.create_task(_health_loop(), name="health-checker")
    yield
    log.info("LLM Router shutting down.")


app = FastAPI(
    title="K-9 LLM Router",
    version="0.3.0",
    description="Sprint 3 — routes inference to local Ollama/VLLM or cloud fallback",
    lifespan=lifespan,
)
# CORS — configurable via ALLOWED_ORIGINS env var (comma-separated).
# Defaults to localhost dev origin; set explicitly in production.
ALLOWED_ORIGINS = [o.strip() for o in os.getenv(
    "ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:5173,http://localhost:8744"
).split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=True,
)


if _cb5_enabled and mcts_router:
    app.include_router(mcts_router)

@app.get("/")
async def root():
    return {"service": "k9-llm-router", "sprint": 3, "mode": ROUTER_MODE}


@app.post("/route", response_model=RouterResponse)
async def route_request(req: RouterRequest):
    """Main routing endpoint. Accepts task_type + messages, returns model response."""
    return await router_instance.route(req)


@app.post("/query", response_model=TextToSQLResponse)
async def text_to_sql_query(req: TextToSQLRequest):
    if not text_to_sql_engine:
        raise HTTPException(status_code=503, detail="CB-3 text-to-sql module unavailable")
    result = await text_to_sql_engine.query(req.query, user_id=req.user_id)
    return TextToSQLResponse(
        success=result.success,
        sql=result.query,
        rows=result.rows,
        row_count=result.row_count,
        columns=result.columns,
        execution_time_ms=result.execution_time_ms,
        error=result.error,
    )


@app.get("/query/stats")
async def query_stats():
    if not text_to_sql_engine:
        return {"enabled": False}
    return text_to_sql_engine.stats()


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
    # Validate payload structure before processing
    if not isinstance(payload, dict) or "content" not in payload:
        raise HTTPException(status_code=400, detail="Invalid payload: missing 'content'")
    if not isinstance(payload["content"], dict) or "action" not in payload["content"]:
        raise HTTPException(status_code=400, detail="Invalid payload: missing 'action' in content")

    action = payload["content"].get("action")
    if action == "route":
        request_data = payload["content"].get("request")
        if not request_data or not isinstance(request_data, dict):
            raise HTTPException(status_code=400, detail="Invalid route request: missing request data")
        req = RouterRequest(**request_data)
        result = await router_instance.route(req)
        return {"ok": True, "result": result.dict()}
    return {"ok": True, "ack": payload.get("msg_id", "unknown")}



# ── SUPABASE BRIDGE ENDPOINTS ─────────────────────────────────────────────────
# These allow local WSL2 services (k9_orchestrator, k9_quant_engine, etc.)
# to publish data to Supabase/Orbitron without needing direct Supabase credentials.

class SignalPublishRequest(BaseModel):
    signal_type: str          # BUY | SELL | HOLD
    asset: str
    confidence: float         # 0-100
    reasoning: str
    metadata: dict = {}

class SharedStateRequest(BaseModel):
    module_name: str          # e.g. "k9_tradingview", "k9_quant_engine"
    data_key: str             # e.g. "latest_signals", "health_status"
    data: dict

@app.post("/signals/publish")
async def publish_signal(req: SignalPublishRequest):
    """
    Publish a trading signal from WSL2 to Supabase trading_signals table.
    Called by k9_orchestrator.py, k9_quant_engine, or any local service.
    Requires ORBITRON_URL and ORBITRON_ANON_KEY env vars.
    """
    try:
        from src.orbitron_client import OrbitronClient
        client = OrbitronClient.from_env()
        ok = await client.write_trading_signal(
            signal_type=req.signal_type,
            asset=req.asset,
            confidence=req.confidence,
            reasoning=req.reasoning,
            metadata=req.metadata,
        )
        if ok:
            await client.broadcast(
                event_type="SIGNAL_GENERATED",
                data={
                    "signal_type": req.signal_type,
                    "asset": req.asset,
                    "confidence": req.confidence,
                    "source": "k9_wsl2",
                },
                source="K9_AGENT",
            )
        return {"ok": ok, "signal": req.dict()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/status/publish")
async def publish_status(req: SharedStateRequest):
    """
    Publish service health/status to Supabase module_shared_data table.
    Called by any WSL2 K-9 service to report its state to Orbitron dashboard.
    """
    try:
        from src.orbitron_client import OrbitronClient
        client = OrbitronClient.from_env()
        ok = await client.write_shared_state(
            module_name=req.module_name,
            data_key=req.data_key,
            data=req.data,
        )
        return {"ok": ok, "module": req.module_name, "key": req.data_key}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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


# ── CB-2: Semantic cache endpoints ────────────────────────────────────────────

@app.get("/cache/stats")
async def cache_stats():
    """Semantic cache stats — hits, misses, index size."""
    if not semantic_cache:
        return {"enabled": False}
    return semantic_cache.stats()

@app.post("/cache/flush")
async def cache_flush():
    """Flush all semantic cache entries."""
    if not semantic_cache or not semantic_cache._ready:
        return {"flushed": 0}
    count = semantic_cache.flush()
    return {"flushed": count}

@app.get("/guardrails/stats")
async def guardrails_stats():
    """Guardrails moderation stats."""
    if not guardrails:
        return {"enabled": False}
    return guardrails.stats()

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



# ─────────────────────────────────────────────────────────────────────────────
# CB-4: QUANT ANALYSIS ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

class QuantAnalysisResponse(BaseModel):
    signal: dict = Field(default_factory=dict)
    cb1_envelope: dict = Field(default_factory=dict)
    route_result: dict = Field(default_factory=dict)
    elapsed_ms: float = 0.0

@app.post("/quant/analyze", response_model=QuantAnalysisResponse)
async def quant_analyze():
    """
    Run full JPY repatriation analysis pipeline.
    Returns QuantSignal + CB v1 envelope + routing result.
    Triggered by n8n daily quant workflow or direct API call.
    """
    if not _cb4_enabled or run_full_analysis is None:
        raise HTTPException(status_code=503, detail="CB-4 quant engine unavailable")
    result = await run_full_analysis()
    return QuantAnalysisResponse(**result)

@app.get("/quant/stats")
async def quant_stats():
    """CB-4 quant engine health + calibration status."""
    if not _cb4_enabled:
        return {"enabled": False, "reason": "CB-4 module not loaded"}
    from src.quant_signal_bridge import get_engine
    engine = get_engine()
    return {
        "enabled": True,
        "calibrated": engine._calibrated,
        "models": {
            "trigger":  "TriggerModel (logit)",
            "flow":     "FlowMagnitudeModel (ARIMAX)",
            "gpif":     "GPIFOptimizationModel (QP)",
            "regime":   "MarkovRegimeSwitchingModel (3-state)",
        },
        "signal_gate": "confidence >= 0.40 → aeg_signal_router :9004 → Orbitron",
        "false_signal_gate": "rebalancing/crisis_flight distinguished from repatriation",
    }

# ── Microflow Catalyst Endpoints ──────────────────────────────────────────────
@app.get("/microflow/catalyst/all")
async def microflow_all():
    """All catalyst scores with EMA smoothing."""
    if not _microflow_enabled:
        raise HTTPException(status_code=503, detail="Microflow engine not available")
    from src.k9_microflow import compute_all_scores
    return {"scores": compute_all_scores()}


@app.get("/microflow/catalyst/{instrument}")
async def microflow_catalyst(instrument: str):
    """Catalyst score for a specific instrument."""
    if not _microflow_enabled:
        raise HTTPException(status_code=503, detail="Microflow engine not available")
    from src.k9_microflow import compute_catalyst_score
    return compute_catalyst_score(instrument.upper())


@app.get("/microflow/divergence")
async def microflow_divergence():
    """Micro vs macro divergence flags. SUPPRESS_SHORT when micro dominates."""
    if not _microflow_enabled:
        raise HTTPException(status_code=503, detail="Microflow engine not available")
    from src.k9_microflow import compute_divergence
    flags = compute_divergence()
    suppress = [f for f in flags if f["divergence_signal"] == "SUPPRESS_SHORT"]
    return {
        "flags": flags,
        "suppress_short_instruments": [f["instrument"] for f in suppress],
        "alert_level": "SUPPRESS_SHORT" if suppress else "NORMAL",
    }


@app.post("/microflow/ingest")
async def microflow_ingest():
    """Manual trigger microflow data ingestion."""
    if not _microflow_enabled:
        raise HTTPException(status_code=503, detail="Microflow engine not available")
    from src.k9_microflow import ingest_all
    return ingest_all()


@app.get("/microflow/coverage")
async def microflow_coverage():
    """Data coverage stats for microflow."""
    if not _microflow_enabled:
        raise HTTPException(status_code=503, detail="Microflow engine not available")
    from src.k9_microflow import get_db
    db = get_db()
    try:
        stats = db.execute("""
            SELECT instrument, metric_type, COUNT(*) as count, MAX(timestamp) as latest
            FROM Fact_Microflow GROUP BY instrument, metric_type ORDER BY instrument
        """).fetchall()
    except Exception:
        stats = []
    return {"total_metrics": len(stats), "details": [
        {"instrument": r[0], "metric_type": r[1], "count": r[2], "latest": str(r[3])}
        for r in stats
    ]}


# ── 3Commas Signal Bridge Endpoints ─────────────────────────────────────────────
@app.post("/3commas/signal")
async def threecommas_signal(
    action: str, instrument: str, source: str = "api",
    confidence: float = 0.80, reason: str = ""
):
    """Dispatch a trading signal to 3Commas with all safety gates."""
    if not _threecommas_enabled:
        raise HTTPException(status_code=503, detail="3Commas bridge not available")
    from src.k9_3commas_bridge import dispatch_signal
    return dispatch_signal(action=action, instrument=instrument, source=source,
                           confidence=confidence, reason=reason)


@app.post("/3commas/auto")
async def threecommas_auto():
    """Run all automated signal generators (microflow, GEX, FOMC)."""
    if not _threecommas_enabled:
        raise HTTPException(status_code=503, detail="3Commas bridge not available")
    from src.k9_3commas_bridge import generate_all_automated
    return generate_all_automated()


@app.get("/3commas/suppress")
async def threecommas_suppress():
    """Check which instruments have SUPPRESS_SHORT active."""
    if not _threecommas_enabled:
        raise HTTPException(status_code=503, detail="3Commas bridge not available")
    from src.k9_3commas_bridge import _refresh_suppress_cache, _suppress_short_cache, _suppress_cache_ts
    _refresh_suppress_cache()
    from datetime import datetime, timezone
    return {
        "suppress_short_instruments": _suppress_short_cache,
        "cached_at": datetime.fromtimestamp(_suppress_cache_ts, timezone.utc).isoformat() if _suppress_cache_ts else None,
    }


@app.get("/3commas/stats")
async def threecommas_stats():
    """Signal dispatch statistics."""
    if not _threecommas_enabled:
        raise HTTPException(status_code=503, detail="3Commas bridge not available")
    from src.k9_3commas_bridge import _stats, _rate_limits, _rate_limit_remaining, _suppress_short_cache, _signal_log
    from datetime import datetime, timezone
    return {
        "stats": dict(_stats),
        "rate_limits": {
            inst: {"remaining_seconds": _rate_limit_remaining(inst)}
            for inst in _rate_limits
        },
        "suppress_short_active": _suppress_short_cache,
        "total_logged": len(_signal_log),
    }


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
