"""
k9_automation_coordinator.py — K-9 Phase 5 Automation Hub
─────────────────────────────────────────────────────────────────────────────
Runs alongside n8n (or replaces it if n8n is down). Coordinates:

1. OSINT News Scraping (every 10 min)
   - Fetches sentiment from k9-sentiment-engine :9006
   - Detects sentiment regime shifts
   - Routes significant shifts → aeg-signal-router :9004

2. GEX Monitoring (every 5 min)
   - Fetches BTC/ETH GEX from k9-gex-engine :9008
   - Detects gamma flip crossings
   - Routes significant GEX shifts → aeg-signal-router :9004

3. Ecosystem Health Monitor (every 2 min)
   - Polls all K-9 services
   - Alerts on critical service failures
   - Routes alerts → aeg-signal-router :9004

4. Polymarket Edge Detection (every 15 min)
   - Fetches prediction market edges from :9007
   - Routes mispricings → aeg-signal-router :9004

All signals use the K9-CB v1 envelope schema.
Zero external API keys — all data from local K-9 services.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("k9.automation")

# ── Config ────────────────────────────────────────────────────────────────────

AEG_SIGNAL_ROUTER = os.getenv("AEG_SIGNAL_ROUTER_URL", "http://localhost:9004")
K9_LLM_ROUTER = os.getenv("K9_LLM_ROUTER_URL", "http://localhost:8765")
SENTIMENT_ENGINE = os.getenv("SENTIMENT_ENGINE_URL", "http://localhost:9006")
GEX_ENGINE = os.getenv("GEX_ENGINE_URL", "http://localhost:9008")
POLYMARKET_ADAPTER = os.getenv("POLYMARKET_ADAPTER_URL", "http://localhost:9007")
FISCAL_DOMINANCE_URL = os.getenv("FISCAL_DOMINANCE_URL", "http://localhost:9010")

# Intervals (seconds)
OSINT_INTERVAL = int(os.getenv("AUTOMATION_OSINT_INTERVAL", "600"))      # 10 min
GEX_INTERVAL = int(os.getenv("AUTOMATION_GEX_INTERVAL", "300"))         # 5 min
HEALTH_INTERVAL = int(os.getenv("AUTOMATION_HEALTH_INTERVAL", "120"))   # 2 min
POLY_INTERVAL = int(os.getenv("AUTOMATION_POLY_INTERVAL", "900"))       # 15 min
FOMC_INTERVAL = int(os.getenv("AUTOMATION_FOMC_INTERVAL", "1800"))     # 30 min

# Sentiment thresholds
SENTIMENT_SHIFT_THRESHOLD = 0.25  # |Δ sentiment| to trigger signal

# ── K-9 Service Registry ──────────────────────────────────────────────────────

SERVICES = [
    {"name": "k9-llm-router", "url": f"{K9_LLM_ROUTER}/health", "port": 8765, "tier": "critical"},
    {"name": "k9-orchestrator", "url": "http://localhost:8744/health", "port": 8744, "tier": "critical"},
    {"name": "k9-paymaster", "url": "http://localhost:9002/health", "port": 9002, "tier": "critical"},
    {"name": "k9-mcp-manager", "url": "http://localhost:3030/health", "port": 3030, "tier": "critical"},
    {"name": "k9-quant-engine", "url": "http://localhost:9001/health", "port": 9001, "tier": "important"},
    {"name": "aeg-token-model", "url": "http://localhost:9003/health", "port": 9003, "tier": "important"},
    {"name": "aeg-signal-router", "url": f"{AEG_SIGNAL_ROUTER}/health", "port": 9004, "tier": "important"},
    {"name": "k9-sentiment-engine", "url": f"{SENTIMENT_ENGINE}/health", "port": 9006, "tier": "monitor"},
    {"name": "k9-polymarket-adapter", "url": f"{POLYMARKET_ADAPTER}/health", "port": 9007, "tier": "monitor"},
    {"name": "k9-gex-engine", "url": f"{GEX_ENGINE}/gex/health", "port": 9008, "tier": "monitor"},
    {"name": "n8n", "url": "http://localhost:5678/healthz", "port": 5678, "tier": "infra"},
    {"name": "ollama", "url": "http://localhost:11434/api/tags", "port": 11434, "tier": "infra"},
]

# ── State ────────────────────────────────────────────────────────────────────

_last_sentiment: float | None = None
_last_gex_net: dict[str, float] = {"BTC": 0.0, "ETH": 0.0}
_last_health_summary: str = ""

# ── CB v1 Envelope Builder ───────────────────────────────────────────────────

def make_envelope(source: str, msg_type: str, payload: dict, confidence: float = 0.8,
                  tags: list[str] | None = None) -> dict:
    """Build a K9-CB v1 envelope."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "type": msg_type,
        "ontology_tags": tags or [],
        "confidence": confidence,
        "payload": payload,
        "trace_id": str(uuid.uuid4()),
        "causation_id": str(uuid.uuid4()),
        "signature": "",
    }


async def route_signal(envelope: dict) -> bool:
    """Route a CB v1 envelope to the signal router."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{AEG_SIGNAL_ROUTER}/route",
                json=envelope,
            )
            return resp.status_code == 200
    except Exception as e:
        log.debug(f"Signal routing failed: {e}")
        return False


async def push_to_wallpaper(data: dict) -> bool:
    """Push data to the wallpaper via the n8n webhook bridge."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{K9_LLM_ROUTER}/n8n/webhook",
                json=data,
            )
            return True
    except Exception:
        return False


# ── 1. OSINT News Scraping ────────────────────────────────────────────────────

async def osint_cycle():
    """Fetch sentiment from multiple sources, detect shifts, route signals."""
    global _last_sentiment

    log.info("OSINT cycle starting...")
    results = {}

    async with httpx.AsyncClient(timeout=30) as client:
        # Fetch news sentiment
        try:
            resp = await client.get(
                f"{SENTIMENT_ENGINE}/sentiment/news",
                params={"q": "Bitcoin OR BTC OR crypto OR Federal Reserve OR FOMC", "limit": 20}
            )
            results["news"] = resp.json() if resp.status_code == 200 else {}
        except Exception as e:
            log.warning(f"News fetch failed: {e}")
            results["news"] = {}

        # Fetch Reddit sentiment
        for sub in ["CryptoCurrency", "wallstreetbets"]:
            try:
                resp = await client.get(
                    f"{SENTIMENT_ENGINE}/sentiment/reddit/{sub}",
                    params={"limit": 25}
                )
                results[f"reddit_{sub}"] = resp.json() if resp.status_code == 200 else {}
            except Exception as e:
                log.warning(f"Reddit {sub} fetch failed: {e}")
                results[f"reddit_{sub}"] = {}

    # Calculate overall sentiment
    news_sent = results.get("news", {}).get("average_sentiment", 0)
    crypto_sent = results.get("reddit_CryptoCurrency", {}).get("average_sentiment", 0)
    wsb_sent = results.get("reddit_wallstreetbets", {}).get("average_sentiment", 0)
    overall = (news_sent + crypto_sent + wsb_sent) / 3 if any([news_sent, crypto_sent, wsb_sent]) else 0

    # Determine regime
    regime = "NEUTRAL"
    if overall > 0.3:
        regime = "BULLISH"
    elif overall < -0.3:
        regime = "BEARISH"

    # Detect shift
    shift = abs(overall - (_last_sentiment or 0))
    is_significant = shift > SENTIMENT_SHIFT_THRESHOLD or abs(overall) > 0.4

    # Build summary
    news_count = results.get("news", {}).get("count", 0)
    reddit_count = (
        results.get("reddit_CryptoCurrency", {}).get("count", 0) +
        results.get("reddit_wallstreetbets", {}).get("count", 0)
    )

    top_stories = results.get("news", {}).get("results", [])[:5]
    top_reddit = results.get("reddit_CryptoCurrency", {}).get("results", [])[:5]

    summary = (
        f"OSINT: {regime} ({overall:.2f}) | "
        f"News: {news_sent:.2f} ({news_count} articles) | "
        f"Reddit: Crypto {crypto_sent:.2f}, WSB {wsb_sent:.2f} | "
        f"Shift: {shift:.2f}"
    )

    log.info(f"OSINT: {summary}")

    # Route if significant
    if is_significant:
        envelope = make_envelope(
            source="k9-automation-osint",
            msg_type="sentiment_snapshot",
            payload={
                "regime": regime,
                "overall_sentiment": round(overall, 4),
                "news_sentiment": round(news_sent, 4),
                "reddit_crypto_sentiment": round(crypto_sent, 4),
                "reddit_wsb_sentiment": round(wsb_sent, 4),
                "shift_from_last": round(shift, 4),
                "news_count": news_count,
                "reddit_count": reddit_count,
                "top_stories": top_stories,
                "top_reddit": top_reddit,
                "summary": summary,
            },
            confidence=0.82 if is_significant else 0.5,
            tags=["sentiment", "osint", "news", "reddit"],
        )
        routed = await route_signal(envelope)
        log.info(f"  → Signal routed: {routed}")

    # Push to wallpaper
    await push_to_wallpaper({
        "type": "osint_sentiment",
        "data": {
            "regime": regime,
            "overall_sentiment": overall,
            "news_sentiment": news_sent,
            "reddit_crypto_sentiment": crypto_sent,
            "reddit_wsb_sentiment": wsb_sent,
            "top_stories": top_stories,
            "top_reddit": top_reddit,
            "summary": summary,
        }
    })

    _last_sentiment = overall


# ── 2. GEX Monitoring ────────────────────────────────────────────────────────

async def gex_cycle():
    """Fetch GEX data, detect gamma flip crossings, route signals."""
    global _last_gex_net

    log.info("GEX cycle starting...")

    async with httpx.AsyncClient(timeout=30) as client:
        for asset in ["BTC", "ETH"]:
            try:
                resp = await client.get(f"{GEX_ENGINE}/gex/{asset}/walls", params={"top": 5})
                if resp.status_code != 200:
                    continue
                data = resp.json()

                spot = data.get("spot", 0)
                flip = data.get("gamma_flip", 0)
                net_gex = data.get("net_gex", 0)

                # Detect significant GEX shift
                prev_net = _last_gex_net.get(asset, 0)
                shift = abs(net_gex - prev_net)
                shift_pct = (shift / abs(prev_net) * 100) if prev_net else 0

                # Detect gamma flip crossing (spot crosses flip level)
                flip_distance_pct = abs(spot - flip) / spot * 100 if spot else 999

                is_significant = shift_pct > 20 or flip_distance_pct < 2

                log.info(
                    f"GEX {asset}: spot=${spot:,.0f} flip=${flip:,.0f} "
                    f"net={net_gex:,.0f} shift={shift_pct:.1f}% flip_dist={flip_distance_pct:.1f}%"
                )

                if is_significant and prev_net != 0:
                    sentiment = "LONG GAMMA" if net_gex > 0 else "SHORT GAMMA"
                    envelope = make_envelope(
                        source="k9-automation-gex",
                        msg_type="gex_shift",
                        payload={
                            "asset": asset,
                            "spot": spot,
                            "gamma_flip": flip,
                            "net_gex": net_gex,
                            "prev_net_gex": prev_net,
                            "shift_pct": round(shift_pct, 2),
                            "flip_distance_pct": round(flip_distance_pct, 2),
                            "call_walls": data.get("call_walls", []),
                            "put_walls": data.get("put_walls", []),
                            "sentiment": sentiment,
                        },
                        confidence=0.85,
                        tags=["gex", "options", "gamma", asset.lower()],
                    )
                    routed = await route_signal(envelope)
                    log.info(f"  → GEX signal routed: {routed}")

                _last_gex_net[asset] = net_gex

            except Exception as e:
                log.warning(f"GEX {asset} fetch failed: {e}")


# ── 3. Ecosystem Health Monitor ──────────────────────────────────────────────

async def health_cycle():
    """Check all K-9 services, alert on failures."""
    global _last_health_summary

    log.info("Health check cycle starting...")

    healthy = 0
    unhealthy = 0
    critical_down = 0
    down_services = []

    async with httpx.AsyncClient(timeout=5) as client:
        for svc in SERVICES:
            try:
                resp = await client.get(svc["url"])
                if resp.status_code < 500:
                    healthy += 1
                else:
                    unhealthy += 1
                    if svc["tier"] == "critical":
                        critical_down += 1
                    down_services.append(svc["name"])
            except Exception:
                unhealthy += 1
                if svc["tier"] == "critical":
                    critical_down += 1
                down_services.append(svc["name"])

    total = len(SERVICES)
    health_pct = round(healthy / total * 100)

    if critical_down > 0:
        summary = f"🔴 CRITICAL: {critical_down} down — {', '.join(down_services)}"
    elif unhealthy > 0:
        summary = f"🟡 {unhealthy} down: {', '.join(down_services)}"
    else:
        summary = f"🟢 All {total} services healthy"

    log.info(f"Health: {health_pct}% ({healthy}/{total}) — {summary}")

    # Only alert if state changed or critical
    if summary != _last_health_summary and (critical_down > 0 or unhealthy >= 3):
        envelope = make_envelope(
            source="k9-automation-health",
            msg_type="ecosystem_alert",
            payload={
                "health_percent": health_pct,
                "healthy_count": healthy,
                "unhealthy_count": unhealthy,
                "critical_down": critical_down,
                "down_services": down_services,
                "summary": summary,
            },
            confidence=0.95,
            tags=["health", "monitoring", "infrastructure"],
        )
        await route_signal(envelope)
        await push_to_wallpaper({"type": "health_alert", "data": envelope["payload"]})

    _last_health_summary = summary


# ── 4. Polymarket Edge Detection ─────────────────────────────────────────────

async def polymarket_cycle():
    """Fetch prediction market edges, route mispricings."""
    log.info("Polymarket cycle starting...")

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(f"{POLYMARKET_ADAPTER}/edges")
            if resp.status_code != 200:
                return
            data = resp.json()

            edges = data.get("edges", [])
            if not edges:
                log.info("Polymarket: no significant edges")
                return

            log.info(f"Polymarket: {len(edges)} edges detected")

            # Route top 5 edges
            for edge in edges[:5]:
                envelope = make_envelope(
                    source="k9-automation-polymarket",
                    msg_type="prediction_market_edge",
                    payload={
                        "market": edge.get("question", ""),
                        "category": edge.get("category", ""),
                        "implied_prob": edge.get("implied_prob", 0),
                        "edge_pct": edge.get("edge_pct", 0),
                        "volume": edge.get("volume", 0),
                        "liquidity": edge.get("liquidity", 0),
                    },
                    confidence=0.75,
                    tags=["polymarket", "prediction_market", "edge"],
                )
                await route_signal(envelope)

            await push_to_wallpaper({
                "type": "polymarket_edges",
                "data": {"edges": edges[:5], "count": len(edges)}
            })

        except Exception as e:
            log.warning(f"Polymarket fetch failed: {e}")


# ── 5. FOMC Fiscal Fragility Monitor ─────────────────────────────────────────

async def fomc_cycle():
    """Monitor fiscal dominance score and FOMC cascade, route shifts."""
    log.info("FOMC fiscal cycle starting...")

    try:
        # Import locally to handle missing module gracefully
        sys.path.insert(0, '.')
        from src.k9_fomc_fiscal_modifier import get_fiscal_dominance_score, compute_cascade, route_fomc_signal

        fiscal_score = get_fiscal_dominance_score()
        cascade = compute_cascade(fiscal_score)

        flips = cascade["aggregate"]["flip_count"]
        regime = cascade["fiscal_regime"]

        log.info(
            f"FOMC: fiscal_score={fiscal_score:.1f} regime={regime} "
            f"flips={flips} hike={cascade['aggregate']['hike_probability']:.2f} "
            f"hold={cascade['aggregate']['hold_probability']:.2f} "
            f"cut={cascade['aggregate']['cut_probability']:.2f}"
        )

        if flips > 0:
            routed = route_fomc_signal(cascade)
            log.info(f"  -> FOMC signal routed: {routed} ({flips} flips)")

            # Push to wallpaper
            await push_to_wallpaper({
                "type": "fomc_cascade_shift",
                "data": {
                    "fiscal_score": fiscal_score,
                    "regime": regime,
                    "flips": flips,
                    "aggregate": cascade["aggregate"],
                    "trilemma": cascade["trilemma"],
                }
            })

    except ImportError:
        log.debug("FOMC fiscal modifier not available")
    except Exception as e:
        log.warning(f"FOMC cycle error: {e}")


# ── Main Loop ────────────────────────────────────────────────────────────────

async def run_cycle(coro, interval: int, name: str):
    """Run a coroutine on a fixed interval."""
    while True:
        try:
            await coro()
        except Exception as e:
            log.error(f"{name} cycle error: {e}")
        await asyncio.sleep(interval)


async def main():
    log.info("═══════════════════════════════════════════")
    log.info("  K-9 Phase 5 Automation Coordinator")
    log.info("  OSINT: 10min | GEX: 5min | Health: 2min | Poly: 15min | FOMC: 30min")
    log.info("═══════════════════════════════════════════")

    # Stagger initial runs
    await asyncio.sleep(1)
    await health_cycle()
    await asyncio.sleep(5)
    await osint_cycle()
    await asyncio.sleep(5)
    await gex_cycle()
    await asyncio.sleep(5)
    await polymarket_cycle()
    await asyncio.sleep(5)
    await fomc_cycle()

    # Start all loops
    await asyncio.gather(
        run_cycle(osint_cycle, OSINT_INTERVAL, "OSINT"),
        run_cycle(gex_cycle, GEX_INTERVAL, "GEX"),
        run_cycle(health_cycle, HEALTH_INTERVAL, "Health"),
        run_cycle(polymarket_cycle, POLY_INTERVAL, "Polymarket"),
        run_cycle(fomc_cycle, FOMC_INTERVAL, "FOMC"),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Automation coordinator stopped by user")
