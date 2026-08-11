#!/usr/bin/env bash
# ============================================================
# K-9 Economic Stack — Launch Script
# v2.0 — Updated Aug 2026 (AEG-6 + TX Adapter + Sentiment Engine)
#
# Services (start order matters):
#   1.  k9-paymaster          :9002  — economic gate + CLOB signing
#   2.  k9-mcp-manager        :3030  — tool registry
#   3.  k9-orchestrator       :8744  — L3 coordination
#   4.  k9-llm-router         :8765  — LLM inference router
#   5.  k9-knowledge-ingestor :8767  — RAG embedding server
#   6.  k9-control-plane      :8769  — auth + rate-limit surface
#   7.  k9-gemini-agent       :8770  — Gemini computer_use
#   8.  aeg-node-adapter      :8768  — AEG protocol wrapper
#   9.  aeg-token-model       :9003  — AEG scoring + PoA gate
#   10. aeg-signal-router     :9004  — Orbitron signal formatter
#   11. lovable-bridge        :8780  — INT-1 Lovable→MCP bridge
#   12. k9-wallpaper-ws       :8790  — BOOT_COMPLETE WebSocket bridge (overlay)
#
# Usage:
#   ./launch-economic-stack.sh start          — start all in tmux
#   ./launch-economic-stack.sh stop           — kill all K-9 tmux sessions
#   ./launch-economic-stack.sh status         — show running services
#   ./launch-economic-stack.sh restart        — stop then start
#   ./launch-economic-stack.sh logs <svc>     — tail logs for a service
#   ./launch-economic-stack.sh start <svc>    — start a single service
# ============================================================

set -euo pipefail

K9_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$K9_DIR/.venv"
LOG_DIR="$K9_DIR/logs"
mkdir -p "$LOG_DIR"

log()  { echo "[K9-STACK] $*"; }
fail() { echo "[K9-STACK ERROR] $*" >&2; exit 1; }

# ── Environment assertions ────────────────────────────────────────────────────
# These must be set before starting — warn loudly if missing
check_env() {
  local missing=()
  local -a REQUIRED=(
    "GEMINI_API_KEY"
    "AEG_INTEGRATION_KEY"
    "AEG_NODE_ID"
    "AEG_OPERATOR_KEY"
    "K9_HOME"
    "K9_WALLPAPER"
  )
  for var in "${REQUIRED[@]}"; do
    [[ -z "${!var:-}" ]] && missing+=("$var")
  done

  if [[ ${#missing[@]} -gt 0 ]]; then
    echo ""
    echo "  ⚠️  MISSING ENV VARS — services may fail silently:"
    for v in "${missing[@]}"; do
      echo "      ❌ $v"
    done
    echo "  Set these in ~/.bashrc or K9_HOME/.env and re-source before starting."
    echo ""
  else
    echo "  ✅ All required env vars set"
  fi
}

# ── Service definitions ───────────────────────────────────────────────────────
declare -A SVC_CMD
declare -A SVC_DIR
declare -A SVC_PORT

SVC_CMD[k9-paymaster]="python k9_paymaster.py"
SVC_DIR[k9-paymaster]="${K9_PAYMASTER_DIR:-$HOME/k9-paymaster}"
SVC_PORT[k9-paymaster]="9002"

SVC_CMD[k9-mcp-manager]="python k9_mcp_manager.py"
SVC_DIR[k9-mcp-manager]="${K9_MCP_DIR:-$HOME/k9-mcp-manager}"
SVC_PORT[k9-mcp-manager]="3030"

SVC_CMD[k9-orchestrator]="python k9_orchestrator.py"
SVC_DIR[k9-orchestrator]="${K9_ORCH_DIR:-$HOME/k9-orchestrator}"
SVC_PORT[k9-orchestrator]="8744"

SVC_CMD[k9-llm-router]="python main.py"
SVC_DIR[k9-llm-router]="$K9_DIR"
SVC_PORT[k9-llm-router]="8765"

SVC_CMD[k9-knowledge-ingestor]="python k9_knowledge_ingestor/k9_knowledge_ingestor.py"
SVC_DIR[k9-knowledge-ingestor]="$K9_DIR"
SVC_PORT[k9-knowledge-ingestor]="8767"

SVC_CMD[k9-control-plane]="deno run --allow-net --allow-env --allow-run --allow-read --allow-write --env control-plane/config/.env control-plane/src/index.ts"
SVC_DIR[k9-control-plane]="${ORBITRON_DIR:-$HOME/orbitron-integrator}"
SVC_PORT[k9-control-plane]="8769"

SVC_CMD[k9-gemini-agent]="python k9_gemini_agent/k9_gemini_agent.py"
SVC_DIR[k9-gemini-agent]="$K9_DIR"
SVC_PORT[k9-gemini-agent]="8770"

SVC_CMD[aeg-node-adapter]="npx tsx sdk/aegAdapter.ts"
SVC_DIR[aeg-node-adapter]="${AEG_DIR:-$HOME/aeg-protocol}"
SVC_PORT[aeg-node-adapter]="8768"

# Sprint AEG-5 — token model + signal router (both live in k9-llm-router/src/)
SVC_CMD[aeg-token-model]="python -m uvicorn src.aeg_token_model:app --host 0.0.0.0 --port 9003 --reload"
SVC_DIR[aeg-token-model]="$K9_DIR"
SVC_PORT[aeg-token-model]="9003"

SVC_CMD[aeg-signal-router]="python -m uvicorn src.aeg_signal_router:app --host 0.0.0.0 --port 9004 --reload"
SVC_DIR[aeg-signal-router]="$K9_DIR"
SVC_PORT[aeg-signal-router]="9004"

# Sprint INT-1 — Lovable bridge
SVC_CMD[lovable-bridge]="npx ts-node k9-integration/lovable-bridge/server.ts"
SVC_DIR[lovable-bridge]="$K9_DIR"
SVC_PORT[lovable-bridge]="8780"

# Wallpaper WebSocket bridge — BOOT_COMPLETE signal to browser overlay
SVC_CMD[k9-wallpaper-ws]="python k9_wallpaper_ws.py"
SVC_DIR[k9-wallpaper-ws]="$K9_DIR"
SVC_PORT[k9-wallpaper-ws]="8790"

# TX Blockchain Adapter — Coreum/Sologenic surveillance (read-only)
SVC_CMD[k9-tx-adapter]="python -m uvicorn src.k9_tx_adapter:app --host 0.0.0.0 --port 9005"
SVC_DIR[k9-tx-adapter]="$K9_DIR"
SVC_PORT[k9-tx-adapter]="9005"

# Sentiment Engine — zero-cost multi-source sentiment (Reddit + News + VADER)
SVC_CMD[k9-sentiment-engine]="uvicorn main:app --host 0.0.0.0 --port 9006"
SVC_DIR[k9-sentiment-engine]="$K9_DIR/k9-sentiment-engine"
SVC_PORT[k9-sentiment-engine]="9006"

# Quant Engine — k9-quant-engine
SVC_CMD[k9-quant-engine]="python -m uvicorn src.aeg_token_model:app --host 0.0.0.0 --port 9001"
SVC_DIR[k9-quant-engine]="$K9_DIR"
SVC_PORT[k9-quant-engine]="9001"

# Canonical start order
START_ORDER=(
  k9-paymaster
  k9-mcp-manager
  k9-orchestrator
  k9-llm-router
  k9-knowledge-ingestor
  k9-control-plane
  k9-gemini-agent
  aeg-node-adapter
  aeg-token-model
  aeg-signal-router
  k9-tx-adapter
  k9-sentiment-engine
  k9-quant-engine
  lovable-bridge
  k9-wallpaper-ws
)

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

  # Kill existing tmux window if already running
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
  # Single-service mode: ./launch-economic-stack.sh start <svc>
  if [[ -n "${2:-}" ]]; then
    ensure_session
    start_service "$2"
    return
  fi

  log "=== K-9 Economic Stack START (Sprint AEG-5 + INT-1) ==="
  check_env
  ensure_session

  for svc in "${START_ORDER[@]}"; do
    start_service "$svc"
    sleep 2
  done

  # Allow services to settle, then fire BOOT_COMPLETE
  sleep 5
  log "Firing BOOT_COMPLETE signal to wallpaper overlay..."
  curl -s -X POST http://localhost:8790/boot-complete \
    -H "Content-Type: application/json" \
    -d '{"event":"BOOT_COMPLETE","stack":"k9-economic","version":"AEG-5"}' \
    && log "✅ BOOT_COMPLETE sent" \
    || log "⚠️  BOOT_COMPLETE failed — wallpaper-ws may not be up yet"

  sleep 2
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
    printf "  %-24s %-7s %s %s\n" "$svc" ":$port" "$icon" "$status"
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
    log "Attach: tmux attach -t k9  (then Ctrl+B, pick window)"
  fi
}

cmd_restart() {
  cmd_stop
  sleep 2
  cmd_start
}

# ── Dispatch ──────────────────────────────────────────────────────────────────

case "${1:-status}" in
  start)   cmd_start "$@" ;;
  stop)    cmd_stop ;;
  status)  cmd_status ;;
  restart) cmd_restart ;;
  logs)    cmd_logs "${2:-}" ;;
  *)       fail "Unknown command: $1. Use: start [svc] | stop | status | restart | logs <svc>" ;;
esac
