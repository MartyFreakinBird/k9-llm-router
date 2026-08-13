"""
k9_polymarket_adapter.py — K-9 Polymarket Prediction Market Adapter
─────────────────────────────────────────────────────────────────────────────
Port 9007 · READ-ONLY · Zero API keys required

Fetches prediction market data from Polymarket's public APIs:
  - Gamma API (gamma-api.polymarket.com): market metadata + implied probabilities
  - CLOB API (clob.polymarket.com): orderbook depth for precise mid-point

Stores in DuckDB (k9_polymarket.duckdb) with analysis views:
  - Polymarket_Markets: all active market metadata
  - Polymarket_Prices: time-series of implied probability snapshots
  - v_prediction_market_edges: mispriced events ranked by edge magnitude
  - v_fed_prob_comparison: Fed hike model vs market probability

Packages significant edges as CB v1 envelopes → routes to aeg_signal_router :9004.

Integration:
  - fed-whisperer HikeScore → model_prob for Fed markets
  - Cornwall Capital framework: asymmetric opportunity detection
  - Cross-module: sentiment engine + quant engine + signal router

Endpoints:
  GET  /health                    — service liveness + DuckDB stats
  GET  /markets                   — all stored markets (paginated)
  GET  /markets/{id}              — single market detail + orderbook
  GET  /edges                     — top mispriced opportunities
  GET  /edges/fed                 — Fed hike probability comparison
  GET  /edges/crypto              — crypto prediction markets
  POST /refresh                   — manual trigger: fetch from Gamma API
  POST /refresh/orderbook/{id}    — fetch CLOB orderbook for a market
  GET  /stats                     — aggregate stats + DuckDB table sizes
  GET  /search?q=                 — full-text search across market questions

Config: polymarket_config.yaml (optional event_slugs filter)
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

# Optional DuckDB import — service still works without it (in-memory mode)
try:
    import duckdb
    DUCKDB_AVAILABLE = True
except ImportError:
    DUCKDB_AVAILABLE = False

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

log = logging.getLogger("k9.polymarket")

# ── Config ────────────────────────────────────────────────────────────────────

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
DUCKDB_PATH = os.getenv("POLYMARKET_DUCKDB", "k9_polymarket.duckdb")
CONFIG_PATH = os.getenv("POLYMARKET_CONFIG", "polymarket_config.yaml")

AEG_SIGNAL_ROUTER_URL = os.getenv("AEG_SIGNAL_ROUTER_URL", "http://localhost:9004")
K9_INTEGRATION_KEY = os.getenv("K9_INTEGRATION_KEY", "")

# ── In-memory cache (fallback if DuckDB unavailable) ─────────────────────────
_cache: dict[str, Any] = {
    "markets": [],
    "prices": [],
    "last_refresh": None,
    "refresh_count": 0,
}

# ── Config loader ─────────────────────────────────────────────────────────────

_event_slugs: list[str] | None = None


def load_config() -> list[str] | None:
    """Load event_slugs from polymarket_config.yaml if present."""
    global _event_slugs
    if _event_slugs is not None:
        return _event_slugs

    config_file = Path(CONFIG_PATH)
    if config_file.exists():
        try:
            with open(config_file) as f:
                cfg = yaml.safe_load(f) or {}
            _event_slugs = cfg.get("event_slugs", [])
            log.info(f"Loaded {len(_event_slugs)} event slugs from {CONFIG_PATH}")
        except Exception as e:
            log.warning(f"Failed to load config: {e}")
            _event_slugs = []
    else:
        _event_slugs = []
    return _event_slugs


# ── DuckDB helpers ────────────────────────────────────────────────────────────

_con: "duckdb.DuckDBPyConnection | None" = None


def get_db():
    """Get or create DuckDB connection."""
    global _con
    if not DUCKDB_AVAILABLE:
        return None
    if _con is None:
        _con = duckdb.connect(DUCKDB_PATH)
        _init_tables(_con)
    return _con


def _init_tables(con):
    """Create tables and views if they don't exist."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS Polymarket_Markets (
            id VARCHAR PRIMARY KEY,
            question TEXT,
            slug VARCHAR,
            condition_id VARCHAR,
            outcomes JSON,
            outcome_prices JSON,
            end_date TIMESTAMP,
            active BOOLEAN,
            closed BOOLEAN,
            volume DOUBLE,
            liquidity DOUBLE,
            volume_24hr DOUBLE,
            volume_1wk DOUBLE,
            volume_1mo DOUBLE,
            best_bid DOUBLE,
            best_ask DOUBLE,
            last_trade_price DOUBLE,
            clob_token_ids JSON,
            implied_prob_yes DOUBLE,
            implied_prob_no DOUBLE,
            category VARCHAR,
            fetched_at TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS Polymarket_Prices (
            id BIGINT,
            market_id VARCHAR,
            implied_prob_yes DOUBLE,
            implied_prob_no DOUBLE,
            best_bid DOUBLE,
            best_ask DOUBLE,
            last_trade_price DOUBLE,
            volume_24hr DOUBLE,
            timestamp TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS Polymarket_Orderbook (
            id BIGINT,
            market_id VARCHAR,
            token_id VARCHAR,
            side VARCHAR,
            price DOUBLE,
            size DOUBLE,
            depth_rank INTEGER,
            timestamp TIMESTAMP
        )
    """)
    _create_views(con)


def _create_views(con):
    """Create analysis views."""
    con.execute("""
        CREATE OR REPLACE VIEW v_prediction_market_edges AS
        SELECT
            m.id,
            m.question,
            m.slug,
            m.end_date,
            m.implied_prob_yes AS market_prob,
            m.volume,
            m.liquidity,
            m.volume_24hr,
            m.best_bid,
            m.best_ask,
            m.best_ask - m.best_bid AS spread,
            m.category,
            m.fetched_at,
            ABS(m.implied_prob_yes - 0.5) AS edge_magnitude,
            CASE
                WHEN m.implied_prob_yes < 0.10 THEN 'deep_tail_yes'
                WHEN m.implied_prob_yes < 0.20 THEN 'low_prob_yes'
                WHEN m.implied_prob_yes > 0.90 THEN 'high_conviction_yes'
                WHEN m.implied_prob_yes > 0.80 THEN 'likely_yes'
                ELSE 'contested'
            END AS prob_bucket
        FROM Polymarket_Markets m
        WHERE m.active = true
          AND m.closed = false
          AND m.volume > 50000
          AND m.best_bid > 0
          AND m.best_ask > 0
        ORDER BY m.volume DESC
    """)

    con.execute("""
        CREATE OR REPLACE VIEW v_fed_prob_comparison AS
        SELECT
            m.id,
            m.question,
            m.slug,
            m.implied_prob_yes AS market_prob,
            m.best_bid,
            m.best_ask,
            m.last_trade_price,
            m.volume,
            m.volume_24hr,
            m.end_date,
            m.fetched_at
        FROM Polymarket_Markets m
        WHERE m.question ILIKE '%fed%'
           OR m.question ILIKE '%rate%'
           OR m.question ILIKE '%fomc%'
           OR m.question ILIKE '%hike%'
           OR m.question ILIKE '%cut%'
           OR m.question ILIKE '%interest%'
           OR m.question ILIKE '%powell%'
        ORDER BY m.volume DESC
    """)

    con.execute("""
        CREATE OR REPLACE VIEW v_crypto_markets AS
        SELECT
            m.id,
            m.question,
            m.slug,
            m.implied_prob_yes AS market_prob,
            m.volume,
            m.volume_24hr,
            m.liquidity,
            m.end_date,
            m.fetched_at
        FROM Polymarket_Markets m
        WHERE (m.question ILIKE '%bitcoin%'
            OR m.question ILIKE '%btc%'
            OR m.question ILIKE '%ethereum%'
            OR m.question ILIKE '%eth%'
            OR m.question ILIKE '%crypto%')
          AND m.active = true
          AND m.closed = false
        ORDER BY m.volume DESC
    """)

    con.execute("""
        CREATE OR REPLACE VIEW v_price_history AS
        SELECT
            p.market_id,
            m.question,
            p.implied_prob_yes,
            p.timestamp
        FROM Polymarket_Prices p
        JOIN Polymarket_Markets m ON p.market_id = m.id
        ORDER BY p.timestamp DESC
    """)


# ── Market categorization ────────────────────────────────────────────────────

def categorize_market(question: str) -> str:
    """Categorize a market by its question text."""
    q = question.lower()
    if any(k in q for k in ["fed", "fomc", "rate", "interest", "hike", "cut", "powell", "treasury", "yield"]):
        return "macro_fed"
    if any(k in q for k in ["recession", "gdp", "inflation", "cpi", "unemployment"]):
        return "macro_economy"
    if any(k in q for k in ["bitcoin", "btc", "ethereum", "eth", "crypto"]):
        return "crypto"
    if any(k in q for k in ["oil", "gold", "sp500", "s&p", "nasdaq", "commodity"]):
        return "macro_commodities"
    if any(k in q for k in ["election", "senate", "house", "president", "trump", "biden"]):
        return "politics"
    if any(k in q for k in ["china", "tariff", "trade war", "geopolit"]):
        return "geopolitics"
    return "other"


# ── Gamma API fetch ──────────────────────────────────────────────────────────

def fetch_all_markets(limit: int = 500, max_markets: int = 5000) -> list[dict]:
    """Fetch active markets from Gamma API (paginated)."""
    markets: list[dict] = []
    offset = 0

    with httpx.Client(timeout=30.0) as client:
        while offset < max_markets:
            params = {
                "limit": limit,
                "offset": offset,
                "active": "true",
                "closed": "false",
                "order": "volume",
                "ascending": "false",
            }
            try:
                resp = client.get(f"{GAMMA_BASE}/markets", params=params)
                resp.raise_for_status()
                data = resp.json()
                if not data:
                    break
                markets.extend(data)
                offset += limit
                if len(data) < limit:
                    break
                log.debug(f"Fetched {len(markets)} markets so far...")
            except Exception as e:
                log.error(f"Gamma API error at offset {offset}: {e}")
                break

    log.info(f"Fetched {len(markets)} markets from Gamma API")
    return markets


def parse_market(raw: dict) -> dict:
    """Parse a raw Gamma API market into our schema."""
    prices_str = raw.get("outcomePrices", "[]")
    try:
        prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
    except Exception:
        prices = []

    outcomes_str = raw.get("outcomes", "[]")
    try:
        outcomes = json.loads(outcomes_str) if isinstance(outcomes_str, str) else outcomes_str
    except Exception:
        outcomes = []

    clob_str = raw.get("clobTokenIds", "[]")
    try:
        clob_ids = json.loads(clob_str) if isinstance(clob_str, str) else clob_str
    except Exception:
        clob_ids = []

    question = raw.get("question", "")
    implied_yes = float(prices[0]) if len(prices) > 0 else 0.0
    implied_no = float(prices[1]) if len(prices) > 1 else 1.0 - implied_yes

    return {
        "id": str(raw.get("id", "")),
        "question": question,
        "slug": raw.get("slug", ""),
        "condition_id": raw.get("conditionId", ""),
        "outcomes": json.dumps(outcomes),
        "outcome_prices": json.dumps(prices),
        "end_date": raw.get("endDate", ""),
        "active": raw.get("active", True),
        "closed": raw.get("closed", False),
        "volume": float(raw.get("volumeNum", raw.get("volume", 0)) or 0),
        "liquidity": float(raw.get("liquidityNum", raw.get("liquidity", 0)) or 0),
        "volume_24hr": float(raw.get("volume24hr", 0) or 0),
        "volume_1wk": float(raw.get("volume1wk", 0) or 0),
        "volume_1mo": float(raw.get("volume1mo", 0) or 0),
        "best_bid": float(raw.get("bestBid", 0) or 0),
        "best_ask": float(raw.get("bestAsk", 0) or 0),
        "last_trade_price": float(raw.get("lastTradePrice", 0) or 0),
        "clob_token_ids": json.dumps(clob_ids),
        "implied_prob_yes": implied_yes,
        "implied_prob_no": implied_no,
        "category": categorize_market(question),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def store_markets(parsed: list[dict]):
    """Store parsed markets in DuckDB + in-memory cache."""
    _cache["markets"] = parsed
    _cache["last_refresh"] = datetime.now(timezone.utc).isoformat()
    _cache["refresh_count"] += 1

    now = datetime.now(timezone.utc).isoformat()
    price_snapshots = []
    for m in parsed:
        price_snapshots.append({
            "market_id": m["id"],
            "implied_prob_yes": m["implied_prob_yes"],
            "implied_prob_no": m["implied_prob_no"],
            "best_bid": m["best_bid"],
            "best_ask": m["best_ask"],
            "last_trade_price": m["last_trade_price"],
            "volume_24hr": m["volume_24hr"],
            "timestamp": now,
        })
    _cache["prices"] = price_snapshots

    con = get_db()
    if con is None:
        log.warning("DuckDB not available — using in-memory cache only")
        return

    for m in parsed:
        try:
            con.execute("""
                INSERT OR REPLACE INTO Polymarket_Markets VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, [
                m["id"], m["question"], m["slug"], m["condition_id"],
                m["outcomes"], m["outcome_prices"], m["end_date"],
                m["active"], m["closed"], m["volume"], m["liquidity"],
                m["volume_24hr"], m["volume_1wk"], m["volume_1mo"],
                m["best_bid"], m["best_ask"], m["last_trade_price"],
                m["clob_token_ids"], m["implied_prob_yes"], m["implied_prob_no"],
                m["category"], m["fetched_at"]
            ])
        except Exception as e:
            log.debug(f"Failed to store market {m['id']}: {e}")

    for p in price_snapshots:
        try:
            con.execute("""
                INSERT INTO Polymarket_Prices VALUES (
                    DEFAULT, ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, [
                p["market_id"], p["implied_prob_yes"], p["implied_prob_no"],
                p["best_bid"], p["best_ask"], p["last_trade_price"],
                p["volume_24hr"], p["timestamp"]
            ])
        except Exception as e:
            log.debug(f"Failed to store price: {e}")

    _create_views(con)
    log.info(f"Stored {len(parsed)} markets in DuckDB")


# ── CLOB orderbook fetch ─────────────────────────────────────────────────────

def fetch_orderbook(token_id: str) -> dict | None:
    """Fetch full orderbook from CLOB API for a specific token."""
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(f"{CLOB_BASE}/book", params={"token_id": token_id})
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        log.error(f"CLOB API error for token {token_id}: {e}")
        return None


def parse_orderbook(ob: dict, market_id: str, token_id: str) -> dict:
    """Parse CLOB orderbook into structured data."""
    bids = ob.get("bids", [])
    asks = ob.get("asks", [])

    best_bid = float(bids[0]["price"]) if bids else 0.0
    best_ask = float(asks[0]["price"]) if asks else 0.0
    mid = (best_bid + best_ask) / 2 if best_bid and best_ask else 0.0

    bid_depth = sum(float(b["size"]) * float(b["price"]) for b in bids[:10])
    ask_depth = sum(float(a["size"]) * float(a["price"]) for a in asks[:10])

    return {
        "market_id": market_id,
        "token_id": token_id,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid_price": mid,
        "implied_probability": mid,
        "bid_depth_top10": round(bid_depth, 2),
        "ask_depth_top10": round(ask_depth, 2),
        "total_bids": len(bids),
        "total_asks": len(asks),
        "spread": round(best_ask - best_bid, 4),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── CB v1 signal packaging ───────────────────────────────────────────────────

def build_cb1_envelope(source: str, signal_type: str, payload: dict, confidence: float = 0.65) -> dict:
    """Build a K9-CB v1 envelope for routing to aeg_signal_router."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "type": signal_type,
        "ontology_tags": ["prediction_market", "polymarket", "asymmetric_opportunity"],
        "confidence": confidence,
        "payload": payload,
        "trace_id": str(uuid.uuid4()),
        "causation_id": str(uuid.uuid4()),
        "signature": "",
    }


def route_edge_signal(edge: dict) -> bool:
    """Route a significant edge as a CB v1 envelope to aeg_signal_router."""
    payload = {
        "market_id": edge.get("id"),
        "question": edge.get("question"),
        "market_prob": edge.get("market_prob"),
        "edge_magnitude": edge.get("edge_magnitude"),
        "volume": edge.get("volume"),
        "category": edge.get("category"),
        "action": "investigate_mispricing",
    }
    envelope = build_cb1_envelope(
        source="k9-polymarket-adapter",
        signal_type="prediction_market_edge",
        payload=payload,
        confidence=min(0.95, 0.5 + (edge.get("edge_magnitude", 0) * 0.9)),
    )

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                f"{AEG_SIGNAL_ROUTER_URL}/route",
                json=envelope,
                headers={"x-integration-key": K9_INTEGRATION_KEY} if K9_INTEGRATION_KEY else {},
            )
            return resp.status_code == 200
    except Exception as e:
        log.debug(f"Signal routing failed (expected if router not running): {e}")
        return False


# ── Edge detection ────────────────────────────────────────────────────────────

def _prob_bucket(p: float) -> str:
    if p < 0.10: return "deep_tail_yes"
    if p < 0.20: return "low_prob_yes"
    if p > 0.90: return "high_conviction_yes"
    if p > 0.80: return "likely_yes"
    return "contested"


def detect_edges(min_volume: float = 50000, min_edge: float = 0.15) -> list[dict]:
    """Detect mispriced markets — significant divergence from 0.5 or model."""
    con = get_db()
    if con is not None:
        try:
            result = con.execute("""
                SELECT * FROM v_prediction_market_edges
                WHERE volume > ?
                ORDER BY edge_magnitude DESC
                LIMIT 50
            """, [min_volume]).fetchdf()
            return result.to_dict("records")
        except Exception as e:
            log.error(f"DuckDB edge query failed: {e}")

    edges = []
    for m in _cache["markets"]:
        if m["volume"] < min_volume:
            continue
        edge_mag = abs(m["implied_prob_yes"] - 0.5)
        if edge_mag >= min_edge:
            edges.append({
                "id": m["id"],
                "question": m["question"],
                "slug": m["slug"],
                "end_date": m["end_date"],
                "market_prob": m["implied_prob_yes"],
                "volume": m["volume"],
                "liquidity": m["liquidity"],
                "volume_24hr": m["volume_24hr"],
                "best_bid": m["best_bid"],
                "best_ask": m["best_ask"],
                "spread": m["best_ask"] - m["best_bid"],
                "category": m["category"],
                "fetched_at": m["fetched_at"],
                "edge_magnitude": edge_mag,
                "prob_bucket": _prob_bucket(m["implied_prob_yes"]),
            })
    edges.sort(key=lambda x: x["volume"], reverse=True)
    return edges[:50]


# ── Fed probability comparison ───────────────────────────────────────────────

def get_fed_markets() -> list[dict]:
    """Get all Fed-related markets for comparison with HikeScore."""
    con = get_db()
    if con is not None:
        try:
            result = con.execute("SELECT * FROM v_fed_prob_comparison").fetchdf()
            return result.to_dict("records")
        except Exception:
            pass

    return [
        m for m in _cache["markets"]
        if any(k in m["question"].lower() for k in ["fed", "rate", "fomc", "hike", "cut", "interest", "powell"])
    ]


# ── Search ────────────────────────────────────────────────────────────────────

def search_markets(query: str, limit: int = 20) -> list[dict]:
    """Full-text search across market questions."""
    q = query.lower()
    results = []
    for m in _cache["markets"]:
        if q in m["question"].lower():
            results.append({
                "id": m["id"],
                "question": m["question"],
                "slug": m["slug"],
                "market_prob": m["implied_prob_yes"],
                "volume": m["volume"],
                "volume_24hr": m["volume_24hr"],
                "category": m["category"],
                "end_date": m["end_date"],
            })
    results.sort(key=lambda x: x["volume"], reverse=True)
    return results[:limit]


# ── Refresh orchestrator ─────────────────────────────────────────────────────

def refresh_markets(event_slugs: list[str] | None = None) -> dict:
    """Full refresh: fetch from Gamma API → parse → store → detect edges → route signals."""
    slugs = event_slugs if event_slugs is not None else load_config()
    t0 = time.time()

    raw_markets = fetch_all_markets()
    if not raw_markets:
        return {"success": False, "error": "No markets fetched from Gamma API"}

    if slugs:
        raw_markets = [m for m in raw_markets if m.get("slug") in slugs]
        log.info(f"Filtered to {len(raw_markets)} markets matching {len(slugs)} event slugs")

    parsed = [parse_market(m) for m in raw_markets]
    store_markets(parsed)

    edges = detect_edges(min_volume=100000, min_edge=0.30)
    signals_routed = 0
    for edge in edges[:10]:
        if route_edge_signal(edge):
            signals_routed += 1

    elapsed = time.time() - t0
    return {
        "success": True,
        "markets_fetched": len(raw_markets),
        "markets_stored": len(parsed),
        "edges_detected": len(edges),
        "signals_routed": signals_routed,
        "elapsed_seconds": round(elapsed, 2),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="K-9 Polymarket Adapter",
    description="Prediction market data integration — Gamma + CLOB APIs → DuckDB → CB v1 signals",
    version="1.0.0",
)


@app.get("/health")
async def health():
    """Service liveness + DuckDB stats."""
    con = get_db()
    db_stats = {}
    if con is not None:
        try:
            db_stats["markets_count"] = con.execute("SELECT COUNT(*) FROM Polymarket_Markets").fetchone()[0]
            db_stats["prices_count"] = con.execute("SELECT COUNT(*) FROM Polymarket_Prices").fetchone()[0]
        except Exception:
            db_stats["error"] = "Tables not initialized"
    else:
        db_stats["mode"] = "in_memory"
        db_stats["markets_count"] = len(_cache["markets"])

    return {
        "status": "online",
        "service": "k9-polymarket-adapter",
        "port": 9007,
        "duckdb_available": DUCKDB_AVAILABLE,
        "duckdb_path": DUCKDB_PATH,
        "last_refresh": _cache["last_refresh"],
        "refresh_count": _cache["refresh_count"],
        "cached_markets": len(_cache["markets"]),
        "db_stats": db_stats,
    }


@app.get("/markets")
async def get_markets(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    category: str | None = Query(None, description="Filter by category"),
    min_volume: float = Query(0, ge=0),
):
    """List stored markets with optional filtering."""
    markets = _cache["markets"]

    if category:
        markets = [m for m in markets if m["category"] == category]
    if min_volume > 0:
        markets = [m for m in markets if m["volume"] >= min_volume]

    total = len(markets)
    page = markets[offset:offset + limit]

    return {"total": total, "limit": limit, "offset": offset, "markets": page}


@app.get("/markets/{market_id}")
async def get_market_detail(market_id: str):
    """Get a single market with detailed info + optional orderbook."""
    market = next((m for m in _cache["markets"] if m["id"] == market_id), None)
    if not market:
        raise HTTPException(status_code=404, detail=f"Market {market_id} not found")

    clob_ids = json.loads(market.get("clob_token_ids", "[]"))
    orderbooks = {}
    if clob_ids:
        for i, token_id in enumerate(clob_ids[:2]):
            label = "yes" if i == 0 else "no"
            ob = fetch_orderbook(token_id)
            if ob:
                orderbooks[label] = parse_orderbook(ob, market_id, token_id)

    return {"market": market, "orderbooks": orderbooks}


@app.get("/edges")
async def get_edges(
    min_volume: float = Query(50000, ge=0),
    min_edge: float = Query(0.15, ge=0, le=0.5),
    category: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
):
    """Top mispriced prediction market opportunities."""
    edges = detect_edges(min_volume=min_volume, min_edge=min_edge)
    if category:
        edges = [e for e in edges if e.get("category") == category]
    return {
        "edges": edges[:limit],
        "total": len(edges),
        "thresholds": {"min_volume": min_volume, "min_edge": min_edge},
        "last_refresh": _cache["last_refresh"],
    }


@app.get("/edges/fed")
async def get_fed_edges():
    """Fed-related markets for comparison with HikeScore model."""
    fed_markets = get_fed_markets()
    return {
        "fed_markets": fed_markets,
        "count": len(fed_markets),
        "note": "Compare market_prob with your FOMC model HikeScore/100 for mispricing detection",
        "last_refresh": _cache["last_refresh"],
    }


@app.get("/edges/crypto")
async def get_crypto_edges():
    """Crypto-related prediction markets."""
    con = get_db()
    if con is not None:
        try:
            result = con.execute("SELECT * FROM v_crypto_markets").fetchdf()
            return {"crypto_markets": result.to_dict("records"), "count": len(result)}
        except Exception:
            pass

    crypto = [m for m in _cache["markets"] if m["category"] == "crypto"]
    return {"crypto_markets": crypto, "count": len(crypto)}


@app.post("/refresh")
async def refresh(event_slugs: list[str] | None = None):
    """Manual trigger: fetch from Gamma API → store → detect edges → route signals."""
    result = refresh_markets(event_slugs)
    return result


@app.post("/refresh/orderbook/{market_id}")
async def refresh_orderbook(market_id: str):
    """Fetch CLOB orderbook for a specific market."""
    market = next((m for m in _cache["markets"] if m["id"] == market_id), None)
    if not market:
        raise HTTPException(status_code=404, detail=f"Market {market_id} not found")

    clob_ids = json.loads(market.get("clob_token_ids", "[]"))
    if not clob_ids:
        raise HTTPException(status_code=400, detail="No CLOB token IDs for this market")

    orderbooks = {}
    for i, token_id in enumerate(clob_ids[:2]):
        label = "yes" if i == 0 else "no"
        ob = fetch_orderbook(token_id)
        if ob:
            orderbooks[label] = parse_orderbook(ob, market_id, token_id)

    return {"market_id": market_id, "orderbooks": orderbooks}


@app.get("/stats")
async def stats():
    """Aggregate stats across all markets."""
    markets = _cache["markets"]
    if not markets:
        return {"total_markets": 0, "message": "No data — run POST /refresh first"}

    by_category: dict[str, int] = {}
    for m in markets:
        cat = m["category"]
        by_category[cat] = by_category.get(cat, 0) + 1

    total_volume = sum(m["volume"] for m in markets)
    total_24hr = sum(m["volume_24hr"] for m in markets)

    prob_buckets = {"deep_tail_yes": 0, "low_prob_yes": 0, "contested": 0, "likely_yes": 0, "high_conviction_yes": 0}
    for m in markets:
        p = m["implied_prob_yes"]
        bucket = _prob_bucket(p)
        prob_buckets[bucket] = prob_buckets.get(bucket, 0) + 1

    return {
        "total_markets": len(markets),
        "by_category": by_category,
        "total_volume": round(total_volume, 2),
        "total_volume_24hr": round(total_24hr, 2),
        "probability_distribution": prob_buckets,
        "last_refresh": _cache["last_refresh"],
        "refresh_count": _cache["refresh_count"],
    }


@app.get("/search")
async def search(q: str = Query(..., min_length=2), limit: int = Query(20, ge=1, le=100)):
    """Full-text search across market questions."""
    results = search_markets(q, limit)
    return {"query": q, "results": results, "count": len(results)}


# ── Startup ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    """Initialize DuckDB and load config."""
    log.info("K-9 Polymarket Adapter starting on :9007")
    get_db()
    load_config()
    log.info(f"Ready. DuckDB: {DUCKDB_PATH}, Config: {CONFIG_PATH}")


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=9007)
