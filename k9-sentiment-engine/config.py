"""
Configuration settings for K-9 Sentiment Engine.
"""
import os
from typing import List

# Port & Server settings
K9_SENTIMENT_PORT: int = int(os.getenv("K9_SENTIMENT_PORT", 9006))
HOST: str = os.getenv("K9_SENTIMENT_HOST", "0.0.0.0")

# Base URLs
REDDIT_BASE: str = os.getenv("REDDIT_BASE", "https://www.reddit.com")
NEWS_RSS: str = os.getenv("NEWS_RSS", "https://news.google.com/rss/search")
NEWS_TOP_STORIES_RSS: str = os.getenv("NEWS_TOP_STORIES_RSS", "https://news.google.com/rss")

# Caching & Rate Limiting
CACHE_TTL: int = int(os.getenv("CACHE_TTL", 60))  # seconds
RATE_LIMIT_DELAY: float = float(os.getenv("RATE_LIMIT_DELAY", 2.0))  # seconds between requests to same source

# User-Agent list (5 realistic browser User-Agent strings)
USER_AGENTS: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0"
]

# Default list of subreddits to monitor for market / ecosystem sentiment
DEFAULT_SUBREDDITS: List[str] = [
    "wallstreetbets",
    "stocks",
    "investing",
    "cryptocurrency",
    "economics",
    "worldnews",
    "geopolitics"
]

# Optional FinBERT model name
FINBERT_MODEL: str = os.getenv("FINBERT_MODEL", "ProsusAI/finbert")
