#!/usr/bin/env bash
# ============================================================
# K-9 Pipeline Meta-Script
# Runs Replit import OR wallpaper deploy based on context
# Usage:
#   ./k9_pipeline_meta.sh replit <service_name> <export_path>
#   ./k9_pipeline_meta.sh wallpaper <wallpaper_src_path>
#   ./k9_pipeline_meta.sh both <service_name> <export_path> <wallpaper_src_path>
# ============================================================

set -e

MODE=$1
GITHUB_BASE="https://github.com/MartyFreakinBird"

log() { echo "[K9-PIPELINE] $*"; }
fail() { echo "[K9-PIPELINE ERROR] $*" >&2; exit 1; }

run_replit_import() {
  SERVICE_NAME=$1
  EXPORT_PATH=$2
  [ -z "$SERVICE_NAME" ] && fail "service_name required"
  [ -z "$EXPORT_PATH" ] && fail "export_path required"
  [ -d "$EXPORT_PATH" ] || fail "Export path not found: $EXPORT_PATH"

  log "Starting Replit import: $SERVICE_NAME"
  git pull origin main

  mkdir -p "services/$SERVICE_NAME"
  cp -R "$EXPORT_PATH"/. "services/$SERVICE_NAME/"

  # Remove Replit artifacts
  rm -f "services/$SERVICE_NAME/.replit"
  rm -f "services/$SERVICE_NAME/replit.nix"
  rm -f "services/$SERVICE_NAME/.env"
  log "Cleaned Replit artifacts"

  # Generate README
  cat > "services/$SERVICE_NAME/README.md" << RDME
# $SERVICE_NAME
Imported from Replit export. Part of the K-9 ecosystem.
RDME

  # Generate Dockerfile if missing
  if [ ! -f "services/$SERVICE_NAME/Dockerfile" ]; then
    cat > "services/$SERVICE_NAME/Dockerfile" << DKR
FROM node:20-slim AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci || true
COPY . .
RUN npm run build || true

FROM node:20-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/dist ./dist
COPY --from=builder /app/node_modules ./node_modules
COPY package*.json ./
EXPOSE 5000
CMD ["npm", "run", "start"]
DKR
    log "Generated Dockerfile"
  fi

  # Generate CI workflow
  mkdir -p .github/workflows
  cat > ".github/workflows/${SERVICE_NAME}_ci.yml" << CIEOF
name: ${SERVICE_NAME} CI
on:
  push:
    branches: [main]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
          cache: 'npm'
      - run: cd services/${SERVICE_NAME} && npm ci || true
      - run: cd services/${SERVICE_NAME} && npm run build || true
      - run: docker build -t ${SERVICE_NAME}-test services/${SERVICE_NAME}
CIEOF
  log "Generated CI workflow"

  git add .
  git commit -m "Import Replit service: $SERVICE_NAME"
  git push origin main
  log "Pushed: $SERVICE_NAME → $GITHUB_BASE/k9-llm-router"
}

run_wallpaper_deploy() {
  WALLPAPER_SRC=$1
  [ -z "$WALLPAPER_SRC" ] && fail "wallpaper_src_path required"
  [ -d "$WALLPAPER_SRC" ] || fail "Wallpaper source not found: $WALLPAPER_SRC"

  log "Starting wallpaper deploy from: $WALLPAPER_SRC"
  git pull origin main

  mkdir -p wallpaper
  cp -R "$WALLPAPER_SRC"/. wallpaper/

  if [ -f wallpaper/package.json ]; then
    log "JS build system detected — installing deps"
    (cd wallpaper && npm install)
    log "Building wallpaper bundle"
    (cd wallpaper && npm run build-wallpaper 2>/dev/null || npm run build 2>/dev/null || true)
  fi

  if [ -d wallpaper/dist ]; then
    zip -r wallpaper-dist.zip wallpaper/dist
    log "Packaged: wallpaper-dist.zip"
  else
    log "No dist folder — skipping packaging (static assets only)"
  fi

  git add .
  git commit -m "Update Octos wallpaper bundle"
  git push origin main
  log "Wallpaper deployed → $GITHUB_BASE/k9-llm-router"
}

# ---- Dispatch ----
case "$MODE" in
  replit)
    run_replit_import "$2" "$3"
    ;;
  wallpaper)
    run_wallpaper_deploy "$2"
    ;;
  both)
    run_replit_import "$2" "$3"
    run_wallpaper_deploy "$4"
    ;;
  *)
    fail "Unknown mode: $MODE. Use: replit | wallpaper | both"
    ;;
esac

log "Pipeline complete."
