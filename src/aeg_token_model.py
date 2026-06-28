"""
aeg_token_model.py — AEG Token Model
─────────────────────────────────────────────────────────────────────────────
Base44 Superagent Vectors — Sprint AEG-5
Alignment: BASE44_SUPERAGENT_ALIGNMENT_PROMPT.md v1.0

Responsibilities:
  - Score $AEG token utility (infrastructure-grade / utility-backed / speculative)
  - Push scores to Orbitron /external-integration/insights
  - Submit governance proofs to /proof-of-alignment before autonomous actions
  - Send heartbeat to /external-integration/heartbeat every 60s
  - Pull FedWhisperer macro context and factor into risk scoring
  - Route signals to /signal-aggregator (source: "base44-aeg-vector")

Governance rules (from alignment doc):
  - risk_score > 0.9 → SLASH (do not submit)
  - leverage > 10    → SLASH (do not submit)
  - All autonomous actions require Proof-of-Alignment oracle BEFORE execution
  - Agent ID: "base44-aeg-vector"
  - Log prefix: 🐕 AEG:

Run:
  uvicorn aeg_token_model:app --host 0.0.0.0 --port 9003
─────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()
log = logging.getLogger("aeg-token-model")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [🐕 AEG] %(levelname)s %(message)s")

# ── Config ────────────────────────────────────────────────────────────────────

SUPABASE_URL       = os.getenv("SUPABASE_URL", "")
INTEGRATION_KEY    = os.getenv("AEG_INTEGRATION_KEY", "")      # x-integration-key header
AGENT_ID           = "base44-aeg-vector"
MODEL_VERSION      = "1.0.0"
PORT               = int(os.getenv("AEG_TOKEN_MODEL_PORT", "9003"))
HEARTBEAT_INTERVAL = 60  # seconds

_start        = time.time()
_signals_processed = 0

# ── Classification ────────────────────────────────────────────────────────────

Classification = Literal["infrastructure-grade", "utility-backed", "speculative"]


@dataclass
class AEGTokenScore:
    token:                str
    utility_score:        float          # 0–100
    gas_usage:            bool
    collateral_accepted:  bool
    governance_weight:    float          # 0–1.0
    integration_count:    int
    classification:       Classification
    confidence:           float          # 0.0–1.0
    risk_score:           float          # 0.0–1.0 (>0.9 → slash)
    leverage:             float          # (>10 → slash)
    fed_aligned:          bool           # factored from FedWhisperer
    entropy_state:        str            # low|medium|high
    recommendations:      list[str]      = field(default_factory=list)
    timestamp:            str            = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def compute_aeg_score(
    on_chain_data:   dict[str, Any],
    fed_context:     dict[str, Any],
    entropy_state:   str = "medium",
) -> AEGTokenScore:
    """
    Compute AEG token utility score.
    Inputs sourced from:
      - on_chain_data: k9-quant-engine / onchain_data tool
      - fed_context:   GET /external-integration/sync → fed_policy_signals
      - entropy_state: pulled from platform entropy_states table
    """
    # ── Base utility metrics ──────────────────────────────────────────────
    staking_depth     = on_chain_data.get("staking_depth",        0.0)   # 0–1
    governance_active = on_chain_data.get("governance_proposals",  0)
    integrations      = on_chain_data.get("integration_count",     0)
    collateral_use    = on_chain_data.get("collateral_accepted",   False)
    gas_use           = on_chain_data.get("gas_usage",             False)
    circulating_pct   = on_chain_data.get("circulating_pct",       0.0)  # 0–1

    # ── Utility score (0–100) ─────────────────────────────────────────────
    utility_score = (
        staking_depth       * 30 +
        min(integrations/10, 1.0) * 25 +
        (1.0 if collateral_use else 0.0) * 20 +
        (1.0 if gas_use else 0.0) * 15 +
        min(governance_active/5, 1.0) * 10
    )

    # ── Classification ────────────────────────────────────────────────────
    if utility_score >= 70 and (gas_use or collateral_use) and staking_depth > 0.5:
        classification: Classification = "infrastructure-grade"
    elif utility_score >= 40:
        classification = "utility-backed"
    else:
        classification = "speculative"

    # ── Governance weight (ERC20Votes delegation ratio) ───────────────────
    governance_weight = min(staking_depth * 0.8 + (governance_active / 20) * 0.2, 1.0)

    # ── Fed alignment ─────────────────────────────────────────────────────
    fed_stance = fed_context.get("fed_stance", "neutral")  # hawkish|dovish|neutral
    fed_aligned = fed_stance == "dovish"

    # ── Risk score ────────────────────────────────────────────────────────
    base_risk = 1.0 - (utility_score / 100)
    entropy_penalty = {"low": 0.0, "medium": 0.1, "high": 0.25}.get(entropy_state, 0.1)
    fed_penalty = 0.15 if fed_stance == "hawkish" else 0.0
    risk_score = min(base_risk + entropy_penalty + fed_penalty, 1.0)

    # ── Recommendations ───────────────────────────────────────────────────
    recs = []
    if fed_stance == "hawkish" and entropy_state == "low":
        recs.append("Reduce position sizing — hawkish Fed + low entropy regime")
    if fed_stance == "dovish" and entropy_state == "high":
        recs.append("Flag potential breakout — dovish Fed + high entropy")
    if risk_score > 0.7:
        recs.append("Elevated risk — await Proof-of-Alignment oracle clearance")
    if classification == "infrastructure-grade":
        recs.append("$AEG qualifies as collateral — eligible for DeFi gate deposit")

    confidence = max(0.0, min(1.0, (utility_score / 100) * (1.0 - risk_score * 0.3)))

    return AEGTokenScore(
        token="AEG",
        utility_score=round(utility_score, 2),
        gas_usage=gas_use,
        collateral_accepted=collateral_use,
        governance_weight=round(governance_weight, 4),
        integration_count=integrations,
        classification=classification,
        confidence=round(confidence, 4),
        risk_score=round(risk_score, 4),
        leverage=on_chain_data.get("leverage", 1.0),
        fed_aligned=fed_aligned,
        entropy_state=entropy_state,
        recommendations=recs,
    )

# ── Orbitron Client ───────────────────────────────────────────────────────────

def _orbitron_headers() -> dict[str, str]:
    return {
        "Content-Type":    "application/json",
        "x-integration-key": INTEGRATION_KEY,
    }


async def _post_orbitron(client: httpx.AsyncClient, endpoint: str, payload: dict) -> dict:
    url = f"{SUPABASE_URL}/functions/v1/{endpoint}"
    try:
        r = await client.post(url, json=payload, headers=_orbitron_headers(), timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("🐕 AEG: Orbitron POST /%s failed: %s", endpoint, e)
        return {"error": str(e)}


async def pull_platform_context(client: httpx.AsyncClient) -> dict:
    """GET /external-integration/sync → fed_policy_signals + trading_signals."""
    url = f"{SUPABASE_URL}/functions/v1/external-integration"
    try:
        r = await client.post(
            url,
            json={"action": "sync", "agent_id": AGENT_ID},
            headers=_orbitron_headers(),
            timeout=10,
        )
        return r.json() if r.is_success else {}
    except Exception as e:
        log.warning("🐕 AEG: sync failed: %s", e)
        return {}


async def proof_of_alignment(
    client: httpx.AsyncClient,
    action_description: str,
    risk_score: float,
    leverage: float,
    metadata: dict,
) -> tuple[bool, str]:
    """
    Submit action to Proof-of-Alignment oracle.
    Returns (approved, proof_hash).
    SLASH conditions: risk_score > 0.9 OR leverage > 10
    """
    if risk_score > 0.9:
        log.warning("🐕 AEG: SLASH — risk_score %.3f > 0.9, action BLOCKED", risk_score)
        return False, ""
    if leverage > 10:
        log.warning("🐕 AEG: SLASH — leverage %.1f > 10, action BLOCKED", leverage)
        return False, ""

    action_hash = hashlib.sha256(
        json.dumps({"action": action_description, "metadata": metadata}, sort_keys=True).encode()
    ).hexdigest()

    payload = {
        "action":      "generate",
        "output":      action_description,
        "action_type": "aeg_token_score",
        "agent_id":    AGENT_ID,
        "metadata":    {**metadata, "risk_score": risk_score, "leverage": leverage},
    }

    result = await _post_orbitron(client, "proof-of-alignment", payload)
    approved = "error" not in result
    log.info("🐕 AEG: PoA result — approved=%s hash=%s", approved, action_hash[:16])
    return approved, action_hash


async def push_token_score(client: httpx.AsyncClient, score: AEGTokenScore) -> dict:
    """Push AEG token score to /external-integration/insights."""
    payload = {
        "insight_type": "aeg_token_score",
        "confidence":   score.confidence,
        "data": {
            "token":                score.token,
            "utility_score":        score.utility_score,
            "gas_usage":            score.gas_usage,
            "collateral_accepted":  score.collateral_accepted,
            "governance_weight":    score.governance_weight,
            "integration_count":    score.integration_count,
            "classification":       score.classification,
        },
        "recommendations": score.recommendations,
    }
    return await _post_orbitron(client, "external-integration", {**payload, "action": "insights"})


async def push_signal(client: httpx.AsyncClient, score: AEGTokenScore) -> dict:
    """Route AEG signal to /signal-aggregator."""
    direction = (
        "bullish"  if score.utility_score >= 70 else
        "bearish"  if score.utility_score < 40 else
        "neutral"
    )
    payload = {
        "source":      AGENT_ID,
        "signal_type": "token_utility",
        "asset":       "AEG",
        "direction":   direction,
        "confidence":  score.confidence,
        "timeframe":   "1d",
        "metadata": {
            "model_version": MODEL_VERSION,
            "entropy_state": score.entropy_state,
            "fed_alignment": score.fed_aligned,
        },
    }
    return await _post_orbitron(client, "signal-aggregator", payload)


async def send_heartbeat(client: httpx.AsyncClient) -> dict:
    """POST /external-integration/heartbeat every 60s."""
    global _signals_processed
    payload = {
        "action":    "heartbeat",
        "agent_id":  AGENT_ID,
        "metrics": {
            "uptime":            round(time.time() - _start, 1),
            "signals_processed": _signals_processed,
            "last_action":       datetime.now(timezone.utc).isoformat(),
        },
    }
    return await _post_orbitron(client, "external-integration", payload)


async def send_emergency(client: httpx.AsyncClient, alert_type: str, message: str, data: dict) -> dict:
    """Bypass consensus — critical alert to platform owner."""
    payload = {
        "action":     "emergency",
        "alert_type": alert_type,
        "severity":   "critical",
        "message":    message,
        "data":       data,
        "agent_id":   AGENT_ID,
    }
    return await _post_orbitron(client, "external-integration", payload)

# ── FastAPI App ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="AEG Token Model",
    version=MODEL_VERSION,
    description="Base44 Superagent Vectors — $AEG utility scoring + Orbitron integration [AEG-5]",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
async def startup():
    log.info("🐕 AEG: Token Model starting on port %d", PORT)
    asyncio.create_task(_heartbeat_loop())
    asyncio.create_task(_score_loop())


async def _heartbeat_loop():
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            result = await send_heartbeat(client)
            log.info("🐕 AEG: Heartbeat sent — %s", result.get("status", "ok"))


async def _score_loop():
    """Auto-score AEG every 5 minutes using latest platform context."""
    global _signals_processed
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(300)
            try:
                ctx = await pull_platform_context(client)
                fed_signals = ctx.get("fed_policy_signals", {})
                fed_context = {"fed_stance": fed_signals.get("fed_stance", "neutral")}

                # Placeholder on-chain data — replace with k9-quant-engine call
                on_chain_data = {
                    "staking_depth":        0.6,
                    "governance_proposals": 3,
                    "integration_count":    8,
                    "collateral_accepted":  True,
                    "gas_usage":            True,
                    "circulating_pct":      0.35,
                    "leverage":             1.0,
                }
                entropy = ctx.get("entropy_state", "medium")
                score = compute_aeg_score(on_chain_data, fed_context, entropy)

                # Proof-of-Alignment gate
                approved, proof_hash = await proof_of_alignment(
                    client, f"aeg_token_score:{score.classification}",
                    score.risk_score, score.leverage,
                    {"utility_score": score.utility_score}
                )

                if approved:
                    await push_token_score(client, score)
                    await push_signal(client, score)
                    _signals_processed += 1
                    log.info("🐕 AEG: Score pushed — %s %.1f (PoA: %s)", score.classification, score.utility_score, proof_hash[:12])
                else:
                    log.warning("🐕 AEG: Score BLOCKED by PoA oracle — risk_score=%.3f", score.risk_score)

            except Exception as e:
                log.error("🐕 AEG: Score loop error: %s", e)


@app.get("/health")
def health():
    return {
        "status":            "online",
        "component":         AGENT_ID,
        "version":           MODEL_VERSION,
        "sprint":            "AEG-5",
        "uptime_s":          round(time.time() - _start, 1),
        "signals_processed": _signals_processed,
        "timestamp":         datetime.now(timezone.utc).isoformat(),
    }


@app.post("/score")
async def score_endpoint(payload: dict):
    """
    Manually trigger AEG token scoring.
    Optionally pass on_chain_data, fed_context, entropy_state in body.
    """
    async with httpx.AsyncClient() as client:
        ctx = await pull_platform_context(client)
        fed_signals = ctx.get("fed_policy_signals", payload.get("fed_context", {}))
        fed_context = {"fed_stance": fed_signals.get("fed_stance", "neutral")}

        on_chain_data = payload.get("on_chain_data", {
            "staking_depth": 0.6, "governance_proposals": 3,
            "integration_count": 8, "collateral_accepted": True,
            "gas_usage": True, "circulating_pct": 0.35, "leverage": 1.0,
        })
        entropy = payload.get("entropy_state", ctx.get("entropy_state", "medium"))
        score = compute_aeg_score(on_chain_data, fed_context, entropy)

        approved, proof_hash = await proof_of_alignment(
            client, f"aeg_token_score:{score.classification}",
            score.risk_score, score.leverage,
            {"utility_score": score.utility_score}
        )

        if not approved:
            raise HTTPException(status_code=403, detail="Proof-of-Alignment oracle rejected action")

        ins_result = await push_token_score(client, score)
        sig_result = await push_signal(client, score)

        return {
            "score":         asdict(score),
            "proof_hash":    proof_hash,
            "insights_push": ins_result,
            "signal_push":   sig_result,
        }


@app.post("/emergency")
async def emergency_endpoint(payload: dict):
    async with httpx.AsyncClient() as client:
        return await send_emergency(
            client,
            payload.get("alert_type", "unknown"),
            payload.get("message", ""),
            payload.get("data", {}),
        )


if __name__ == "__main__":
    log.info("🐕 AEG: Token Model starting — port %d", PORT)
    uvicorn.run("aeg_token_model:app", host="0.0.0.0", port=PORT, reload=False)
