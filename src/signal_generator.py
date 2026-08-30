"""
signal_generator.py — Serverless K-9 Signal Generator
─────────────────────────────────────────────────────────────────────────────
Self-contained: fetches from public APIs, computes catalyst score + divergence,
sends signals to 3Commas. No homelab dependency, no DuckDB, no persistence.

Designed for:
  - Render.com cron jobs (free tier)
  - PythonAnywhere scheduled tasks
  - AWS Lambda / GCP Cloud Functions
  - Any cron environment

Can also run as a lightweight FastAPI server for manual signals:
  python -m src.signal_generator --server --port 9013

Usage (cron mode):
  python -m src.signal_generator --run

Usage (server mode):
  python -m src.signal_generator --server --port 9013

Public APIs (no keys required):
  - DeFi Llama: on-chain revenue + TVL
  - CoinGecko: price momentum
  - yfinance: ETF momentum (equities, commodities)
  - CME FedWatch: FOMC hike/cut probability (via JSON feed)
  - 3Commas: webhook delivery
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger("k9.serverless")

# ── Config ────────────────────────────────────────────────────────────────────

THREECOMMAS_SECRET = os.getenv("THREECOMMASSECRET", "")
THREECOMMAS_BOT_UUID = os.getenv("THREECOMMASBOTUUID", "ee41599e-95dd-4b6e-8374-d0968cbff2de")
THREECOMMAS_WEBHOOK_URL = os.getenv("THREECOMMASWEBHOOKURL", "https://app.3commas.io/trade_signal/trading_view")

# Optional: homelab fallback for richer data (if reachable)
HOMELAB_URL = os.getenv("K9_HOMELAB_URL", "")  # e.g. http://your-homelab:9012

MAX_LAG = int(os.getenv("THREECOMMASMAXLAG", "300"))
CONFIDENCE_THRESHOLD = float(os.getenv("THREECOMMASCONFIDENCETHRESHOLD", "0.65"))

# Catalyst score weights (same as k9_microflow.py)
METRIC_WEIGHTS = {
    "etf_flow": 0.30,
    "onchain_revenue": 0.25,
    "price_momentum": 0.20,
    "onchain_tvl": 0.15,
    "etf_momentum": 0.15,
    "social_sentiment": 0.10,
}

INSTRUMENT_MAP = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "HBAR": "HBARUSDT",
    "SPX": "SPY", "QQQ": "QQQ", "GLD": "GLD", "TLT": "TLT", "KRE": "KRE",
}

VALID_ACTIONS = {
    "enter_long", "exit_long", "enter_short", "exit_short",
    "close_position", "add_position",
}

# Instruments to track
TRACKED_INSTRUMENTS = ["BTC", "ETH", "SOL", "HBAR", "SPX", "QQQ", "GLD", "TLT", "KRE"]

# ── State (in-memory, ephemeral) ──────────────────────────────────────────────

_signal_log: list[dict] = []
_stats: dict[str, int] = defaultdict(int)
_last_run: str = ""
_rate_limits: dict[str, float] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Public API Fetchers ──────────────────────────────────────────────────────

def fetch_defillama_revenue(protocol: str) -> dict | None:
    """Fetch 24h revenue from DeFi Llama (public, no key)."""
    try:
        with httpx.Client(timeout=10) as c:
            resp = c.get(f"https://api.llama.fi/summary/fees/{protocol}")
            if resp.status_code != 200:
                return None
            data = resp.json()
            revenue = data.get("total24h")
            if revenue is None:
                return None
            return {
                "value": float(revenue),
                "fees_24h": data.get("totalFees24h"),
                "change_1d": data.get("change_1d"),
            }
    except Exception as e:
        log.debug(f"DeFiLlama revenue {protocol}: {e}")
        return None


def fetch_defillama_tvl(chain: str) -> dict | None:
    """Fetch TVL from DeFi Llama (public, no key)."""
    try:
        with httpx.Client(timeout=10) as c:
            resp = c.get("https://api.llama.fi/v2/chains")
            if resp.status_code != 200:
                return None
            for ch in resp.json():
                if ch.get("name", "").lower() == chain.lower():
                    return {"value": float(ch.get("tvl", 0)), "change_7d": ch.get("chainTvlData", {}).get("tvl_7d_change", 0)}
            return None
    except Exception as e:
        log.debug(f"DeFiLlama TVL {chain}: {e}")
        return None


def fetch_coingecko_momentum(coin_id: str) -> dict | None:
    """Fetch 7-day price momentum from CoinGecko (public, no key)."""
    try:
        with httpx.Client(timeout=10) as c:
            resp = c.get(
                f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart",
                params={"vs_currency": "usd", "days": "7", "interval": "daily"}
            )
            if resp.status_code != 200:
                return None
            prices = resp.json().get("prices", [])
            if len(prices) < 2:
                return None
            current = prices[-1][1]
            week_ago = prices[0][1]
            if week_ago == 0:
                return None
            momentum = ((current - week_ago) / week_ago) * 100
            return {"value": float(momentum), "current_price": current}
    except Exception as e:
        log.debug(f"CoinGecko {coin_id}: {e}")
        return None


def fetch_yfinance_momentum(ticker: str) -> dict | None:
    """Fetch 5-day ETF momentum from yfinance (public, no key)."""
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="1mo")
        if hist.empty or len(hist) < 6:
            return None
        current = float(hist["Close"].iloc[-1])
        week_ago = float(hist["Close"].iloc[-5])
        if week_ago == 0:
            return None
        momentum = ((current - week_ago) / week_ago) * 100
        return {"value": float(momentum), "current_price": current}
    except Exception as e:
        log.debug(f"yfinance {ticker}: {e}")
        return None


def fetch_cme_fedwatch() -> dict:
    """
    Fetch FOMC rate hike/cut probability from CME FedWatch Tool.
    Uses the public JSON feed from CME Group.
    Falls back to neutral if unavailable.
    """
    try:
        # CME FedWatch public API endpoint
        with httpx.Client(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as c:
            resp = c.get("https://www.cmegroup.com/services/fed-funds-futures.json")
            if resp.status_code == 200:
                data = resp.json()
                # Parse the latest meeting probabilities
                meetings = data.get("meetings", data.get("data", []))
                if meetings and len(meetings) > 0:
                    next_meeting = meetings[0]
                    hike_prob = float(next_meeting.get("hikeProb", next_meeting.get("hike_probability", 0)))
                    cut_prob = float(next_meeting.get("cutProb", next_meeting.get("cut_probability", 0)))
                    return {"hike_probability": hike_prob, "cut_probability": cut_prob, "source": "CME FedWatch"}
    except Exception as e:
        log.debug(f"CME FedWatch: {e}")

    # Fallback: try Polymarket public API for Fed funds rate markets
    try:
        with httpx.Client(timeout=10) as c:
            resp = c.get("https://gamma-api.polymarket.com/markets",
                         params={"tag": "federal-reserve", "limit": 5, "active": "true"})
            if resp.status_code == 200:
                markets = resp.json()
                for m in markets:
                    q = m.get("question", "").lower()
                    if "rate" in q and ("hike" in q or "raise" in q):
                        outcomes = m.get("outcomePrices", m.get("outcomes", []))
                        if outcomes and len(outcomes) > 0:
                            return {"hike_probability": float(outcomes[0]), "cut_probability": float(outcomes[1]) if len(outcomes) > 1 else 0, "source": "Polymarket"}
    except Exception as e:
        log.debug(f"Polymarket FedWatch fallback: {e}")

    # Neutral fallback
    return {"hike_probability": 0.0, "cut_probability": 0.0, "source": "fallback-neutral"}


def fetch_fiscal_dominance_proxy() -> float:
    """
    Proxy fiscal dominance from public Treasury yield data.
    High 10Y yield + high debt/GDP → high fiscal dominance.
    Returns 0-100 score.
    """
    try:
        import yfinance as yf
        tnx = yf.Ticker("^TNX").history(period="5d")
        if tnx.empty:
            return 50.0
        current_yield = float(tnx["Close"].iloc[-1])
        # Map yield to dominance score: 4% → 50, 5%+ → 80+, 3% → 30
        if current_yield >= 5.0:
            return 85.0
        elif current_yield >= 4.5:
            return 70.0
        elif current_yield >= 4.0:
            return 50.0
        elif current_yield >= 3.5:
            return 35.0
        else:
            return 20.0
    except Exception as e:
        log.debug(f"Fiscal dominance proxy: {e}")
        return 50.0


# ── Catalyst Score Computation ───────────────────────────────────────────────

COINGECKO_IDS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "HBAR": "hedera-hashgraph"}
DEFILLAMA_PROTOCOLS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}
DEFILLAMA_CHAINS = {"ETH": "Ethereum", "SOL": "Solana"}
YFINANCE_TICKERS = {"SPX": "SPY", "QQQ": "QQQ", "GLD": "GLD", "TLT": "TLT", "KRE": "KRE"}


def fetch_all_metrics(instrument: str) -> dict[str, float]:
    """Fetch all available metrics for an instrument from public APIs."""
    metrics: dict[str, float] = {}

    # On-chain revenue (crypto)
    if instrument in DEFILLAMA_PROTOCOLS:
        rev = fetch_defillama_revenue(DEFILLAMA_PROTOCOLS[instrument])
        if rev:
            metrics["onchain_revenue"] = rev["value"]

    # On-chain TVL (crypto)
    if instrument in DEFILLAMA_CHAINS:
        tvl = fetch_defillama_tvl(DEFILLAMA_CHAINS[instrument])
        if tvl:
            metrics["onchain_tvl"] = tvl["value"]

    # Price momentum (crypto)
    if instrument in COINGECKO_IDS:
        mom = fetch_coingecko_momentum(COINGECKO_IDS[instrument])
        if mom:
            metrics["price_momentum"] = mom["value"]

    # ETF momentum (equities/commodities)
    if instrument in YFINANCE_TICKERS:
        mom = fetch_yfinance_momentum(YFINANCE_TICKERS[instrument])
        if mom:
            metrics["etf_momentum"] = mom["value"]

    return metrics


def compute_catalyst_score(instrument: str, metrics: dict[str, float]) -> float:
    """
    Compute catalyst score (0-100) from available metrics.
    Uses simplified normalization since we don't have historical data in serverless mode.
    """
    if not metrics:
        return 50.0

    # For serverless: use directional scoring instead of min-max
    # Positive momentum/revenue/TVL → higher catalyst score
    # Negative momentum → lower catalyst score
    score = 50.0
    total_weight = 0.0

    for metric_type, value in metrics.items():
        weight = METRIC_WEIGHTS.get(metric_type, 0.05)

        if metric_type in ("price_momentum", "etf_momentum"):
            # Momentum: +10% week → +40 points, -10% → -40 points
            normalized = 50.0 + (value * 4.0)  # scale: 10% → 40pp shift
        elif metric_type in ("onchain_revenue", "onchain_tvl"):
            # Revenue/TVL: use log scale — high values → higher score
            if value > 0:
                import math
                log_val = math.log10(max(value, 1))
                # $1M → 6, $1B → 9, $10B → 10
                normalized = 30.0 + (log_val * 7.0)  # 6 → 72, 9 → 93
            else:
                normalized = 50.0
        else:
            normalized = 50.0

        normalized = max(0.0, min(100.0, normalized))
        score += (normalized - 50.0) * weight
        total_weight += weight

    if total_weight > 0:
        score = 50.0 + (score - 50.0) / total_weight * 0.5  # dampen

    return max(0.0, min(100.0, score))


# ── 3Commas Signal Dispatch ───────────────────────────────────────────────────

def send_3commas_signal(action: str, instrument: str, trigger_price: float | None = None, extra: dict | None = None) -> dict:
    """Send a signal to 3Commas webhook."""
    tv_instrument = INSTRUMENT_MAP.get(instrument.upper(), instrument.upper())
    payload = {
        "secret": THREECOMMAS_SECRET,
        "max_lag": str(MAX_LAG),
        "timestamp": str(int(time.time())),
        "trigger_price": str(trigger_price) if trigger_price else "{{close}}",
        "tv_exchange": "gateio",
        "tv_instrument": tv_instrument,
        "action": action,
        "bot_uuid": THREECOMMAS_BOT_UUID,
    }
    if extra:
        payload.update(extra)

    if not THREECOMMAS_SECRET:
        return {"success": False, "error": "THREECOMMASSECRET not set"}

    try:
        with httpx.Client(timeout=15) as c:
            resp = c.post(THREECOMMAS_WEBHOOK_URL, json=payload)
            return {"success": resp.status_code == 200, "status_code": resp.status_code, "response": resp.text[:200]}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Signal Generation ─────────────────────────────────────────────────────────

def check_rate_limit(instrument: str) -> bool:
    now = time.time()
    last = _rate_limits.get(instrument, 0)
    if (now - last) < 300:  # 5 min
        return False
    _rate_limits[instrument] = now
    return True


def generate_signals() -> dict:
    """
    Main signal generation loop.
    Fetches all data from public APIs, computes catalyst scores,
    checks divergence, and dispatches signals to 3Commas.
    """
    global _last_run
    _last_run = _now_iso()
    results: list[dict] = []

    # Fetch FOMC probability (shared across all instruments)
    fomc = fetch_cme_fedwatch()
    hike_prob = fomc["hike_probability"]
    cut_prob = fomc["cut_probability"]

    # Fetch fiscal dominance proxy
    fiscal_score = fetch_fiscal_dominance_proxy()

    log.info(f"FOMC: hike={hike_prob:.1%} cut={cut_prob:.1%} | Fiscal: {fiscal_score:.0f}")

    # Compute catalyst scores for all tracked instruments
    suppress_short: list[str] = []
    catalyst_scores: dict[str, float] = {}

    for instrument in TRACKED_INSTRUMENTS:
        metrics = fetch_all_metrics(instrument)
        score = compute_catalyst_score(instrument, metrics)
        catalyst_scores[instrument] = score

        # SUPPRESS_SHORT: catalyst >80 AND hike prob >50%
        if score > 80 and hike_prob > 0.50:
            suppress_short.append(instrument)

        log.info(f"  {instrument:5s} catalyst={score:.1f} metrics={len(metrics)}")

    # Generate signals based on divergence
    for instrument in TRACKED_INSTRUMENTS:
        score = catalyst_scores[instrument]

        # MACRO_DOMINANT: catalyst <20 AND fiscal >70 → short
        if score < 20 and fiscal_score > 70:
            if check_rate_limit(instrument):
                result = send_3commas_signal("enter_short", instrument)
                results.append({
                    "instrument": instrument, "action": "enter_short",
                    "source": "macro-dominant", "catalyst_score": score,
                    "fiscal_score": fiscal_score, "delivery": result,
                })
                _stats["signals_sent"] += 1
                log.info(f"  → enter_short {instrument} (MACRO_DOMINANT)")
            else:
                _stats["throttled"] += 1

        # SUPPRESS_SHORT: catalyst >80 AND hike >50% → block shorts (no action needed, just flag)
        elif instrument in suppress_short:
            _stats["suppress_short"] += 1
            log.info(f"  → SUPPRESS_SHORT active for {instrument}")

        # FOMC cut >65% → enter long (dovish surprise)
        if cut_prob > 0.65 and instrument in ("BTC", "ETH", "SOL"):
            if check_rate_limit(instrument):
                result = send_3commas_signal("enter_long", instrument)
                results.append({
                    "instrument": instrument, "action": "enter_long",
                    "source": "fomc-cut-signal", "catalyst_score": score,
                    "cut_probability": cut_prob, "delivery": result,
                })
                _stats["signals_sent"] += 1
                log.info(f"  → enter_long {instrument} (FOMC cut signal)")

        # FOMC hike >65% → exit long (hawkish surprise)
        if hike_prob > 0.65 and instrument in ("BTC", "ETH", "SOL"):
            if check_rate_limit(instrument):
                result = send_3commas_signal("exit_long", instrument)
                results.append({
                    "instrument": instrument, "action": "exit_long",
                    "source": "fomc-hike-signal", "catalyst_score": score,
                    "hike_probability": hike_prob, "delivery": result,
                })
                _stats["signals_sent"] += 1
                log.info(f"  → exit_long {instrument} (FOMC hike signal)")

    # Log summary
    summary = {
        "timestamp": _last_run,
        "fomc": fomc,
        "fiscal_dominance": fiscal_score,
        "catalyst_scores": catalyst_scores,
        "suppress_short": suppress_short,
        "signals": results,
        "stats": dict(_stats),
    }
    _signal_log.insert(0, summary)
    if len(_signal_log) > 100:
        _signal_log = _signal_log[:100]

    return summary


# ── CLI Entry Points ─────────────────────────────────────────────────────────

def run_cron():
    """Run signal generation once (for cron/scheduled execution)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("=== K-9 Serverless Signal Generator ===")
    log.info(f"Bot: {THREECOMMAS_BOT_UUID[:8]}... | Secret: {'SET' if THREECOMMAS_SECRET else 'NOT SET'}")

    result = generate_signals()

    log.info(f"=== Complete: {len(result['signals'])} signals generated ===")
    log.info(f"Suppress: {result['suppress_short']} | Stats: {result['stats']}")

    # Exit 0 for success, 1 if secret missing
    if not THREECOMMAS_SECRET:
        log.warning("⚠ THREECOMMASSECRET not set — signals not delivered")
        sys.exit(1)

    # Print JSON summary for logging
    print(json.dumps(result, indent=2, default=str))
    sys.exit(0)


def run_server(port: int = 9013):
    """Run as a lightweight FastAPI server for manual signals."""
    from fastapi import FastAPI
    from pydantic import BaseModel

    app = FastAPI(title="K-9 Serverless Signal Bridge", version="1.0")

    class SignalRequest(BaseModel):
        action: str
        instrument: str
        confidence: float = 0.80
        trigger_price: float | None = None
        reason: str = ""

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "mode": "serverless",
            "bot_uuid": THREECOMMAS_BOT_UUID[:8] + "..." if THREECOMMAS_BOT_UUID else "NOT_SET",
            "secret_set": bool(THREECOMMAS_SECRET),
            "last_run": _last_run,
            "stats": dict(_stats),
        }

    @app.post("/signal")
    async def signal(req: SignalRequest):
        if req.action not in VALID_ACTIONS:
            return {"error": f"Invalid action: {req.action}"}
        if req.confidence < CONFIDENCE_THRESHOLD:
            return {"error": f"Confidence {req.confidence} below threshold {CONFIDENCE_THRESHOLD}"}
        result = send_3commas_signal(req.action, req.instrument, req.trigger_price)
        _stats["manual_signals"] += 1
        return {"action": req.action, "instrument": req.instrument, "delivery": result}

    @app.post("/auto")
    async def auto():
        return generate_signals()

    @app.get("/log")
    async def signal_log(limit: int = 20):
        return {"count": min(limit, len(_signal_log)), "runs": _signal_log[:limit]}

    @app.get("/scores")
    async def scores():
        """Fetch current catalyst scores from public APIs."""
        results = {}
        for inst in TRACKED_INSTRUMENTS:
            metrics = fetch_all_metrics(inst)
            score = compute_catalyst_score(inst, metrics)
            results[inst] = {"catalyst_score": round(score, 1), "metrics": len(metrics)}
        fomc = fetch_cme_fedwatch()
        fiscal = fetch_fiscal_dominance_proxy()
        return {
            "scores": results,
            "fomc": fomc,
            "fiscal_dominance": fiscal,
            "suppress_short": [k for k, v in results.items() if v["catalyst_score"] > 80 and fomc["hike_probability"] > 0.50],
        }

    import uvicorn
    log.info(f"Starting serverless server on :{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="K-9 Serverless Signal Generator")
    parser.add_argument("--run", action="store_true", help="Run signal generation once (cron mode)")
    parser.add_argument("--server", action="store_true", help="Run as FastAPI server")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "9013")), help="Server port (default 9013)")
    args = parser.parse_args()

    if args.server:
        run_server(args.port)
    elif args.run:
        run_cron()
    else:
        parser.print_help()
