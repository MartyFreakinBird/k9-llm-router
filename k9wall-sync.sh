#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════
# k9wall-sync.sh — K-9 Wallpaper Sync Pipeline
# ════════════════════════════════════════════════════════════════════
# Automatically syncs wallpaper.html + assets from git repo to runtime.
# Designed to be called from:
#   1. Git post-merge hook (auto-sync on git pull)
#   2. Manual: ./k9wall-sync.sh or k9wall-deploy alias
#   3. File watcher: ./k9wall-sync.sh --watch
#
# Usage:
#   k9wall-sync.sh           # one-shot sync
#   k9wall-sync.sh --watch   # file-watch mode (inotifywait)
#   k9wall-sync.sh --check    # check if sync needed (exit 0 = yes, 1 = no)
#   k9wall-sync.sh --install  # install post-merge hook + alias
#
# Config:
#   K9_REPO_DIR   — git repo with wallpaper/ (default: ~/k9/k9-llm-router)
#   K9_RUNTIME    — runtime deployment path (auto-detected)
# ════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Config ──────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
K9_REPO_DIR="${K9_REPO_DIR:-$SCRIPT_DIR}"

# Auto-detect runtime path (check F: drive first, then C:)
for _path in /mnt/f/K9/vectos/k9-wallpaper /mnt/f/K9/Kiosk/wallpaper /mnt/c/K9/vectos/k9-wallpaper; do
  if [ -d "$_path" ]; then
    K9_RUNTIME="$_path"
    break
  fi
done
K9_RUNTIME="${K9_RUNTIME:-/mnt/f/K9/vectos/k9-wallpaper}"

# Source wallpaper directory in the repo
K9_WALLPAPER_SRC="$K9_REPO_DIR/wallpaper"

# Colors
G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; C='\033[0;36m'; D='\033[0;90m'; N='\033[0m'

log()  { echo -e "${C}[k9wall-sync]${N} $1"; }
ok()   { echo -e "${G}✅${N} $1"; }
warn() { echo -e "${Y}⚠️${N} $1"; }
err()  { echo -e "${R}❌${N} $1"; }

# ── Functions ────────────────────────────────────────────────────────

sync_wallpaper() {
  local src="$K9_WALLPAPER_SRC/wallpaper.html"
  local dst="$K9_RUNTIME/wallpaper.html"

  if [ ! -f "$src" ]; then
    err "Source not found: $src"
    return 1
  fi

  if [ ! -d "$K9_RUNTIME" ]; then
    warn "Runtime path doesn't exist: $K9_RUNTIME"
    echo -e "${D}Creating it...${N}"
    mkdir -p "$K9_RUNTIME"
  fi

  # Check if sync is needed
  if [ -f "$dst" ] && diff -q "$src" "$dst" >/dev/null 2>&1; then
    log "wallpaper.html already in sync"
    return 0
  fi

  # Copy wallpaper.html
  cp "$src" "$dst"
  local lines
  lines=$(wc -l < "$dst")
  ok "wallpaper.html → $dst (${lines} lines)"
}

sync_assets() {
  local src_assets="$K9_WALLPAPER_SRC/assets"
  local dst_assets="$K9_RUNTIME/assets"

  if [ ! -d "$src_assets" ]; then
    log "No assets directory in repo — skipping"
    return 0
  fi

  mkdir -p "$dst_assets/img" "$dst_assets/video"

  # Sync images
  local synced=0
  for f in "$src_assets/img/"*; do
    [ -f "$f" ] || continue
    local name
    name=$(basename "$f")
    if [ ! -f "$dst_assets/img/$name" ] || ! diff -q "$f" "$dst_assets/img/$name" >/dev/null 2>&1; then
      cp "$f" "$dst_assets/img/$name"
      ok "asset: img/$name"
      ((synced++))
    fi
  done

  # Sync videos
  for f in "$src_assets/video/"*; do
    [ -f "$f" ] || continue
    local name
    name=$(basename "$f")
    if [ ! -f "$dst_assets/video/$name" ] || ! diff -q "$f" "$dst_assets/video/$name" >/dev/null 2>&1; then
      cp "$f" "$dst_assets/video/$name"
      ok "asset: video/$name"
      ((synced++))
    fi
  done

  # Sync other root files
  for f in "$K9_WALLPAPER_SRC"/index.html "$K9_WALLPAPER_SRC"/monitor-panel.html "$K9_WALLPAPER_SRC"/manifest.json "$K9_WALLPAPER_SRC"/background.png "$K9_WALLPAPER_SRC"/thumbnail.png; do
    [ -f "$f" ] || continue
    local name
    name=$(basename "$f")
    if [ ! -f "$K9_RUNTIME/$name" ] || ! diff -q "$f" "$K9_RUNTIME/$name" >/dev/null 2>&1; then
      cp "$f" "$K9_RUNTIME/$name"
      ok "file: $name"
      ((synced++))
    fi
  done

  if [ "$synced" -eq 0 ]; then
    log "All assets already in sync"
  fi
}

clean_runtime() {
  # Remove Zone.Identifier files
  find "$K9_RUNTIME" -type f -name "*:Zone.Identifier" -delete 2>/dev/null || true
  find "$K9_RUNTIME" -type f -name "*:*" -delete 2>/dev/null || true

  # Remove legacy files directory if it exists
  if [ -d "$K9_RUNTIME/assets/legacy files" ]; then
    rm -rf "$K9_RUNTIME/assets/legacy files"
    ok "Removed legacy files directory"
  fi

  # Remove legacy directory
  if [ -d "$K9_RUNTIME/legacy" ]; then
    rm -rf "$K9_RUNTIME/legacy"
    ok "Removed legacy directory"
  fi
}

write_sync_health
  notify_openclaw() {
  # Notify OpenClaw if running (hot-reload trigger)
  if command -v curl &>/dev/null; then
    curl -s -o /dev/null -w '' "http://127.0.0.1:18789/reload" 2>/dev/null && \
      ok "OpenClaw hot-reload triggered" || true
  fi
}

do_sync() {
  log "Syncing K-9 wallpaper → $K9_RUNTIME"
  sync_wallpaper
  sync_assets
  clean_runtime
  write_sync_health
  notify_openclaw
  ok "Sync complete"
}

do_check() {
  local src="$K9_WALLPAPER_SRC/wallpaper.html"
  local dst="$K9_RUNTIME/wallpaper.html"

  if [ ! -f "$dst" ] || ! diff -q "$src" "$dst" >/dev/null 2>&1; then
    return 0  # sync needed
  else
    return 1  # already in sync
  fi
}

do_watch() {
  if ! command -v inotifywait &>/dev/null; then
    err "inotifywait not installed. Run: sudo apt install inotify-tools"
    return 1
  fi

  log "Watching $K9_WALLPAPER_SRC for changes..."
  inotifywait -m -r -e modify,create,delete,move \
    --exclude '\..*' \
    "$K9_WALLPAPER_SRC" 2>/dev/null | while read -r _ event file; do
    log "Change detected: $event $file"
    do_sync
  done
}

do_install() {
  # Install git post-merge hook
  local hook="$K9_REPO_DIR/.git/hooks/post-merge"

  cat > "$hook" << 'HOOK'
#!/usr/bin/env bash
# Auto-sync wallpaper on git pull/merge
exec "$(dirname "$(dirname "$0")")/k9wall-sync.sh" 2>/dev/null || true
HOOK
  chmod +x "$hook"
  ok "Installed post-merge hook: $hook"

  # Install k9wall-deploy alias
  local bashrc="$HOME/.bashrc"
  if ! grep -q "k9wall-deploy" "$bashrc" 2>/dev/null; then
    echo "alias k9wall-deploy='$K9_REPO_DIR/k9wall-sync.sh'" >> "$bashrc"
    ok "Added k9wall-deploy alias to ~/.bashrc"
  else
    log "k9wall-deploy alias already in ~/.bashrc"
  fi

  # Also add to .bash_aliases for some distros
  local bash_aliases="$HOME/.bash_aliases"
  if [ -f "$bash_aliases" ] && ! grep -q "k9wall-deploy" "$bash_aliases" 2>/dev/null; then
    echo "alias k9wall-deploy='$K9_REPO_DIR/k9wall-sync.sh'" >> "$bash_aliases"
  fi

  ok "Installation complete. Run 'source ~/.bashrc' to activate alias."
  echo -e "${D}Post-merge hook will auto-sync wallpaper after every git pull.${N}"
}

# ── Main ────────────────────────────────────────────────────────────

case "${1:-sync}" in
  sync|"")
    do_sync
    ;;
  --watch|watch)
    do_watch
    ;;
  --check|check)
    if do_check; then
      log "Sync needed"
      exit 0
    else
      log "Already in sync"
      exit 1
    fi
    ;;
  --install|install)
    do_install
    ;;
  *)
    echo "Usage: k9wall-sync.sh [sync|--watch|--check|--install]"
    echo ""
    echo "  sync        One-shot sync (default)"
    echo "  --watch     File-watch mode (auto-sync on change)"
    echo "  --check     Check if sync needed (exit 0=yes, 1=no)"
    echo "  --install   Install post-merge hook + k9wall-deploy alias"
    exit 1
    ;;
esac

# ── Sync health writer ───────────────────────────────────────────────
write_sync_health() {
  local health_file="$K9_RUNTIME/.sync-health.json"
  local commit_hash
  commit_hash=$(cd "$K9_REPO_DIR" && git rev-parse --short HEAD 2>/dev/null || echo "unknown")
  local commit_date
  commit_date=$(cd "$K9_REPO_DIR" && git log -1 --format='%ci' 2>/dev/null || echo "unknown")
  local wallpaper_lines
  wallpaper_lines=$(wc -l < "$K9_RUNTIME/wallpaper.html" 2>/dev/null || echo 0)

  cat > "$health_file" << JSON
{
  "synced": true,
  "commit": "$commit_hash",
  "commit_date": "$commit_date",
  "wallpaper_lines": $wallpaper_lines,
  "synced_at": "$(date -Iseconds)",
  "runtime_path": "$K9_RUNTIME"
}
JSON
  log "Sync health written: $commit_hash · ${wallpaper_lines} lines"
}
