# K-9 Ecosystem — Installation Guide

## Quick Start (New Machine)

```bash
# Option 1: One-liner (clones + installs + configures + starts)
curl -fsSL https://raw.githubusercontent.com/MartyFreakinBird/k9-llm-router/main/k9-install.sh | bash

# Option 2: Clone first, then install
git clone https://github.com/MartyFreakinBird/k9-llm-router.git ~/k9/k9-llm-router
cd ~/k9/k9-llm-router
bash k9-install.sh

# Option 3: Minimal (core services only)
bash k9-install.sh --minimal

# Option 4: Full + interactive config
bash k9-install.sh --no-start    # install only
./k9-setup-wizard.sh            # configure .env interactively
./launch-economic-stack.sh start
```

## Prerequisites

### Required
| Tool | Version | Install |
|------|---------|---------|
| Git | 2.40+ | `apt install git` |
| Python | 3.11+ | `apt install python3 python3-venv` |
| Node.js | 20+ | `curl -fsSL https://deb.nodesource.com/setup_20.x \| sudo -E bash -` |
| npm | 10+ | comes with Node.js |
| tmux | 3.2+ | `apt install tmux` |
| curl | any | `apt install curl` |

### Optional (but recommended)
| Tool | Purpose | Install |
|------|---------|---------|
| Docker | Containerized stack | `curl -fsSL https://get.docker.com \| sh` |
| Ollama | Local LLM inference | `curl -fsSL https://ollama.com/install.sh \| sh` |
| Foundry | Solidity contracts (AEG) | `curl -L https://foundry.paradigm.xyz \| bash && foundryup` |
| Noir | ZK circuits (AEG-9) | `curl -L https://raw.githubusercontent.com/noir-lang/noirup/main/install \| bash && noirup` |

## What Gets Installed

### Repositories (9 total)
| Repo | Runtime | Purpose |
|------|---------|---------|
| k9-llm-router | Python FastAPI | Core K-9 services (router, paymaster, AEG, sentiment, etc.) |
| K9 | Static HTML | Canonical wallpaper + deployment configs |
| aeg-protocol | Solidity/TS | AEG contracts + ZK circuits + node adapter |
| fed-whisperer | React/Vite | Fed policy sentiment (Lovable cloud) |
| ai-yield-whisperer | React/Vite | Market intelligence (Lovable cloud) |
| orbitron-integrator | React/Vite | OSINT globe platform (Lovable cloud) |
| ScalpingTrader | Node/Express | Trading signals |
| MapPackManager | Node/Express | Map pack management |
| AlexaMobileWeb | React/Vite | Alexa mobile web app |

### Services (15 ports)
| Port | Service | Status |
|------|---------|--------|
| :8080 | k9-orchestrator | Core coordination |
| :8765 | k9-llm-router | LLM inference routing |
| :9001 | k9-quant-engine | Quant analysis + AEG scoring |
| :9002 | k9-paymaster | Economic gate |
| :9003 | aeg-token-model | Token scoring + PoA gate |
| :9004 | aeg-signal-router | Orbitron signal routing |
| :9005 | k9-tx-adapter | TX blockchain surveillance (read-only) |
| :9006 | k9-sentiment-engine | Multi-source sentiment (Reddit + News + VADER) |
| :8767 | k9-knowledge-ingestor | RAG embedding server |
| :8768 | aeg-node-adapter | AEG protocol wrapper |
| :8769 | k9-control-plane | Auth + rate limiting |
| :8770 | k9-gemini-agent | Gemini computer_use agent |
| :8780 | lovable-bridge | Lovable→MCP bridge |
| :8790 | k9-wallpaper-ws | BOOT_COMPLETE WebSocket |
| :3030 | k9-mcp-manager | Tool registry |

### External Services
| Port | Service | Install |
|------|---------|---------|
| :11434 | Ollama | `ollama serve` |
| :5678 | n8n | `docker run -d --name n8n -p 5678:5678 -v n8n_data:/home/node/.n8n docker.n8n.io/n8nio/n8n` |
| :5050 | LibreTranslate | `docker run -d -p 5050:5000 libretranslate/libretranslate` |
| :5432 | PostgreSQL (k9_local) | via docker-compose |

## Installation Steps

### 1. Prerequisites Check
The installer auto-checks for required tools and reports missing ones with install instructions.

### 2. Clone Repositories
All 9 repos are cloned to `~/k9/` (or `K9_HOME` if set).

### 3. Python Environment
Creates a virtualenv at `~/k9/k9-llm-router/.venv` and installs all requirements:
- Main: fastapi, uvicorn, httpx, transformers, torch, etc.
- Sentiment: vaderSentiment, beautifulsoup4, feedparser
- Knowledge: sentence-transformers, pgvector
- Gemini: google-generativeai

### 4. Node.js Environment
Installs Node dependencies for:
- AEG protocol (tsx for TypeScript execution)
- Lovable bridge
- Web apps (optional with `--full`)

### 5. Environment Configuration
Creates `.env` from template. **You must fill in API keys:**
- `GEMINI_API_KEY` — from Google AI Studio
- `SUPABASE_ANON_KEY` — from Supabase dashboard
- `AEG_INTEGRATION_KEY` — from Orbitron api_configurations
- `DEPLOYER_PK` — for contract deployment (optional if contracts already deployed)

Use `./k9-setup-wizard.sh` for interactive configuration.

### 6. Wallpaper Deployment
Copies `wallpaper.html` to `~/.local/share/k9-wallpaper/` and creates a `k9wall-deploy` alias.

### 7. Service Launch
- **With Docker**: `docker compose up -d` (auto-detected if Docker is present)
- **Without Docker**: `./launch-economic-stack.sh start` (tmux sessions)

### 8. Health Check
Verifies all service endpoints respond on their ports.

## Post-Install

```bash
# Check service status
cd ~/k9/k9-llm-router
./launch-economic-stack.sh status

# View logs
./launch-economic-stack.sh logs k9-llm-router

# Open wallpaper
xdg-open ~/.local/share/k9-wallpaper/wallpaper.html  # Linux
# or: explorer.exe "%LOCALAPPDATA%\k9-wallpaper\wallpaper.html"  # WSL2

# Update wallpaper after code changes
k9wall-deploy

# Stop all services
./launch-economic-stack.sh stop

# Restart
./launch-economic-stack.sh restart
```

## Troubleshooting

### Services not starting
- Check `.env` has all required keys filled in
- Check Python venv: `source ~/k9/k9-llm-router/.venv/bin/activate`
- Check logs: `./launch-economic-stack.sh logs <service-name>`
- Check ports: `ss -tlnp | grep -E '8765|9001|9002|9003|9004|9005|9006'`

### Ollama not found
```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3
ollama serve
```

### PostgreSQL not running (Docker)
```bash
docker compose up -d k9-postgres
docker compose logs k9-postgres
```

### Git clone fails (private repos)
```bash
# Set up SSH key
ssh-keygen -t ed25519 -C "k9-setup"
# Add public key to GitHub: Settings → SSH and GPG keys
# Or use PAT:
git clone https://<token>@github.com/MartyFreakinBird/k9-llm-router.git
```
