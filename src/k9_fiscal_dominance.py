from __future__ import annotations

import asyncio
import io
import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import numpy as np
import pandas as pd
import yfinance as yf
from fastapi import FastAPI, HTTPException

try:
    import duckdb
    DUCKDB_AVAILABLE = True
except ImportError:
    DUCKDB_AVAILABLE = False

# ── Logging Setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("k9.fiscal_dominance")

# ── Configuration & URLs ──────────────────────────────────────────────────────
AEG_SIGNAL_ROUTER_URL = os.getenv("AEG_SIGNAL_ROUTER_URL", "http://localhost:9004")
DUCKDB_PATH = os.getenv("FISCAL_DOMINANCE_DUCKDB", "k9_fiscal_dominance.duckdb")

TREASURY_AUCTION_PRIMARY_URL = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/debt/auction"
TREASURY_AUCTION_FALLBACK_URL = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query"

FRED_BASE_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

# Graceful fallback values in case of API outages/rate limits
FALLBACK_DATA = {
    "ten_year_yield": 4.69,
    "term_premium": 0.84,
    "net_interest_gdp": 3.84,
    "nominal_gdp_growth": 6.53,
    "treasury_b2c_ratio": 2.85,
    "gross_issuance_bn": 69.9,
    "move_proxy": 56.5,
    "kre_price": 74.86,
    "kre_52w_high": 78.35,
    "kre_drawdown": -4.45,
}

# ── 60-Second Cache ───────────────────────────────────────────────────────────
class FiscalCache:
    """In-memory cache with 60-second TTL."""
    def __init__(self, ttl_seconds: int = 60):
        self.ttl = ttl_seconds
        self.last_updated: float = 0
        self.data: dict[str, Any] | None = None
        self.lock = asyncio.Lock()

    def is_valid(self) -> bool:
        return self.data is not None and (time.time() - self.last_updated) < self.ttl

cache = FiscalCache(ttl_seconds=60)

# ── DuckDB Database Storage ───────────────────────────────────────────────────
def get_db_connection():
    """Get DuckDB connection and ensure FiscalDominance_Snapshots table exists."""
    if not DUCKDB_AVAILABLE:
        return None
    try:
        con = duckdb.connect(DUCKDB_PATH)
        con.execute("""
            CREATE TABLE IF NOT EXISTS FiscalDominance_Snapshots (
                timestamp TIMESTAMP,
                dominance_score DOUBLE,
                ten_year_yield DOUBLE,
                term_premium DOUBLE,
                net_interest_gdp DOUBLE,
                mortgage_rate DOUBLE,
                kre_drawdown DOUBLE
            )
        """)
        return con
    except Exception as e:
        log.error(f"DuckDB connection/init failed: {e}")
        return None

def save_snapshot(
    score: float,
    ten_year_yield: float,
    term_premium: float,
    net_interest_gdp: float,
    mortgage_rate: float,
    kre_drawdown: float
):
    """Save snapshot metrics to DuckDB table FiscalDominance_Snapshots."""
    con = get_db_connection()
    if con is None:
        return
    try:
        con.execute("""
            INSERT INTO FiscalDominance_Snapshots VALUES (
                CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?
            )
        """, [score, ten_year_yield, term_premium, net_interest_gdp, mortgage_rate, kre_drawdown])
        con.close()
        log.info(f"Saved snapshot to DuckDB: score={score}, 10Y={ten_year_yield}%")
    except Exception as e:
        log.error(f"Failed to insert snapshot into DuckDB: {e}")

# ── 1. Fiscal Dominance Calculator Class ──────────────────────────────────────
class FiscalDominanceCalculator:
    """
    Computes Fiscal Dominance Composite Score (0-100) for US Treasury market.
    Components:
      - Treasury supply/demand (20%)
      - Term premium (20%)
      - Net interest expense % of GDP (25%)
      - 10Y yield vs nominal GDP growth (20%)
      - Bond volatility (15%)
    """

    @staticmethod
    def calc_supply_demand_score(b2c_ratio: float, gross_issuance_bn: float) -> float:
        """
        Calculates supply/demand stress score (0-100).
        Lower auction bid-to-cover ratio (< 2.8) and heavy gross issuance (> $60B) increase stress.
        """
        # Baseline BTC: 2.85. Lower BTC -> higher stress
        b2c_stress = max(0.0, min(100.0, (2.85 - b2c_ratio) * 100.0))
        issuance_stress = max(0.0, min(20.0, (gross_issuance_bn - 60.0) * 0.5)) if gross_issuance_bn > 60.0 else 0.0
        score = b2c_stress + issuance_stress
        return round(max(0.0, min(100.0, score)), 2)

    @staticmethod
    def calc_term_premium_score(term_premium: float) -> float:
        """
        Calculates 10Y term premium stress score (0-100).
        Historical TP ranges from -0.5% to +2.0%. Higher term premium indicates rising fiscal risk.
        Linear scale: -0.2% -> 0, +0.8% -> 50, +1.8% -> 100.
        """
        score = (term_premium - (-0.2)) / 2.0 * 100.0
        return round(max(0.0, min(100.0, score)), 2)

    @staticmethod
    def calc_net_interest_gdp_score(net_int_gdp: float) -> float:
        """
        Calculates Net Interest Expense as % of GDP stress score (0-100).
        Historical baseline < 2.0%. 3.0%+ is high warning, 4.5%+ is severe fiscal dominance.
        Linear scale: 1.5% -> 0, 3.0% -> 50, 4.5% -> 100.
        """
        score = (net_int_gdp - 1.5) / 3.0 * 100.0
        return round(max(0.0, min(100.0, score)), 2)

    @staticmethod
    def calc_yield_vs_gdp_score(ten_year_yield: float, gdp_growth: float) -> float:
        """
        Calculates 10Y yield vs nominal GDP growth stress score (0-100).
        If yield > growth (r > g), debt dynamics are explosive.
        Spread = yield - growth. Linear scale: -3.0% -> 0, -0.5% -> 50, +2.0% -> 100.
        """
        spread = ten_year_yield - gdp_growth
        score = (spread - (-3.0)) / 5.0 * 100.0
        return round(max(0.0, min(100.0, score)), 2)

    @staticmethod
    def calc_bond_volatility_score(move_proxy: float) -> float:
        """
        Calculates bond volatility (MOVE index proxy) stress score (0-100).
        MOVE proxy: normal ~ 50-70, high > 100, extreme > 140.
        Linear scale: 50 -> 0, 95 -> 50, 140 -> 100.
        """
        score = (move_proxy - 50.0) / 90.0 * 100.0
        return round(max(0.0, min(100.0, score)), 2)

    @classmethod
    def compute(cls, raw_data: dict[str, Any]) -> dict[str, Any]:
        """Compute complete fiscal dominance metrics and composite score."""
        b2c = raw_data.get("treasury_b2c_ratio", 2.85)
        issuance = raw_data.get("gross_issuance_bn", 69.9)
        tp = raw_data.get("term_premium", 0.84)
        net_int = raw_data.get("net_interest_gdp", 3.84)
        y10 = raw_data.get("ten_year_yield", 4.69)
        gdp_g = raw_data.get("nominal_gdp_growth", 6.53)
        move = raw_data.get("move_proxy", 56.5)

        s_sd = cls.calc_supply_demand_score(b2c, issuance)
        s_tp = cls.calc_term_premium_score(tp)
        s_int = cls.calc_net_interest_gdp_score(net_int)
        s_yg = cls.calc_yield_vs_gdp_score(y10, gdp_g)
        s_vol = cls.calc_bond_volatility_score(move)

        composite = round(
            0.20 * s_sd + 0.20 * s_tp + 0.25 * s_int + 0.20 * s_yg + 0.15 * s_vol, 2
        )

        regime = "SUSTAINABLE"
        if composite >= 70.0:
            regime = "SEVERE_FISCAL_DOMINANCE"
        elif composite >= 55.0:
            regime = "ELEVATED_DOMINANCE"
        elif composite >= 35.0:
            regime = "MODERATE_RISK"

        return {
            "dominance_score": composite,
            "regime": regime,
            "components": {
                "supply_demand_score": s_sd,
                "term_premium_score": s_tp,
                "net_interest_gdp_score": s_int,
                "yield_vs_gdp_score": s_yg,
                "bond_volatility_score": s_vol,
            },
            "weights": {
                "supply_demand": 0.20,
                "term_premium": 0.20,
                "net_interest_gdp": 0.25,
                "yield_vs_gdp": 0.20,
                "bond_volatility": 0.15,
            },
        }

# ── 3. Mortgage Rate Model ────────────────────────────────────────────────────
def calc_mortgage_model(ten_year_yield: float, move_proxy: float = 56.5) -> dict[str, Any]:
    """
    Mortgage rate model:
      - 30Y fixed mortgage rate = 10Y Treasury yield + MBS OAS
      - MBS OAS starts at 150 bps (1.50%), widens to 180-200 bps when 10Y > 5%
      - Prepayment model: CPR drops to 0.02-0.05 when mortgage rate > 7%
      - Monthly payment calculator for $400k loan
    """
    if ten_year_yield > 5.0:
        extra_oas = min(50.0, (ten_year_yield - 5.0) * 100.0)
        mbs_oas_bps = 180.0 + (extra_oas * 0.4)
        mbs_oas_bps = min(200.0, max(180.0, mbs_oas_bps))
    else:
        mbs_oas_bps = 150.0 + max(0.0, (move_proxy - 80.0) * 0.5)
        mbs_oas_bps = min(179.0, max(150.0, mbs_oas_bps))

    mbs_oas_pct = mbs_oas_bps / 100.0
    mortgage_rate_30y = round(ten_year_yield + mbs_oas_pct, 2)

    # Prepayment CPR model
    if mortgage_rate_30y > 7.0:
        cpr = round(max(0.02, min(0.05, 0.05 - (mortgage_rate_30y - 7.0) * 0.02)), 3)
        cpr_status = "SEVERELY_DEPRESSED"
    elif mortgage_rate_30y > 6.0:
        cpr = 0.06
        cpr_status = "MODERATE"
    else:
        cpr = 0.12
        cpr_status = "NORMAL"

    loan_amount = 400000.0
    years = 30
    n = years * 12
    r = (mortgage_rate_30y / 100.0) / 12.0
    monthly_payment = round(loan_amount * (r * (1 + r) ** n) / ((1 + r) ** n - 1), 2)
    total_payment = monthly_payment * n
    total_interest = round(total_payment - loan_amount, 2)

    return {
        "ten_year_yield": ten_year_yield,
        "mbs_oas_bps": round(mbs_oas_bps, 1),
        "mbs_oas_pct": round(mbs_oas_pct, 4),
        "mortgage_rate_30y": mortgage_rate_30y,
        "prepayment_cpr": cpr,
        "prepayment_status": cpr_status,
        "loan_amount": loan_amount,
        "monthly_payment": monthly_payment,
        "total_interest_30y": total_interest,
        "oas_regime": "ELEVATED" if ten_year_yield > 5.0 else "STANDARD",
    }

# ── 4. Banking Stress Monitor ─────────────────────────────────────────────────
def calc_banking_stress(
    kre_price: float,
    kre_52w_high: float,
    ten_year_yield: float,
    baseline_yield: float = 3.50,
    portfolio_size_bn: float = 100.0,
    duration_gap_years: float = 3.0,
) -> dict[str, Any]:
    """
    Banking stress monitor:
      - Track KRE ETF drawdown from 52-week high
      - Unrealized loss proxy = duration gap (3 years) x yield change x securities portfolio size
      - Deposit beta proxy = comparison of bank deposit rates vs money market yields
    """
    drawdown_pct = (
        round(((kre_price - kre_52w_high) / kre_52w_high) * 100.0, 2)
        if kre_52w_high > 0
        else 0.0
    )

    yield_change_pct = max(0.0, ten_year_yield - baseline_yield)
    unrealized_loss_bn = round(
        duration_gap_years * (yield_change_pct / 100.0) * portfolio_size_bn, 2
    )

    money_market_yield = ten_year_yield
    est_bank_deposit_rate = round(money_market_yield * 0.35, 2)
    deposit_gap = round(money_market_yield - est_bank_deposit_rate, 2)
    deposit_beta = (
        round(est_bank_deposit_rate / money_market_yield, 2)
        if money_market_yield > 0
        else 0.0
    )

    if drawdown_pct < -20.0 or unrealized_loss_bn > 6.0:
        stress_level = "CRITICAL"
    elif drawdown_pct < -10.0 or unrealized_loss_bn > 4.5:
        stress_level = "HIGH"
    elif drawdown_pct < -5.0 or unrealized_loss_bn > 3.0:
        stress_level = "MODERATE"
    else:
        stress_level = "LOW"

    return {
        "kre_price": kre_price,
        "kre_52w_high": kre_52w_high,
        "kre_drawdown_pct": drawdown_pct,
        "unrealized_loss_proxy_bn": unrealized_loss_bn,
        "unrealized_loss_details": {
            "duration_gap_years": duration_gap_years,
            "yield_change_pct": round(yield_change_pct, 2),
            "baseline_yield_pct": baseline_yield,
            "securities_portfolio_bn": portfolio_size_bn,
        },
        "deposit_beta_proxy": {
            "est_bank_deposit_rate": est_bank_deposit_rate,
            "money_market_yield": money_market_yield,
            "deposit_gap_pct": deposit_gap,
            "estimated_deposit_beta": deposit_beta,
        },
        "banking_stress_level": stress_level,
    }

# ── 5. Stress Test Scenarios ──────────────────────────────────────────────────
def calc_stress_scenarios(raw_data: dict[str, Any]) -> dict[str, Any]:
    """
    Stress test scenarios for 10Y at 5.0%, 5.25%, 5.5%, 6.0%.
    Calculates mortgage rate, monthly payment impact, banking stress, and fiscal dominance score.
    """
    base_10y = raw_data.get("ten_year_yield", 4.69)
    base_mortgage_info = calc_mortgage_model(base_10y, raw_data.get("move_proxy", 56.5))
    base_payment = base_mortgage_info["monthly_payment"]
    base_score_info = FiscalDominanceCalculator.compute(raw_data)

    target_yields = [5.00, 5.25, 5.50, 6.00]
    scenarios = []

    for y_target in target_yields:
        if y_target == 5.00:
            oas_bps = 180.0
        elif y_target == 5.25:
            oas_bps = 185.0
        elif y_target == 5.50:
            oas_bps = 190.0
        else:
            oas_bps = 200.0

        mortgage_rate = round(y_target + (oas_bps / 100.0), 2)
        cpr = round(0.03 if mortgage_rate > 7.0 else 0.05, 3)

        r = (mortgage_rate / 100.0) / 12.0
        n = 360
        payment = round(400000.0 * (r * (1 + r) ** n) / ((1 + r) ** n - 1), 2)
        payment_increase = round(payment - base_payment, 2)

        banking_info = calc_banking_stress(
            kre_price=raw_data.get("kre_price", 74.86),
            kre_52w_high=raw_data.get("kre_52w_high", 78.35),
            ten_year_yield=y_target,
            baseline_yield=3.50,
            portfolio_size_bn=100.0,
            duration_gap_years=3.0,
        )

        scen_raw_data = dict(raw_data)
        scen_raw_data["ten_year_yield"] = y_target
        yield_delta = y_target - base_10y
        scen_raw_data["net_interest_gdp"] = round(
            raw_data.get("net_interest_gdp", 3.84) + yield_delta * 0.25, 2
        )
        scen_raw_data["move_proxy"] = round(
            raw_data.get("move_proxy", 56.5) + yield_delta * 15.0, 1
        )

        scen_score_info = FiscalDominanceCalculator.compute(scen_raw_data)

        scenarios.append({
            "scenario_name": f"10Y @ {y_target:.2f}%",
            "ten_year_yield": y_target,
            "mbs_oas_bps": oas_bps,
            "mortgage_rate_30y": mortgage_rate,
            "prepayment_cpr": cpr,
            "monthly_payment_400k": payment,
            "monthly_payment_increase": payment_increase,
            "unrealized_loss_proxy_bn": banking_info["unrealized_loss_proxy_bn"],
            "banking_stress_level": banking_info["banking_stress_level"],
            "projected_dominance_score": scen_score_info["dominance_score"],
            "projected_regime": scen_score_info["regime"],
        })

    return {
        "baseline": {
            "ten_year_yield": base_10y,
            "mortgage_rate_30y": base_mortgage_info["mortgage_rate_30y"],
            "monthly_payment_400k": base_payment,
            "dominance_score": base_score_info["dominance_score"],
            "regime": base_score_info["regime"],
        },
        "scenarios": scenarios,
    }

# ── 6. CB v1 Signal Routing ───────────────────────────────────────────────────
def make_cb1_envelope(payload: dict, confidence: float = 0.85) -> dict:
    """Build K9-CB v1 envelope structure."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "k9-fiscal-dominance",
        "type": "fiscal_dominance_alert",
        "ontology_tags": ["fiscal", "macro", "treasury", "rates", "banking"],
        "confidence": confidence,
        "payload": payload,
        "trace_id": str(uuid.uuid4()),
        "causation_id": str(uuid.uuid4()),
        "signature": "",
    }

async def route_cb1_signal(envelope: dict) -> bool:
    """Route CB v1 signal to aeg-signal-router :9004."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{AEG_SIGNAL_ROUTER_URL}/route",
                json=envelope,
            )
            if resp.status_code == 200:
                log.info("CB v1 signal successfully routed to aeg-signal-router :9004")
                return True
            else:
                log.warning(f"CB v1 signal router returned status code {resp.status_code}")
                return False
    except Exception as e:
        log.debug(f"CB v1 signal routing failed (router offline): {e}")
        return False

# ── Data Ingestion & Async Fetchers ───────────────────────────────────────────
async def fetch_treasury_auction_data(client: httpx.AsyncClient) -> tuple[float, float]:
    """Fetch bid-to-cover and gross issuance volume from Treasury.gov API."""
    urls = [TREASURY_AUCTION_PRIMARY_URL, TREASURY_AUCTION_FALLBACK_URL]
    for url in urls:
        try:
            resp = await client.get(url, params={"page[size]": 30, "sort": "-record_date"})
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                b2c_list = []
                issuance_list = []
                for item in data:
                    b2c = item.get("bid_to_cover_ratio")
                    offering = (
                        item.get("offering_amt")
                        or item.get("total_accepted")
                        or item.get("issue_amt")
                    )
                    if b2c:
                        try:
                            b2c_list.append(float(b2c))
                        except Exception:
                            pass
                    if offering:
                        try:
                            issuance_list.append(float(offering))
                        except Exception:
                            pass

                avg_b2c = (
                    round(sum(b2c_list) / len(b2c_list), 2)
                    if b2c_list
                    else FALLBACK_DATA["treasury_b2c_ratio"]
                )
                avg_issuance_bn = (
                    round((sum(issuance_list) / len(issuance_list)) / 1e9, 1)
                    if issuance_list
                    else FALLBACK_DATA["gross_issuance_bn"]
                )
                log.info(
                    f"Treasury API success ({url}): B2C={avg_b2c}, Issuance=${avg_issuance_bn}B"
                )
                return avg_b2c, avg_issuance_bn
        except Exception as e:
            log.warning(f"Treasury API fetch failed for {url}: {e}")

    return FALLBACK_DATA["treasury_b2c_ratio"], FALLBACK_DATA["gross_issuance_bn"]

async def fetch_fred_series(client: httpx.AsyncClient, series_id: str) -> pd.DataFrame | None:
    """Fetch CSV data from FRED for a given series_id."""
    url = f"{FRED_BASE_URL}?id={series_id}"
    try:
        resp = await client.get(url, timeout=10.0)
        if resp.status_code == 200:
            df = pd.read_csv(io.StringIO(resp.text))
            df[series_id] = pd.to_numeric(df[series_id], errors="coerce")
            return df
    except Exception as e:
        log.warning(f"FRED fetch failed for {series_id}: {e}")
    return None

def fetch_yfinance_data() -> tuple[float, float, float, float]:
    """
    Fetch KRE (price, 52w high, drawdown) and TLT return volatility via yfinance.
    Returns: (kre_price, kre_52w_high, kre_drawdown, move_proxy)
    """
    try:
        kre = yf.Ticker("KRE").history(period="1y")
        if not kre.empty:
            kre_price = float(kre["Close"].iloc[-1])
            kre_52w_high = float(kre["High"].max())
            kre_drawdown = round(((kre_price - kre_52w_high) / kre_52w_high) * 100.0, 2)
        else:
            kre_price = FALLBACK_DATA["kre_price"]
            kre_52w_high = FALLBACK_DATA["kre_52w_high"]
            kre_drawdown = FALLBACK_DATA["kre_drawdown"]

        tlt = yf.Ticker("TLT").history(period="1y")
        if not tlt.empty and len(tlt) > 10:
            returns = tlt["Close"].pct_change().dropna()
            ann_vol = float(returns.std() * np.sqrt(252) * 100.0)
            move_proxy = round(ann_vol * 6.0, 1)
        else:
            move_proxy = FALLBACK_DATA["move_proxy"]

        log.info(
            f"yfinance fetched: KRE=${kre_price:.2f} ({kre_drawdown}%), MOVE proxy={move_proxy}"
        )
        return kre_price, kre_52w_high, kre_drawdown, move_proxy
    except Exception as e:
        log.warning(f"yfinance fetch failed: {e}")
        return (
            FALLBACK_DATA["kre_price"],
            FALLBACK_DATA["kre_52w_high"],
            FALLBACK_DATA["kre_drawdown"],
            FALLBACK_DATA["move_proxy"],
        )

# ── Core Data Engine Refresh ──────────────────────────────────────────────────
async def update_fiscal_data(force: bool = False) -> dict[str, Any]:
    """
    Fetch latest metrics from FRED, Treasury.gov API, and yfinance,
    compute Fiscal Dominance score and component models, update cache and DuckDB.
    """
    async with cache.lock:
        if not force and cache.is_valid():
            return cache.data

        log.info("Refreshing Fiscal Dominance data from FRED, yfinance, and Treasury API...")

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            b2c_ratio, gross_issuance_bn = await fetch_treasury_auction_data(client)

            tp_df = await fetch_fred_series(client, "THREEFYTP10")
            int_df = await fetch_fred_series(client, "A091RC1Q027SBEA")
            gdp_df = await fetch_fred_series(client, "GDP")
            dgs10_df = await fetch_fred_series(client, "DGS10")

        if tp_df is not None and not tp_df["THREEFYTP10"].dropna().empty:
            term_premium = round(float(tp_df["THREEFYTP10"].dropna().iloc[-1]), 2)
        else:
            term_premium = FALLBACK_DATA["term_premium"]

        if int_df is not None and gdp_df is not None:
            int_clean = int_df["A091RC1Q027SBEA"].dropna()
            gdp_clean = gdp_df["GDP"].dropna()
            if not int_clean.empty and not gdp_clean.empty:
                last_int = float(int_clean.iloc[-1])
                last_gdp = float(gdp_clean.iloc[-1])
                net_interest_gdp = round((last_int / last_gdp) * 100.0, 2)

                prev_gdp = (
                    float(gdp_clean.iloc[-5])
                    if len(gdp_clean) >= 5
                    else float(gdp_clean.iloc[0])
                )
                nominal_gdp_growth = round(((last_gdp - prev_gdp) / prev_gdp) * 100.0, 2)
            else:
                net_interest_gdp = FALLBACK_DATA["net_interest_gdp"]
                nominal_gdp_growth = FALLBACK_DATA["nominal_gdp_growth"]
        else:
            net_interest_gdp = FALLBACK_DATA["net_interest_gdp"]
            nominal_gdp_growth = FALLBACK_DATA["nominal_gdp_growth"]

        if dgs10_df is not None and not dgs10_df["DGS10"].dropna().empty:
            ten_year_yield = round(float(dgs10_df["DGS10"].dropna().iloc[-1]), 2)
        else:
            ten_year_yield = FALLBACK_DATA["ten_year_yield"]

        loop = asyncio.get_running_loop()
        kre_price, kre_52w_high, kre_drawdown, move_proxy = await loop.run_in_executor(
            None, fetch_yfinance_data
        )

        raw_data = {
            "ten_year_yield": ten_year_yield,
            "term_premium": term_premium,
            "net_interest_gdp": net_interest_gdp,
            "nominal_gdp_growth": nominal_gdp_growth,
            "treasury_b2c_ratio": b2c_ratio,
            "gross_issuance_bn": gross_issuance_bn,
            "move_proxy": move_proxy,
            "kre_price": kre_price,
            "kre_52w_high": kre_52w_high,
            "kre_drawdown": kre_drawdown,
            "yield_vs_gdp_spread": round(ten_year_yield - nominal_gdp_growth, 2),
        }

        score_data = FiscalDominanceCalculator.compute(raw_data)
        mortgage_data = calc_mortgage_model(ten_year_yield, move_proxy)
        banking_data = calc_banking_stress(kre_price, kre_52w_high, ten_year_yield)
        stress_data = calc_stress_scenarios(raw_data)

        composite_score = score_data["dominance_score"]
        signal_routed = False
        if composite_score > 70.0:
            log.warning(
                f"Fiscal Dominance Score {composite_score} > 70! Routing CB v1 signal..."
            )
            envelope = make_cb1_envelope({
                "score": composite_score,
                "regime": score_data["regime"],
                "ten_year_yield": ten_year_yield,
                "term_premium": term_premium,
                "net_interest_gdp": net_interest_gdp,
                "mortgage_rate": mortgage_data["mortgage_rate_30y"],
                "kre_drawdown": kre_drawdown,
                "components": score_data["components"],
                "alert": f"Fiscal Dominance Score {composite_score:.1f} exceeds threshold 70",
            })
            signal_routed = await route_cb1_signal(envelope)

        save_snapshot(
            score=composite_score,
            ten_year_yield=ten_year_yield,
            term_premium=term_premium,
            net_interest_gdp=net_interest_gdp,
            mortgage_rate=mortgage_data["mortgage_rate_30y"],
            kre_drawdown=kre_drawdown,
        )

        full_result = {
            "dominance_score": composite_score,
            "regime": score_data["regime"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metrics": raw_data,
            "components": score_data["components"],
            "weights": score_data["weights"],
            "mortgage": mortgage_data,
            "banking": banking_data,
            "stress_test": stress_data,
            "signal_routed": signal_routed,
            "cached_at": time.time(),
        }

        cache.data = full_result
        cache.last_updated = time.time()
        return full_result

# ── 2. FastAPI Endpoints ──────────────────────────────────────────────────────
app = FastAPI(
    title="K9 Fiscal Dominance Engine",
    description="Computes Fiscal Dominance Score, Treasury metrics, mortgage projections, and regional banking stress.",
    version="1.0.0",
)

@app.on_event("startup")
async def startup_event():
    log.info("K9 Fiscal Dominance Engine starting on port 9010...")
    asyncio.create_task(update_fiscal_data(force=True))

@app.get("/fiscal/health")
async def get_health():
    """Service liveness & status check."""
    return {
        "status": "ok",
        "service": "k9-fiscal-dominance",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duckdb_available": DUCKDB_AVAILABLE,
        "cache_valid": cache.is_valid(),
        "last_updated_seconds_ago": (
            round(time.time() - cache.last_updated, 1)
            if cache.last_updated > 0
            else None
        ),
    }

@app.get("/fiscal/score")
async def get_fiscal_score():
    """Full fiscal dominance score with component breakdown."""
    data = await update_fiscal_data()
    return {
        "dominance_score": data["dominance_score"],
        "regime": data["regime"],
        "timestamp": data["timestamp"],
        "metrics": data["metrics"],
        "components": data["components"],
        "signal_routed": data["signal_routed"],
    }

@app.get("/fiscal/score/components")
async def get_score_components():
    """Just the component scores."""
    data = await update_fiscal_data()
    return {
        **data["components"],
        "composite_score": data["dominance_score"],
        "regime": data["regime"],
    }

@app.get("/fiscal/stress")
async def get_fiscal_stress():
    """Stress test scenarios for 10Y yield at 5.0%, 5.25%, 5.5%, 6.0%."""
    data = await update_fiscal_data()
    return data["stress_test"]

@app.get("/fiscal/mortgage")
async def get_fiscal_mortgage():
    """Mortgage rate projections (30Y fixed = 10Y + MBS OAS, prepayment CPR, monthly payment calculator)."""
    data = await update_fiscal_data()
    return data["mortgage"]

@app.get("/fiscal/banking")
async def get_fiscal_banking():
    """Banking stress indicators (KRE ETF drawdown, deposit beta proxy, unrealized loss proxy)."""
    data = await update_fiscal_data()
    return data["banking"]

@app.post("/fiscal/refresh")
async def refresh_fiscal_data():
    """Manual trigger to refresh data, re-evaluate score, store snapshot, and route signal if score > 70."""
    data = await update_fiscal_data(force=True)
    return {
        "status": "refreshed",
        "timestamp": data["timestamp"],
        "dominance_score": data["dominance_score"],
        "regime": data["regime"],
        "signal_routed": data["signal_routed"],
        "metrics": data["metrics"],
    }
