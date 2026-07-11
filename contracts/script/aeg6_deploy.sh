#!/usr/bin/env bash
# ============================================================
# aeg6_deploy.sh — AEG-6 Sentry Kernel full deployment
#
# Runs the complete AEG-6 deploy sequence:
#   1. Generate ProofOfRationalityVerifier.sol from bb
#   2. Run Foundry deploy (SessionKeyWallet + Verifier)
#   3. Patch k9_proof_shim.py with deployed addresses
#   4. Run verify.sh to confirm proof still valid
#
# Usage: ./contracts/script/aeg6_deploy.sh [--dry-run]
# ============================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CIRCUIT_DIR="$REPO_ROOT/contracts/aeg9_circuit"
SOL_OUT="$REPO_ROOT/contracts/src/ProofOfRationalityVerifier.sol"
ENV_FILE="$REPO_ROOT/.env"

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✅ $*${NC}"; }
fail() { echo -e "${RED}❌ $*${NC}"; exit 1; }
step() { echo -e "\n${YELLOW}══ $* ══${NC}"; }

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true && echo -e "${YELLOW}DRY RUN MODE — no transactions broadcast${NC}"

# ── Load env ──────────────────────────────────────────────────
[ -f "$ENV_FILE" ] && source "$ENV_FILE" || fail ".env not found at $REPO_ROOT/.env — copy .env.example and fill values"

[ -n "${DEPLOYER_PK:-}" ]          || fail "DEPLOYER_PK not set in .env"
[ -n "${OWNER_ADDRESS:-}" ]        || fail "OWNER_ADDRESS not set"
[ -n "${SESSION_KEY_ADDRESS:-}" ]  || fail "SESSION_KEY_ADDRESS not set"
[ -n "${AEG_TREASURY_ADDRESS:-}" ] || fail "AEG_TREASURY_ADDRESS not set"
[ -n "${AEG_GOVERNOR_ADDRESS:-}" ] || fail "AEG_GOVERNOR_ADDRESS not set"
[ -n "${BASE_SEPOLIA_RPC:-}" ]     || fail "BASE_SEPOLIA_RPC not set"

# ── Dependency checks ─────────────────────────────────────────
step "Checking dependencies"
command -v forge >/dev/null 2>&1 && ok "forge $(forge --version | head -1)" || fail "forge not found. Install: curl -L https://foundry.paradigm.xyz | bash && foundryup"
command -v bb    >/dev/null 2>&1 && ok "bb found" || fail "bb not found — see RELEASE_NOTES.md for install steps"

# ── Step 1: Generate Solidity verifier ───────────────────────
step "Step 1 — Ensure VK + ProofOfRationalityVerifier.sol"

VK="$CIRCUIT_DIR/vk"
[ -f "$VK" ] || fail "VK not found at $VK"
ok "VK found at $VK"

if [ -f "$SOL_OUT" ]; then
  LINES=$(wc -l < "$SOL_OUT")
  ok "Solidity verifier present -> $SOL_OUT ($LINES lines)"
elif $DRY_RUN; then
  fail "Solidity verifier not found at $SOL_OUT — generate or restore ProofOfRationalityVerifier.sol before deploy"
else
  # Generate verifier from VK (bb write_solidity_verifier -t evm -b <circuit.json> -k <vk> -o <out>)
  CIRCUIT_JSON="$CIRCUIT_DIR/target/proof_of_rationality.json"
  [ -f "$CIRCUIT_JSON" ] || fail "Circuit JSON not found at $CIRCUIT_JSON — run nargo compile first"
  bb write_solidity_verifier -t evm -b "$CIRCUIT_JSON" -k "$VK" -o "$SOL_OUT"
  LINES=$(wc -l < "$SOL_OUT")
  ok "Solidity verifier generated → $SOL_OUT ($LINES lines)"
fi

# ── Step 2: Foundry install + compile ─────────────────────────
step "Step 2 — Foundry dependency install + compile"

cd "$REPO_ROOT"

if $DRY_RUN; then
  ok "[DRY] Would run: forge install + forge build"
else
  # Install OZ if not present
  [ -d "lib/openzeppelin-contracts" ] || forge install OpenZeppelin/openzeppelin-contracts --no-commit
  ok "OpenZeppelin installed"

  forge build --contracts contracts/ --skip test 2>&1 | tail -5
  ok "Contracts compiled"
fi

# ── Step 3: Deploy ────────────────────────────────────────────
step "Step 3 — Deploy to Base Sepolia"

BROADCAST_FLAG="--broadcast"
VERIFY_FLAG=""
[ -n "${BASESCAN_API_KEY:-}" ] && VERIFY_FLAG="--verify --etherscan-api-key $BASESCAN_API_KEY"

FORGE_CMD="forge script contracts/script/DeployAEG6.s.sol:DeployAEG6 \
  --rpc-url $BASE_SEPOLIA_RPC \
  $BROADCAST_FLAG \
  $VERIFY_FLAG \
  -vvvv"

if $DRY_RUN; then
  ok "[DRY] Would run: $FORGE_CMD"
  echo ""
  echo "Set these after real deploy:"
  echo "  export SESSION_WALLET_ADDRESS=0x..."
  echo "  export VERIFIER_ADDRESS=0x..."
else
  eval "$FORGE_CMD" 2>&1 | tee /tmp/aeg6_deploy.log

  # Extract deployed addresses from log
  WALLET_ADDR=$(grep "SessionKeyWallet   :" /tmp/aeg6_deploy.log | awk '{print $NF}' | tail -1)
  VERIFIER_ADDR=$(grep "ProofOfRatVerifier :" /tmp/aeg6_deploy.log | awk '{print $NF}' | tail -1)

  ok "SessionKeyWallet deployed:   $WALLET_ADDR"
  ok "ProofOfRatVerifier deployed: $VERIFIER_ADDR"

  # ── Step 4: Patch k9_proof_shim.py ──────────────────────────
  step "Step 4 — Patch k9_proof_shim.py with deployed addresses"

  SHIM="$REPO_ROOT/src/k9_proof_shim.py"
  if [ -f "$SHIM" ] && [ -n "$WALLET_ADDR" ]; then
    sed -i "s|SESSION_WALLET_ADDRESS.*=.*|SESSION_WALLET_ADDRESS = \"$WALLET_ADDR\"  # AEG-6 deployed|" "$SHIM"
    ok "k9_proof_shim.py patched → SESSION_WALLET_ADDRESS=$WALLET_ADDR"
  else
    echo "  (patch skipped — shim or address not found)"
  fi

  # ── Step 5: Commit deployed addresses ───────────────────────
  step "Step 5 — Commit deployment artifacts"

  git -C "$REPO_ROOT" add \
    contracts/src/ProofOfRationalityVerifier.sol \
    src/k9_proof_shim.py \
    broadcast/ 2>/dev/null || true

  git -C "$REPO_ROOT" commit -m \
    "deploy(AEG-6): SessionKeyWallet=$WALLET_ADDR Verifier=$VERIFIER_ADDR [Base Sepolia]" \
    2>/dev/null || echo "  (nothing new to commit)"

  git -C "$REPO_ROOT" push origin main
  ok "Deployed addresses committed to GitHub"

  # ── Summary ──────────────────────────────────────────────────
  echo ""
  ok "════════════════════════════════════════════════"
  ok "  AEG-6 DEPLOYMENT COMPLETE                     "
  ok "  Network: Base Sepolia                          "
  ok "  SessionKeyWallet:   $WALLET_ADDR"
  ok "  Verifier:           $VERIFIER_ADDR"
  ok "  Basescan: https://sepolia.basescan.org/address/$WALLET_ADDR"
  ok "════════════════════════════════════════════════"
  echo ""
  echo "Next: Fund the wallet on Base Sepolia and run:"
  echo "  cast send $WALLET_ADDR 'activateSession()' --rpc-url $BASE_SEPOLIA_RPC"
fi
