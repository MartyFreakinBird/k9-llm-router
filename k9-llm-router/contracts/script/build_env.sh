#!/usr/bin/env bash
# ============================================================
# build_env.sh — Generate .env for AEG-4 + AEG-6 deploy
#
# If AEG-4 has already been deployed:
#   Reads deployments/base-sepolia.json from aeg-protocol repo
#   Extracts AEGTreasury + AEGGovernor addresses automatically
#
# If AEG-4 has NOT been deployed:
#   Generates a session key and outputs a full .env template
#   ready for AEG-4 → AEG-6 sequential deploy
#
# Usage:
#   cd ~/k9-llm-router
#   ./contracts/script/build_env.sh [--aeg-protocol-path ~/aeg-protocol]
# ============================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
AEG_PROTOCOL_PATH="${2:-$HOME/aeg-protocol}"
ENV_OUT="$REPO_ROOT/.env"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✅ $*${NC}"; }
info() { echo -e "${CYAN}ℹ  $*${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $*${NC}"; }

echo ""
echo "═══════════════════════════════════════════"
echo "   K-9 Sentry Kernel — .env Builder"
echo "═══════════════════════════════════════════"
echo ""

# ── Dependency check ──────────────────────────────────────────
command -v openssl >/dev/null 2>&1 || { echo "openssl required"; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 required"; exit 1; }

# ── Generate session key ──────────────────────────────────────
info "Generating fresh session key (Ed25519 → Ethereum)..."
SESSION_PK=$(openssl rand -hex 32)

# Derive Ethereum address from private key using Python (no ethers dep needed)
SESSION_ADDRESS=$(python3 - << PYEOF
import hashlib, struct

# Minimal secp256k1 pubkey → keccak → ETH address
# Uses coincurve if available, otherwise prints placeholder
try:
    from eth_keys import keys
    pk_bytes = bytes.fromhex("$SESSION_PK")
    pk = keys.PrivateKey(pk_bytes)
    print(pk.public_key.to_checksum_address())
except ImportError:
    try:
        import coincurve, sha3
        pk_bytes = bytes.fromhex("$SESSION_PK")
        pub = coincurve.PublicKey.from_valid_secret(pk_bytes).format(compressed=False)[1:]
        k = sha3.keccak_256()
        k.update(pub)
        print("0x" + k.hexdigest()[-40:])
    except ImportError:
        print("0x_INSTALL_eth_keys_to_derive: pip install eth-keys")
PYEOF
)

ok "Session private key generated (32 bytes)"
info "Session address: $SESSION_ADDRESS"
echo ""

# ── Check AEG-4 deployment manifest ──────────────────────────
MANIFEST="$AEG_PROTOCOL_PATH/deployments/base-sepolia.json"
AEG4_DEPLOYED=false

TREASURY_ADDR=""
GOVERNOR_ADDR=""
AEG_TOKEN_ADDR=""
POA_ADDR=""

if [ -f "$MANIFEST" ] && [ "$(cat "$MANIFEST")" != "" ] && [ "$(wc -c < "$MANIFEST")" -gt 10 ]; then
    AEG4_DEPLOYED=true
    ok "AEG-4 deployment manifest found: $MANIFEST"
    TREASURY_ADDR=$(python3 -c "import json; d=json.load(open('$MANIFEST')); print(d.get('aegTreasury',''))")
    GOVERNOR_ADDR=$(python3 -c "import json; d=json.load(open('$MANIFEST')); print(d.get('aegGovernor',''))")
    AEG_TOKEN_ADDR=$(python3 -c "import json; d=json.load(open('$MANIFEST')); print(d.get('aegToken',''))")
    POA_ADDR=$(python3 -c "import json; d=json.load(open('$MANIFEST')); print(d.get('proofOfAlignment',''))")
    ok "AEGTreasury:       $TREASURY_ADDR"
    ok "AEGGovernor:       $GOVERNOR_ADDR"
    ok "AEGToken:          $AEG_TOKEN_ADDR"
    ok "ProofOfAlignment:  $POA_ADDR"
else
    warn "AEG-4 not yet deployed (deployments/base-sepolia.json is empty or missing)"
    warn "You must run AEG-4 deploy first, THEN AEG-6"
    TREASURY_ADDR="0x_FILL_AFTER_AEG4_DEPLOY"
    GOVERNOR_ADDR="0x_FILL_AFTER_AEG4_DEPLOY"
    AEG_TOKEN_ADDR="0x_FILL_AFTER_AEG4_DEPLOY"
    POA_ADDR="0x_FILL_AFTER_AEG4_DEPLOY"
fi

# ── Write .env ────────────────────────────────────────────────
echo ""
info "Writing .env to $ENV_OUT ..."

cat > "$ENV_OUT" << EOF
# ============================================================
# K-9 Sentry Kernel — Auto-generated $(date -u +"%Y-%m-%d %H:%M UTC")
# NEVER COMMIT THIS FILE
# ============================================================

# ── Deployer (fill before deploy) ────────────────────────────
DEPLOYER_PK=0x_YOUR_DEPLOYER_PRIVATE_KEY
OWNER_ADDRESS=0x_YOUR_EOA_OR_MULTISIG

# ── Session Key (auto-generated — KEEP SECRET) ───────────────
# Private key: $SESSION_PK
# ⚠️  Store the private key offline. Only the address goes on-chain.
SESSION_KEY_ADDRESS=$SESSION_ADDRESS

# ── AEG-4 Deployed Contracts ──────────────────────────────────
AEG_TREASURY_ADDRESS=$TREASURY_ADDR
AEG_GOVERNOR_ADDRESS=$GOVERNOR_ADDR
AEG_TOKEN_ADDRESS=$AEG_TOKEN_ADDR
POA_CONTRACT_ADDRESS=$POA_ADDR

# ── Post-AEG-6 (fill after deploy) ───────────────────────────
SESSION_WALLET_ADDRESS=0x_FILL_AFTER_AEG6_DEPLOY
VERIFIER_ADDRESS=0x_FILL_AFTER_AEG6_DEPLOY

# ── RPC / Chain ───────────────────────────────────────────────
BASE_SEPOLIA_RPC=https://sepolia.base.org
BASE_MAINNET_RPC=https://mainnet.base.org
BASESCAN_API_KEY=_GET_FROM_BASESCAN_ORG

# ── K-9 Runtime ──────────────────────────────────────────────
K9_JEPA_ENDPOINT=http://localhost:8765/route
CIRCUITS_DIR=./contracts/aeg9_circuit
PROOFS_DIR=./proofs

# ── Orbitron / Supabase ───────────────────────────────────────
SUPABASE_URL=https://ziqenqqgnqxqrazmjohs.supabase.co
SUPABASE_ANON_KEY=_GET_FROM_SUPABASE_DASHBOARD
SUPABASE_SERVICE_KEY=_GET_FROM_SUPABASE_DASHBOARD
ORBITRON_WEBHOOK_URL=_GET_FROM_LOVABLE_EDGE_FUNCTIONS
ORBITRON_WEBHOOK_SECRET=_SET_SHARED_SECRET

# ── CB-2 ─────────────────────────────────────────────────────
REDIS_URL=redis://localhost:6379
COHERE_API_KEY=_GET_FROM_COHERE

# ── Guardrails ────────────────────────────────────────────────
GUARDRAILS_ENABLED=true
GUARDRAILS_BLOCK_PII=true
GUARDRAILS_TOX_THRESH=0.7

# ── AEG Token Model ──────────────────────────────────────────
AEG_INTEGRATION_KEY=_GET_FROM_SUPABASE_API_CONFIGURATIONS_TABLE
AEG_NODE_ID=$SESSION_ADDRESS
AEG_OPERATOR_KEY=_REPLACE_WITH_REAL_ED25519_BEFORE_MAINNET

# ── Gemini ────────────────────────────────────────────────────
GEMINI_API_KEY=_GET_FROM_GOOGLE_AI_STUDIO
EOF

ok ".env written to $ENV_OUT"
echo ""

# ── Print next steps ──────────────────────────────────────────
echo "═══════════════════════════════════════════"
if $AEG4_DEPLOYED; then
    echo "  ✅ AEG-4 addresses auto-populated"
    echo "  Fill: DEPLOYER_PK, OWNER_ADDRESS, BASESCAN_API_KEY"
    echo "  Then: ./contracts/script/aeg6_deploy.sh --dry-run"
    echo "  Then: ./contracts/script/aeg6_deploy.sh"
else
    echo "  ⚠️  AEG-4 NOT YET DEPLOYED"
    echo ""
    echo "  Step 1 — Deploy AEG-4 first:"
    echo "    cd ~/aeg-protocol"
    echo "    cp .env.example .env && nano .env  # fill PRIVATE_KEY, PROTOCOL_ADDRESS"
    echo "    forge script contracts/script/Deploy.s.sol \\"
    echo "      --rpc-url \$BASE_SEPOLIA_RPC --broadcast --verify"
    echo ""
    echo "  Step 2 — Re-run this script to pick up the addresses:"
    echo "    cd ~/k9-llm-router"
    echo "    ./contracts/script/build_env.sh --aeg-protocol-path ~/aeg-protocol"
    echo ""
    echo "  Step 3 — Deploy AEG-6:"
    echo "    ./contracts/script/aeg6_deploy.sh --dry-run"
    echo "    ./contracts/script/aeg6_deploy.sh"
fi
echo "═══════════════════════════════════════════"
