#!/usr/bin/env bash
# ============================================================
# K-9 ECOSYSTEM — UNIFIED INSTALLER
# v1.0.0 — August 2026
#
# One-time setup script for a fresh machine (WSL2/Linux/macOS).
# Clones all repos, installs dependencies, configures environment,
# deploys wallpaper, and launches the full K-9 stack.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/MartyFreakinBird/k9-llm-router/main/k9-install.sh | bash
#   — or —
#   git clone https://github.com/MartyFreakinBird/k9-llm-router.git && cd k9-llm-router && bash k9-install.sh
#
# Options:
#   --minimal     — core services only (orchestrator, router, paymaster, MCP)
#   --full        — everything including web apps
#   --no-clone    — skip repo cloning (repos already present)
#   --no-start    — install only, don't start services
#   --no-docker   — skip Docker setup, use tmux-based launch
#   --dry-run     — show what would happen without executing
#
# Prerequisites (auto-checked):
#   - Git, Python 3.11+, Node 20+, tmux, curl
#   - Optional: Docker, Foundry (forge), Noir (nargo), Ollama
# ============================================================

set -euo pipefail

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

ok()   { echo -e "${GREEN}✅ $*${NC}"; }
fail() { echo -e "${RED}❌ $*${NC}"; exit 1; }
warn() { echo -e "${YELLOW}⚠️  $*${NC}"; }
info() { echo -e "${CYAN}ℹ️  $*${NC}"; }
step() { echo -e "\n${BOLD}${CYAN}── $* ──${NC}"; }

# ── Config ────────────────────────────────────────────────────────────────────
K9_GITHUB_USER="MartyFreakinBird"
K9_HOME="${K9_HOME:-$HOME/k9}"
K9_WALLPAPER_DIR="${K9_WALLPAPER_DIR:-$HOME/.local/share/k9-wallpaper}"

# Service repos to clone
ALL_REPOS=(
  "k9-llm-router"
  "K9"
  "aeg-protocol"
  "fed-whisperer"
  "ai-yield-whisperer"
  "orbitron-integrator"
  "ScalpingTrader"
  "MapPackManager"
  "AlexaMobileWeb"
)

MINIMAL_REPOS=(
  "k9-llm-router"
  "K9"
  "aeg-protocol"
)

# Python services and their config (port:dir:cmd)
declare -A PY_SVC_PORT PY_SVC_DIR PY_SVC_CMD

PY_SVC_PORT[k9-llm-router]="8765"
PY_SVC_DIR[k9-llm-router]="$K9_HOME/k9-llm-router"
PY_SVC_CMD[k9-llm-router]="python main.py"

PY_SVC_PORT[k9-paymaster]="9002"
PY_SVC_DIR[k9-paymaster]="$K9_HOME/k9-llm-router"
PY_SVC_CMD[k9-paymaster]="python k9_paymaster.py"

PY_SVC_PORT[aeg-token-model]="9003"
PY_SVC_DIR[aeg-token-model]="$K9_HOME/k9-llm-router"
PY_SVC_CMD[aeg-token-model]="python -m uvicorn src.aeg_token_model:app --host 0.0.0.0 --port 9003"

PY_SVC_PORT[aeg-signal-router]="9004"
PY_SVC_DIR[aeg-signal-router]="$K9_HOME/k9-llm-router"
PY_SVC_CMD[aeg-signal-router]="python -m uvicorn src.aeg_signal_router:app --host 0.0.0.0 --port 9004"

PY_SVC_PORT[k9-tx-adapter]="9005"
PY_SVC_DIR[k9-tx-adapter]="$K9_HOME/k9-llm-router"
PY_SVC_CMD[k9-tx-adapter]="python -m uvicorn src.k9_tx_adapter:app --host 0.0.0.0 --port 9005"

PY_SVC_PORT[k9-sentiment-engine]="9006"
PY_SVC_DIR[k9-sentiment-engine]="$K9_HOME/k9-llm-router/k9-sentiment-engine"
PY_SVC_CMD[k9-sentiment-engine]="uvicorn main:app --host 0.0.0.0 --port 9006"

PY_SVC_PORT[k9-knowledge-ingestor]="8767"
PY_SVC_DIR[k9-knowledge-ingestor]="$K9_HOME/k9-llm-router"
PY_SVC_CMD[k9-knowledge-ingestor]="python k9_knowledge_ingestor/k9_knowledge_ingestor.py"

PY_SVC_PORT[k9-gemini-agent]="8770"
PY_SVC_DIR[k9-gemini-agent]="$K9_HOME/k9-llm-router"
PY_SVC_CMD[k9-gemini-agent]="python k9_gemini_agent/k9_gemini_agent.py"

# Canonical start order
START_ORDER=(
  k9-paymaster
  k9-llm-router
  aeg-token-model
  aeg-signal-router
  k9-tx-adapter
  k9-sentiment-engine
  k9-knowledge-ingestor
  k9-gemini-agent
)

# ── Parse args ────────────────────────────────────────────────────────────────
MINIMAL=false
FULL=false
NO_CLONE=false
NO_START=false
NO_DOCKER=false
DRY_RUN=false

for arg in "$@"; do
  case "$arg" in
    --minimal)   MINIMAL=true ;;
    --full)      FULL=true ;;
    --no-clone)  NO_CLONE=true ;;
    --no-start)  NO_START=true ;;
    --no-docker) NO_DOCKER=true ;;
    --dry-run)   DRY_RUN=true ;;
    *) warn "Unknown option: $arg" ;;
  esac
done

# Default: full if no flags
if ! $MINIMAL && ! $FULL; then FULL=true; fi

run() {
  if $DRY_RUN; then
    echo -e "  ${CYAN}[DRY] $*${NC}"
  else
    eval "$@"
  fi
}

# ══════════════════════════════════════════════════════════════════════════════
# STEP 1: PREREQUISITES CHECK
# ══════════════════════════════════════════════════════════════════════════════
step "Step 1/7: Prerequisites Check"

MISSING=()
check_cmd() {
  local cmd=$1 name=${2:-$1}
  if command -v "$cmd" &>/dev/null; then
    local ver; ver=$("$cmd" --version 2>&1 | head -1)
    ok "$name: $ver"
    return 0
  else
    warn "$name not found"
    return 1
  fi
}

check_cmd git || MISSING+=("git")
check_cmd python3 "Python 3" || MISSING+=("python3")
check_cmd node "Node.js" || MISSING+=("node")
check_cmd npm "npm" || MISSING+=("npm")
check_cmd tmux || MISSING+=("tmux")
check_cmd curl || MISSING+=("curl")

# Python version check
if command -v python3 &>/dev/null; then
  PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)
  PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
  PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
  if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
    fail "Python 3.11+ required, found $PY_VER"
  fi
fi

# Node version check
if command -v node &>/dev/null; then
  NODE_VER=$(node -v 2>/dev/null | tr -d 'v')
  NODE_MAJOR=$(echo "$NODE_VER" | cut -d. -f1)
  [ "$NODE_MAJOR" -lt 20 ] && warn "Node 20+ recommended, found $NODE_VER"
fi

# Optional tools
HAS_DOCKER=false; HAS_FOUNDRY=false; HAS_NOIR=false; HAS_OLLAMA=false
check_cmd docker "Docker" 2>/dev/null && HAS_DOCKER=true || true
check_cmd forge "Foundry (forge)" 2>/dev/null && HAS_FOUNDRY=true || true
check_cmd nargo "Noir (nargo)" 2>/dev/null && HAS_NOIR=true || true
check_cmd ollama "Ollama" 2>/dev/null && HAS_OLLAMA=true || true

if [ ${#MISSING[@]} -gt 0 ]; then
  echo ""
  warn "Missing required tools:"
  for m in "${MISSING[@]}"; do echo "  ❌ $m"; done
  echo ""
  echo "  Install:"
  echo "  Ubuntu/Debian: sudo apt update && sudo apt install -y git python3 python3-venv nodejs npm tmux curl"
  echo "  Fedora:        sudo dnf install -y git python3 nodejs npm tmux curl"
  echo "  macOS:         brew install git python@3.11 node tmux curl"
  echo "  Node 20+:      curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt install -y nodejs"
  echo "  Foundry:       curl -L https://foundry.paradigm.xyz | bash && foundryup"
  echo "  Noir:          curl -L https://raw.githubusercontent.com/noir-lang/noirup/main/install | bash && noirup"
  echo "  Ollama:        curl -fsSL https://ollama.com/install.sh | sh"
  fail "Install missing prerequisites first."
fi

ok "All required prerequisites found"

# ══════════════════════════════════════════════════════════════════════════════
# STEP 2: CLONE REPOS
# ══════════════════════════════════════════════════════════════════════════════
step "Step 2/7: Clone Repositories"

mkdir -p "$K9_HOME"
cd "$K9_HOME"

if $MINIMAL; then
  REPOS_TO_CLONE=("${MINIMAL_REPOS[@]}")
else
  REPOS_TO_CLONE=("${ALL_REPOS[@]}")
fi

if ! $NO_CLONE; then
  for repo in "${REPOS_TO_CLONE[@]}"; do
    dir="$K9_HOME/$repo"
    if [ -d "$dir/.git" ]; then
      info "$repo already cloned, pulling latest..."
      run "cd '$dir' && git pull --quiet 2>/dev/null || true"
    else
      info "Cloning $repo..."
      run "git clone https://github.com/$K9_GITHUB_USER/$repo.git '$dir' --quiet"
    fi
  done
  ok "All repos cloned to $K9_HOME"
else
  info "Skipping clone (--no-clone)"
fi

# ══════════════════════════════════════════════════════════════════════════════
# STEP 3: PYTHON ENVIRONMENT
# ══════════════════════════════════════════════════════════════════════════════
step "Step 3/7: Python Environment Setup"

MAIN_DIR="$K9_HOME/k9-llm-router"
VENV="$MAIN_DIR/.venv"

if [ ! -d "$VENV" ]; then
  info "Creating Python venv..."
  run "python3 -m venv '$VENV'"
fi

info "Upgrading pip..."
run "source '$VENV/bin/activate' && pip install --upgrade pip wheel setuptools --quiet"

# Install all requirements files
for reqfile in requirements.txt k9-sentiment-engine/requirements.txt k9_knowledge_ingestor/requirements.txt k9_gemini_agent/requirements.txt; do
  if [ -f "$MAIN_DIR/$reqfile" ]; then
    info "Installing $reqfile..."
    run "source '$VENV/bin/activate' && pip install -r '$MAIN_DIR/$reqfile' --quiet"
  fi
done

ok "Python environment ready at $VENV"

# ══════════════════════════════════════════════════════════════════════════════
# STEP 4: NODE.JS ENVIRONMENT
# ══════════════════════════════════════════════════════════════════════════════
step "Step 4/7: Node.js Environment Setup"

# AEG protocol (uses tsx)
if [ -f "$K9_HOME/aeg-protocol/package.json" ]; then
  info "Installing AEG protocol Node deps..."
  run "cd '$K9_HOME/aeg-protocol' && npm install --silent 2>/dev/null"
  ok "AEG protocol Node deps installed"
fi

# Lovable bridge (inside k9-llm-router)
if [ -f "$MAIN_DIR/k9-integration/lovable-bridge/package.json" ]; then
  info "Installing Lovable bridge Node deps..."
  run "cd '$MAIN_DIR/k9-integration/lovable-bridge' && npm install --silent 2>/dev/null"
  ok "Lovable bridge Node deps installed"
fi

# Web apps (optional)
if $FULL; then
  for webapp in fed-whisperer ai-yield-whisperer orbitron-integrator ScalpingTrader MapPackManager AlexaMobileWeb; do
    if [ -f "$K9_HOME/$webapp/package.json" ]; then
      info "Installing $webapp deps..."
      run "cd '$K9_HOME/$webapp' && npm install --silent 2>/dev/null || true"
    fi
  done
  ok "Web app dependencies installed"
fi

# ══════════════════════════════════════════════════════════════════════════════
# STEP 5: ENVIRONMENT CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════
step "Step 5/7: Environment Configuration"

ENV_FILE="$MAIN_DIR/.env"

if [ -f "$ENV_FILE" ]; then
  info ".env already exists — skipping"
else
  if [ -f "$MAIN_DIR/.env.example" ]; then
    info "Creating .env from template..."
    run "cp '$MAIN_DIR/.env.example' '$ENV_FILE'"
    warn ".env created with placeholders — edit $ENV_FILE before production"
    echo ""
    echo "  Required values to fill:"
    echo "  ───────────────────────────────"
    echo "  GEMINI_API_KEY       → https://aistudio.google.com/apikey"
    echo "  SUPABASE_ANON_KEY   → Supabase dashboard → Settings → API"
    echo "  AEG_INTEGRATION_KEY  → from api_configurations table (Orbitron)"
    echo "  AEG_NODE_ID          → your Base L2 wallet address"
    echo "  DEPLOYER_PK          → deployer private key (AEG contracts)"
    echo "  BASESCAN_API_KEY     → https://basescan.org"
  else
    warn "No .env.example — creating minimal .env"
    run "cat > '$ENV_FILE' << 'ENVEOF'
K9_HOME=$K9_HOME
K9_WALLPAPER=$K9_WALLPAPER_DIR
GEMINI_API_KEY=
SUPABASE_URL=https://ziqenqqgnqxqrazmjohs.supabase.co
SUPABASE_ANON_KEY=
AEG_INTEGRATION_KEY=
AEG_NODE_ID=
AEG_OPERATOR_KEY=dev
BASE_SEPOLIA_RPC=https://sepolia.base.org
BASESCAN_API_KEY=
ENVEOF"
  fi
fi

# Knowledge ingestor .env
INGESTOR_ENV="$MAIN_DIR/k9_knowledge_ingestor/.env"
if [ ! -f "$INGESTOR_ENV" ] && [ -f "$MAIN_DIR/k9_knowledge_ingestor/.env.example" ]; then
  run "cp '$MAIN_DIR/k9_knowledge_ingestor/.env.example' '$INGESTOR_ENV'"
  info "Created knowledge ingestor .env from template"
fi

ok "Environment configuration complete"

# ══════════════════════════════════════════════════════════════════════════════
# STEP 6: WALLPAPER DEPLOYMENT
# ══════════════════════════════════════════════════════════════════════════════
step "Step 6/7: Wallpaper Deployment"

WALLPAPER_SRC="$K9_HOME/K9/vectos/k9-wallpaper/wallpaper.html"
WALLPAPER_ALT="$MAIN_DIR/wallpaper/wallpaper.html"

if [ -f "$WALLPAPER_SRC" ]; then
  mkdir -p "$K9_WALLPAPER_DIR"
  run "cp '$WALLPAPER_SRC' '$K9_WALLPAPER_DIR/wallpaper.html'"
  ok "Wallpaper deployed to $K9_WALLPAPER_DIR/wallpaper.html"
elif [ -f "$WALLPAPER_ALT" ]; then
  mkdir -p "$K9_WALLPAPER_DIR"
  run "cp '$WALLPAPER_ALT' '$K9_WALLPAPER_DIR/wallpaper.html'"
  ok "Wallpaper deployed (from k9-llm-router) to $K9_WALLPAPER_DIR/wallpaper.html"
else
  warn "Wallpaper source not found — skipped"
fi

# k9wall-deploy alias
ALIAS_FILE="$HOME/.bashrc"
if ! grep -q "k9wall-deploy" "$ALIAS_FILE" 2>/dev/null; then
  run "echo \"alias k9wall-deploy='cp $K9_HOME/K9/vectos/k9-wallpaper/wallpaper.html $K9_WALLPAPER_DIR/wallpaper.html && echo Wallpaper deployed'\" >> '$ALIAS_FILE'"
  info "Added k9wall-deploy alias to ~/.bashrc"
fi

ok "Wallpaper deployment configured"

# ══════════════════════════════════════════════════════════════════════════════
# STEP 7: SERVICE LAUNCH
# ══════════════════════════════════════════════════════════════════════════════
step "Step 7/7: Service Launch"

if $NO_START; then
  info "Skipping service start (--no-start)"
  echo ""
  echo "  To start:  cd $MAIN_DIR && ./launch-economic-stack.sh start"
  echo "  Docker:    cd $MAIN_DIR && docker compose up -d"
  exit 0
fi

if $HAS_DOCKER && ! $NO_DOCKER; then
  info "Docker detected — starting via docker compose..."
  cd "$MAIN_DIR"
  if [ -f "docker-compose.yml" ]; then
    run "docker compose --env-file .env up -d"
    ok "Docker stack started"
    echo ""
    echo "  Status: docker compose ps"
    echo "  Logs:   docker compose logs -f k9-llm-router"
  else
    warn "docker-compose.yml not found — falling back to tmux"
    $NO_DOCKER=true
  fi
fi

if ! $HAS_DOCKER || $NO_DOCKER; then
  LAUNCH_SCRIPT="$MAIN_DIR/launch-economic-stack.sh"
  if [ -f "$LAUNCH_SCRIPT" ]; then
    info "Using launch-economic-stack.sh"
    run "cd '$MAIN_DIR' && chmod +x launch-economic-stack.sh"
    run "cd '$MAIN_DIR' && source .venv/bin/activate && ./launch-economic-stack.sh start"
  else
    warn "launch-economic-stack.sh not found — starting core services..."
    run "tmux has-session -t k9 2>/dev/null || tmux new-session -d -s k9 -n main"
    for svc in "${START_ORDER[@]}"; do
      port="${PY_SVC_PORT[$svc]:-}"
      dir="${PY_SVC_DIR[$svc]:-}"
      cmd="${PY_SVC_CMD[$svc]:-}"
      [ -z "$port" ] || [ -z "$dir" ] || [ -z "$cmd" ] && { warn "Skipping $svc"; continue; }
      [ ! -d "$dir" ] && { warn "Skipping $svc — dir not found: $dir"; continue; }
      info "Starting $svc on :$port..."
      run "tmux kill-window -t 'k9:$svc' 2>/dev/null || true"
      run "tmux new-window -t k9 -n '$svc' 'cd $dir && source .venv/bin/activate && $cmd 2>&1 | tee logs/$svc.log'"
      sleep 2
    done
  fi
fi

ok "K-9 stack launched"

# ══════════════════════════════════════════════════════════════════════════════
# HEALTH CHECK
# ══════════════════════════════════════════════════════════════════════════════
step "Post-Install: Health Check"

echo "  Checking services (10s warm-up)..."
sleep 10

HEALTH_PORTS=(
  "8765:k9-llm-router"
  "9002:k9-paymaster"
  "9003:aeg-token-model"
  "9004:aeg-signal-router"
  "9005:k9-tx-adapter"
  "9006:k9-sentiment-engine"
  "8767:k9-knowledge-ingestor"
  "8770:k9-gemini-agent"
  "8768:aeg-node-adapter"
)

for entry in "${HEALTH_PORTS[@]}"; do
  port=$(echo "$entry" | cut -d: -f1)
  name=$(echo "$entry" | cut -d: -f2)
  if curl -sf "http://localhost:$port/health" --connect-timeout 3 &>/dev/null; then
    ok "$name (:$port) — healthy"
  elif curl -sf "http://localhost:$port/swarm/health" --connect-timeout 3 &>/dev/null; then
    ok "$name (:$port) — healthy (swarm/health)"
  else
    warn "$name (:$port) — not responding yet"
  fi
done

# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
step "Installation Summary"

echo ""
echo -e "${BOLD}K-9 Ecosystem Installation Complete${NC}"
echo ""
echo "  Home:       $K9_HOME"
echo "  Venv:       $VENV"
echo "  Wallpaper:  $K9_WALLPAPER_DIR/wallpaper.html"
echo "  Config:     $ENV_FILE"
echo ""
echo -e "${YELLOW}Next steps:${NC}"
echo "  1. Edit $ENV_FILE — fill in API keys"
echo "  2. Open wallpaper: file://$K9_WALLPAPER_DIR/wallpaper.html"
echo "  3. Restart: cd $MAIN_DIR && ./launch-economic-stack.sh restart"
echo ""
echo -e "${CYAN}Optional installs:${NC}"
$HAS_OLLAMA || echo "  Ollama:     curl -fsSL https://ollama.com/install.sh | sh"
$HAS_FOUNDRY || echo "  Foundry:    curl -L https://foundry.paradigm.xyz | bash && foundryup"
$HAS_NOIR || echo "  Noir:       curl -L https://raw.githubusercontent.com/noir-lang/noirup/main/install | bash && noirup"
$HAS_DOCKER && echo "  n8n:        docker run -d --name n8n -p 5678:5678 -v n8n_data:/home/node/.n8n docker.n8n.io/n8nio/n8n"
$HAS_DOCKER && echo "  LibreTrans: docker run -d -p 5050:5000 libretranslate/libretranslate"
echo ""
echo -e "${GREEN}K-9 is ready. 🐕${NC}"
