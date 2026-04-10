# CLAUDE.md — K-9 [COMPONENT_NAME]

> This file gives Claude Code persistent context for every session in this repo.
> Replace [COMPONENT_NAME] and [PORT] with the actual values for this component.
> Commit this file to the root of each K-9 component repo.

---

## Project identity

- **Component**: [COMPONENT_NAME] (e.g. k9-orchestrator)
- **Role in K-9**: UI orchestration layer for Orbitron (automotive diagnostics) and PackAI
  (cloud AI / educational modules) within the K-9 distributed AI agent network
- **Language**: Python 3.11+ with async/await (FastAPI, uvicorn, httpx)
- **Environment**: WSL2 Fedora Remix on Windows 11 · Tailscale mesh across all devices
- **Sprint**: 3 (Giant Steps framework — Phase 1 → Phase 2 bridge)

---

## Environment activation

```bash
# Always activate the component venv before any work
k9-orchestrator          # alias defined in ~/.bashrc
# or explicitly:
source ~/k9-workspace/venvs/k9-orchestrator/bin/activate
cd ~/k9-workspace/core/k9-orchestrator
```

## Common commands

```bash
# Start the service
python main.py

# Start the swarm agent
python k9-swarm-agent.py --role orchestrator --port 8744

# Run tests
pytest tests/ -v --cov=src/

# Lint + format
pylint src/ && black src/

# Check swarm agent health
curl http://localhost:8744/swarm/health | python -m json.tool

# Check peer list
curl http://localhost:8744/swarm/peers | python -m json.tool

# Tail logs
tail -f ~/k9-workspace/logs/[COMPONENT_NAME].log
```

## Coding standards

- **Async first**: all I/O must use `async/await`. No blocking calls in async context.
- **Type hints**: required on all public functions and class methods.
- **Docstrings**: Google-style on all classes and public methods.
- **Error handling**: every external call wrapped in try/except with structured logging.
- **No hardcoded secrets**: use `os.getenv()` + `.env` file. Never commit `.env`.
- **DeFi signing paths**: any function that touches wallet signing must be clearly marked
  with `# LEVEL-1 ADVISORY` or `# LEVEL-2 AUTONOMOUS` and reviewed by Security Auditor.
- **Line length**: 100 chars (black default).
- **Imports**: stdlib → third-party → local, separated by blank lines.

## Architecture context

```
K-9 Swarm (5-layer)
├── L1 Transport    — Tailscale/WireGuard mesh (k9-device-hub)
├── L2 Discovery    — libp2p + DHT (k9-mcp-server) [Sprint 6–8]
├── L3 Coordination — Gossip + Raft (k9-orchestrator) [Sprint 7–9]
├── L4 Governance   — DAO + Reputation (k9-quant-engine) [Sprint 10–12]
└── L5 Agency       — FIPA ACL + MARL + Claude oracles [Phase 4]
```

This component sits at **[LAYER]** of the swarm. Changes here must not break
the SwarmAgent API contract (`/swarm/health`, `/swarm/peers`, `/swarm/message`).

## Swarm agent API contract

Every K-9 component exposes these endpoints (do not remove or rename):

| Endpoint | Method | Description |
|---|---|---|
| `/swarm/health` | GET | Live health snapshot (AgentHealth dataclass) |
| `/swarm/peers` | GET | Known peer list with reachability |
| `/swarm/message` | POST | Receive SwarmMessage (FIPA-lite ACL) |
| `/swarm/identity` | GET | Agent identity + capabilities |
| `/swarm/stats` | GET | Messenger stats, inbox size, uptime |
| `/swarm/peer/register` | POST | Register a new peer |

## Deployment

- **Local**: `python main.py` (FastAPI on port [PORT])
- **Swarm agent**: `python k9-swarm-agent.py --role [ROLE]` (port 8744)
- **Vercel** (web artifacts only — PackAI/Orbitron UI):
  ```bash
  vercel env pull .env.local   # sync production env vars first
  vercel build                 # local build check
  vercel --prod                # deploy
  ```
- **GitHub**: push triggers Claude Code Review action automatically
- **Persistent session**: always run inside `tmux new -s k9-[COMPONENT_NAME]`

## GitHub App integration

- Claude GitHub App installed with Read+Write on Contents, Issues, Pull Requests
- Mention `@claude` on any GitHub issue to trigger automatic fix on a new branch
- Every PR automatically gets Claude Code Review
- Branch naming: `feature/`, `fix/`, `sprint-N/` prefixes

## Key files

```
k9-[COMPONENT]/
├── CLAUDE.md               ← this file
├── main.py                 ← service entry point
├── k9-swarm-agent.py       ← swarm agent (copy from k9-workspace root)
├── src/
│   ├── [domain modules]
│   └── ...
├── tests/
├── requirements.txt
├── .env.example            ← committed · .env is gitignored
└── .github/
    └── workflows/
        └── ci.yml          ← pytest + pylint + black check
```

## Sprint 3 checklist (current)

- [ ] SwarmAgent running and reachable at `http://[TAILSCALE_IP]:8744/swarm/health`
- [ ] At least one peer registered via `/swarm/peer/register`
- [ ] Gossip heartbeats appearing in logs every 10s
- [ ] K-9 mobile dashboard Swarm tab shows this agent as online
- [ ] CLAUDE.md committed to repo root

## Do not

- Do not run `pip install` without `--break-system-packages` on WSL2 Fedora
- Do not use synchronous `requests` library — use `httpx` with async
- Do not hardcode Tailscale IPs — use `get_tailscale_ip()` from `k9-swarm-agent.py`
- Do not bypass the SwarmMessage/FIPA-lite protocol for inter-agent comms
- Do not touch Level-2 DeFi signing paths without Security Auditor review
