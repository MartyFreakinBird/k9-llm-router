"""
FastAPI Main Application for K-9 Sentiment Engine.
Provides HTTP and WebSocket endpoints for Reddit and Google News sentiment analysis.
Runs on port 9006 for the K-9 ecosystem.
"""
import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, Query, Path, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

import config
from scraper import RedditScraper, NewsScraper, stats, clean_text
from analyzer import analyzer, FINBERT_AVAILABLE

app = FastAPI(
    title="K-9 Sentiment Engine",
    description="Self-hosted, zero-API-cost sentiment engine for the K-9 ecosystem.",
    version="1.0.0"
)

# Enable CORS for all K-9 ecosystem origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Instantiate scrapers
reddit_scraper = RedditScraper()
news_scraper = NewsScraper()


class ConnectionManager:
    """Manages active WebSocket connections and broadcasting."""
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def send_personal_message(self, message: Dict[str, Any], websocket: WebSocket):
        await websocket.send_json(message)

    async def broadcast(self, message: Dict[str, Any]):
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                self.disconnect(connection)


websocket_manager = ConnectionManager()


def normalize_post(post: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure post object contains strictly required fields."""
    return {
        "title": str(post.get("title", "")),
        "text": str(post.get("text", "")),
        "author": str(post.get("author", "anonymous")),
        "url": str(post.get("url", "")),
        "score": float(post.get("score", 0.0)),
        "sentiment": post.get("sentiment", {
            "compound": 0.0,
            "positive": 0.0,
            "negative": 0.0,
            "neutral": 1.0,
            "label": "neutral"
        }),
        "created_utc": str(post.get("created_utc", ""))
    }


def format_response(
    source: str,
    query: str,
    analyzed_data: Dict[str, Any]
) -> Dict[str, Any]:
    """Format final endpoint JSON response conforming to K-9 ecosystem schema."""
    normalized_posts = [normalize_post(p) for p in analyzed_data.get("posts", [])]
    return {
        "source": source,
        "query": query,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "posts": normalized_posts,
        "aggregate": analyzed_data.get("aggregate", {
            "total_posts": 0,
            "avg_sentiment": 0.0,
            "bullish_count": 0,
            "bearish_count": 0,
            "neutral_count": 0,
            "sentiment_score": 0.0,
            "trend": "stable"
        })
    }


@app.get("/health")
def health_check():
    """Liveness check endpoint."""
    return {
        "status": "ok",
        "service": "k9-sentiment-engine",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finbert_available": FINBERT_AVAILABLE,
        "uptime_seconds": stats.uptime_seconds
    }


@app.get("/sentiment/reddit/{subreddit}")
def get_reddit_sentiment(
    subreddit: str = Path(..., description="Subreddit name to scrape"),
    limit: int = Query(25, ge=1, le=100, description="Number of posts to fetch"),
    sort: str = Query("hot", description="Sort order: hot, new, or top")
):
    """Scrape Reddit posts for a subreddit and analyze sentiment with VADER."""
    stats.total_requests += 1
    raw_posts = reddit_scraper.fetch_subreddit_posts(subreddit=subreddit, sort=sort, limit=limit)
    analyzed = analyzer.analyze_batch(raw_posts, query_key=f"reddit:{subreddit}")
    return format_response(source="reddit", query=f"r/{subreddit}", analyzed_data=analyzed)


@app.get("/sentiment/news")
def get_news_sentiment(
    query: str = Query(..., description="Search query for Google News"),
    limit: int = Query(20, ge=1, le=100, description="Number of news items to fetch")
):
    """Scrape Google News RSS for a query and analyze sentiment."""
    stats.total_requests += 1
    raw_posts = news_scraper.fetch_news(query=query, limit=limit)
    analyzed = analyzer.analyze_batch(raw_posts, query_key=f"news:{query}")
    return format_response(source="news", query=query, analyzed_data=analyzed)


@app.get("/sentiment/aggregate")
def get_aggregate_sentiment(
    query: Optional[str] = Query(None, description="Optional search query or ticker"),
    sources: str = Query("reddit,news", description="Comma-separated list of sources: reddit, news")
):
    """Cross-source aggregate sentiment dashboard combining Reddit and Google News."""
    stats.total_requests += 1
    source_list = [s.strip().lower() for s in sources.split(",") if s.strip()]
    all_raw_posts: List[Dict[str, Any]] = []

    limit_per_source = 20

    if "reddit" in source_list:
        if query:
            sub_posts = reddit_scraper.fetch_subreddit_posts(subreddit=query, sort="hot", limit=limit_per_source)
            all_raw_posts.extend(sub_posts)
        else:
            for sub in config.DEFAULT_SUBREDDITS[:3]:
                sub_posts = reddit_scraper.fetch_subreddit_posts(subreddit=sub, sort="hot", limit=10)
                all_raw_posts.extend(sub_posts)

    if "news" in source_list:
        news_query = query if query else "market economy"
        news_posts = news_scraper.fetch_news(query=news_query, limit=limit_per_source)
        all_raw_posts.extend(news_posts)

    query_label = query if query else "market-aggregate"
    analyzed = analyzer.analyze_batch(all_raw_posts, query_key=f"aggregate:{query_label}")
    return format_response(source="aggregate", query=query_label, analyzed_data=analyzed)


@app.get("/sentiment/users/{username}")
def get_user_sentiment(
    username: str = Path(..., description="Reddit username to monitor"),
    platform: str = Query("reddit", description="Platform name"),
    limit: int = Query(25, ge=1, le=100, description="Max posts to analyze")
):
    """Monitor specific user posts on Reddit and analyze sentiment."""
    stats.total_requests += 1
    if platform.lower() != "reddit":
        raise HTTPException(status_code=400, detail="Only 'reddit' platform is currently supported for user monitoring.")

    raw_posts = reddit_scraper.fetch_user_posts(username=username, limit=limit)
    analyzed = analyzer.analyze_batch(raw_posts, query_key=f"user:{username}")
    return format_response(source="reddit", query=f"u/{username}", analyzed_data=analyzed)


@app.get("/sentiment/symbols/{symbol}")
def get_symbol_sentiment(
    symbol: str = Path(..., description="Stock or crypto ticker symbol (e.g. AAPL, BTC, NVDA)"),
    limit: int = Query(25, ge=1, le=100, description="Max items per source")
):
    """Stock/crypto ticker sentiment extracted from Reddit and Google News."""
    stats.total_requests += 1
    clean_sym = symbol.strip().upper()
    all_raw_posts: List[Dict[str, Any]] = []

    # 1. Fetch Reddit posts from relevant stock/crypto subreddits or query
    reddit_posts = reddit_scraper.fetch_subreddit_posts(subreddit="stocks", sort="hot", limit=limit // 2)
    filtered_reddit = [
        p for p in reddit_posts
        if clean_sym in p.get("title", "").upper() or clean_sym in p.get("text", "").upper()
    ]
    if not filtered_reddit:
        # Fallback to fetching directly or wsb
        filtered_reddit = reddit_scraper.fetch_subreddit_posts(subreddit="wallstreetbets", sort="hot", limit=limit // 2)
    all_raw_posts.extend(filtered_reddit[:limit])

    # 2. Fetch Google News for the symbol
    news_posts = news_scraper.fetch_news(query=f"{clean_sym} stock OR crypto", limit=limit)
    all_raw_posts.extend(news_posts)

    analyzed = analyzer.analyze_batch(all_raw_posts, query_key=f"symbol:{clean_sym}")
    return format_response(source="aggregate", query=clean_sym, analyzed_data=analyzed)


@app.get("/sentiment/stats")
def get_service_stats():
    """Service statistics endpoint."""
    return {
        "status": "ok",
        "service": "k9-sentiment-engine",
        "uptime_seconds": stats.uptime_seconds,
        "total_requests": stats.total_requests,
        "cache_hits": stats.cache_hits,
        "cache_misses": stats.cache_misses,
        "total_posts_analyzed": stats.total_posts_analyzed,
        "active_websocket_connections": len(websocket_manager.active_connections),
        "finbert_available": FINBERT_AVAILABLE,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    }


@app.websocket("/sentiment/stream")
async def websocket_sentiment_stream(websocket: WebSocket):
    """
    WebSocket endpoint providing real-time sentiment updates every 30 seconds.
    Clients can also send JSON messages like {"query": "NVDA"} to update stream query.
    """
    await websocket_manager.connect(websocket)
    target_query = "wallstreetbets"

    async def stream_loop():
        nonlocal target_query
        try:
            while True:
                # Scrape and analyze latest sentiment
                raw_posts = reddit_scraper.fetch_subreddit_posts(subreddit=target_query, sort="hot", limit=15)
                analyzed = analyzer.analyze_batch(raw_posts, query_key=f"ws:{target_query}")
                payload = format_response(source="reddit", query=target_query, analyzed_data=analyzed)

                await websocket_manager.send_personal_message(payload, websocket)
                await asyncio.sleep(30)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass

    stream_task = asyncio.create_task(stream_loop())

    try:
        while True:
            # Listen for client messages to switch query/symbol dynamically
            data = await websocket.receive_json()
            if isinstance(data, dict):
                new_q = data.get("query") or data.get("subreddit") or data.get("symbol")
                if new_q and isinstance(new_q, str):
                    target_query = new_q.strip()
                    # Trigger immediate update
                    raw_posts = reddit_scraper.fetch_subreddit_posts(subreddit=target_query, sort="hot", limit=15)
                    analyzed = analyzer.analyze_batch(raw_posts, query_key=f"ws:{target_query}")
                    payload = format_response(source="reddit", query=target_query, analyzed_data=analyzed)
                    await websocket_manager.send_personal_message(payload, websocket)
    except (WebSocketDisconnect, Exception):
        stream_task.cancel()
        websocket_manager.disconnect(websocket)


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=config.HOST,
        port=config.K9_SENTIMENT_PORT,
        reload=False
    )
