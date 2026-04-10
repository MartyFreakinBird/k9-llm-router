# CLAUDE.md — k9-llm-router

> Persistent context for every Claude Code session in this repo.

---

## Project identity

- **Component**: k9-llm-router
- **Port**: 8765
- **Role in K-9**: L3 inference routing layer — maps task types to model backends (local Ollama/VLLM or cloud Anthropic fallback)
- **Language**: Python 3.11+ with async/await (FastAPI, uvicorn, httpx)
- **Environment**: WSL2 Fedora Remix on Windows 11 · Tailscale mesh across all devices
- **Sprint**: 3 (Giant Steps framework — Phase 1 → Phase 2 bridge)
- **Migration status**: Migrated OFF Replit → WSL2 local. Replit dependency eliminated.

---

## Environment activation

```bash
k9-llm-router   # alias in ~/.bashrc
# or:
source ~/k9-workspace/venvs/k9-llm-router/bin/activate
cd ~/k9-workspace/core/k9-llm-router
```

## Common commands

```bash
# Start the router
python main.py

# Start as swarm agent on port 8765
python main.py  # ROUTER_PORT=8765 in .env

# Health check
curl http://localhost:8765/swarm/health | python -m json.tool

# List all models + health
curl http://localhost:8765/models | python -m json.tool

# Test a route
curl -X POST http://localhost:8765/route \
  -H "Content-Type: application/json" \
  -d '{
    "task_type": "scout_tutor",
    "component": "packai-scout",
    "messages": [{"role": "user", "content": "What is compound interest?"}]
  }' | python -m json.tool

# Test finance coach route
curl -X POST http://localhost:8765/route \
  -H "Content-Type: application/json" \
  -d '{
    "task_type": "finance_coach",
    "component": "packai-finance",
    "messages": [{"role": "user", "content": "Give weekly insight for $230 gross week"}]
  }'

# Test Orbitron trading signal route
curl -X POST http://localhost:8765/route \
  -H "Content-Type: application/json" \
  -d '{
    "task_type": "trading_signal",
    "component": "orbitron-quant",
    "messages": [{"role": "user", "content": "Analyze BTC/USD 4h chart"}]
  }'

# Tail logs
tail -f ~/k9-workspace/logs/k9-llm-router.log
```

## Model routing map

| Task Type | Model Family | Reason |
|---|---|---|
| scout_tutor, ui_codegen | GLM-5 | Human-preference ranked, vibe coding |
| finance_coach, trading_signal | DeepSeek V4 | Cost-efficient, 1M+ token context |
| agent_swarm, desktop_control, math | Qwen 3.5 | Visual agents, 100+ concurrent |
| long_context, multimodal | Kimi K2.5 | 10M token Scout variant |
| reasoning, coding | Mistral | Efficient hybrid, high throughput |
| general, fallback | Llama 4 | Industry standard |

## Architecture context

```
K-9 Swarm (5-layer)
├── L1 Transport    — Tailscale/WireGuard mesh (k9-device-hub)
├── L2 Discovery    — libp2p + DHT (k9-mcp-server) [Sprint 6–8]
├── L3 Coordination — Gossip + Raft (k9-orchestrator)  ← peers with this component
├── L4 Governance   — DAO + Reputation (k9-quant-engine)
└── L5 Agency       — FIPA ACL + MARL + Claude oracles [Phase 4]

k9-llm-router sits BETWEEN L3 and L5 — it's the inference dispatch layer.
k9-orchestrator sends FIPA-lite REQUEST messages to /swarm/message
  with action="route" to trigger inference.
```

## Swarm API contract (DO NOT BREAK)

| Endpoint | Method | Description |
|---|---|---|
| `/swarm/health` | GET | Live health snapshot |
| `/swarm/peers` | GET | Known peers (delegates to orchestrator) |
| `/swarm/message` | POST | Receive FIPA-lite ACL message |
| `/swarm/identity` | GET | Agent identity + capabilities |
| `/swarm/stats` | GET | Router stats, uptime |
| `/swarm/peer/register` | POST | Peer registration (delegates) |

## Key endpoints (router-specific)

| Endpoint | Method | Description |
|---|---|---|
| `/route` | POST | Main routing endpoint |
| `/models` | GET | All models + health status |
| `/task-map` | GET | Full task_type → model map |
| `/health` | GET | Router health (non-swarm) |

## Coding standards

- Async first: all I/O uses `async/await`
- Type hints required on all public functions
- Google-style docstrings
- Error handling: every external call in try/except with structured logging
- No hardcoded secrets: `os.getenv()` + `.env`
- DeFi signing: `# LEVEL-1 ADVISORY` or `# LEVEL-2 AUTONOMOUS` markers required
- Line length: 100 chars

## Ollama model tags (update as models land)

```
glm4           → GLM-5 proxy (update when glm5 tag available)
deepseek-coder-v2 → DeepSeek V4 proxy
qwen2.5:72b    → Qwen 3.5 proxy
llama3.3:70b   → Llama 4 proxy
mistral:latest → Mistral
```

To pull models:
```bash
ollama pull qwen2.5:72b
ollama pull deepseek-coder-v2
ollama pull mistral
ollama pull llama3.3:70b
```

## Do not

- Do not run `pip install` without `--break-system-packages` on WSL2 Fedora
- Do not use synchronous `requests` — use `httpx` async
- Do not hardcode Tailscale IPs — use env vars or `get_tailscale_ip()`
- Do not bypass FIPA-lite protocol for inter-agent calls
- Do not touch Level-2 DeFi paths without Security Auditor review
