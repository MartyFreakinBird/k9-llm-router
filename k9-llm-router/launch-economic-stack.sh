#!/usr/bin/env bash
# ============================================================
# K-9 Economic Stack — Launch Script
# Sprint 6 — includes k9-knowledge-ingestor
#
# Services (start order matters):
#   1. k9-paymaster          :9002  — economic gate + CLOB signing
#   2. k9-mcp-manager        :3030  — tool registry
#   3. k9-orchestrator       :8744  — k9_orchestrator.py
#   4. k9-llm-router         :8765  — main.py (this repo)
#   5. k9-knowledge-ingestor :8767  — Sprint 9: HTTP embed server + ingest loop
#   6. k9-control-plane      :8769  — Sprint 8: auth, rate-limit, local Postgres
#   7. k9-gemini-agent       :8770  — Sprint 10: Gemini computer_use + task API
#   8. aeg-node-adapter       :8768  — Sprint AEG-3: AEG protocol wrapper for K-9
#
# Usage:
#   ./launch-economic-stack.sh start     — start all services in tmux
#   ./launch-economic-stack.sh stop      — kill all K-9 tmux sessions
#   ./launch-economic-stack.sh status    — show running services
#   ./launch-economic-stack.sh restart   — stop then start
#   ./launch-economic-stack.sh logs <svc> — tail logs for a service
# ============================================================

set -euo pipefail

K9_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$K9_DIR/.venv"
LOG_DIR="$K9_DIR/logs"
mkdir -p "$LOG_DIR"

log()  { echo "[K9-STACK] $*"; }
fail() { echo "[K9-STACK ERROR] $*" >&2; exit 1; }

# ── Service definitions ───────────────────────────────────────────────────────
declare -A SVC_CMD
declare -A SVC_DIR
declare -A SVC_PORT

# k9-paymaster (lives in sibling dir ../k9-paymaster or ~/k9-paymaster)
SVC_CMD[k9-paymaster]="python k9_paymaster.py"
SVC_DIR[k9-paymaster]="${K9_PAYMASTER_DIR:-$HOME/k9-paymaster}"
SVC_PORT[k9-paymaster]="9002"

# k9-mcp-manager
SVC_CMD[k9-mcp-manager]="python k9_mcp_manager.py"
SVC_DIR[k9-mcp-manager]="${K9_MCP_DIR:-$HOME/k9-mcp-manager}"
SVC_PORT[k9-mcp-manager]="3030"

# k9-orchestrator
SVC_CMD[k9-orchestrator]="python k9_orchestrator.py"
SVC_DIR[k9-orchestrator]="${K9_ORCH_DIR:-$HOME/k9-orchestrator}"
SVC_PORT[k9-orchestrator]="8744"

# k9-llm-router (this repo)
SVC_CMD[k9-llm-router]="python main.py"
SVC_DIR[k9-llm-router]="$K9_DIR"
SVC_PORT[k9-llm-router]="8765"

# k9-knowledge-ingestor (Sprint 6 — background RAG embedding loop)
SVC_CMD[k9-knowledge-ingestor]="python k9_knowledge_ingestor/k9_knowledge_ingestor.py"
SVC_DIR[k9-knowledge-ingestor]="$K9_DIR"
SVC_PORT[k9-knowledge-ingestor]="8767"

# k9-control-plane (Sprint 8 — unified HTTP surface for Base44/Emergent/Bolt)
SVC_CMD[k9-control-plane]="deno run --allow-net --allow-env --allow-run --allow-read --allow-write --env control-plane/config/.env control-plane/src/index.ts"
SVC_DIR[k9-control-plane]="${ORBITRON_DIR:-$HOME/orbitron-integrator}"
SVC_PORT[k9-control-plane]="8769"

# k9-gemini-agent (Sprint 10 — Gemini 2.5 computer_use + task API, Paymaster-gated)
SVC_CMD[k9-gemini-agent]="python k9_gemini_agent/k9_gemini_agent.py"
SVC_DIR[k9-gemini-agent]="$K9_DIR"
SVC_PORT[k9-gemini-agent]="8770"

# aeg-node-adapter (Sprint AEG-3 — AEG protocol wrapper for K-9, :8768)
SVC_CMD[aeg-node-adapter]="npx tsx sdk/aegAdapter.ts"
SVC_DIR[aeg-node-adapter]="${AEG_DIR:-$HOME/aeg-protocol}"
SVC_PORT[aeg-node-adapter]="8768"

START_ORDER=(k9-paymaster k9-mcp-manager k9-orchestrator k9-llm-router k9-knowledge-ingestor k9-control-plane k9-gemini-agent aeg-node-adapter)

# ── Helpers ───────────────────────────────────────────────────────────────────

check_port() {
  local port=$1
  ss -tlnp 2>/dev/null | grep -q ":${port} " && echo "UP" || echo "DOWN"
}

start_service() {
  local svc=$1
  local cmd="${SVC_CMD[$svc]}"
  local dir="${SVC_DIR[$svc]}"
  local port="${SVC_PORT[$svc]}"
  local logfile="$LOG_DIR/${svc}.log"

  if [ ! -d "$dir" ]; then
    log "⚠️  $svc dir not found: $dir — skipping"
    return
  fi

  # Kill existing tmux window if running
  tmux kill-window -t "k9:$svc" 2>/dev/null || true

  log "Starting $svc on :$port"
  tmux new-window -t k9 -n "$svc" \
    "cd '$dir' && source '$VENV/bin/activate' 2>/dev/null || true && $cmd 2>&1 | tee '$logfile'"

  sleep 1
}

stop_service() {
  local svc=$1
  tmux kill-window -t "k9:$svc" 2>/dev/null || true
  log "Stopped: $svc"
}

ensure_session() {
  tmux has-session -t k9 2>/dev/null || tmux new-session -d -s k9 -n main
}

# ── Commands ──────────────────────────────────────────────────────────────────

cmd_start() {
  log "=== K-9 Economic Stack START (Sprint AEG-3) ==="
  ensure_session

  for svc in "${START_ORDER[@]}"; do
    start_service "$svc"
    sleep 2
  done

  sleep 3
  cmd_status
}

cmd_stop() {
  log "=== K-9 Economic Stack STOP ==="
  for svc in "${START_ORDER[@]}"; do
    stop_service "$svc"
  done
}

cmd_status() {
  echo ""
  echo "  Service               Port    Status"
  echo "  ─────────────────────────────────────"
  for svc in "${START_ORDER[@]}"; do
    local port="${SVC_PORT[$svc]}"
    local status
    status=$(check_port "$port")
    local icon="✅"
    [ "$status" = "DOWN" ] && icon="❌"
    printf "  %-22s %-7s %s %s\n" "$svc" ":$port" "$icon" "$status"
  done
  echo ""
}

cmd_logs() {
  local svc="${1:-k9-llm-router}"
  local logfile="$LOG_DIR/${svc}.log"
  if [ -f "$logfile" ]; then
    tail -f "$logfile"
  else
    log "No log file yet: $logfile"
    log "Attach to tmux window: tmux attach -t k9 (then Ctrl+B, select window)"
  fi
}

cmd_restart() {
  cmd_stop
  sleep 2
  cmd_start
}

# ── Dispatch ──────────────────────────────────────────────────────────────────

case "${1:-status}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  status)  cmd_status ;;
  restart) cmd_restart ;;
  logs)    cmd_logs "${2:-}" ;;
  *)       fail "Unknown command: $1. Use: start | stop | status | restart | logs <svc>" ;;
esac
