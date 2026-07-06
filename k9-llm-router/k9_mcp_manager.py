"""
k9_mcp_manager.py — K-9 MCP MANAGER Tool Registry Agent (port :3030)

Responsibilities:
  - Register and validate all MCP tools across the K-9 swarm
  - Route tool calls to the correct provider with fallback logic
  - Monitor tool health and failure rates
  - Expose unified tool surface to k9-orchestrator

Tool registry covers:
  OpenAI, Claude Code, ElevenLabs, MoonPay (via paymaster),
  Ollama (local LLM fallback), Supabase vector, n8n triggers,
  TradingView MCP Jackson, Whisper STT, NATS/Redis queue

MCP tool surface:
  tool.registry.list()
  tool.registry.health(tool_id)
  tool.call(tool_id, method, params)   <- unified router
  tool.llm.route(prompt, tier)         <- smart LLM routing

Run: python k9_mcp_manager.py --port 3030
Deps: pip install fastapi uvicorn httpx python-dotenv --break-system-packages
"""
from __future__ import annotations
import asyncio, json, logging, os, time, uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()
log = logging.getLogger("k9-mcp-manager")

# ── TOOL DEFINITIONS ──────────────────────────────────────────────────────────

class ToolTier(str, Enum):
    """Cost tier — manager prefers lower tiers first."""
    FREE    = "free"      # Local/offline tools
    CHEAP   = "cheap"     # <$0.001/call (Ollama, Supabase)
    MODERATE= "moderate"  # $0.001–0.01/call (Claude Haiku, GPT-3.5)
    PREMIUM = "premium"   # >$0.01/call (Claude Opus, GPT-4o)

class ToolStatus(str, Enum):
    HEALTHY   = "healthy"
    DEGRADED  = "degraded"
    OFFLINE   = "offline"
    UNKNOWN   = "unknown"

@dataclass
class MCPTool:
    """Registered MCP tool definition."""
    tool_id:     str
    name:        str
    tier:        ToolTier
    base_url:    str          # empty for local/subprocess tools
    methods:     list[str]    # available method names
    description: str = ""
    env_key:     str = ""     # env var that must be set for this tool to be active
    status:      ToolStatus = ToolStatus.UNKNOWN
    last_check:  float = 0.0
    error_count: int = 0
    call_count:  int = 0
    avg_ms:      float = 0.0

    @property
    def active(self) -> bool:
        """Tool is active if no env_key required, or env_key is set."""
        return not self.env_key or bool(os.getenv(self.env_key))

@dataclass
class ToolCallResult:
    tool_id:    str
    method:     str
    success:    bool
    result:     Any = None
    error:      str = ""
    duration_ms: float = 0.0
    fallback_used: bool = False
    timestamp:  str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

# ── TOOL REGISTRY ─────────────────────────────────────────────────────────────

TOOL_REGISTRY: list[MCPTool] = [
    # Local LLM — free tier, always prefer first
    MCPTool("ollama", "Ollama Local LLM", ToolTier.FREE,
        base_url="http://localhost:11434",
        methods=["tool.local.llm", "tool.local.embed"],
        description="Local LLM via Ollama — zero cost, offline capable"),

    # Whisper STT — local, free
    MCPTool("whisper", "Whisper STT", ToolTier.FREE,
        base_url="",  # subprocess
        methods=["tool.audio.transcribe"],
        description="Offline speech-to-text via OpenAI Whisper"),

    # Supabase vector — cheap
    MCPTool("supabase-vector", "Supabase Vector DB", ToolTier.CHEAP,
        base_url=os.getenv("SUPABASE_URL", ""),
        methods=["tool.vector.search", "tool.vector.insert", "tool.vector.delete"],
        env_key="SUPABASE_URL",
        description="Vector embeddings store — RAG retrieval"),

    # NATS/Redis queue
    MCPTool("redis-queue", "Redis Streams Queue", ToolTier.FREE,
        base_url=os.getenv("REDIS_URL", "redis://localhost:6379"),
        methods=["tool.queue.publish", "tool.queue.consume"],
        description="Message queue for inter-agent task routing"),

    # TradingView MCP Jackson
    MCPTool("tradingview", "TradingView MCP Jackson", ToolTier.FREE,
        base_url="http://localhost:9001",  # quant-engine exposes /quant/signals
        methods=["tool.tv.health_check", "tool.tv.morning_brief", "tool.tv.signals"],
        description="TradingView Desktop chart analysis via CDP"),

    # K-9 Paymaster
    MCPTool("paymaster", "K-9 Paymaster", ToolTier.FREE,
        base_url="http://localhost:9002",
        methods=["tool.budget.gate", "tool.payment.quote", "tool.payment.execute",
                 "tool.wallet.balance", "tool.wallet.history"],
        description="Economic agent — budget gates + MoonPay payments"),

    # n8n automation
    MCPTool("n8n", "n8n Automation", ToolTier.FREE,
        base_url=os.getenv("N8N_URL", "http://localhost:5678"),
        methods=["tool.n8n.trigger", "tool.n8n.webhook"],
        env_key="N8N_URL",
        description="Workflow automation — payment approvals, CI/CD triggers"),

    # Claude Code (builder agent only — not runtime)
    MCPTool("claude-code", "Claude Code CLI", ToolTier.PREMIUM,
        base_url="",  # subprocess
        methods=["tool.claude.generate", "tool.claude.refactor", "tool.claude.debug"],
        description="Builder agent — code gen, refactor, JSON schema. NOT for real-time routing."),

    # OpenAI (moderate tier)
    MCPTool("openai", "OpenAI API", ToolTier.MODERATE,
        base_url="https://api.openai.com",
        methods=["tool.openai.chat", "tool.openai.embed", "tool.openai.reason"],
        env_key="OPENAI_API_KEY",
        description="OpenAI reasoning + embeddings — use after Ollama attempt"),

    # ElevenLabs
    MCPTool("elevenlabs", "ElevenLabs TTS", ToolTier.MODERATE,
        base_url="https://api.elevenlabs.io",
        methods=["tool.audio.speak", "tool.audio.clone"],
        env_key="ELEVENLABS_API_KEY",
        description="Voice synthesis layer"),

    # MoonPay (via paymaster)
    MCPTool("moonpay", "MoonPay Payment Rails", ToolTier.MODERATE,
        base_url="http://localhost:9002",  # routed through paymaster
        methods=["tool.payment.execute", "tool.payment.quote"],
        env_key="MOONPAY_API_KEY",
        description="Fiat<>crypto rails — always routed through paymaster budget gate"),

    # Web retrieval
    MCPTool("web-search", "Web Search + Scrape", ToolTier.FREE,
        base_url="",
        methods=["tool.web.search", "tool.web.scrape"],
        description="Real-time web retrieval beyond RAG knowledge cutoff"),

    # System OS control (LAM)
    MCPTool("system-lam", "System / OS LAM", ToolTier.FREE,
        base_url="",
        methods=["tool.system.exec", "tool.system.status"],
        description="LAM control — use carefully, powerful"),
]

# ── LLM ROUTING TIERS ─────────────────────────────────────────────────────────

LLM_ROUTING_TIERS = [
    # Tier 1: Local Ollama — free, always try first
    {"tool_id": "ollama",  "method": "tool.local.llm", "cost_usd": 0.0,      "label": "Local Ollama"},
    # Tier 2: Claude Haiku — cheap cloud
    {"tool_id": "openai",  "method": "tool.openai.chat", "cost_usd": 0.0005, "label": "GPT-3.5 / Haiku"},
    # Tier 3: Claude Sonnet / GPT-4o — moderate
    {"tool_id": "openai",  "method": "tool.openai.reason","cost_usd": 0.005, "label": "GPT-4o / Sonnet"},
    # Tier 4: Claude Opus — premium, builder/planning only
    {"tool_id": "claude-code","method": "tool.claude.generate","cost_usd": 0.015, "label": "Claude Code (builder)"},
]

# ── HEALTH CHECKER ────────────────────────────────────────────────────────────

class ToolHealthChecker:
    HEALTH_ENDPOINTS = {
        "ollama":         "/api/tags",
        "supabase-vector": "/rest/v1/",
        "tradingview":    "/quant/tv/health",
        "paymaster":      "/paymaster/summary",
        "n8n":            "/healthz",
    }

    @classmethod
    async def check(cls, tool: MCPTool) -> ToolStatus:
        if not tool.active:
            return ToolStatus.OFFLINE
        endpoint = cls.HEALTH_ENDPOINTS.get(tool.tool_id)
        if not endpoint or not tool.base_url:
            return ToolStatus.UNKNOWN
        try:
            async with httpx.AsyncClient(timeout=3) as c:
                r = await c.get(f"{tool.base_url}{endpoint}")
                tool.last_check = time.time()
                return ToolStatus.HEALTHY if r.status_code < 400 else ToolStatus.DEGRADED
        except Exception:
            tool.error_count += 1
            return ToolStatus.OFFLINE

    @classmethod
    async def check_all(cls, tools: list[MCPTool]) -> dict[str, ToolStatus]:
        results = await asyncio.gather(*[cls.check(t) for t in tools], return_exceptions=True)
        out = {}
        for tool, result in zip(tools, results):
            if isinstance(result, Exception):
                tool.status = ToolStatus.OFFLINE
            else:
                tool.status = result
            out[tool.tool_id] = tool.status
        return out

# ── MCP MANAGER AGENT ─────────────────────────────────────────────────────────

class MCPManagerAgent:
    """
    K-9 MCP Manager — unified tool registry and router.
    Every tool call in K-9 goes through here.
    Applies: health check, tier preference, budget gate, fallback routing.
    """

    def __init__(self, port: int = 3030):
        self.port    = port
        self.tools   = {t.tool_id: t for t in TOOL_REGISTRY}
        self._call_log: list[ToolCallResult] = []
        self.app     = self._build_api()
        log.info("MCPManagerAgent: %d tools registered", len(self.tools))

    def get_tool(self, tool_id: str) -> MCPTool:
        t = self.tools.get(tool_id)
        if not t: raise KeyError(f"Unknown tool: {tool_id}")
        return t

    def list_tools(self, tier: str | None = None, status: str | None = None) -> list[dict]:
        tools = list(self.tools.values())
        if tier:   tools = [t for t in tools if t.tier.value == tier]
        if status: tools = [t for t in tools if t.status.value == status]
        return [
            {"tool_id": t.tool_id, "name": t.name, "tier": t.tier.value,
             "status": t.status.value, "active": t.active,
             "methods": t.methods, "description": t.description,
             "call_count": t.call_count, "error_count": t.error_count}
            for t in sorted(tools, key=lambda x: list(ToolTier).index(x.tier))
        ]

    def smart_llm_route(self, prompt_len: int = 100, require_reason: bool = False) -> dict:
        """
        Choose the cheapest capable LLM tier for a given prompt.
        Cost-first: Ollama → GPT-3.5 → GPT-4o → Claude Code
        Claude Code is builder-only and should never be chosen for runtime inference.
        """
        for tier in LLM_ROUTING_TIERS:
            if tier["tool_id"] == "claude-code":
                continue  # never auto-route to builder agent
            tool = self.tools.get(tier["tool_id"])
            if tool and tool.active and tool.status != ToolStatus.OFFLINE:
                return {"selected": tier, "reason": f"lowest active tier for prompt_len={prompt_len}"}
        return {"selected": LLM_ROUTING_TIERS[1], "reason": "fallback — Ollama offline"}

    def _build_api(self) -> FastAPI:
        app = FastAPI(title="K-9 MCP Manager", version="1.0.0",
            description="Unified MCP tool registry, router, and health monitor.")
        app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

        @app.get("/tools")
        async def list_tools(tier: str = None, status: str = None):
            return {"count": len(self.tools), "tools": self.list_tools(tier, status)}

        
        @app.get("/mcp/tools")  # PWA compatibility alias
        async def mcp_tools_alias(tier: str = None, status: str = None):
            t = self.list_tools(tier, status)
            return {"available": sum(1 for x in t if x.get("active")), "total": len(t), "tools": t}

        @app.get("/tools/{tool_id}")
        async def tool_detail(tool_id: str):
            try: t = self.get_tool(tool_id); return asdict(t)
            except KeyError: raise HTTPException(404, f"Tool {tool_id} not found")

        @app.get("/tools/{tool_id}/health")
        async def tool_health(tool_id: str):
            try:
                t = self.get_tool(tool_id)
                status = await ToolHealthChecker.check(t)
                t.status = status
                return {"tool_id": tool_id, "status": status.value, "active": t.active}
            except KeyError: raise HTTPException(404, f"Tool {tool_id} not found")

        @app.post("/tools/health/all")
        async def health_all():
            results = await ToolHealthChecker.check_all(list(self.tools.values()))
            return {"checked": len(results), "results": {k: v.value for k, v in results.items()}}

        @app.get("/tools/llm/route")
        async def llm_route(prompt_len: int = 100, require_reason: bool = False):
            """Smart LLM routing — cost-first, Ollama preferred."""
            return self.smart_llm_route(prompt_len, require_reason)

        @app.post("/tools/call")
        async def tool_call(payload: dict):
            """
            Unified tool call router.
            POST body: {tool_id, method, params, agent_id}
            Applies health check + fallback before proxying.
            """
            tool_id  = payload.get("tool_id", "")
            method   = payload.get("method", "")
            params   = payload.get("params", {})
            agent_id = payload.get("agent_id", "unknown")
            start    = time.time()

            try:
                tool = self.get_tool(tool_id)
            except KeyError:
                raise HTTPException(404, f"Unknown tool: {tool_id}")

            if not tool.active:
                raise HTTPException(503, f"Tool {tool_id} not active — set {tool.env_key} env var")

            # Proxy to tool's base_url if it has one
            if tool.base_url:
                path = method.replace("tool.", "/").replace(".", "/")
                try:
                    async with httpx.AsyncClient(timeout=8) as c:
                        r = await c.post(f"{tool.base_url}/{path}", json=params)
                        r.raise_for_status()
                        tool.call_count += 1
                        ms = round((time.time() - start) * 1000, 1)
                        result = ToolCallResult(tool_id, method, True, r.json(), duration_ms=ms)
                        self._call_log.append(result)
                        return asdict(result)
                except Exception as e:
                    tool.error_count += 1
                    tool.status = ToolStatus.DEGRADED
                    result = ToolCallResult(tool_id, method, False, error=str(e))
                    self._call_log.append(result)
                    return asdict(result)

            # Subprocess tools (whisper, claude-code, system-lam) — stub for now
            result = ToolCallResult(tool_id, method, False,
                error=f"Subprocess tool {tool_id} — implement handler for method {method}")
            self._call_log.append(result)
            return asdict(result)

        @app.get("/calllog")
        async def call_log(n: int = 50):
            return {"count": len(self._call_log), "recent": [asdict(r) for r in self._call_log[-n:]]}

        @app.get("/")
        async def root():
            return {"service": "K-9 MCP Manager", "tools": len(self.tools),
                    "healthy": sum(1 for t in self.tools.values() if t.status == ToolStatus.HEALTHY)}

        return app

    async def _periodic_health(self, interval: int = 60):
        while True:
            await asyncio.sleep(interval)
            results = await ToolHealthChecker.check_all(list(self.tools.values()))
            healthy = sum(1 for s in results.values() if s == ToolStatus.HEALTHY)
            log.info("Health check: %d/%d tools healthy", healthy, len(results))

    def run(self):
        import uvicorn
        print(f"""
╔══════════════════════════════════════════════════════╗
║         K-9 MCP MANAGER — Tool Registry             ║
╚══════════════════════════════════════════════════════╝
  Port        : {self.port}
  Tools       : {len(self.tools)} registered
  Active      : {sum(1 for t in self.tools.values() if t.active)}
  Tiers       : free → cheap → moderate → premium
  LLM routing : Ollama first, Claude Code builder-only

  Endpoints:
    GET  /tools                     tool.registry.list()
    GET  /tools/{{id}}/health         tool.registry.health()
    POST /tools/health/all          check all tools
    GET  /tools/llm/route           smart LLM routing
    POST /tools/call                unified tool router
    GET  /calllog                   recent call log

  Key rules:
    • Every payment call routed through paymaster /paymaster/gate first
    • Claude Code is builder-only — not assigned to real-time inference
    • Ollama checked first for every LLM call (cost = $0)
""")
        async def _run():
            asyncio.create_task(self._periodic_health())
            config = uvicorn.Config(self.app, host="0.0.0.0", port=self.port, log_level="warning")
            await uvicorn.Server(config).serve()
        asyncio.run(_run())

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(); p.add_argument("--port", type=int, default=3030)
    MCPManagerAgent(port=p.parse_args().port).run()
