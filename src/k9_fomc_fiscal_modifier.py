"""
k9_fomc_fiscal_modifier.py — FOMC Fiscal Fragility Modifier
─────────────────────────────────────────────────────────────────────────────
Extends the FOMC vote-flip / cascade model with a fiscal fragility factor.

The trilemma: Fed cannot hike (fiscal cost), cannot cut (inflation),
cannot repress (dollar crisis). This modifier adjusts the probability
that centrists (Jefferson, Kugler) resist hiking even with hot inflation
data, because they fear triggering a fiscal crisis.

Integration:
  - Reads fiscal dominance score from k9_fiscal_dominance :9010
  - Modifies FOMC member vote probabilities
  - Outputs adjusted cascade probabilities
  - Routes significant shifts → aeg-signal-router :9004

This module is standalone (no DB dependency) and can be imported
by the quant engine or called directly.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger("k9.fomc.fiscal")

# ── Config ────────────────────────────────────────────────────────────────────

FISCAL_DOMINANCE_URL = os.getenv("FISCAL_DOMINANCE_URL", "http://localhost:9010")
AEG_SIGNAL_ROUTER = os.getenv("AEG_SIGNAL_ROUTER_URL", "http://localhost:9004")

# ── FOMC Member Profiles ──────────────────────────────────────────────────────
# Base probabilities (pre-fiscal modifier) for voting to hike at next meeting
# These are the model's priors before the fiscal fragility adjustment

FOMC_MEMBERS = {
    # Hawks (generally pro-hike)
    "Bowman":      {"base_hike_prob": 0.85, "type": "hawk",  "fiscal_sensitivity": 0.10},
    "Waller":      {"base_hike_prob": 0.75, "type": "hawk",  "fiscal_sensitivity": 0.15},
    # Centrists (swing votes — most affected by fiscal fragility)
    "Jefferson":   {"base_hike_prob": 0.55, "type": "centrist", "fiscal_sensitivity": 0.35},
    "Kugler":      {"base_hike_prob": 0.50, "type": "centrist", "fiscal_sensitivity": 0.40},
    "Cook":        {"base_hike_prob": 0.45, "type": "centrist", "fiscal_sensitivity": 0.25},
    "Barr":        {"base_hike_prob": 0.50, "type": "centrist", "fiscal_sensitivity": 0.30},
    # Doves (generally anti-hike)
    "Collins":     {"base_hike_prob": 0.35, "type": "dove",  "fiscal_sensitivity": 0.20},
    "Williams":    {"base_hike_prob": 0.40, "type": "dove",  "fiscal_sensitivity": 0.15},
    # Chair (Powell/Warsh proxy — "neutral" stance, no forward guidance)
    "Chair":       {"base_hike_prob": 0.50, "type": "chair", "fiscal_sensitivity": 0.50},
}

# ── Cache ────────────────────────────────────────────────────────────────────

_last_score: float | None = None
_last_computation: dict | None = None
_last_fetch: float = 0
CACHE_TTL = 120  # 2 minutes


# ── Fiscal Fragility Adjustment ──────────────────────────────────────────────

def get_fiscal_dominance_score() -> float:
    """Fetch fiscal dominance score from :9010, with fallback."""
    global _last_score, _last_fetch

    # Use cache if fresh
    if _last_score is not None and (time.time() - _last_fetch) < CACHE_TTL:
        return _last_score

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(f"{FISCAL_DOMINANCE_URL}/fiscal/score")
            if resp.status_code == 200:
                data = resp.json()
                score = data.get("dominance_score", 50.0)
                _last_score = float(score)
                _last_fetch = time.time()
                return _last_score
    except Exception as e:
        log.debug(f"Fiscal dominance fetch failed: {e}")

    # Fallback: moderate score
    return 50.0


def adjust_member_prob(member: str, base_prob: float, fiscal_sensitivity: float,
                       fiscal_score: float) -> dict:
    """
    Adjust a member's hike probability based on fiscal fragility.

    The fiscal fragility factor reduces hike probability for fiscally
    sensitive members when the fiscal dominance score is high.

    Formula:
      adjustment = -fiscal_sensitivity × (fiscal_score - 50) / 100
      adjusted_prob = clamp(base_prob + adjustment, 0.01, 0.99)

    This means:
      - At fiscal_score=50 (neutral), no adjustment
      - At fiscal_score=80 (high dominance), centrists lose up to 10.5pp
      - At fiscal_score=100 (extreme), centrists lose up to 17.5pp
    """
    # Fiscal fragility factor: 0 at score=50, 1 at score=100
    fragility = max(0, (fiscal_score - 50) / 50)

    # Adjustment: reduce hike prob by sensitivity × fragility
    adjustment = -fiscal_sensitivity * fragility * 0.30  # max 30% reduction

    adjusted_prob = max(0.01, min(0.99, base_prob + adjustment))

    return {
        "member": member,
        "base_hike_prob": round(base_prob, 3),
        "adjusted_hike_prob": round(adjusted_prob, 3),
        "adjustment": round(adjustment, 4),
        "fiscal_sensitivity": fiscal_sensitivity,
        "fragility_factor": round(fragility, 3),
        "flip_triggered": (base_prob >= 0.50 and adjusted_prob < 0.50),
    }


def compute_cascade(fiscal_score: float | None = None) -> dict:
    """
    Compute the full FOMC cascade with fiscal fragility modifier.

    Returns:
      - Per-member adjusted probabilities
      - Aggregate hike/cut/hold probabilities
      - Flip count (members who crossed 50% threshold)
      - Fiscal dominance impact summary
    """
    if fiscal_score is None:
        fiscal_score = get_fiscal_dominance_score()

    members = []
    flip_count = 0
    total_hike_prob = 0
    total_hold_prob = 0
    total_cut_prob = 0

    for member, profile in FOMC_MEMBERS.items():
        result = adjust_member_prob(
            member,
            profile["base_hike_prob"],
            profile["fiscal_sensitivity"],
            fiscal_score
        )
        members.append({**result, "type": profile["type"]})

        if result["flip_triggered"]:
            flip_count += 1

        # Simple aggregation: hike if >55%, cut if <45%, hold otherwise
        adj = result["adjusted_hike_prob"]
        if adj > 0.55:
            total_hike_prob += adj
        elif adj < 0.45:
            total_cut_prob += adj
        else:
            total_hold_prob += 1 - abs(adj - 0.5) * 2

    n = len(FOMC_MEMBERS)
    # Normalize to get aggregate probabilities
    hike_pct = sum(1 for m in members if m["adjusted_hike_prob"] > 0.55) / n
    cut_pct = sum(1 for m in members if m["adjusted_hike_prob"] < 0.45) / n
    hold_pct = 1 - hike_pct - cut_pct

    # Fiscal dominance impact
    fiscal_impact = {
        "dominance_score": round(fiscal_score, 1),
        "regime": "HIGH" if fiscal_score > 70 else "MODERATE" if fiscal_score > 50 else "LOW",
        "flips_triggered": flip_count,
        "avg_adjustment": round(
            sum(m["adjustment"] for m in members) / n, 4
        ),
        "centrist_avg_adjustment": round(
            sum(m["adjustment"] for m in members if m["type"] == "centrist") /
            max(1, sum(1 for m in members if m["type"] == "centrist")), 4
        ),
    }

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fiscal_score": round(fiscal_score, 1),
        "fiscal_regime": fiscal_impact["regime"],
        "members": members,
        "aggregate": {
            "hike_probability": round(hike_pct, 3),
            "hold_probability": round(hold_pct, 3),
            "cut_probability": round(cut_pct, 3),
            "flip_count": flip_count,
        },
        "fiscal_impact": fiscal_impact,
        "trilemma": {
            "hike_risk": "Raises government interest expense, widening deficit, forcing more issuance, pushing yields higher",
            "cut_risk": "Risks de-anchoring inflation expectations",
            "repress_risk": "Destroys dollar reserve status, causes currency crisis",
            "most_likely_outcome": "Hold with rising term premium" if fiscal_score > 60 else "Data-dependent",
        },
    }


def grid_bot_stress_test(scenario_10y: float = 5.5) -> dict:
    """
    Stress test the grid bot portfolio under a given 10Y yield scenario.

    Portfolio: HBAR, SOL, BTC, ETH (from user's grid bot)
    Under a 10Y 5.5% scenario with housing freeze and bank stress:
      - Risk-off sentiment hits crypto hard
      - HBAR (small cap, low liquidity) exits
      - SOL reduces aggressively
      - BTC holds (store of value narrative)
      - ETH holds (DeFi utility)
    """
    scenarios = {
        5.0: {
            "label": "Elevated",
            "mortgage_rate": 6.80,
            "kre_drawdown_pct": 15,
            "risk_off_intensity": 0.3,
            "actions": {
                "HBAR": "REDUCE 50%",
                "SOL": "REDUCE 25%",
                "BTC": "HOLD",
                "ETH": "HOLD",
            },
        },
        5.25: {
            "label": "Stress",
            "mortgage_rate": 7.05,
            "kre_drawdown_pct": 25,
            "risk_off_intensity": 0.5,
            "actions": {
                "HBAR": "EXIT",
                "SOL": "REDUCE 50%",
                "BTC": "REDUCE 10%",
                "ETH": "HOLD",
            },
        },
        5.5: {
            "label": "Crisis",
            "mortgage_rate": 7.50,
            "kre_drawdown_pct": 35,
            "risk_off_intensity": 0.7,
            "actions": {
                "HBAR": "EXIT",
                "SOL": "REDUCE 75%",
                "BTC": "REDUCE 20%",
                "ETH": "REDUCE 15%",
            },
        },
        6.0: {
            "label": "Systemic",
            "mortgage_rate": 8.00,
            "kre_drawdown_pct": 50,
            "risk_off_intensity": 0.9,
            "actions": {
                "HBAR": "EXIT",
                "SOL": "EXIT",
                "BTC": "REDUCE 40%",
                "ETH": "REDUCE 30%",
            },
        },
    }

    # Find the closest scenario
    closest = min(scenarios.keys(), key=lambda x: abs(x - scenario_10y))
    scenario = scenarios[closest]

    # Calculate estimated P&L impact
    portfolio = {
        "HBAR": {"weight": 0.20, "beta_to_risk_off": 1.8},
        "SOL":  {"weight": 0.30, "beta_to_risk_off": 1.5},
        "BTC":  {"weight": 0.30, "beta_to_risk_off": 0.9},
        "ETH":  {"weight": 0.20, "beta_to_risk_off": 1.1},
    }

    # Estimated drawdown by asset
    est_drawdowns = {}
    for asset, info in portfolio.items():
        action = scenario["actions"][asset]
        if "EXIT" in action:
            est_drawdowns[asset] = -info["beta_to_risk_off"] * scenario["risk_off_intensity"] * 100
        elif "REDUCE" in action:
            pct = int(action.split()[-1].rstrip("%"))
            est_drawdowns[asset] = -info["beta_to_risk_off"] * scenario["risk_off_intensity"] * (pct / 100) * 100
        else:
            est_drawdowns[asset] = -info["beta_to_risk_off"] * scenario["risk_off_intensity"] * 50  # partial

    portfolio_drawdown = sum(
        est_drawdowns[a] * portfolio[a]["weight"] / 100
        for a in portfolio
    )

    return {
        "scenario": {
            "10y_yield": closest,
            "label": scenario["label"],
            "mortgage_rate": scenario["mortgage_rate"],
            "kre_drawdown_pct": scenario["kre_drawdown_pct"],
            "risk_off_intensity": scenario["risk_off_intensity"],
        },
        "portfolio_actions": scenario["actions"],
        "estimated_drawdowns": {k: round(v, 1) for k, v in est_drawdowns.items()},
        "portfolio_weighted_drawdown_pct": round(portfolio_drawdown * 100, 1),
        "recommendation": (
            f"Exit HBAR, reduce SOL aggressively ({scenario['actions']['SOL']}), "
            f"trim BTC ({scenario['actions']['BTC']}) if {closest}% 10Y materializes. "
            f"Expected portfolio drawdown: {portfolio_drawdown*100:.1f}%"
        ),
    }


def route_fomc_signal(cascade: dict) -> bool:
    """Route significant FOMC cascade shifts to signal router."""
    if cascade["fiscal_impact"]["flips_triggered"] == 0:
        return False

    envelope = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "k9-fomc-fiscal-modifier",
        "type": "fomc_cascade_shift",
        "ontology_tags": ["fomc", "fiscal_dominance", "vote_flip", "monetary_policy"],
        "confidence": 0.78,
        "payload": {
            "fiscal_score": cascade["fiscal_score"],
            "fiscal_regime": cascade["fiscal_regime"],
            "flips_triggered": cascade["fiscal_impact"]["flips_triggered"],
            "aggregate": cascade["aggregate"],
            "trilemma_outcome": cascade["trilemma"]["most_likely_outcome"],
            "centrist_adjustment": cascade["fiscal_impact"]["centrist_avg_adjustment"],
        },
        "trace_id": str(uuid.uuid4()),
        "causation_id": str(uuid.uuid4()),
        "signature": "",
    }

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(f"{AEG_SIGNAL_ROUTER}/route", json=envelope)
            return resp.status_code == 200
    except Exception:
        return False


def run_full_analysis() -> dict:
    """Run the full FOMC fiscal fragility analysis pipeline."""
    fiscal_score = get_fiscal_dominance_score()
    cascade = compute_cascade(fiscal_score)

    # Run grid bot stress test at the projected 10Y
    stress = grid_bot_stress_test(5.5)  # crisis scenario

    # Route if flips detected
    routed = route_fomc_signal(cascade)

    return {
        "cascade": cascade,
        "grid_bot_stress": stress,
        "signal_routed": routed,
    }
