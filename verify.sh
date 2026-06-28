#!/usr/bin/env bash
# ============================================================
# verify.sh — Sentry Kernel v1.0.0 proof verification
#
# Verifies the AEG-9 ZK Proof of Rationality against the
# circuit's verification key using Barretenberg Ultra Honk.
#
# Usage:
#   ./verify.sh                  — verify existing proof
#   ./verify.sh --regenerate     — re-run full prove pipeline first
#   ./verify.sh --export-sol     — also export Solidity verifier
# ============================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
CIRCUIT_DIR="$REPO_ROOT/contracts/aeg9_circuit"
PROOF_DIR="$CIRCUIT_DIR/proof_of_rationality.proof"
TARGET="$CIRCUIT_DIR/target"
VK="$CIRCUIT_DIR/vk"
SRC="$CIRCUIT_DIR/src/main.nr"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { echo -e "${GREEN}✅ $*${NC}"; }
fail() { echo -e "${RED}❌ $*${NC}"; exit 1; }
warn() { echo -e "${YELLOW}⚠️  $*${NC}"; }
step() { echo -e "\n${YELLOW}── $* ──${NC}"; }

# ── Dependency checks ─────────────────────────────────────────
step "Checking dependencies"

command -v nargo >/dev/null 2>&1 && ok "nargo $(nargo --version 2>&1 | grep 'nargo version' | awk '{print $NF}')" \
  || fail "nargo not found. Install: curl -L https://raw.githubusercontent.com/noir-lang/noirup/main/install | bash && noirup"

command -v bb >/dev/null 2>&1 && ok "bb found at $(which bb)" \
  || fail "bb not found. Install: curl -L https://raw.githubusercontent.com/AztecProtocol/aztec-packages/master/barretenberg/cpp/installation/install | bash && bbup -v 5.0.0-nightly.20260522"

# ── File checks ───────────────────────────────────────────────
step "Checking artifacts"

[ -f "$SRC" ]                     && ok "Circuit source: $SRC"         || fail "Missing circuit: $SRC"
[ -f "$TARGET/proof_of_rationality.json" ] && ok "Proving key present" || fail "Missing proving key. Run: nargo compile"
[ -f "$TARGET/proof_of_rationality.gz" ]   && ok "Witness present"     || fail "Missing witness. Run: nargo execute"
[ -f "$VK" ]                      && ok "Verification key present"     || warn "No VK found — will generate"
[ -d "$PROOF_DIR" ]               && ok "Proof directory present"      || warn "No proof directory — will generate"

# ── Optional: regenerate ──────────────────────────────────────
if [[ "${1:-}" == "--regenerate" ]]; then
  step "Regenerating witness + proof"

  cd "$CIRCUIT_DIR"

  echo "Compiling circuit..."
  nargo compile
  ok "Circuit compiled"

  echo "Regenerating Prover.toml..."
  python3 "$REPO_ROOT/src/k9_proof_shim.py"
  ok "Prover.toml generated"

  echo "Solving witness..."
  nargo execute
  ok "Witness solved → $TARGET/proof_of_rationality.gz"

  echo "Generating VK..."
  bb write_vk -b "$TARGET/proof_of_rationality.json" -o "$VK"
  ok "VK written → $VK"

  echo "Generating proof..."
  mkdir -p "$PROOF_DIR"
  bb prove \
    -b "$TARGET/proof_of_rationality.json" \
    -w "$TARGET/proof_of_rationality.gz" \
    -o "$PROOF_DIR/proof"
  ok "Proof generated → $PROOF_DIR/proof"
fi

# ── Generate VK if missing ────────────────────────────────────
if [ ! -f "$VK" ]; then
  step "Generating verification key"
  bb write_vk -b "$TARGET/proof_of_rationality.json" -o "$VK"
  ok "VK written → $VK"
fi

# ── Verify ───────────────────────────────────────────────────
step "Verifying proof"

PROOF_FILE="$PROOF_DIR/proof"
[ -f "$PROOF_FILE" ] || fail "Proof not found at $PROOF_FILE. Run: ./verify.sh --regenerate"

PROOF_SIZE=$(wc -c < "$PROOF_FILE")
ok "Proof file: $PROOF_SIZE bytes"

PUBLIC_INPUTS="$PROOF_DIR/public_inputs"
[ -f "$PUBLIC_INPUTS" ] && ok "Public inputs: $(wc -c < "$PUBLIC_INPUTS") bytes" || warn "No public_inputs file"

bb verify \
  -k "$VK" \
  -p "$PROOF_FILE" \
  2>&1

if [ $? -eq 0 ]; then
  echo ""
  ok "═══════════════════════════════════════════════"
  ok "  PROOF VALID — Sentry Kernel AEG-9 verified   "
  ok "  Six invariants confirmed:                     "
  ok "    A) drawdown_risk_bps ≤ 150                  "
  ok "    B) slippage integrity enforced              "
  ok "    C) alignment_score ≥ 80                     "
  ok "    D) blast_radius_metric ≥ 75                 "
  ok "    E) session_wallet binding present           "
  ok "    F) feature_vector_hash ≠ 0                  "
  ok "═══════════════════════════════════════════════"
else
  fail "Proof verification FAILED"
fi

# ── Optional: export Solidity verifier ───────────────────────
if [[ "${1:-}" == "--export-sol" ]] || [[ "${2:-}" == "--export-sol" ]]; then
  step "Exporting Solidity verifier"
  SOL_OUT="$REPO_ROOT/contracts/src/ProofOfRationalityVerifier.sol"
  bb contract -k "$VK" -o "$SOL_OUT"
  ok "Solidity verifier → $SOL_OUT"
  echo ""
  warn "Next step: deploy verifier to Base Sepolia"
  echo "  forge script contracts/script/DeployAEG7.s.sol --rpc-url \$BASE_SEPOLIA_RPC --broadcast --verify"
fi

echo ""
ok "verify.sh complete"
