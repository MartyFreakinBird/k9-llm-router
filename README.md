# K-9 Sentry Kernel `v1.0.0-sentry-kernel`

> Autonomous DeFi execution engine with cryptographic proof of rational intent.  
> JEPA model inference → ZK-verified trade execution → on-chain settlement.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    COGNITIVE BUS (cb.v1)                     │
│   Lovable Edge Functions → orbitron-bus → :8769 Sovereign   │
└─────────────────────────────────────────────────────────────┘
           │                        │
    ┌──────▼──────┐         ┌───────▼───────┐
    │  LLM ROUTER │         │  SENTRY KERNEL │
    │   :8765     │         │               │
    │             │         │  AEG-7        │
    │  CB-2 Cache │         │  SessionKey   │
    │  CB-2 RAG   │         │  Wallet       │
    │  CB-2 Guard │         │               │
    │  CB-3 SQL   │         │  AEG-9        │
    └─────────────┘         │  ZK Proof of  │
                            │  Rationality  │
                            └───────────────┘
                                    │
                            ┌───────▼───────┐
                            │  BASE SEPOLIA  │
                            │  SessionKey   │
                            │  Wallet +     │
                            │  Verifier.sol │
                            └───────────────┘
```

---

## Sprints in this release

| Sprint | Component | Status |
|--------|-----------|--------|
| CB-1 | Cognitive Bus — cb.v1 schema + orbitron-bus edge functions | ✅ |
| CB-2 | Semantic Cache (FAISS+Redis) + Guardrails + Hybrid RAG | ✅ |
| CB-3 | Text-to-SQL query engine on cb_messages | ✅ |
| AEG-7 | SessionKeyWallet — ERC-4337 + 7 Genesis Risk Parameters | ✅ |
| AEG-9 | ZK Proof of Rationality — Noir circuit + Barretenberg | ✅ |

---

## ZK Circuit — Proof of Rationality

**File:** `contracts/aeg9_circuit/src/main.nr`  
**Opcodes:** 275 ACIR + 61 Brillig  
**Prover:** Ultra Honk (Barretenberg)

The circuit proves — without revealing JEPA model weights — that a DeFi trade intent was rationally derived from real market data and satisfies all risk parameters.

### Six invariants enforced

| ID | Invariant | Threshold |
|----|-----------|-----------|
| A | Drawdown risk | `predicted_drawdown_risk_bps ≤ 150` (1.5%) |
| B | Slippage integrity | `min_amount_out = amount_in − (impact_bps × amount_in / 10000)` |
| C | Alignment score | `alignment_score ≥ 80` (0.80 governance grade) |
| D | Blast radius | `blast_radius_metric ≥ 75` (exposure ≤ 25%) |
| E | Wallet binding | proof bound to `session_wallet` address (anti-replay) |
| F | Feature vector | `feature_vector_hash ≠ 0` (real market data present) |

### Proof artifacts

```
contracts/aeg9_circuit/
├── src/main.nr                          # Circuit source
├── Nargo.toml                           # Noir package config
├── Prover.toml                          # Witness inputs (pre-validated)
├── target/
│   ├── proof_of_rationality.json        # Compiled circuit (proving key)
│   └── proof_of_rationality.gz          # Solved witness (7,656 bytes)
├── proof_of_rationality.proof/
│   ├── proof                            # SNARK proof bytes (Ultra Honk)
│   └── public_inputs                    # Public input vector
└── vk                                   # Verification key
```

---

## SessionKeyWallet — AEG-7

**File:** `contracts/src/SessionKeyWallet.sol`  
**Standard:** ERC-4337

### Genesis Risk Parameters (hardcoded)

| Parameter | Value |
|-----------|-------|
| maxDrawdownBps | 150 (1.5%) |
| maxPoolShareBps | 200 (2%) |
| maxPositionUsd | $25,000 |
| slippageBlueChip | 30 bps |
| slippageAlt | 75 bps |
| maxGasUsd | $3.00 |
| cooldownSeconds | 60 |

### Whitelisted protocols (Base Mainnet)
- Uniswap V3: `0x2626664c2603336E57B271c5C0b26F421741e481`
- Aave V3, Curve, Balancer V2

---

## CB-2 — Semantic Cache + Guardrails + Hybrid RAG

**Semantic Cache** (`src/semantic_cache.py`)
- FAISS IndexFlatIP + Redis backend
- all-MiniLM-L6-v2 local embeddings
- Cosine similarity threshold: 0.92
- TTL: 1 hour

**Guardrails** (`src/guardrails.py`)
- Prompt injection detection (9 regex patterns)
- Toxicity classification (distilbert)
- PII detection + redaction (Presidio + regex fallback)
- Redis audit stream `k9:guardrails:audit`

**Hybrid RAG** (`src/hybrid_rag.py`)
- Dense: Supabase pgvector RPC
- Sparse: BM25Okapi (rank-bm25)
- Fusion: Reciprocal Rank Fusion (k=60)
- Rerank: Cohere rerank-english-v3.0

---

## CB-3 — Text-to-SQL

**File:** `src/text_to_sql.py`

Query K-9 execution history in natural language:

```bash
curl -X POST http://localhost:8765/query \
  -H "Content-Type: application/json" \
  -d '{"question": "Show all timeouts on Ollama in the last 48 hours"}'
```

Safety: SELECT-only, LIMIT injection, rate limiting (10 q/min), 30s timeout.

---

## Quick start

```bash
# 1. Clone
git clone git@github.com:MartyFreakinBird/k9-llm-router.git
cd k9-llm-router

# 2. Install Python deps
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env  # fill in SUPABASE_URL, COHERE_API_KEY, REDIS_URL

# 4. Start the router
python main.py

# 5. Verify the ZK proof (see verify.sh)
./verify.sh
```

---

## Deployment targets

| Component | Target | Command |
|-----------|--------|---------|
| LLM Router | Local WSL2 :8765 | `python main.py` |
| SessionKeyWallet | Base Sepolia | `forge script contracts/script/DeployAEG7.s.sol --broadcast` |
| ProofOfRationalityVerifier | Base Sepolia | `bb contract -k vk -o contracts/src/ProofOfRationalityVerifier.sol` |
| orbitron-bus | Supabase Edge | `supabase functions deploy orbitron-bus` |
| cb_messages table | Supabase | Run `k9-integration/orbitron-bus/migration_cb_messages.sql` |

---

## Architecture invariants

- **Lovable = ingress + persistence + UI only.** Never executes trades.
- **Sovereign :8769 kernel** = sole signing + execution authority.
- All `execution_request` messages require `signature` field — unsigned → 403.
- JEPA target encoder: EMA momentum=0.999, **no backprop through target encoder**.
- AEG scoring gate: `risk_score > 0.9` OR `leverage > 10` → SLASH.

---

## Toolchain

```
nargo 1.0.0-beta.22
Barretenberg (bb) — Ultra Honk prover
Python 3.11+ / FastAPI / uvicorn
Foundry (forge, cast, anvil)
Deno Oak (control-plane :8769)
```

---

*K-9 Sentry Kernel — built by VectOS Carbon + Vectos*
