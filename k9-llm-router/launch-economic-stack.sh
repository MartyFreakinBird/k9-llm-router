#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# K-9 Economic Stack — Full Layer Launch
# Starts: Paymaster (:9002) + MCP Manager (:3030) + LLM Router (:8765)
# All in named tmux sessions, sourcing .env, with health checks.
#
# Usage:
#   ./launch-economic-stack.sh         — start all
#   ./launch-economic-stack.sh stop    — stop all
#   ./launch-economic-stack.sh status  — health check all
# ─────────────────────────────────────────────────────────────────────────────

REPO="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$REPO/.env"
GRN='\033[0;32m'; RED='\033[0;31m'; BLU='\033[0;34m'; YLW='\033[1;33m'; NC='\033[0m'

check_deps() {
  [[ -f "$ENV_FILE" ]] || { echo -e "${RED}✗ No .env — copy .env.example to .env${NC}"; exit 1; }
  command -v tmux &>/dev/null || { echo -e "${RED}✗ tmux required${NC}"; exit 1; }
  command -v python3 &>/dev/null || { echo -e "${RED}✗ python3 required${NC}"; exit 1; }
}

start_service() {
  local name=$1 cmd=$2 port=$3
  if tmux has-session -t "$name" 2>/dev/null; then
    echo -e "${YLW}⚠  $name already running${NC}"
    return
  fi
  pip install -q fastapi uvicorn httpx python-dotenv 2>/dev/null
  tmux new-session -d -s "$name" -x 200 -y 40 \
    "cd '$REPO' && source '$ENV_FILE' && $cmd; echo 'STOPPED — press Enter'; read"
  sleep 2
  if tmux has-session -t "$name" 2>/dev/null; then
    echo -e "${GRN}✓  $name → tmux:$name (port $port)${NC}"
  else
    echo -e "${RED}✗  $name failed to start${NC}"
  fi
}

start_all() {
  check_deps
  echo -e "${BLU}▶  K-9 Economic Stack${NC}\n"

  # 1. Paymaster — must start first (router gates against it)
  start_service "k9-paymaster"    "python3 k9_paymaster.py --port 9002"    9002
  sleep 1

  # 2. MCP Manager
  start_service "k9-mcp-manager"  "python3 k9_mcp_manager.py --port 3030"  3030
  sleep 1

  # 3. LLM Router
  start_service "k9-router"       "python3 main.py"                         8765

  echo ""
  echo -e "${BLU}─── Endpoints ───────────────────────────────${NC}"
  echo -e "  LLM Router:   http://localhost:8765/swarm/health"
  echo -e "  Paymaster:    http://localhost:9002/paymaster/summary"
  echo -e "  MCP Manager:  http://localhost:3030/tools"
  echo -e "${BLU}─── Attach ──────────────────────────────────${NC}"
  echo -e "  tmux attach -t k9-paymaster"
  echo -e "  tmux attach -t k9-mcp-manager"
  echo -e "  tmux attach -t k9-router"
}

stop_all() {
  for s in k9-paymaster k9-mcp-manager k9-router; do
    tmux kill-session -t $s 2>/dev/null && echo -e "${GRN}✓  stopped $s${NC}" || echo -e "${YLW}  $s not running${NC}"
  done
}

status_all() {
  echo -e "\n${BLU}═══ K-9 Economic Stack Status ═══${NC}"
  declare -A PORTS=([k9-paymaster]=9002 [k9-mcp-manager]=3030 [k9-router]=8765)
  declare -A PATHS=([k9-paymaster]="/paymaster/summary" [k9-mcp-manager]="/" [k9-router]="/swarm/health")
  for svc in k9-paymaster k9-mcp-manager k9-router; do
    port=${PORTS[$svc]}; path=${PATHS[$svc]}
    if tmux has-session -t "$svc" 2>/dev/null; then
      printf "${GRN}●${NC} %-20s tmux:up   " "$svc"
    else
      printf "${RED}○${NC} %-20s tmux:down " "$svc"
    fi
    result=$(curl -sf --max-time 2 "http://localhost:$port$path" 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    if 'remaining_usd' in d: print(f'budget=\${d[\"remaining_usd\"]:.2f}')
    elif 'count' in d: print(f'tools={d[\"count\"]}')
    elif 'status' in d: print(f'status={d[\"status\"]}')
    else: print('ok')
except: print('ok')
" 2>/dev/null)
    [[ -n "$result" ]] && echo -e "${GRN}http:up${NC}   $result" || echo -e "${RED}http:down${NC}"
  done
  echo ""
}

case "${1:-start}" in
  start)  start_all ;;
  stop)   stop_all ;;
  status) status_all ;;
  restart) stop_all; sleep 1; start_all ;;
  *) echo "Usage: $0 {start|stop|restart|status}" ;;
esac
