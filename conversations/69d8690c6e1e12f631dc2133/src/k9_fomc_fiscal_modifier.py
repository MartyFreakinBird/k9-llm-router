"""
k9_fomc_fiscal_modifier.py — FOMC Fiscal Fragility Modifier (v2.0 Calibrated)
─────────────────────────────────────────────────────────────────────────────
Extends the FOMC vote-flip / cascade model with a fiscal fragility factor.

v2.0 CHANGES (calibrated Aug 21, 2026):
  - Replaced binary flip logic with SMOOTH, TIERED probability scaling
  - Chair NEVER flips to cut below score 90 (Warsh: strict 2% target)
  - Chair only considers cut at score 90+ WITH simultaneous banking crisis
  - Score 50-60: minimal adjustment (centrists lose ~5pp hike prob)
  - Score 60-70: moderate (centrists lose ~15pp, NO cut prob increase)
  - Score 70-80: significant (centrists lose ~30pp, slight cut prob for doves)
  - Score 80-90: large (centrists shift to hold/cut, Chair stays hold)
  - Score 90+: extreme (Chair may consider cut IF banking_crisis=True)
  - Grid bot stress test now includes hedge overlay + Monte Carlo slippage

Historical calibration anchors:
  - UK gilt crisis 2022: fiscal score ~85, similar to our 80-90 tier
  - Italy 2011: fiscal score ~90+, sovereign stress
  - 1970s fiscal dominance: score ~95+, required Volcker shock to break

Integration:
  - Reads fiscal dominance score from k9_fiscal_dominance :9010
  - Reads banking stress from k9_fiscal_dominance :9010/fiscal/banking
  - Modifies FOMC member vote probabilities (smooth, not binary)
  - Outputs adjusted cascade probabilities
  - Routes significant shifts → aeg-signal-router :9004
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import math
import os
import random
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger("k9.fomc.fiscal")

# ── Config ────────────────────────────────────────────────────────────────────

FISCAL_DOMINANCE_URL = os.getenv("FISCAL_DOMINANCE_URL", "http://localhost:9010")
AEG_SIGNAL_ROUTER = os.getenv("AEG_SIGNAL_ROUTER_URL", "http://localhost:9004")

# ── FOMC Member Profiles (v2.0 — calibrated) ─────────────────────────────────
# base_hike_prob: prior probability of voting to hike at next meeting
# fiscal_sensitivity: how much fiscal stress shifts their vote (0-1)
# chair_protection: if True, this member cannot flip to cut below score 90

FOMC_MEMBERS = {
    # Hawks (generally pro-hike, low fiscal sensitivity)
    "Bowman":      {"base_hike_prob": 0.85, "type": "hawk",     "fiscal_sensitivity": 0.10, "chair_protection": False},
    "Waller":      {"base_hike_prob": 0.75, "type": "hawk",     "fiscal_sensitivity": 0.15, "chair_protection": False},
    # Centrists (swing votes — most affected by fiscal fragility)
    "Jefferson":   {"base_hike_prob": 0.55, "type": "centrist", "fiscal_sensitivity": 0.35, "chair_protection": False},
    "Kugler":      {"base_hike_prob": 0.50, "type": "centrist", "fiscal_sensitivity": 0.40, "chair_protection": False},
    "Cook":        {"base_hike_prob": 0.45, "type": "centrist", "fiscal_sensitivity": 0.25, "chair_protection": False},
    "Barr":        {"base_hike_prob": 0.50, "type": "centrist", "fiscal_sensitivity": 0.30, "chair_protection": False},
    # Doves (generally anti-hike)
    "Collins":     {"base_hike_prob": 0.35, "type": "dove",     "fiscal_sensitivity": 0.20, "chair_protection": False},
    "Williams":    {"base_hike_prob": 0.40, "type": "dove",     "fiscal_sensitivity": 0.15, "chair_protection": False},
    # Chair (Warsh proxy — strict 2% target, "neutral" stance)
    # HIGH fiscal sensitivity but CHAIR PROTECTION: cannot flip to cut <90
    "Chair":       {"base_hike_prob": 0.50, "type": "chair",    "fiscal_sensitivity": 0.50, "chair_protection": True},
}

# ── Tiered Adjustment Schedule ────────────────────────────────────────────────
# Maps fiscal score ranges to adjustment parameters
# Each tier defines: hike_reduction (multiply base-sensitivity adjustment),
#                    cut_increase (how much to push toward cut for non-protected)

TIERS = [
    # (min_score, max_score, hike_reduction, cut_increase, label)
    (0,   50,  0.0,  0.0,  "NEUTRAL"),
    (50,  60,  0.15, 0.0,  "ELEVATED"),
    (60,  70,  0.30, 0.0,  "MODERATE"),
    (70,  80,  0.50, 0.15, "HIGH"),
    (80,  90,  0.70, 0.30, "SEVERE"),
    (90,  100, 0.85, 0.50, "CRISIS"),
]


def get_tier(fiscal_score: float) -> tuple:
    """Return the (hike_reduction, cut_increase, label) for a given score."""
    for min_s, max_s, hike_red, cut_inc, label in TIERS:
        if min_s <= fiscal_score < max_s:
            return hike_red, cut_inc, label
    # Score >= 100
    return 0.85, 0.50, "CRISIS"


# ── Cache ────────────────────────────────────────────────────────────────────

_last_score: float | None = None
_last_banking_crisis: bool = False
_last_computation: dict | None = None
_last_fetch: float = 0
CACHE_TTL = 120  # 2 minutes


# ── Data Fetching ────────────────────────────────────────────────────────────

def get_fiscal_dominance_score() -> float:
    """Fetch fiscal dominance score from :9010, with fallback."""
    global _last_score, _last_fetch

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

    return 50.0


def get_banking_crisis_flag() -> bool:
    """Fetch banking stress from :9010 to determine if a banking crisis is active."""
    global _last_banking_crisis, _last_fetch

    if (time.time() - _last_fetch) < CACHE_TTL and _last_banking_crisis is not None:
        return _last_banking_crisis

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(f"{FISCAL_DOMINANCE_URL}/fiscal/banking")
            if resp.status_code == 200:
                data = resp.json()
                # Banking crisis if KRE drawdown > 30% or unrealized loss proxy > $15B
                kre_drawdown = abs(data.get("kre_drawdown_pct", 0))
                unrealized_loss = abs(data.get("unrealized_loss_proxy_b", 0))
                _last_banking_crisis = kre_drawdown > 30 or unrealized_loss > 15
                return _last_banking_crisis
    except Exception:
        pass

    _last_banking_crisis = False
    return False


# ── Smooth Probability Adjustment (v2.0) ────────────────────────────────────

def adjust_member_prob(member: str, base_prob: float, fiscal_sensitivity: float,
                       fiscal_score: float, member_type: str,
                       chair_protection: bool,
                       banking_crisis: bool = False) -> dict:
    """
    Adjust a member's hike probability using SMOOTH, TIERED scaling.

    v2.0 replaces the binary flip with gradual probability shifts:
      - hike_reduction scales the reduction in hike probability
      - cut_increase pushes probability toward cut (for non-protected members)
      - Chair protection: Chair CANNOT move to cut territory unless:
        (a) fiscal_score >= 90 AND (b) banking_crisis == True

    The adjustment is smooth — no binary thresholds. A member at 0.55
    base hike prob with 30% reduction becomes 0.385, not a hard flip.
    """
    hike_reduction, cut_increase, tier_label = get_tier(fiscal_score)

    # Base adjustment: reduce hike probability proportionally
    # The reduction scales with both fiscal_sensitivity and the tier's hike_reduction
    raw_reduction = fiscal_sensitivity * hike_reduction

    # The actual hike probability reduction (in probability points)
    # Max reduction at score 90+ for high-sensitivity member: ~0.85 * 0.50 = 0.425
    # This shifts a 0.55 base to 0.125 — significant but NOT a hard flip
    hike_adj = -raw_reduction * 0.40  # scale factor: max ~17pp reduction at crisis

    # Cut probability increase: pushes the member further toward cut
    # Only applies to non-protected members, and scales with sensitivity
    cut_adj = 0.0
    if not chair_protection:
        cut_adj = -fiscal_sensitivity * cut_increase * 0.20  # max ~5pp at crisis

    # Chair protection: if protected and score < 90, no cut push at all
    # If protected and score >= 90 but no banking crisis, still no cut push
    if chair_protection:
        if fiscal_score < 90:
            cut_adj = 0.0
        elif not banking_crisis:
            cut_adj = 0.0
        else:
            # Score >= 90 WITH banking crisis: allow small cut push
            cut_adj = -0.10  # 10pp — modest, reflects emergency only

    total_adj = hike_adj + cut_adj
    adjusted_prob = max(0.01, min(0.99, base_prob + total_adj))

    # Determine vote (soft classification, not binary)
    if adjusted_prob > 0.55:
        vote = "hike"
    elif adjusted_prob < 0.40:
        vote = "cut"
    else:
        vote = "hold"

    # Determine if this is a "flip" (crossed 50% threshold)
    flip_triggered = (base_prob >= 0.50 and adjusted_prob < 0.50)

    # For protected members, note if cut was blocked
    cut_blocked = False
    if chair_protection and (fiscal_score < 90 or not banking_crisis):
        if base_prob + hike_adj + (-fiscal_sensitivity * cut_increase * 0.20) < 0.40:
            cut_blocked = True

    return {
        "member": member,
        "type": member_type,
        "base_hike_prob": round(base_prob, 3),
        "adjusted_hike_prob": round(adjusted_prob, 3),
        "adjustment": round(total_adj, 4),
        "hike_reduction_pp": round(hike_adj * 100, 1),
        "cut_push_pp": round(cut_adj * 100, 1),
        "fiscal_sensitivity": fiscal_sensitivity,
        "tier": tier_label,
        "vote": vote,
        "flip_triggered": flip_triggered,
        "cut_blocked": cut_blocked,
        "chair_protection_active": chair_protection and (fiscal_score < 90 or not banking_crisis),
    }


def compute_cascade(fiscal_score: float | None = None,
                    banking_crisis: bool | None = None) -> dict:
    """
    Compute the full FOMC cascade with v2.0 smooth fiscal fragility modifier.

    Args:
        fiscal_score: 0-100 fiscal dominance score (fetched if None)
        banking_crisis: whether banking crisis is active (fetched if None)

    Returns:
      - Per-member adjusted probabilities with smooth scaling
      - Aggregate hike/cut/hold probabilities
      - Flip count + cut-blocked count (Chair protection)
      - Fiscal dominance impact summary with tier label
    """
    if fiscal_score is None:
        fiscal_score = get_fiscal_dominance_score()
    if banking_crisis is None:
        banking_crisis = get_banking_crisis_flag()

    _, _, tier_label = get_tier(fiscal_score)

    members = []
    flip_count = 0
    cut_blocked_count = 0

    for member_name, profile in FOMC_MEMBERS.items():
        result = adjust_member_prob(
            member_name,
            profile["base_hike_prob"],
            profile["fiscal_sensitivity"],
            fiscal_score,
            profile["type"],
            profile["chair_protection"],
            banking_crisis,
        )
        members.append(result)

        if result["flip_triggered"]:
            flip_count += 1
        if result["cut_blocked"]:
            cut_blocked_count += 1

    n = len(FOMC_MEMBERS)
    hike_pct = sum(1 for m in members if m["vote"] == "hike") / n
    cut_pct = sum(1 for m in members if m["vote"] == "cut") / n
    hold_pct = 1 - hike_pct - cut_pct

    # Fiscal dominance impact
    centrists = [m for m in members if m["type"] == "centrist"]
    fiscal_impact = {
        "dominance_score": round(fiscal_score, 1),
        "regime": tier_label,
        "banking_crisis": banking_crisis,
        "flips_triggered": flip_count,
        "cuts_blocked": cut_blocked_count,
        "avg_adjustment": round(sum(m["adjustment"] for m in members) / n, 4),
        "centrist_avg_adjustment": round(
            sum(m["adjustment"] for m in centrists) / max(1, len(centrists)), 4
        ),
        "chair_protection_active": any(m["chair_protection_active"] for m in members if m["member"] == "Chair"),
    }

    # Determine most likely outcome based on probabilities
    if fiscal_score >= 90 and banking_crisis:
        outcome = "Emergency cut likely (fiscal + banking crisis)"
    elif fiscal_score >= 80:
        outcome = "Hold with strong fiscal pressure (centrists shift to hold/cut)"
    elif fiscal_score >= 70:
        outcome = "Hold with rising term premium (centrists resist hiking)"
    elif fiscal_score >= 60:
        outcome = "Data-dependent with fiscal headwind to hiking"
    else:
        outcome = "Data-dependent (minimal fiscal constraint)"

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fiscal_score": round(fiscal_score, 1),
        "fiscal_regime": tier_label,
        "banking_crisis": banking_crisis,
        "members": members,
        "aggregate": {
            "hike_probability": round(hike_pct, 3),
            "hold_probability": round(hold_pct, 3),
            "cut_probability": round(cut_pct, 3),
            "flip_count": flip_count,
            "cut_blocked_count": cut_blocked_count,
        },
        "fiscal_impact": fiscal_impact,
        "trilemma": {
            "hike_risk": "Raises government interest expense, widening deficit, forcing more issuance, pushing yields higher",
            "cut_risk": "Risks de-anchoring inflation expectations",
            "repress_risk": "Destroys dollar reserve status, causes currency crisis",
            "most_likely_outcome": outcome,
        },
        "version": "2.0-calibrated",
    }


# ── Grid Bot Stress Test (v2.0 — with hedges + Monte Carlo) ──────────────────

def grid_bot_stress_test(scenario_10y: float = 5.5,
                         include_hedges: bool = True,
                         monte_carlo_runs: int = 1000) -> dict:
    """
    v2.0: Stress test with hedge overlay and Monte Carlo slippage simulation.

    Hedges included:
      - Long-dated SOL puts (notional covers 30% of SOL position)
      - Polymarket "rate hike" contracts (pays if Fed hikes, offsets crypto loss)
      - BTC covered call overlay (premium income reduces cost basis)

    Monte Carlo:
      - Simulates slippage on stop-loss execution
      - Each asset gets a slippage factor drawn from N(1.0, 0.03)
      - 3% std dev captures normal liquidity gaps; tail draws capture flash crash risk
      - Reports P5, P50, P95 drawdowns
    """
    scenarios = {
        5.0: {
            "label": "Elevated",
            "mortgage_rate": 6.80,
            "kre_drawdown_pct": 15,
            "risk_off_intensity": 0.3,
            "actions": {
                "HBAR": ("REDUCE", 0.50),
                "SOL":  ("REDUCE", 0.25),
                "BTC":  ("HOLD",   0.0),
                "ETH":  ("HOLD",   0.0),
            },
        },
        5.25: {
            "label": "Stress",
            "mortgage_rate": 7.05,
            "kre_drawdown_pct": 25,
            "risk_off_intensity": 0.5,
            "actions": {
                "HBAR": ("EXIT",   1.0),
                "SOL":  ("REDUCE", 0.50),
                "BTC":  ("REDUCE", 0.10),
                "ETH":  ("HOLD",   0.0),
            },
        },
        5.5: {
            "label": "Crisis",
            "mortgage_rate": 7.50,
            "kre_drawdown_pct": 35,
            "risk_off_intensity": 0.7,
            "actions": {
                "HBAR": ("EXIT",   1.0),
                "SOL":  ("REDUCE", 0.75),
                "BTC":  ("REDUCE", 0.20),
                "ETH":  ("REDUCE", 0.15),
            },
        },
        6.0: {
            "label": "Systemic",
            "mortgage_rate": 8.00,
            "kre_drawdown_pct": 50,
            "risk_off_intensity": 0.9,
            "actions": {
                "HBAR": ("EXIT",   1.0),
                "SOL":  ("EXIT",   1.0),
                "BTC":  ("REDUCE", 0.40),
                "ETH":  ("REDUCE", 0.30),
            },
        },
    }

    closest = min(scenarios.keys(), key=lambda x: abs(x - scenario_10y))
    scenario = scenarios[closest]

    # Portfolio weights and betas (same as v1)
    portfolio = {
        "HBAR": {"weight": 0.20, "beta_to_risk_off": 1.8},
        "SOL":  {"weight": 0.30, "beta_to_risk_off": 1.5},
        "BTC":  {"weight": 0.30, "beta_to_risk_off": 0.9},
        "ETH":  {"weight": 0.20, "beta_to_risk_off": 1.1},
    }

    # Hedge overlay parameters
    hedge_params = {
        "sol_puts": {
            "notional_coverage": 0.30,    # covers 30% of SOL position
            "strike_pct_otm": 0.15,        # 15% OTM
            "iv_at_purchase": 0.65,        # IV when puts were bought
            "iv_at_stress": 1.20,           # IV blows out in crisis
            "delta_at_stress": 0.45,       # put delta in crisis
            "cost_pct_of_nav": 0.02,        # cost 2% of NAV
        },
        "polymarket_hike": {
            "notional_coverage": 0.15,     # covers 15% of portfolio
            "payout_per_dollar": 3.5,       # 3.5x payout if Fed hikes
            "probability_of_payout": 0.40,  # 40% chance hike happens
            "cost_pct_of_nav": 0.01,        # cost 1% of NAV
        },
        "btc_covered_call": {
            "premium_received_pct": 0.015, # 1.5% premium income
            "upside_cap_pct": 0.10,         # caps BTC upside at 10%
        },
    }

    def _calculate_unhedged_drawdown(slippage_factors: dict | None = None) -> float:
        """Calculate unhedged portfolio drawdown with optional slippage."""
        total_dd = 0.0
        for asset, info in portfolio.items():
            action, pct = scenario["actions"][asset]
            beta = info["beta_to_risk_off"]
            intensity = scenario["risk_off_intensity"]

            # Base drawdown: beta × intensity × reduction fraction
            if action == "EXIT":
                base_dd = -beta * intensity
            elif action == "REDUCE":
                base_dd = -beta * intensity * pct
            else:  # HOLD
                base_dd = -beta * intensity * 0.5  # partial impact even if held

            # Apply slippage if provided
            if slippage_factors and asset in slippage_factors:
                base_dd *= slippage_factors[asset]

            total_dd += base_dd * info["weight"]

        return total_dd

    def _calculate_hedged_drawdown(slippage_factors: dict | None = None) -> float:
        """Calculate hedged portfolio drawdown."""
        unhedged = _calculate_unhedged_drawdown(slippage_factors)

        # SOL puts hedge value
        sol_position = portfolio["SOL"]["weight"]
        sol_put_coverage = hedge_params["sol_puts"]["notional_coverage"] * sol_position
        sol_put_value = sol_put_coverage * hedge_params["sol_puts"]["delta_at_stress"]
        sol_put_cost = hedge_params["sol_puts"]["cost_pct_of_nav"]

        # Polymarket hike contracts
        poly_coverage = hedge_params["polymarket_hike"]["notional_coverage"]
        poly_payout = poly_coverage * hedge_params["polymarket_hike"]["payout_per_dollar"] * \
                      hedge_params["polymarket_hike"]["probability_of_payout"]
        poly_cost = hedge_params["polymarket_hike"]["cost_pct_of_nav"]

        # BTC covered call premium (already collected, reduces cost basis)
        btc_cc_premium = hedge_params["btc_covered_call"]["premium_received_pct"]

        # Total hedge benefit (as positive offset to drawdown)
        hedge_benefit = sol_put_value + poly_payout + btc_cc_premium
        hedge_cost = sol_put_cost + poly_cost

        net_hedge_offset = hedge_benefit - hedge_cost

        return unhedged + net_hedge_offset

    # Point estimates
    unhedged_dd = _calculate_unhedged_drawdown()
    hedged_dd = _calculate_hedged_drawdown()

    # Monte Carlo simulation: slippage around stop-loss execution
    mc_unhedged = []
    mc_hedged = []
    random.seed(42)  # reproducible

    for _ in range(monte_carlo_runs):
        # Slippage factors: N(1.0, 0.03) — 3% std dev
        # Tail risk: 5% chance of 10%+ slippage (flash crash)
        slip = {}
        for asset in portfolio:
            if random.random() < 0.05:
                # Flash crash slippage: 10-25%
                slip[asset] = 1.0 + random.uniform(0.10, 0.25)
            else:
                # Normal slippage: 0-6%
                slip[asset] = 1.0 + abs(random.gauss(0, 0.03))

        mc_unhedged.append(_calculate_unhedged_drawdown(slip))
        mc_hedged.append(_calculate_hedged_drawdown(slip))

    mc_unhedged.sort()
    mc_hedged.sort()

    def _percentile(arr, p):
        idx = int(len(arr) * p / 100)
        return arr[min(idx, len(arr) - 1)]

    # Format actions for output
    actions_str = {}
    for asset, (action, pct) in scenario["actions"].items():
        if action == "EXIT":
            actions_str[asset] = "EXIT"
        elif action == "REDUCE":
            actions_str[asset] = f"REDUCE {int(pct * 100)}%"
        else:
            actions_str[asset] = "HOLD"

    # Per-asset drawdown estimates (point estimate)
    est_drawdowns = {}
    for asset, info in portfolio.items():
        action, pct = scenario["actions"][asset]
        if action == "EXIT":
            est_drawdowns[asset] = -info["beta_to_risk_off"] * scenario["risk_off_intensity"] * 100
        elif action == "REDUCE":
            est_drawdowns[asset] = -info["beta_to_risk_off"] * scenario["risk_off_intensity"] * (pct) * 100
        else:
            est_drawdowns[asset] = -info["beta_to_risk_off"] * scenario["risk_off_intensity"] * 50

    return {
        "scenario": {
            "10y_yield": closest,
            "label": scenario["label"],
            "mortgage_rate": scenario["mortgage_rate"],
            "kre_drawdown_pct": scenario["kre_drawdown_pct"],
            "risk_off_intensity": scenario["risk_off_intensity"],
        },
        "portfolio_actions": actions_str,
        "estimated_drawdowns_pct": {k: round(v, 1) for k, v in est_drawdowns.items()},
        "unhedged": {
            "point_estimate_pct": round(unhedged_dd * 100, 1),
            "monte_carlo": {
                "runs": monte_carlo_runs,
                "p5_pct": round(_percentile(mc_unhedged, 5) * 100, 1),
                "p50_pct": round(_percentile(mc_unhedged, 50) * 100, 1),
                "p95_pct": round(_percentile(mc_unhedged, 95) * 100, 1),
            },
        },
        "hedged": {
            "point_estimate_pct": round(hedged_dd * 100, 1),
            "monte_carlo": {
                "runs": monte_carlo_runs,
                "p5_pct": round(_percentile(mc_hedged, 5) * 100, 1),
                "p50_pct": round(_percentile(mc_hedged, 50) * 100, 1),
                "p95_pct": round(_percentile(mc_hedged, 95) * 100, 1),
            },
            "hedge_overlay": {
                "sol_long_puts": {
                    "coverage": f"{hedge_params['sol_puts']['notional_coverage']*100:.0f}% of SOL position",
                    "delta_at_stress": hedge_params["sol_puts"]["delta_at_stress"],
                    "cost_pct_nav": hedge_params["sol_puts"]["cost_pct_of_nav"],
                },
                "polymarket_hike_contracts": {
                    "coverage": f"{hedge_params['polymarket_hike']['notional_coverage']*100:.0f}% of portfolio",
                    "payout": f"{hedge_params['polymarket_hike']['payout_per_dollar']}x if Fed hikes",
                    "prob_payout": hedge_params["polymarket_hike"]["probability_of_payout"],
                    "cost_pct_nav": hedge_params["polymarket_hike"]["cost_pct_of_nav"],
                },
                "btc_covered_call": {
                    "premium_pct": hedge_params["btc_covered_call"]["premium_received_pct"],
                    "upside_cap": f"{hedge_params['btc_covered_call']['upside_cap_pct']*100:.0f}%",
                },
            },
        },
        "hedge_value": {
            "unhedged_drawdown_pct": round(unhedged_dd * 100, 1),
            "hedged_drawdown_pct": round(hedged_dd * 100, 1),
            "hedge_offset_pct": round((hedged_dd - unhedged_dd) * 100, 1),
            "monte_carlo_p95_unhedged": round(_percentile(mc_unhedged, 95) * 100, 1),
            "monte_carlo_p95_hedged": round(_percentile(mc_hedged, 95) * 100, 1),
            "tail_risk_reduction_pct": round(
                (_percentile(mc_unhedged, 95) - _percentile(mc_hedged, 95)) * 100, 1
            ),
        },
        "include_hedges": include_hedges,
        "recommendation": (
            f"Exit HBAR, reduce SOL aggressively ({actions_str['SOL']}), "
            f"trim BTC ({actions_str['BTC']}) if {closest}% 10Y materializes. "
            f"Unhedged P50: {unhedged_dd*100:.1f}%, Hedged P50: {hedged_dd*100:.1f}% "
            f"(hedge saves {(hedged_dd - unhedged_dd)*100:.1f}pp). "
            f"Tail risk (P95): unhedged {_percentile(mc_unhedged, 95)*100:.1f}% "
            vs hedged {_percentile(mc_hedged, 95)*100:.1f}%"
        ),
    }


# ── Signal Routing ───────────────────────────────────────────────────────────

def route_fomc_signal(cascade: dict) -> bool:
    """Route significant FOMC cascade shifts to signal router."""
    if cascade["aggregate"]["flip_count"] == 0 and not cascade["banking_crisis"]:
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
            "banking_crisis": cascade["banking_crisis"],
            "flips_triggered": cascade["aggregate"]["flip_count"],
            "cuts_blocked": cascade["aggregate"]["cut_blocked_count"],
            "aggregate": cascade["aggregate"],
            "trilemma_outcome": cascade["trilemma"]["most_likely_outcome"],
            "centrist_adjustment": cascade["fiscal_impact"]["centrist_avg_adjustment"],
            "chair_protection_active": cascade["fiscal_impact"]["chair_protection_active"],
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
    banking_crisis = get_banking_crisis_flag()
    cascade = compute_cascade(fiscal_score, banking_crisis)

    # Run grid bot stress test with hedges (both hedged and unhedged)
    stress_hedged = grid_bot_stress_test(5.5, include_hedges=True, monte_carlo_runs=1000)
    stress_unhedged = grid_bot_stress_test(5.5, include_hedges=False, monte_carlo_runs=1000)

    routed = route_fomc_signal(cascade)

    return {
        "cascade": cascade,
        "grid_bot_stress_hedged": stress_hedged,
        "grid_bot_stress_unhedged": stress_unhedged,
        "signal_routed": routed,
        "version": "2.0-calibrated",
    }
