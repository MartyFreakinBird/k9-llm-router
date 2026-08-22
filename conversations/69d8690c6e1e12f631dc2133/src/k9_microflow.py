"""
k9_microflow.py — Microflow Ingestion & Catalyst Score Engine
─────────────────────────────────────────────────────────────────────────────
Tracks institutionally relevant, asset-specific catalysts across crypto,
equities, fixed income, commodities, and FX. Computes a Catalyst Score (0-100)
that measures how much micro momentum is dominating macro signals.

When catalyst_score > 80 AND macro signals are bearish → SUPPRESS_SHORT
When catalyst_score < 20 AND macro signals are dominant → MACRO_DOMINANT

Connectors (all free, no API keys):
  - DeFi Llama: on-chain revenue + TVL (crypto)
  - CoinGecko: price momentum (crypto)
  - Farside: ETF flow data (BTC/ETH ETFs, HTML scraping)
  - yfinance: ETF momentum (equities, commodities, fixed income)
  - Local sentiment engine: social sentiment (:9006)

Port: 9012
DuckDB: fomc_cockpit.duckdb (Fact_Microflow table)
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

import duckdb
import httpx

log = logging.getLogger("k9.microflow")

# ── Config ────────────────────────────────────────────────────────────────────

DUCKDB_PATH = os.getenv("MICROFLOW_DUCKDB", "fomc_cockpit.duckdb")
FISCAL_DOMINANCE_URL = os.getenv("FISCAL_DOMINANCE_URL", "http://localhost:9010")
LLM_ROUTER_URL = os.getenv("LLM_ROUTER_URL", "http://localhost:8765")
SENTIMENT_ENGINE_URL = os.getenv("SENTIMENT_ENGINE_URL", "http://localhost:9006")
AEG_SIGNAL_ROUTER = os.getenv("AEG_SIGNAL_ROUTER_URL", "http://localhost:9004")

# Metric weights for catalyst score
METRIC_WEIGHTS: dict[str, float] = {
    "etf_flow": 0.30,
    "onchain_revenue": 0.25,
    "onchain_tvl": 0.15,
    "price_momentum": 0.20,
    "etf_momentum": 0.15,
    "social_sentiment": 0.10,
}

# EMA smoothing for catalyst score
EMA_ALPHA = 0.3

# ── DuckDB ────────────────────────────────────────────────────────────────────

_con: duckdb.DuckDBPyConnection | None = None
_catalyst_cache: dict[str, Any] = {}
_catalyst_cache_ts: float = 0
_divergence_cache: dict[str, Any] = {}
_divergence_cache_ts: float = 0
CACHE_TTL = 300  # 5 minutes


def get_db() -> duckdb.DuckDBPyConnection:
    global _con
    if _con is None:
        _con = duckdb.connect(DUCKDB_PATH)
        _con.execute("""
            CREATE TABLE IF NOT EXISTS Fact_Microflow (
                timestamp TIMESTAMP,
                instrument VARCHAR,
                metric_type VARCHAR,
                value DOUBLE,
                source VARCHAR,
                metadata VARCHAR,
                PRIMARY KEY (timestamp, instrument, metric_type)
            )
        """)
    return _con


# ── Connector Architecture ────────────────────────────────────────────────────

class MicroflowConnector(ABC):
    instrument: str = ""
    metric_type: str = ""
    source: str = ""

    @abstractmethod
    def fetch(self) -> list[dict]:
        """Return list of dicts: timestamp, instrument, metric_type, value, source, metadata"""
        ...

    def _record(self, value: float, metadata: dict | None = None) -> dict:
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "instrument": self.instrument,
            "metric_type": self.metric_type,
            "value": float(value),
            "source": self.source,
            "metadata": json.dumps(metadata or {}),
        }


class DeFiLlamaRevenueConnector(MicroflowConnector):
    source = "DeFiLlama"

    def __init__(self, instrument: str, protocol: str):
        self.instrument = instrument
        self.metric_type = "onchain_revenue"
        self.protocol = protocol

    def fetch(self) -> list[dict]:
        try:
            with httpx.Client(timeout=10) as c:
                resp = c.get(f"https://api.llama.fi/summary/fees/{self.protocol}")
                if resp.status_code != 200:
                    return []
                data = resp.json()
                revenue_24h = data.get("total24h")
                fees_24h = data.get("totalFees24h")
                if revenue_24h is None:
                    return []
                return [self._record(float(revenue_24h), {
                    "protocol": self.protocol,
                    "revenue_24h": revenue_24h,
                    "fees_24h": fees_24h,
                    "change_1d": data.get("change_1d"),
                })]
        except Exception as e:
            log.debug(f"DeFiLlama revenue {self.instrument}: {e}")
            return []


class DeFiLlamaTVLConnector(MicroflowConnector):
    source = "DeFiLlama"
    metric_type = "onchain_tvl"

    def __init__(self, instrument: str, chain: str):
        self.instrument = instrument
        self.chain = chain

    def fetch(self) -> list[dict]:
        try:
            with httpx.Client(timeout=10) as c:
                resp = c.get("https://api.llama.fi/v2/chains")
                if resp.status_code != 200:
                    return []
                chains = resp.json()
                for ch in chains:
                    if ch.get("name", "").lower() == self.chain.lower():
                        tvl = ch.get("tvl", 0)
                        change_7d = ch.get("chainTvlData", {}).get("tvl_7d_change", 0)
                        return [self._record(float(tvl), {
                            "chain": self.chain,
                            "tvl": tvl,
                            "change_7d": change_7d,
                        })]
                return []
        except Exception as e:
            log.debug(f"DeFiLlama TVL {self.instrument}: {e}")
            return []


class CoinGeckoMomentumConnector(MicroflowConnector):
    source = "CoinGecko"
    metric_type = "price_momentum"

    COINGECKO_IDS = {
        "BTC": "bitcoin",
        "ETH": "ethereum",
        "SOL": "solana",
        "HBAR": "hedera-hashgraph",
    }

    def __init__(self, instrument: str):
        self.instrument = instrument
        self.cg_id = self.COINGECKO_IDS.get(instrument, "")

    def fetch(self) -> list[dict]:
        if not self.cg_id:
            return []
        try:
            with httpx.Client(timeout=10) as c:
                resp = c.get(
                    f"https://api.coingecko.com/api/v3/coins/{self.cg_id}/market_chart",
                    params={"vs_currency": "usd", "days": "7", "interval": "daily"}
                )
                if resp.status_code != 200:
                    return []
                data = resp.json()
                prices = data.get("prices", [])
                if len(prices) < 2:
                    return []
                current = prices[-1][1]
                week_ago = prices[0][1]
                if week_ago == 0:
                    return []
                momentum = ((current - week_ago) / week_ago) * 100
                return [self._record(float(momentum), {
                    "current_price": current,
                    "price_7d_ago": week_ago,
                    "momentum_pct": momentum,
                })]
        except Exception as e:
            log.debug(f"CoinGecko momentum {self.instrument}: {e}")
            return []


class FarsideETFConnector(MicroflowConnector):
    source = "Farside"
    metric_type = "etf_flow"

    URLS = {
        "BTC": "https://farside.co.uk/bitcoin-etf-flow-all-data/",
        "ETH": "https://farside.co.uk/ethereum-etf-flow-all-data/",
    }

    def __init__(self, instrument: str):
        self.instrument = instrument
        self.url = self.URLS.get(instrument, "")

    def fetch(self) -> list[dict]:
        if not self.url:
            return []
        try:
            with httpx.Client(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as c:
                resp = c.get(self.url)
                if resp.status_code != 200:
                    return []
                html = resp.text
                # Parse the table rows - Farside uses a simple HTML table
                # Look for date pattern and flow values
                rows = re.findall(r"<tr>(.*?)</tr>", html, re.DOTALL)
                records = []
                for row in rows:
                    cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
                    if len(cells) < 2:
                        continue
                    date_str = cells[0].strip()
                    # Try to parse date (format: "DD Mon YYYY" or similar)
                    try:
                        dt = datetime.strptime(date_str, "%d %b %Y")
                    except ValueError:
                        continue
                    # Last cell is usually the total
                    try:
                        total = float(cells[-1].replace(",", "").replace("$", "").strip())
                    except ValueError:
                        continue
                    if total == 0:
                        continue
                    records.append({
                        "timestamp": dt.isoformat(),
                        "instrument": self.instrument,
                        "metric_type": self.metric_type,
                        "value": total,
                        "source": self.source,
                        "metadata": json.dumps({"date": date_str, "total_flow": total}),
                    })
                # Return only the most recent 30 records
                return records[-30:] if records else []
        except Exception as e:
            log.debug(f"Farside ETF {self.instrument}: {e}")
            return []


class YFinanceETFConnector(MicroflowConnector):
    source = "yfinance"
    metric_type = "etf_momentum"

    ETF_MAP = {
        "SPX": "SPY",
        "QQQ": "QQQ",
        "GLD": "GLD",
        "TLT": "TLT",
        "KRE": "KRE",
        "XLF": "XLF",
    }

    def __init__(self, instrument: str):
        self.instrument = instrument
        self.ticker = self.ETF_MAP.get(instrument, instrument)

    def fetch(self) -> list[dict]:
        try:
            import yfinance as yf
            ticker = yf.Ticker(self.ticker)
            hist = ticker.history(period="1mo")
            if hist.empty or len(hist) < 6:
                return []
            current = float(hist["Close"].iloc[-1])
            week_ago = float(hist["Close"].iloc[-5])
            if week_ago == 0:
                return []
            momentum = ((current - week_ago) / week_ago) * 100
            vol_20d = float(hist["Volume"].iloc[-20:].mean()) if len(hist) >= 20 else float(hist["Volume"].mean())
            vol_5d = float(hist["Volume"].iloc[-5:].mean())
            vol_trend = (vol_5d / vol_20d - 1) * 100 if vol_20d > 0 else 0
            return [self._record(float(momentum), {
                "ticker": self.ticker,
                "current_price": current,
                "momentum_5d_pct": momentum,
                "volume_trend_pct": vol_trend,
            })]
        except Exception as e:
            log.debug(f"yfinance ETF {self.instrument}: {e}")
            return []


class SocialSentimentConnector(MicroflowConnector):
    source = "k9-sentiment"
    metric_type = "social_sentiment"

    def __init__(self, instrument: str):
        self.instrument = instrument

    def fetch(self) -> list[dict]:
        try:
            with httpx.Client(timeout=10) as c:
                resp = c.get(
                    f"{SENTIMENT_ENGINE_URL}/sentiment/score",
                    params={"query": self.instrument}
                )
                if resp.status_code != 200:
                    return []
                data = resp.json()
                score = data.get("aggregate_score", data.get("compound", 0.0))
                return [self._record(float(score), {
                    "query": self.instrument,
                    "sources": data.get("sources", 0),
                    "articles": data.get("total_articles", 0),
                })]
        except Exception as e:
            log.debug(f"Sentiment {self.instrument}: {e}")
            return []


# ── Connector Registry ───────────────────────────────────────────────────────

def build_registry() -> list[MicroflowConnector]:
    return [
        # Crypto on-chain
        DeFiLlamaRevenueConnector("BTC", "bitcoin"),
        DeFiLlamaRevenueConnector("ETH", "ethereum"),
        DeFiLlamaRevenueConnector("SOL", "solana"),
        DeFiLlamaTVLConnector("SOL", "Solana"),
        DeFiLlamaTVLConnector("ETH", "Ethereum"),
        # Crypto price momentum
        CoinGeckoMomentumConnector("BTC"),
        CoinGeckoMomentumConnector("ETH"),
        CoinGeckoMomentumConnector("SOL"),
        CoinGeckoMomentumConnector("HBAR"),
        # ETF flows
        FarsideETFConnector("BTC"),
        FarsideETFConnector("ETH"),
        # Equity / commodity / fixed income ETFs
        YFinanceETFConnector("SPX"),
        YFinanceETFConnector("QQQ"),
        YFinanceETFConnector("GLD"),
        YFinanceETFConnector("TLT"),
        YFinanceETFConnector("KRE"),
        # Social sentiment
        SocialSentimentConnector("BTC"),
        SocialSentimentConnector("ETH"),
        SocialSentimentConnector("SOL"),
    ]


# ── Ingestion ────────────────────────────────────────────────────────────────

def ingest_all() -> dict:
    """Run all connectors and store results in DuckDB."""
    db = get_db()
    registry = build_registry()
    total = 0
    errors = 0
    per_instrument: dict[str, int] = {}

    for connector in registry:
        try:
            records = connector.fetch()
            if not records:
                continue
            for r in records:
                try:
                    db.execute(
                        "INSERT OR REPLACE INTO Fact_Microflow VALUES (?, ?, ?, ?, ?, ?)",
                        [r["timestamp"], r["instrument"], r["metric_type"],
                         r["value"], r["source"], r["metadata"]]
                    )
                    total += 1
                    per_instrument[r["instrument"]] = per_instrument.get(r["instrument"], 0) + 1
                except Exception:
                    errors += 1
        except Exception as e:
            log.warning(f"Connector {connector.source}/{connector.instrument}/{connector.metric_type}: {e}")
            errors += 1

    # Invalidate caches
    global _catalyst_cache_ts, _divergence_cache_ts
    _catalyst_cache_ts = 0
    _divergence_cache_ts = 0

    return {
        "total_records": total,
        "errors": errors,
        "per_instrument": per_instrument,
        "connectors_run": len(registry),
    }


# ── Catalyst Score Computation ───────────────────────────────────────────────

def _normalize_value(values: list[float], current: float) -> float:
    """Min-max normalize a value over a list of values. Returns 0-1."""
    if not values or len(values) < 2:
        return 0.5  # neutral if insufficient data
    vmin = min(values)
    vmax = max(values)
    if vmax == vmin:
        return 0.5
    return (current - vmin) / (vmax - vmin)


def compute_catalyst_score(instrument: str) -> dict:
    """Compute catalyst score (0-100) for a given instrument."""
    db = get_db()

    # Get all metrics for this instrument from last 30 days
    try:
        rows = db.execute("""
            SELECT metric_type, value, timestamp
            FROM Fact_Microflow
            WHERE instrument = ? AND timestamp >= CURRENT_TIMESTAMP - INTERVAL 30 DAY
            ORDER BY timestamp DESC
        """, [instrument]).fetchall()
    except Exception:
        rows = []

    if not rows:
        return {
            "instrument": instrument,
            "catalyst_score": 50.0,
            "metrics_available": 0,
            "metrics": {},
            "ema_score": 50.0,
        }

    # Group by metric_type
    metrics_by_type: dict[str, list[float]] = {}
    latest_by_type: dict[str, float] = {}

    for metric_type, value, ts in rows:
        metrics_by_type.setdefault(metric_type, []).append(value)
        if metric_type not in latest_by_type:
            latest_by_type[metric_type] = value

    # Compute weighted score
    total_weight = 0.0
    weighted_score = 0.0
    metrics_detail: dict[str, Any] = {}

    for metric_type, values in metrics_by_type.items():
        weight = METRIC_WEIGHTS.get(metric_type, 0.05)
        current = latest_by_type.get(metric_type, values[-1] if values else 0)
        norm = _normalize_value(values, current)
        # Clamp to 0-1
        norm = max(0.0, min(1.0, norm))

        weighted_score += norm * weight
        total_weight += weight
        metrics_detail[metric_type] = {
            "current_value": round(current, 4),
            "normalized": round(norm, 4),
            "weight": weight,
            "samples": len(values),
        }

    if total_weight == 0:
        raw_score = 50.0
    else:
        raw_score = (weighted_score / total_weight) * 100

    # EMA smoothing (if we have a previous score)
    ema_score = raw_score
    prev_key = f"{instrument}_ema"
    prev_ema = _catalyst_cache.get(prev_key)
    if prev_ema is not None:
        ema_score = EMA_ALPHA * raw_score + (1 - EMA_ALPHA) * prev_ema
    _catalyst_cache[prev_key] = ema_score

    return {
        "instrument": instrument,
        "catalyst_score": round(raw_score, 1),
        "ema_score": round(ema_score, 1),
        "metrics_available": len(metrics_by_type),
        "metrics": metrics_detail,
    }


def compute_all_scores() -> list[dict]:
    """Compute catalyst scores for all instruments in the database."""
    db = get_db()
    try:
        instruments = db.execute("""
            SELECT DISTINCT instrument FROM Fact_Microflow
            ORDER BY instrument
        """).fetchall()
    except Exception:
        instruments = []

    return [compute_catalyst_score(inst[0]) for inst in instruments]


# ── Divergence Detection ─────────────────────────────────────────────────────

def _fetch_macro_scores() -> dict:
    """Fetch fiscal dominance and FOMC hike probability from other modules."""
    result = {"fiscal_dominance_score": 50.0, "fomc_hike_probability": 0.0}

    # Fiscal dominance
    try:
        with httpx.Client(timeout=10) as c:
            resp = c.get(f"{FISCAL_DOMINANCE_URL}/fiscal/score")
            if resp.status_code == 200:
                result["fiscal_dominance_score"] = float(resp.json().get("dominance_score", 50.0))
    except Exception:
        pass

    # FOMC hike probability
    try:
        with httpx.Client(timeout=10) as c:
            resp = c.get(f"{LLM_ROUTER_URL}/fomc/cascade")
            if resp.status_code == 200:
                cascade = resp.json()
                result["fomc_hike_probability"] = float(
                    cascade.get("aggregate", {}).get("hike_probability", 0.0)
                )
    except Exception:
        pass

    return result


def compute_divergence() -> list[dict]:
    """Compute divergence flags for all instruments."""
    global _divergence_cache, _divergence_cache_ts

    if _divergence_cache and (time.time() - _divergence_cache_ts) < CACHE_TTL:
        return _divergence_cache.get("flags", [])

    scores = compute_all_scores()
    macro = _fetch_macro_scores()

    flags = []
    suppress_instruments = []

    for s in scores:
        catalyst = s["catalyst_score"]
        fiscal = macro["fiscal_dominance_score"]
        hike_prob = macro["fomc_hike_probability"]

        if catalyst > 80 and hike_prob > 0.50:
            signal = "SUPPRESS_SHORT"
            suppress_instruments.append(s["instrument"])
        elif catalyst < 20 and fiscal > 70:
            signal = "MACRO_DOMINANT"
        else:
            signal = "NEUTRAL"

        flags.append({
            "instrument": s["instrument"],
            "catalyst_score": catalyst,
            "ema_score": s["ema_score"],
            "fiscal_dominance_score": round(fiscal, 1),
            "fomc_hike_probability": round(hike_prob, 3),
            "divergence_signal": signal,
            "metrics_available": s["metrics_available"],
        })

    _divergence_cache = {"flags": flags}
    _divergence_cache_ts = time.time()

    # Route SUPPRESS_SHORT signals
    if suppress_instruments:
        _route_suppress_signal(suppress_instruments, flags, macro)

    return flags


def _route_suppress_signal(instruments: list[str], flags: list[dict], macro: dict) -> bool:
    """Route a CB v1 signal when SUPPRESS_SHORT is detected."""
    envelope = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "k9-microflow",
        "type": "micro_macro_divergence",
        "ontology_tags": ["microflow", "catalyst", "divergence", "suppress_short"],
        "confidence": 0.85,
        "payload": {
            "suppress_short_instruments": instruments,
            "catalyst_scores": {f["instrument"]: f["catalyst_score"] for f in flags if f["instrument"] in instruments},
            "fiscal_dominance_score": macro["fiscal_dominance_score"],
            "fomc_hike_probability": macro["fomc_hike_probability"],
            "action": "suppress_short_signals",
            "reason": "Micro catalysts overwhelming macro signals — do not short these instruments",
        },
        "trace_id": str(uuid.uuid4()),
        "causation_id": str(uuid.uuid4()),
        "signature": "",
    }
    try:
        with httpx.Client(timeout=10) as c:
            resp = c.post(f"{AEG_SIGNAL_ROUTER}/route", json=envelope)
            return resp.status_code == 200
    except Exception:
        return False


# ── FastAPI ───────────────────────────────────────────────────────────────────

from fastapi import FastAPI

app = FastAPI(title="K-9 Microflow Engine", version="1.0")


@app.get("/microflow/health")
async def health():
    db = get_db()
    try:
        count = db.execute("SELECT COUNT(*) FROM Fact_Microflow").fetchone()[0]
    except Exception:
        count = 0
    return {
        "status": "ok",
        "port": 9012,
        "duckdb_path": DUCKDB_PATH,
        "total_records": count,
        "cache_age_s": round(time.time() - _catalyst_cache_ts, 0) if _catalyst_cache_ts else None,
    }


@app.post("/microflow/ingest")
async def trigger_ingest():
    return ingest_all()


@app.get("/microflow/catalyst/{instrument}")
async def get_catalyst(instrument: str):
    return compute_catalyst_score(instrument.upper())


@app.get("/microflow/catalyst/all")
async def get_all_catalysts():
    global _catalyst_cache, _catalyst_cache_ts

    if _catalyst_cache and (time.time() - _catalyst_cache_ts) < CACHE_TTL:
        return {"scores": _catalyst_cache.get("scores", []), "cached": True}

    scores = compute_all_scores()
    _catalyst_cache = {"scores": scores}
    _catalyst_cache_ts = time.time()
    return {"scores": scores, "cached": False}


@app.get("/microflow/divergence")
async def get_divergence():
    flags = compute_divergence()
    suppress = [f for f in flags if f["divergence_signal"] == "SUPPRESS_SHORT"]
    macro_dom = [f for f in flags if f["divergence_signal"] == "MACRO_DOMINANT"]
    return {
        "flags": flags,
        "suppress_short_instruments": [f["instrument"] for f in suppress],
        "macro_dominant_instruments": [f["instrument"] for f in macro_dom],
        "alert_level": "SUPPRESS_SHORT" if suppress else "MACRO_DOMINANT" if macro_dom else "NORMAL",
    }


@app.get("/microflow/metrics/{instrument}")
async def get_metrics(instrument: str):
    db = get_db()
    try:
        rows = db.execute("""
            SELECT timestamp, metric_type, value, source, metadata
            FROM Fact_Microflow
            WHERE instrument = ?
            ORDER BY timestamp DESC
            LIMIT 100
        """, [instrument.upper()]).fetchall()
    except Exception:
        rows = []

    return {
        "instrument": instrument.upper(),
        "records": [
            {
                "timestamp": str(r[0]),
                "metric_type": r[1],
                "value": r[2],
                "source": r[3],
                "metadata": r[4],
            }
            for r in rows
        ],
        "count": len(rows),
    }


@app.get("/microflow/coverage")
async def coverage():
    db = get_db()
    try:
        stats = db.execute("""
            SELECT
                instrument,
                metric_type,
                COUNT(*) as count,
                MIN(timestamp) as earliest,
                MAX(timestamp) as latest
            FROM Fact_Microflow
            GROUP BY instrument, metric_type
            ORDER BY instrument, metric_type
        """).fetchall()
    except Exception:
        stats = []

    return {
        "total_instruments": len(set(r[0] for r in stats)),
        "total_metrics": len(stats),
        "details": [
            {
                "instrument": r[0],
                "metric_type": r[1],
                "count": r[2],
                "earliest": str(r[3]),
                "latest": str(r[4]),
            }
            for r in stats
        ],
    }
