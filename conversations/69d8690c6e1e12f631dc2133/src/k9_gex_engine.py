"""
k9_gex_engine.py — K-9 Gamma Exposure (GEX) Engine
─────────────────────────────────────────────────────────────────────────────
Port 9008 · READ-ONLY · Zero API keys (Deribit public API)

Computes dealer gamma exposure (GEX) for BTC and ETH options on Deribit:
  - Fetches all option instruments + book summary in 2 API calls (not 58)
  - Computes Black-Scholes gamma using mark_iv from the exchange
  - Aggregates GEX by strike: GEX = gamma × OI × contract_size × spot² × 0.01
  - Calls = positive GEX (call walls), Puts = negative GEX (put walls)
  - Identifies: top call walls, top put walls, gamma flip (zero-gamma level)

Integration:
  - Routes significant GEX shifts as CB v1 envelopes → aeg_signal_router :9004
  - Stores snapshots in DuckDB (k9_gex.duckdb)
  - Callable from n8n (cron), Jellycuts (1 HTTP call), PineScript (webhook)

Endpoints:
  GET  /gex/{asset}              — full GEX calculation (BTC or ETH)
  GET  /gex/{asset}/walls         — just the walls (lightweight, for shortcuts)
  GET  /gex/{asset}/flip          — gamma flip level only
  GET  /gex/{asset}/history       — historical GEX snapshots
  GET  /gex/health                — service liveness
  POST /gex/refresh               — manual trigger
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

# Optional DuckDB
try:
    import duckdb
    DUCKDB_AVAILABLE = True
except ImportError:
    DUCKDB_AVAILABLE = False

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

log = logging.getLogger("k9.gex")

# ── Config ────────────────────────────────────────────────────────────────────

DERIBIT_API = "https://www.deribit.com/api/v2"
DUCKDB_PATH = os.getenv("GEX_DUCKDB", "k9_gex.duckdb")
AEG_SIGNAL_ROUTER_URL = os.getenv("AEG_SIGNAL_ROUTER_URL", "http://localhost:9004")
K9_INTEGRATION_KEY = os.getenv("K9_INTEGRATION_KEY", "")

GEX_MULTIPLIER = 0.01  # normalizes the raw gamma × OI × spot² values
SUPPORTED_ASSETS = ["BTC", "ETH"]

# ── Cache ────────────────────────────────────────────────────────────────────

_cache: dict[str, Any] = {
    "BTC": {"result": None, "last_fetch": None, "fetch_count": 0},
    "ETH": {"result": None, "last_fetch": None, "fetch_count": 0},
}

# ── DuckDB ────────────────────────────────────────────────────────────────────

_con: "duckdb.DuckDBPyConnection | None" = None


def get_db():
    global _con
    if not DUCKDB_AVAILABLE:
        return None
    if _con is None:
        _con = duckdb.connect(DUCKDB_PATH)
        _con.execute("""
            CREATE TABLE IF NOT EXISTS GEX_Snapshots (
                id BIGINT,
                asset VARCHAR,
                timestamp TIMESTAMP,
                spot_price DOUBLE,
                nearest_expiry TIMESTAMP,
                gamma_flip DOUBLE,
                total_call_gex DOUBLE,
                total_put_gex DOUBLE,
                net_gex DOUBLE,
                call_walls JSON,
                put_walls JSON,
                gex_by_strike JSON
            )
        """)
    return _con


# ── Black-Scholes Gamma ───────────────────────────────────────────────────────

def bs_gamma(spot: float, strike: float, t_years: float, r: float, sigma: float) -> float:
    """Black-Scholes gamma (second derivative of option price wrt spot)."""
    if t_years <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * t_years) / (sigma * math.sqrt(t_years))
    n_prime_d1 = math.exp(-d1 ** 2 / 2) / math.sqrt(2 * math.pi)
    return n_prime_d1 / (spot * sigma * math.sqrt(t_years))


# ── Deribit API ──────────────────────────────────────────────────────────────

def fetch_instruments(asset: str) -> list[dict]:
    """Fetch all active option instruments for an asset."""
    with httpx.Client(timeout=15) as client:
        resp = client.get(f"{DERIBIT_API}/public/get_instruments", params={
            "currency": asset, "kind": "option", "expired": False
        })
        resp.raise_for_status()
        return resp.json().get("result", [])


def fetch_book_summary(asset: str) -> dict[str, dict]:
    """Fetch book summary for all options (1 API call, includes OI + IV)."""
    with httpx.Client(timeout=15) as client:
        resp = client.get(f"{DERIBIT_API}/public/get_book_summary_by_currency", params={
            "currency": asset, "kind": "option"
        })
        resp.raise_for_status()
        results = resp.json().get("result", [])
        return {r["instrument_name"]: r for r in results}


def fetch_underlying(asset: str) -> float:
    """Get current spot price from perpetual ticker."""
    with httpx.Client(timeout=10) as client:
        resp = client.get(f"{DERIBIT_API}/public/ticker", params={
            "instrument_name": f"{asset}-PERPETUAL"
        })
        resp.raise_for_status()
        return float(resp.json().get("result", {}).get("index_price", 0))


# ── GEX Calculation ───────────────────────────────────────────────────────────

def compute_gex(asset: str, top_count: int = 10) -> dict:
    """
    Full GEX calculation pipeline:
    1. Fetch instruments (strikes, expiries, contract sizes)
    2. Fetch book summary (OI, mark_iv for all contracts — 1 call)
    3. Fetch underlying spot price
    4. For nearest expiry: compute BS gamma, aggregate GEX by strike
    5. Find top call walls, put walls, gamma flip
    """
    t0 = time.time()

    # 1. Fetch instruments
    instruments = fetch_instruments(asset)
    if not instruments:
        return {"error": f"No {asset} option instruments found"}

    now_ms = int(time.time() * 1000)

    # 2. Find nearest expiry
    expiries = set()
    for inst in instruments:
        ts = inst.get("expiration_timestamp", 0)
        if ts >= now_ms:
            expiries.add(ts)

    if not expiries:
        return {"error": "No future expiries found"}

    nearest_expiry_ms = min(expiries)
    nearest_expiry_dt = datetime.fromtimestamp(nearest_expiry_ms / 1000, tz=timezone.utc)

    # 3. Filter to nearest expiry
    expiry_contracts = [i for i in instruments if i.get("expiration_timestamp") == nearest_expiry_ms]

    # 4. Fetch book summary (all contracts in 1 call)
    book = fetch_book_summary(asset)
    spot = fetch_underlying(asset)

    if spot <= 0:
        return {"error": f"Could not fetch {asset} spot price"}

    # 5. Time to expiry in years
    t_years = (nearest_expiry_ms - now_ms) / (1000 * 365.25 * 24 * 3600)
    if t_years <= 0:
        # Move to next expiry
        future_expiries = sorted(e for e in expiries if e > now_ms + 3600000)  # at least 1hr out
        if not future_expiries:
            return {"error": "No valid future expiry found (all within 1 hour)"}
        nearest_expiry_ms = future_expiries[0]
        nearest_expiry_dt = datetime.fromtimestamp(nearest_expiry_ms / 1000, tz=timezone.utc)
        expiry_contracts = [i for i in instruments if i.get("expiration_timestamp") == nearest_expiry_ms]
        t_years = (nearest_expiry_ms - now_ms) / (1000 * 365.25 * 24 * 3600)

    # 6. Compute GEX per strike
    gex_by_strike: dict[float, float] = {}
    contracts_processed = 0

    for inst in expiry_contracts:
        name = inst.get("instrument_name", "")
        strike = float(inst.get("strike", 0))
        option_type = inst.get("option_type", "")
        contract_size = float(inst.get("contract_size", 1))

        # Get OI and IV from book summary
        book_data = book.get(name, {})
        oi = float(book_data.get("open_interest", 0) or 0)
        mark_iv = float(book_data.get("mark_iv", 0) or 0) / 100.0  # IV is in percentage

        if oi <= 0 or mark_iv <= 0:
            continue

        # Compute gamma via Black-Scholes
        r = 0.05  # risk-free rate (approx)
        gamma = bs_gamma(spot, strike, t_years, r, mark_iv)

        if gamma <= 0:
            continue

        # GEX = gamma × OI × contract_size × spot² × multiplier
        raw_gex = gamma * oi * contract_size * spot * spot * GEX_MULTIPLIER

        # Calls = positive, Puts = negative
        if option_type == "call":
            gex = raw_gex
        else:
            gex = -raw_gex

        # Aggregate by strike
        gex_by_strike[strike] = gex_by_strike.get(strike, 0.0) + gex
        contracts_processed += 1

    if not gex_by_strike:
        return {
            "error": "No GEX data computed — all contracts had zero OI or IV",
            "contracts_at_expiry": len(expiry_contracts),
            "contracts_processed": contracts_processed,
            "spot": spot,
        }

    # 7. Sort and find walls
    sorted_strikes = sorted(gex_by_strike.keys())

    # Call walls (highest positive GEX)
    call_walls = sorted(
        [(s, g) for s, g in gex_by_strike.items() if g > 0],
        key=lambda x: x[1], reverse=True
    )[:top_count]

    # Put walls (most negative GEX)
    put_walls = sorted(
        [(s, g) for s, g in gex_by_strike.items() if g < 0],
        key=lambda x: x[1]
    )[:top_count]

    # Gamma flip (strike with GEX closest to zero)
    flip_strike = min(gex_by_strike.items(), key=lambda x: abs(x[1]))

    # Aggregate stats
    total_call_gex = sum(g for g in gex_by_strike.values() if g > 0)
    total_put_gex = sum(g for g in gex_by_strike.values() if g < 0)
    net_gex = total_call_gex + total_put_gex

    elapsed = time.time() - t0

    result = {
        "asset": asset,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "spot_price": spot,
        "nearest_expiry": nearest_expiry_dt.isoformat(),
        "contracts_at_expiry": len(expiry_contracts),
        "contracts_processed": contracts_processed,
        "gamma_flip": {
            "strike": flip_strike[0],
            "gex": round(flip_strike[1], 2),
        },
        "call_walls": [
            {"strike": s, "gex": round(g, 2)} for s, g in call_walls
        ],
        "put_walls": [
            {"strike": s, "gex": round(g, 2)} for s, g in put_walls
        ],
        "total_call_gex": round(total_call_gex, 2),
        "total_put_gex": round(total_put_gex, 2),
        "net_gex": round(net_gex, 2),
        "gex_by_strike": {str(s): round(g, 2) for s, g in sorted(gex_by_strike.items())},
        "elapsed_ms": round(elapsed * 1000),
        "api_calls": 3,  # instruments + book_summary + underlying
    }

    # Update cache
    _cache[asset] = {
        "result": result,
        "last_fetch": datetime.now(timezone.utc).isoformat(),
        "fetch_count": _cache[asset]["fetch_count"] + 1,
    }

    # Store in DuckDB
    store_snapshot(asset, result)

    return result


def store_snapshot(asset: str, result: dict):
    """Store GEX snapshot in DuckDB."""
    con = get_db()
    if con is None:
        return

    import json
    try:
        con.execute("""
            INSERT INTO GEX_Snapshots VALUES (
                DEFAULT, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
        """, [
            asset,
            result["timestamp"],
            result["spot_price"],
            result["nearest_expiry"],
            result["gamma_flip"]["strike"],
            result["total_call_gex"],
            result["total_put_gex"],
            result["net_gex"],
            json.dumps(result["call_walls"]),
            json.dumps(result["put_walls"]),
            json.dumps(result["gex_by_strike"]),
        ])
    except Exception as e:
        log.debug(f"DuckDB store failed: {e}")


# ── CB v1 Signal Routing ──────────────────────────────────────────────────────

def route_gex_signal(asset: str, result: dict) -> bool:
    """Route significant GEX data as CB v1 envelope to signal router."""
    envelope = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "k9-gex-engine",
        "type": "gamma_exposure_snapshot",
        "ontology_tags": ["gex", "options", "deribit", asset.lower()],
        "confidence": 0.85,
        "payload": {
            "asset": asset,
            "spot": result["spot_price"],
            "gamma_flip": result["gamma_flip"]["strike"],
            "net_gex": result["net_gex"],
            "call_walls": result["call_walls"][:3],
            "put_walls": result["put_walls"][:3],
            "action": "monitor_gex_shift",
        },
        "trace_id": str(uuid.uuid4()),
        "causation_id": str(uuid.uuid4()),
        "signature": "",
    }

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                f"{AEG_SIGNAL_ROUTER_URL}/route",
                json=envelope,
                headers={"x-integration-key": K9_INTEGRATION_KEY} if K9_INTEGRATION_KEY else {},
            )
            return resp.status_code == 200
    except Exception as e:
        log.debug(f"Signal routing failed: {e}")
        return False


# ── FastAPI ───────────────────────────────────────────────────────────────────

app = FastAPI(
    title="K-9 GEX Engine",
    description="Gamma Exposure (GEX) calculation for BTC/ETH options on Deribit",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/gex/health")
async def health():
    return {
        "status": "online",
        "service": "k9-gex-engine",
        "port": 9008,
        "duckdb_available": DUCKDB_AVAILABLE,
        "supported_assets": SUPPORTED_ASSETS,
        "cache": {
            a: {
                "last_fetch": _cache[a]["last_fetch"],
                "fetch_count": _cache[a]["fetch_count"],
            } for a in SUPPORTED_ASSETS
        },
    }


@app.get("/gex/{asset}")
async def get_gex(
    asset: str,
    top: int = Query(10, ge=1, le=50),
    refresh: bool = Query(False, description="Force refresh from Deribit"),
):
    """Full GEX calculation for BTC or ETH."""
    asset = asset.upper()
    if asset not in SUPPORTED_ASSETS:
        raise HTTPException(400, f"Unsupported asset: {asset}. Supported: {SUPPORTED_ASSETS}")

    # Use cache if fresh (< 5 min) and not forced refresh
    cached = _cache[asset]
    if not refresh and cached["result"] and cached["last_fetch"]:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(cached["last_fetch"])).total_seconds()
        if age < 300:
            return cached["result"]

    result = compute_gex(asset, top)

    # Route signal
    if "error" not in result:
        route_gex_signal(asset, result)

    return result


@app.get("/gex/{asset}/walls")
async def get_walls(asset: str, top: int = Query(10, ge=1, le=50)):
    """Lightweight endpoint — just walls + flip (for Jellycuts/iOS)."""
    asset = asset.upper()
    if asset not in SUPPORTED_ASSETS:
        raise HTTPException(400, f"Unsupported asset: {asset}")

    cached = _cache[asset]
    if not cached["result"] or not cached["last_fetch"]:
        result = compute_gex(asset, top)
        if "error" in result:
            raise HTTPException(503, result["error"])
    else:
        result = cached["result"]

    # Lightweight response for mobile
    return {
        "asset": result["asset"],
        "spot": result["spot_price"],
        "gamma_flip": result["gamma_flip"]["strike"],
        "call_walls": [w["strike"] for w in result["call_walls"][:top]],
        "put_walls": [w["strike"] for w in result["put_walls"][:top]],
        "net_gex": result["net_gex"],
        "timestamp": result["timestamp"],
    }


@app.get("/gex/{asset}/flip")
async def get_flip(asset: str):
    """Gamma flip level only — ultra lightweight."""
    asset = asset.upper()
    if asset not in SUPPORTED_ASSETS:
        raise HTTPException(400, f"Unsupported asset: {asset}")

    cached = _cache[asset]
    if not cached["result"]:
        result = compute_gex(asset)
        if "error" in result:
            raise HTTPException(503, result["error"])
    else:
        result = cached["result"]

    return {
        "asset": result["asset"],
        "spot": result["spot_price"],
        "gamma_flip": result["gamma_flip"]["strike"],
        "net_gex": result["net_gex"],
    }


@app.get("/gex/{asset}/history")
async def get_history(asset: str, limit: int = Query(50, ge=1, le=500)):
    """Historical GEX snapshots from DuckDB."""
    asset = asset.upper()
    con = get_db()
    if con is None:
        return {"error": "DuckDB not available"}

    try:
        result = con.execute("""
            SELECT timestamp, spot_price, gamma_flip, total_call_gex, total_put_gex, net_gex
            FROM GEX_Snapshots
            WHERE asset = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """, [asset, limit]).fetchdf()
        return {"asset": asset, "snapshots": result.to_dict("records")}
    except Exception as e:
        return {"error": str(e)}


@app.post("/gex/refresh")
async def refresh(asset: str | None = None):
    """Manual trigger to refresh GEX data."""
    assets = [asset.upper()] if asset else SUPPORTED_ASSETS
    results = {}
    for a in assets:
        r = compute_gex(a)
        if "error" not in r:
            route_gex_signal(a, r)
        results[a] = {"success": "error" not in r, "data": r}
    return {"results": results, "timestamp": datetime.now(timezone.utc).isoformat()}


@app.on_event("startup")
async def startup():
    log.info("K-9 GEX Engine starting on :9008")
    get_db()
    log.info(f"Ready. DuckDB: {DUCKDB_PATH}")


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=9008)
