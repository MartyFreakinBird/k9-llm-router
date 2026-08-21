"""
k9_options_ingestion.py — K-9 Options Data Ingestion Engine
─────────────────────────────────────────────────────────────────────────────
Ingests historical options data from DoltHub and Kaggle into DuckDB.
Computes vectorized Black-Scholes implied volatilities and greeks using NumPy.
Exposes FastAPI endpoints for health, ingestion triggers, IV percentile, VRP,
and volatility surfaces.

Runnable via:
    uvicorn src.k9_options_ingestion:app --host 0.0.0.0 --port 9011
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import datetime
import glob
import logging
import os
import shutil
import subprocess
import sys
from io import StringIO
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import fastapi
from fastapi import FastAPI, HTTPException, Query
import httpx
import numpy as np
import pandas as pd
from scipy.stats import norm

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("k9_options_ingestion")

# Configuration via Env Vars
DOLT_REPO_PATH = os.getenv("DOLT_REPO_PATH", "./dolt_options")
KAGGLE_SPY_PATH = os.getenv("KAGGLE_SPY_PATH", "./kaggle_spy")
DUCKDB_PATH = os.getenv("DUCKDB_PATH", "fomc_cockpit.duckdb")
DOLT_SYMBOLS_ENV = os.getenv("DOLT_SYMBOLS", "SPY,QQQ,IWM")
DOLT_SYMBOLS = [s.strip().upper() for s in DOLT_SYMBOLS_ENV.split(",") if s.strip()]

# FastAPI App
app = FastAPI(
    title="K-9 Options Ingestion Service",
    description="Historical options data ingestion, vectorized Black-Scholes IV/Greeks, DuckDB views & FastAPI endpoints",
    version="1.0.0",
)


# ─────────────────────────────────────────────────────────────────────────────
# Vectorized Black-Scholes & Greeks Engine
# ─────────────────────────────────────────────────────────────────────────────


def bs_price_vec(
    S: np.ndarray,
    K: np.ndarray,
    T: np.ndarray,
    r: float | np.ndarray,
    sigma: np.ndarray,
    is_call: np.ndarray,
) -> np.ndarray:
    """Vectorized Black-Scholes option price calculation."""
    S = np.asarray(S, dtype=float)
    K = np.asarray(K, dtype=float)
    T = np.asarray(T, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    is_call = np.asarray(is_call, dtype=bool)

    price = np.zeros_like(S, dtype=float)
    exp_rT = np.exp(-r * T)

    intrinsic_call = np.maximum(0.0, S - K * exp_rT)
    intrinsic_put = np.maximum(0.0, K * exp_rT - S)

    valid = (T > 0) & (sigma > 0) & (S > 0) & (K > 0)

    if np.any(valid):
        S_v = S[valid]
        K_v = K[valid]
        T_v = T[valid]
        sig_v = sigma[valid]
        call_v = is_call[valid]
        sqrt_T = np.sqrt(T_v)

        d1 = (np.log(S_v / K_v) + (r + 0.5 * sig_v**2) * T_v) / (sig_v * sqrt_T)
        d2 = d1 - sig_v * sqrt_T

        c_price = S_v * norm.cdf(d1) - K_v * exp_rT[valid] * norm.cdf(d2)
        p_price = K_v * exp_rT[valid] * norm.cdf(-d2) - S_v * norm.cdf(-d1)

        price[valid] = np.where(call_v, c_price, p_price)

    price[~valid] = np.where(is_call[~valid], intrinsic_call[~valid], intrinsic_put[~valid])
    return price


def bs_greeks_vec(
    S: np.ndarray,
    K: np.ndarray,
    T: np.ndarray,
    r: float,
    sigma: np.ndarray,
    is_call: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Vectorized Black-Scholes Greeks calculation:
    returns (delta, gamma, vega, theta, rho).
    Handles edge cases: T <= 0, zero volume, zero bid, deep ITM/OTM.
    """
    S = np.asarray(S, dtype=float)
    K = np.asarray(K, dtype=float)
    T = np.asarray(T, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    is_call = np.asarray(is_call, dtype=bool)

    n = len(S)
    delta = np.zeros(n, dtype=float)
    gamma = np.zeros(n, dtype=float)
    vega = np.zeros(n, dtype=float)
    theta = np.zeros(n, dtype=float)
    rho = np.zeros(n, dtype=float)

    valid = (T > 0) & (sigma > 0) & (S > 0) & (K > 0) & (~np.isnan(sigma))

    if np.any(valid):
        S_v = S[valid]
        K_v = K[valid]
        T_v = T[valid]
        sig_v = sigma[valid]
        call_v = is_call[valid]
        sqrt_T = np.sqrt(T_v)

        d1 = (np.log(S_v / K_v) + (r + 0.5 * sig_v**2) * T_v) / (sig_v * sqrt_T)
        d2 = d1 - sig_v * sqrt_T

        pdf_d1 = norm.pdf(d1)
        cdf_d1 = norm.cdf(d1)
        cdf_d2 = norm.cdf(d2)
        cdf_neg_d2 = norm.cdf(-d2)

        # Delta
        delta[valid] = np.where(call_v, cdf_d1, cdf_d1 - 1.0)

        # Gamma
        denom_gamma = S_v * sig_v * sqrt_T
        gamma[valid] = np.where(denom_gamma > 1e-12, pdf_d1 / denom_gamma, 0.0)

        # Vega
        vega[valid] = S_v * pdf_d1 * sqrt_T

        # Theta (per year)
        term1 = -(S_v * pdf_d1 * sig_v) / (2.0 * sqrt_T)
        theta_call = term1 - r * K_v * np.exp(-r * T_v) * cdf_d2
        theta_put = term1 + r * K_v * np.exp(-r * T_v) * cdf_neg_d2
        theta[valid] = np.where(call_v, theta_call, theta_put)

        # Rho
        rho_call = K_v * T_v * np.exp(-r * T_v) * cdf_d2
        rho_put = -K_v * T_v * np.exp(-r * T_v) * cdf_neg_d2
        rho[valid] = np.where(call_v, rho_call, rho_put)

    # Edge cases for T <= 0
    expired = T <= 0
    if np.any(expired):
        delta[expired] = np.where(
            is_call[expired],
            np.where(S[expired] > K[expired], 1.0, 0.0),
            np.where(S[expired] < K[expired], -1.0, 0.0),
        )

    return delta, gamma, vega, theta, rho


def solve_iv_nr_vec(
    S: np.ndarray,
    K: np.ndarray,
    T: np.ndarray,
    r: float,
    market_price: np.ndarray,
    is_call: np.ndarray,
    max_iter: int = 50,
    tol: float = 1e-5,
) -> np.ndarray:
    """
    Vectorized Newton-Raphson Implied Volatility Solver.
    Handles edge cases: zero bid, zero market price, deep ITM/OTM, T <= 0.
    """
    S = np.asarray(S, dtype=float)
    K = np.asarray(K, dtype=float)
    T = np.asarray(T, dtype=float)
    market_price = np.asarray(market_price, dtype=float)
    is_call = np.asarray(is_call, dtype=bool)

    iv = np.full_like(S, np.nan, dtype=float)

    exp_rT = np.exp(-r * T)
    intrinsic = np.where(
        is_call,
        np.maximum(0.0, S - K * exp_rT),
        np.maximum(0.0, K * exp_rT - S),
    )

    valid = (
        (T > 0)
        & (market_price > 0)
        & (market_price > intrinsic + 1e-4)
        & (S > 0)
        & (K > 0)
        & (~np.isnan(market_price))
    )

    if not np.any(valid):
        return iv

    S_v = S[valid]
    K_v = K[valid]
    T_v = T[valid]
    m_v = market_price[valid]
    call_v = is_call[valid]
    exp_rT_v = exp_rT[valid]

    sigma = np.full(np.sum(valid), 0.25, dtype=float)

    for _ in range(max_iter):
        sqrt_T = np.sqrt(T_v)
        d1 = (np.log(S_v / K_v) + (r + 0.5 * sigma**2) * T_v) / (sigma * sqrt_T)
        d2 = d1 - sigma * sqrt_T

        c_price = S_v * norm.cdf(d1) - K_v * exp_rT_v * norm.cdf(d2)
        p_price = K_v * exp_rT_v * norm.cdf(-d2) - S_v * norm.cdf(-d1)
        price = np.where(call_v, c_price, p_price)

        vega = S_v * norm.pdf(d1) * sqrt_T
        diff = price - m_v

        if np.all(np.abs(diff) < tol):
            break

        vega_adj = np.where(vega < 1e-8, 1e-8, vega)
        sigma = sigma - diff / vega_adj
        sigma = np.clip(sigma, 0.001, 5.0)

    iv[valid] = sigma
    return iv


# ─────────────────────────────────────────────────────────────────────────────
# Data Processing Helper
# ─────────────────────────────────────────────────────────────────────────────


def process_options_df(df: pd.DataFrame, r: float = 0.05) -> pd.DataFrame:
    """Normalizes option dataframe, computes mid price, time to expiry, IV and Greeks."""
    if df.empty:
        return pd.DataFrame(columns=[
            "date", "symbol", "expiry", "strike", "option_type",
            "bid", "ask", "mid", "volume", "open_interest",
            "underlying_price", "implied_vol", "delta", "gamma", "vega", "theta", "rho"
        ])

    df = df.copy()
    df.columns = [c.lower().strip() for c in df.columns]

    col_mapping = {
        "act_symbol": "symbol",
        "ticker": "symbol",
        "expiration": "expiry",
        "exp_date": "expiry",
        "call_put": "option_type",
        "type": "option_type",
        "vol": "volume",
        "oi": "open_interest",
        "openint": "open_interest",
        "underlying": "underlying_price",
        "spot": "underlying_price",
        "spot_price": "underlying_price",
        "iv": "implied_vol",
    }
    df = df.rename(columns=col_mapping)

    required_cols = [
        "date", "symbol", "expiry", "strike", "option_type",
        "bid", "ask", "volume", "open_interest", "underlying_price"
    ]
    for col in required_cols:
        if col not in df.columns:
            if col in ["bid", "ask", "volume", "open_interest"]:
                df[col] = 0.0
            elif col == "underlying_price":
                df[col] = df["strike"] if "strike" in df.columns else 100.0
            else:
                df[col] = None

    # Cast dates
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["expiry"] = pd.to_datetime(df["expiry"]).dt.date

    # Cast numeric
    for col in ["strike", "bid", "ask", "volume", "open_interest", "underlying_price"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    # Compute mid price
    if "mid" not in df.columns or df["mid"].isnull().all():
        df["mid"] = np.where((df["bid"] > 0) & (df["ask"] > 0), (df["bid"] + df["ask"]) / 2.0, np.maximum(df["bid"], df["ask"]))
    else:
        df["mid"] = pd.to_numeric(df["mid"], errors="coerce").fillna(0.0)

    # Normalize option_type
    df["option_type"] = df["option_type"].astype(str).str.upper().str.strip()
    is_call = df["option_type"].isin(["C", "CALL", "1"])
    df["option_type"] = np.where(is_call, "C", "P")

    # Calculate time to expiry in years
    days_to_exp = [(exp - d).days if (exp and d) else 0 for d, exp in zip(df["date"], df["expiry"])]
    T = np.maximum(0.0, np.array(days_to_exp, dtype=float) / 365.25)

    S = df["underlying_price"].values
    K = df["strike"].values
    market_price = df["mid"].values
    is_call_arr = (df["option_type"].values == "C")

    # Compute IV if missing
    if "implied_vol" not in df.columns or df["implied_vol"].isnull().all():
        iv = solve_iv_nr_vec(S, K, T, r, market_price, is_call_arr)
        df["implied_vol"] = iv
    else:
        df["implied_vol"] = pd.to_numeric(df["implied_vol"], errors="coerce")
        missing_iv = df["implied_vol"].isna() | (df["implied_vol"] <= 0)
        if missing_iv.any():
            solved = solve_iv_nr_vec(S, K, T, r, market_price, is_call_arr)
            df["implied_vol"] = np.where(missing_iv, solved, df["implied_vol"])

    # Compute Greeks
    iv_vals = df["implied_vol"].fillna(0.0).values
    delta, gamma, vega, theta, rho = bs_greeks_vec(S, K, T, r, iv_vals, is_call_arr)

    df["delta"] = delta
    df["gamma"] = gamma
    df["vega"] = vega
    df["theta"] = theta
    df["rho"] = rho

    df["volume"] = df["volume"].astype(int)
    df["open_interest"] = df["open_interest"].astype(int)

    out_cols = [
        "date", "symbol", "expiry", "strike", "option_type",
        "bid", "ask", "mid", "volume", "open_interest",
        "underlying_price", "implied_vol", "delta", "gamma", "vega", "theta", "rho"
    ]
    return df[out_cols]


# ─────────────────────────────────────────────────────────────────────────────
# DuckDB Schema & Views Initialization
# ─────────────────────────────────────────────────────────────────────────────


def get_db_connection(db_path: str = DUCKDB_PATH) -> duckdb.DuckDBPyConnection:
    """Connect to DuckDB database."""
    return duckdb.connect(db_path)


def init_duckdb_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Initialize Fact_Options and Fact_Options_Validation tables and views."""
    conn.execute("""
    CREATE TABLE IF NOT EXISTS Fact_Options (
        date DATE,
        symbol VARCHAR,
        expiry DATE,
        strike DOUBLE,
        option_type VARCHAR,
        bid DOUBLE,
        ask DOUBLE,
        mid DOUBLE,
        volume BIGINT,
        open_interest BIGINT,
        underlying_price DOUBLE,
        implied_vol DOUBLE,
        delta DOUBLE,
        gamma DOUBLE,
        vega DOUBLE,
        theta DOUBLE,
        rho DOUBLE
    );
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS Fact_Options_Validation (
        date DATE,
        symbol VARCHAR,
        expiry DATE,
        strike DOUBLE,
        option_type VARCHAR,
        bid DOUBLE,
        ask DOUBLE,
        mid DOUBLE,
        volume BIGINT,
        open_interest BIGINT,
        underlying_price DOUBLE,
        implied_vol DOUBLE,
        delta DOUBLE,
        gamma DOUBLE,
        vega DOUBLE,
        theta DOUBLE,
        rho DOUBLE
    );
    """)

    # Views
    conn.execute("""
    CREATE OR REPLACE VIEW v_iv_percentile AS
    WITH recent_options AS (
        SELECT * FROM Fact_Options
        WHERE date >= (SELECT COALESCE(MAX(date) - INTERVAL '3 years', '1970-01-01'::DATE) FROM Fact_Options)
    )
    SELECT
        date,
        symbol,
        expiry,
        strike,
        option_type,
        implied_vol,
        PERCENT_RANK() OVER (
            PARTITION BY symbol, option_type 
            ORDER BY implied_vol
        ) AS iv_percentile
    FROM recent_options
    WHERE implied_vol IS NOT NULL AND implied_vol > 0;
    """)

    conn.execute("""
    CREATE OR REPLACE VIEW v_variance_risk_premium AS
    WITH daily_underlying AS (
        SELECT 
            date,
            symbol,
            AVG(underlying_price) AS underlying_price,
            AVG(implied_vol) AS avg_iv
        FROM Fact_Options
        WHERE implied_vol IS NOT NULL AND implied_vol > 0
        GROUP BY date, symbol
    ),
    daily_returns AS (
        SELECT
            date,
            symbol,
            underlying_price,
            avg_iv,
            LN(underlying_price / NULLIF(LAG(underlying_price) OVER (PARTITION BY symbol ORDER BY date), 0)) AS log_return
        FROM daily_underlying
    ),
    realized_vol AS (
        SELECT
            date,
            symbol,
            avg_iv,
            STDDEV_SAMP(log_return) OVER (
                PARTITION BY symbol 
                ORDER BY date 
                ROWS BETWEEN 1 FOLLOWING AND 21 FOLLOWING
            ) * SQRT(252) AS realized_vol_21d
        FROM daily_returns
    )
    SELECT
        date,
        symbol,
        avg_iv AS implied_vol,
        realized_vol_21d AS realized_vol,
        (avg_iv - realized_vol_21d) AS vrp
    FROM realized_vol;
    """)

    conn.execute("""
    CREATE OR REPLACE VIEW v_vol_surface AS
    WITH latest_dates AS (
        SELECT symbol, MAX(date) as max_date
        FROM Fact_Options
        GROUP BY symbol
    )
    SELECT 
        f.date,
        f.symbol,
        f.expiry,
        f.strike,
        f.option_type,
        f.implied_vol,
        f.underlying_price,
        f.delta,
        f.gamma,
        f.vega,
        f.theta,
        f.rho
    FROM Fact_Options f
    JOIN latest_dates l ON f.symbol = l.symbol AND f.date = l.max_date
    WHERE f.implied_vol IS NOT NULL AND f.implied_vol > 0;
    """)


# ─────────────────────────────────────────────────────────────────────────────
# Ingestion Functions (DoltHub & Kaggle)
# ─────────────────────────────────────────────────────────────────────────────


def ingest_dolthub_data(
    repo_path: str = DOLT_REPO_PATH,
    symbols: List[str] = DOLT_SYMBOLS,
    db_path: str = DUCKDB_PATH,
    r: float = 0.05,
) -> Dict[str, Any]:
    """
    Clones/pulls Dolt repo 'post-no-preference/options', queries historical options
    for requested symbols, calculates vectorized BS IV and greeks, and writes to Fact_Options.
    Handles missing dolt CLI gracefully.
    """
    dolt_bin = shutil.which("dolt")
    if not dolt_bin:
        logger.warning("Dolt CLI ('dolt') is not installed or not found in PATH. Skipping DoltHub ingestion.")
        return {
            "status": "skipped",
            "reason": "dolt_cli_missing",
            "message": "Dolt CLI not found on system",
            "rows_ingested": 0,
        }

    try:
        abs_repo_path = os.path.abspath(repo_path)
        if not os.path.exists(abs_repo_path) or not os.path.exists(os.path.join(abs_repo_path, ".dolt")):
            logger.info(f"Cloning Dolt repository 'post-no-preference/options' to {abs_repo_path}...")
            os.makedirs(os.path.dirname(abs_repo_path), exist_ok=True)
            res = subprocess.run(
                [dolt_bin, "clone", "post-no-preference/options", abs_repo_path],
                capture_output=True,
                text=True,
                check=True,
            )
            logger.info(f"Dolt clone output: {res.stdout}")
        else:
            logger.info(f"Pulling latest Dolt repository updates at {abs_repo_path}...")
            res = subprocess.run(
                [dolt_bin, "pull"],
                cwd=abs_repo_path,
                capture_output=True,
                text=True,
            )
            logger.info(f"Dolt pull output: {res.stdout}")

        symbols_str = ", ".join(f"'{s}'" for s in symbols)
        sql_query = f"""
        SELECT 
            date, 
            act_symbol as symbol, 
            expiration as expiry, 
            strike, 
            call_put as option_type, 
            bid, 
            ask, 
            vol as volume, 
            open_interest, 
            underlying_price 
        FROM option_chain 
        WHERE act_symbol IN ({symbols_str})
        """

        logger.info(f"Executing Dolt SQL query for symbols {symbols}...")
        cmd = [dolt_bin, "sql", "-q", sql_query, "-r", "csv"]
        res = subprocess.run(cmd, cwd=abs_repo_path, capture_output=True, text=True)

        if not res.stdout or res.stdout.strip() == "":
            fallback_query = f"""
            SELECT 
                date, 
                symbol, 
                expiry, 
                strike, 
                option_type, 
                bid, 
                ask, 
                volume, 
                open_interest, 
                underlying_price 
            FROM option_chain 
            WHERE symbol IN ({symbols_str})
            """
            cmd = [dolt_bin, "sql", "-q", fallback_query, "-r", "csv"]
            res = subprocess.run(cmd, cwd=abs_repo_path, capture_output=True, text=True)

        if not res.stdout or res.stdout.strip() == "":
            logger.warning("Dolt SQL query returned empty result.")
            return {"status": "success", "rows_ingested": 0, "message": "No records found in Dolt repo"}

        df = pd.read_csv(StringIO(res.stdout))
        if df.empty:
            return {"status": "success", "rows_ingested": 0, "message": "Empty DataFrame from Dolt query"}

        processed_df = process_options_df(df, r=r)

        conn = get_db_connection(db_path)
        init_duckdb_schema(conn)

        conn.register("df_dolt_temp", processed_df)
        conn.execute("""
        INSERT INTO Fact_Options 
        (date, symbol, expiry, strike, option_type, bid, ask, mid, volume, open_interest, underlying_price, implied_vol, delta, gamma, vega, theta, rho)
        SELECT 
            CAST(date AS DATE), 
            CAST(symbol AS VARCHAR), 
            CAST(expiry AS DATE), 
            CAST(strike AS DOUBLE), 
            CAST(option_type AS VARCHAR), 
            CAST(bid AS DOUBLE), 
            CAST(ask AS DOUBLE), 
            CAST(mid AS DOUBLE), 
            CAST(volume AS BIGINT), 
            CAST(open_interest AS BIGINT), 
            CAST(underlying_price AS DOUBLE), 
            CAST(implied_vol AS DOUBLE), 
            CAST(delta AS DOUBLE), 
            CAST(gamma AS DOUBLE), 
            CAST(vega AS DOUBLE), 
            CAST(theta AS DOUBLE), 
            CAST(rho AS DOUBLE)
        FROM df_dolt_temp;
        """)
        conn.close()

        logger.info(f"Successfully ingested {len(processed_df)} rows from DoltHub into Fact_Options.")
        return {"status": "success", "rows_ingested": len(processed_df), "symbols": symbols}

    except subprocess.CalledProcessError as cpe:
        logger.warning(f"Dolt command failed: {cpe.stderr or cpe}")
        return {"status": "error", "reason": "dolt_command_failed", "details": str(cpe.stderr or cpe), "rows_ingested": 0}
    except Exception as e:
        logger.error(f"Error during DoltHub ingestion: {e}", exc_info=True)
        return {"status": "error", "reason": str(e), "rows_ingested": 0}


def ingest_kaggle_data(
    kaggle_path: str = KAGGLE_SPY_PATH,
    db_path: str = DUCKDB_PATH,
    r: float = 0.05,
) -> Dict[str, Any]:
    """
    Loads Kaggle SPY vol surface Parquet files from KAGGLE_SPY_PATH,
    processes options data, and writes to Fact_Options_Validation.
    Skips gracefully if path or files are not present.
    """
    abs_kaggle_path = os.path.abspath(kaggle_path)
    if not os.path.exists(abs_kaggle_path):
        logger.warning(f"Kaggle path '{abs_kaggle_path}' does not exist. Skipping Kaggle ingestion.")
        return {
            "status": "skipped",
            "reason": "kaggle_path_missing",
            "message": f"Path '{abs_kaggle_path}' not found",
            "rows_ingested": 0,
        }

    parquet_files = []
    if os.path.isfile(abs_kaggle_path) and (abs_kaggle_path.endswith(".parquet") or abs_kaggle_path.endswith(".pq")):
        parquet_files = [abs_kaggle_path]
    else:
        parquet_files = glob.glob(os.path.join(abs_kaggle_path, "**", "*.parquet"), recursive=True) + \
                        glob.glob(os.path.join(abs_kaggle_path, "**", "*.pq"), recursive=True)

    if not parquet_files:
        logger.warning(f"No Parquet files found in '{abs_kaggle_path}'. Skipping Kaggle ingestion.")
        return {
            "status": "skipped",
            "reason": "no_parquet_files",
            "message": f"No parquet files in '{abs_kaggle_path}'",
            "rows_ingested": 0,
        }

    logger.info(f"Found {len(parquet_files)} Parquet file(s) for Kaggle ingestion.")
    dfs = []
    for pf in parquet_files:
        try:
            df_part = pd.read_parquet(pf)
            dfs.append(df_part)
        except Exception as e:
            logger.warning(f"Failed to read Parquet file {pf}: {e}")

    if not dfs:
        return {"status": "skipped", "reason": "parquet_read_failed", "rows_ingested": 0}

    combined_df = pd.concat(dfs, ignore_index=True)
    if combined_df.empty:
        return {"status": "success", "rows_ingested": 0, "message": "Parquet files contained no rows"}

    processed_df = process_options_df(combined_df, r=r)

    conn = get_db_connection(db_path)
    init_duckdb_schema(conn)

    conn.register("df_kaggle_temp", processed_df)
    conn.execute("""
    INSERT INTO Fact_Options_Validation 
    (date, symbol, expiry, strike, option_type, bid, ask, mid, volume, open_interest, underlying_price, implied_vol, delta, gamma, vega, theta, rho)
    SELECT 
        CAST(date AS DATE), 
        CAST(symbol AS VARCHAR), 
        CAST(expiry AS DATE), 
        CAST(strike AS DOUBLE), 
        CAST(option_type AS VARCHAR), 
        CAST(bid AS DOUBLE), 
        CAST(ask AS DOUBLE), 
        CAST(mid AS DOUBLE), 
        CAST(volume AS BIGINT), 
        CAST(open_interest AS BIGINT), 
        CAST(underlying_price AS DOUBLE), 
        CAST(implied_vol AS DOUBLE), 
        CAST(delta AS DOUBLE), 
        CAST(gamma AS DOUBLE), 
        CAST(vega AS DOUBLE), 
        CAST(theta AS DOUBLE), 
        CAST(rho AS DOUBLE)
    FROM df_kaggle_temp;
    """)
    conn.close()

    logger.info(f"Successfully ingested {len(processed_df)} rows from Kaggle into Fact_Options_Validation.")
    return {"status": "success", "rows_ingested": len(processed_df), "files_processed": len(parquet_files)}


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI Endpoints
# ─────────────────────────────────────────────────────────────────────────────


def _clean_date_str(val: Any) -> Optional[str]:
    """Helper to convert date or timestamp values to YYYY-MM-DD string format."""
    if val is None or pd.isna(val):
        return None
    return str(val)[:10]


@app.on_event("startup")
def startup_event():
    """Ensure DuckDB database and tables exist on startup."""
    try:
        conn = get_db_connection()
        init_duckdb_schema(conn)
        conn.close()
        logger.info(f"DuckDB schema initialized at {DUCKDB_PATH}")
    except Exception as e:
        logger.error(f"Failed to initialize DuckDB schema on startup: {e}")


@app.get("/options/health")
def get_health() -> Dict[str, Any]:
    """Liveness check and data coverage summary."""
    try:
        conn = get_db_connection()
        init_duckdb_schema(conn)

        opt_count = conn.execute("SELECT COUNT(*) FROM Fact_Options").fetchone()[0]
        val_count = conn.execute("SELECT COUNT(*) FROM Fact_Options_Validation").fetchone()[0]

        symbols_res = conn.execute("SELECT DISTINCT symbol FROM Fact_Options").fetchall()
        symbols = [r[0] for r in symbols_res] if symbols_res else []

        date_range = conn.execute("SELECT MIN(date), MAX(date) FROM Fact_Options").fetchone()
        min_date = _clean_date_str(date_range[0]) if date_range else None
        max_date = _clean_date_str(date_range[1]) if date_range else None

        conn.close()

        return {
            "status": "ok",
            "service": "k9_options_ingestion",
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "duckdb_path": DUCKDB_PATH,
            "fact_options_rows": opt_count,
            "fact_options_validation_rows": val_count,
            "symbols": symbols,
            "min_date": min_date,
            "max_date": max_date,
            "dolt_cli_available": shutil.which("dolt") is not None,
            "kaggle_path_exists": os.path.exists(KAGGLE_SPY_PATH),
        }
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return {
            "status": "degraded",
            "error": str(e),
            "timestamp": datetime.datetime.utcnow().isoformat(),
        }


@app.get("/options/coverage")
def get_coverage() -> Dict[str, Any]:
    """Detailed data coverage statistics across tables."""
    try:
        conn = get_db_connection()
        init_duckdb_schema(conn)

        fact_options_stats = conn.execute("""
        SELECT 
            COUNT(*) as total_rows,
            COUNT(DISTINCT symbol) as symbol_count,
            MIN(date) as min_date,
            MAX(date) as max_date,
            COUNT(implied_vol) as non_null_iv_count
        FROM Fact_Options
        """).fetchone()

        fact_validation_stats = conn.execute("""
        SELECT 
            COUNT(*) as total_rows,
            COUNT(DISTINCT symbol) as symbol_count,
            MIN(date) as min_date,
            MAX(date) as max_date,
            COUNT(implied_vol) as non_null_iv_count
        FROM Fact_Options_Validation
        """).fetchone()

        symbol_breakdown = conn.execute("""
        SELECT symbol, COUNT(*) as count, MIN(date) as min_d, MAX(date) as max_d 
        FROM Fact_Options 
        GROUP BY symbol
        """).fetchall()

        conn.close()

        return {
            "fact_options": {
                "total_rows": fact_options_stats[0] if fact_options_stats else 0,
                "symbol_count": fact_options_stats[1] if fact_options_stats else 0,
                "min_date": _clean_date_str(fact_options_stats[2]) if fact_options_stats else None,
                "max_date": _clean_date_str(fact_options_stats[3]) if fact_options_stats else None,
                "non_null_iv_count": fact_options_stats[4] if fact_options_stats else 0,
                "symbols": {
                    r[0]: {"count": r[1], "min_date": _clean_date_str(r[2]), "max_date": _clean_date_str(r[3])} for r in symbol_breakdown
                },
            },
            "fact_options_validation": {
                "total_rows": fact_validation_stats[0] if fact_validation_stats else 0,
                "symbol_count": fact_validation_stats[1] if fact_validation_stats else 0,
                "min_date": _clean_date_str(fact_validation_stats[2]) if fact_validation_stats else None,
                "max_date": _clean_date_str(fact_validation_stats[3]) if fact_validation_stats else None,
                "non_null_iv_count": fact_validation_stats[4] if fact_validation_stats else 0,
            },
        }
    except Exception as e:
        logger.error(f"Error fetching coverage: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/options/iv-percentile/{symbol}")
def get_iv_percentile(symbol: str, limit: int = Query(default=100, ge=1, le=1000)) -> Dict[str, Any]:
    """Returns IV percentile statistics from v_iv_percentile view for given symbol."""
    sym = symbol.upper().strip()
    try:
        conn = get_db_connection()
        init_duckdb_schema(conn)

        df = conn.execute("""
        SELECT date, symbol, expiry, strike, option_type, implied_vol, iv_percentile
        FROM v_iv_percentile
        WHERE UPPER(symbol) = ?
        ORDER BY date DESC, expiry ASC, strike ASC
        LIMIT ?
        """, [sym, limit]).df()

        conn.close()

        records = df.to_dict(orient="records")
        for r in records:
            for k in ["date", "expiry"]:
                if r.get(k) is not None:
                    r[k] = _clean_date_str(r[k])

        return {
            "symbol": sym,
            "count": len(records),
            "data": records,
        }
    except Exception as e:
        logger.error(f"Error fetching IV percentile for {sym}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/options/vrp/{symbol}")
def get_vrp(symbol: str, limit: int = Query(default=100, ge=1, le=1000)) -> Dict[str, Any]:
    """Returns Variance Risk Premium (IV minus realized vol) from v_variance_risk_premium view."""
    sym = symbol.upper().strip()
    try:
        conn = get_db_connection()
        init_duckdb_schema(conn)

        df = conn.execute("""
        SELECT date, symbol, implied_vol, realized_vol, vrp
        FROM v_variance_risk_premium
        WHERE UPPER(symbol) = ?
        ORDER BY date DESC
        LIMIT ?
        """, [sym, limit]).df()

        conn.close()

        records = df.to_dict(orient="records")
        for r in records:
            if r.get("date") is not None:
                r["date"] = _clean_date_str(r["date"])

        return {
            "symbol": sym,
            "count": len(records),
            "data": records,
        }
    except Exception as e:
        logger.error(f"Error fetching VRP for {sym}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/options/vol-surface/{symbol}")
def get_vol_surface(symbol: str) -> Dict[str, Any]:
    """Returns current day cross-section volatility surface from v_vol_surface view."""
    sym = symbol.upper().strip()
    try:
        conn = get_db_connection()
        init_duckdb_schema(conn)

        df = conn.execute("""
        SELECT date, symbol, expiry, strike, option_type, implied_vol, underlying_price, delta, gamma, vega, theta, rho
        FROM v_vol_surface
        WHERE UPPER(symbol) = ?
        ORDER BY expiry ASC, strike ASC
        """, [sym]).df()

        conn.close()

        records = df.to_dict(orient="records")
        current_date = None
        for r in records:
            if r.get("date") is not None:
                current_date = _clean_date_str(r["date"])
                r["date"] = current_date
            if r.get("expiry") is not None:
                r["expiry"] = _clean_date_str(r["expiry"])

        return {
            "symbol": sym,
            "as_of_date": current_date,
            "count": len(records),
            "surface": records,
        }
    except Exception as e:
        logger.error(f"Error fetching vol surface for {sym}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/options/ingest/dolthub")
def trigger_dolthub_ingest() -> Dict[str, Any]:
    """Triggers ingestion of historical options data from DoltHub."""
    result = ingest_dolthub_data()
    return result


@app.post("/options/ingest/kaggle")
def trigger_kaggle_ingest() -> Dict[str, Any]:
    """Triggers ingestion of Kaggle SPY volatility surface Parquet files."""
    result = ingest_kaggle_data()
    return result
