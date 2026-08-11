"""
Data gathering layer for K-9 Sentiment Engine.
Handles scraping Reddit public endpoints and Google News RSS feeds,
with rate limiting, UA rotation, text sanitization, and caching.
"""
import time
import html
import urllib.parse
import threading
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

import requests
import feedparser
import dateutil.parser
from bs4 import BeautifulSoup

import config


class StatsManager:
    """Tracks overall statistics for the sentiment engine service."""
    def __init__(self):
        self.start_time: float = time.time()
        self.total_requests: int = 0
        self.cache_hits: int = 0
        self.cache_misses: int = 0
        self.total_posts_analyzed: int = 0

    @property
    def uptime_seconds(self) -> float:
        return round(time.time() - self.start_time, 2)


stats = StatsManager()


def clean_text(text: Optional[str]) -> str:
    """
    Sanitize and clean text by stripping HTML tags, unescaping HTML entities,
    and trimming whitespace.
    """
    if not text:
        return ""
    # Unescape HTML entities
    unescaped = html.unescape(str(text))
    # Strip HTML tags
    soup = BeautifulSoup(unescaped, "html.parser")
    cleaned = soup.get_text(separator=" ")
    # Normalize whitespace
    words = cleaned.split()
    return " ".join(words).strip()


def format_timestamp(val: Any) -> str:
    """
    Convert timestamp/date representation to ISO-8601 UTC string format (YYYY-MM-DDTHH:MM:SSZ).
    """
    if val is None:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(val, (int, float)):
        try:
            return datetime.fromtimestamp(val, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(val, str):
        try:
            dt = dateutil.parser.parse(val)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(val, time.struct_time):
        try:
            dt = datetime(*val[:6], tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class RateLimiter:
    """Thread-safe rate limiter enforcing minimum delays per source."""
    def __init__(self, delay_seconds: float = 2.0):
        self.delay = delay_seconds
        self.last_calls: Dict[str, float] = {}
        self.lock = threading.Lock()

    def wait_if_needed(self, source: str) -> None:
        with self.lock:
            now = time.time()
            last = self.last_calls.get(source, 0.0)
            elapsed = now - last
            if elapsed < self.delay:
                time.sleep(self.delay - elapsed)
            self.last_calls[source] = time.time()


class CacheManager:
    """Thread-safe in-memory cache with TTL expiration."""
    def __init__(self, ttl: int = 60):
        self.ttl = ttl
        self._cache: Dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key in self._cache:
                timestamp, data = self._cache[key]
                if time.time() - timestamp < self.ttl:
                    return data
                else:
                    del self._cache[key]
        return None

    def set(self, key: str, data: Any) -> None:
        with self._lock:
            self._cache[key] = (time.time(), data)


class RedditScraper:
    """Scrapes public Reddit data without requiring PRAW or API keys."""
    def __init__(self):
        self.rate_limiter = RateLimiter(config.RATE_LIMIT_DELAY)
        self.cache = CacheManager(config.CACHE_TTL)
        self._ua_index = 0
        self._lock = threading.Lock()

    def _get_ua(self) -> str:
        with self._lock:
            ua = config.USER_AGENTS[self._ua_index % len(config.USER_AGENTS)]
            self._ua_index += 1
            return ua

    def fetch_subreddit_posts(self, subreddit: str, sort: str = "hot", limit: int = 25) -> List[Dict[str, Any]]:
        """Fetch posts from a given subreddit using public endpoints with RSS fallback."""
        clean_sub = subreddit.strip().lstrip("r/").lower()
        sort_mode = sort.lower() if sort.lower() in ["hot", "new", "top"] else "hot"
        cache_key = f"reddit:{clean_sub}:{sort_mode}:{limit}"

        cached = self.cache.get(cache_key)
        if cached is not None:
            stats.cache_hits += 1
            return cached
        stats.cache_misses += 1

        self.rate_limiter.wait_if_needed("reddit")

        headers = {
            "User-Agent": self._get_ua(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5"
        }

        url = f"{config.REDDIT_BASE}/r/{clean_sub}/{sort_mode}.json?limit={limit}"
        posts = []

        try:
            resp = requests.get(url, headers=headers, timeout=8)
            if resp.status_code == 200 and "json" in resp.headers.get("Content-Type", ""):
                data = resp.json()
                children = data.get("data", {}).get("children", [])
                for child in children[:limit]:
                    pdata = child.get("data", {})
                    permalink = pdata.get("permalink", "")
                    post_url = f"{config.REDDIT_BASE}{permalink}" if permalink else pdata.get("url", "")

                    raw_text = pdata.get("selftext", "") or ""
                    cleaned_text = clean_text(raw_text)
                    if len(cleaned_text) > 1000:
                        cleaned_text = cleaned_text[:1000] + "..."

                    posts.append({
                        "id": pdata.get("id", ""),
                        "title": clean_text(pdata.get("title", "")),
                        "text": cleaned_text,
                        "author": pdata.get("author", "anonymous"),
                        "url": post_url,
                        "score": float(pdata.get("score", 0)),
                        "num_comments": int(pdata.get("num_comments", 0)),
                        "created_utc": format_timestamp(pdata.get("created_utc")),
                        "subreddit": clean_sub
                    })
            else:
                posts = self._fetch_subreddit_rss(clean_sub, sort_mode, limit)
        except Exception:
            posts = self._fetch_subreddit_rss(clean_sub, sort_mode, limit)

        self.cache.set(cache_key, posts)
        return posts

    def _fetch_subreddit_rss(self, subreddit: str, sort: str = "hot", limit: int = 25) -> List[Dict[str, Any]]:
        posts = []
        try:
            rss_url = f"{config.REDDIT_BASE}/r/{subreddit}/{sort}.rss?limit={limit}"
            headers = {"User-Agent": self._get_ua()}
            resp = requests.get(rss_url, headers=headers, timeout=8)
            if resp.status_code == 200:
                feed = feedparser.parse(resp.text)
                for entry in feed.entries[:limit]:
                    raw_content = ""
                    if hasattr(entry, "content") and entry.content:
                        raw_content = entry.content[0].value
                    elif hasattr(entry, "summary"):
                        raw_content = entry.summary

                    cleaned_text = clean_text(raw_content)
                    if len(cleaned_text) > 1000:
                        cleaned_text = cleaned_text[:1000] + "..."

                    posts.append({
                        "id": getattr(entry, "id", getattr(entry, "link", "")),
                        "title": clean_text(getattr(entry, "title", "")),
                        "text": cleaned_text,
                        "author": getattr(entry, "author", f"r/{subreddit}"),
                        "url": getattr(entry, "link", ""),
                        "score": 1.0,
                        "num_comments": 0,
                        "created_utc": format_timestamp(getattr(entry, "updated", getattr(entry, "published", None))),
                        "subreddit": subreddit
                    })
        except Exception:
            pass
        return posts

    def fetch_user_posts(self, username: str, limit: int = 25) -> List[Dict[str, Any]]:
        """Fetch posts submitted by a specific user profile."""
        clean_user = username.strip().lstrip("u/").lstrip("user/")
        cache_key = f"reddit_user:{clean_user.lower()}:{limit}"

        cached = self.cache.get(cache_key)
        if cached is not None:
            stats.cache_hits += 1
            return cached
        stats.cache_misses += 1

        self.rate_limiter.wait_if_needed("reddit")

        headers = {"User-Agent": self._get_ua()}
        url = f"{config.REDDIT_BASE}/user/{clean_user}/submitted.json?limit={limit}"
        posts = []

        try:
            resp = requests.get(url, headers=headers, timeout=8)
            if resp.status_code == 200 and "json" in resp.headers.get("Content-Type", ""):
                data = resp.json()
                children = data.get("data", {}).get("children", [])
                for child in children[:limit]:
                    pdata = child.get("data", {})
                    permalink = pdata.get("permalink", "")
                    post_url = f"{config.REDDIT_BASE}{permalink}" if permalink else pdata.get("url", "")

                    raw_text = pdata.get("selftext", "") or ""
                    cleaned_text = clean_text(raw_text)
                    if len(cleaned_text) > 1000:
                        cleaned_text = cleaned_text[:1000] + "..."

                    posts.append({
                        "id": pdata.get("id", ""),
                        "title": clean_text(pdata.get("title", "")),
                        "text": cleaned_text,
                        "author": clean_user,
                        "url": post_url,
                        "score": float(pdata.get("score", 0)),
                        "num_comments": int(pdata.get("num_comments", 0)),
                        "created_utc": format_timestamp(pdata.get("created_utc")),
                        "subreddit": pdata.get("subreddit", "")
                    })
            else:
                posts = self._fetch_user_rss(clean_user, limit)
        except Exception:
            posts = self._fetch_user_rss(clean_user, limit)

        self.cache.set(cache_key, posts)
        return posts

    def _fetch_user_rss(self, username: str, limit: int = 25) -> List[Dict[str, Any]]:
        posts = []
        try:
            rss_url = f"{config.REDDIT_BASE}/user/{username}/submitted.rss"
            headers = {"User-Agent": self._get_ua()}
            resp = requests.get(rss_url, headers=headers, timeout=8)
            if resp.status_code == 200:
                feed = feedparser.parse(resp.text)
                for entry in feed.entries[:limit]:
                    raw_content = ""
                    if hasattr(entry, "content") and entry.content:
                        raw_content = entry.content[0].value
                    elif hasattr(entry, "summary"):
                        raw_content = entry.summary

                    posts.append({
                        "id": getattr(entry, "id", getattr(entry, "link", "")),
                        "title": clean_text(getattr(entry, "title", "")),
                        "text": clean_text(raw_content)[:1000],
                        "author": username,
                        "url": getattr(entry, "link", ""),
                        "score": 1.0,
                        "num_comments": 0,
                        "created_utc": format_timestamp(getattr(entry, "updated", getattr(entry, "published", None))),
                        "subreddit": ""
                    })
        except Exception:
            pass
        return posts


class NewsScraper:
    """Scrapes Google News RSS feeds."""
    def __init__(self):
        self.rate_limiter = RateLimiter(config.RATE_LIMIT_DELAY)
        self.cache = CacheManager(config.CACHE_TTL)
        self._ua_index = 0
        self._lock = threading.Lock()

    def _get_ua(self) -> str:
        with self._lock:
            ua = config.USER_AGENTS[self._ua_index % len(config.USER_AGENTS)]
            self._ua_index += 1
            return ua

    def fetch_news(self, query: str = "", limit: int = 20) -> List[Dict[str, Any]]:
        """Fetch Google News RSS articles for a search query or top stories."""
        clean_q = query.strip()
        cache_key = f"news:{clean_q.lower()}:{limit}"

        cached = self.cache.get(cache_key)
        if cached is not None:
            stats.cache_hits += 1
            return cached
        stats.cache_misses += 1

        self.rate_limiter.wait_if_needed("news")

        if clean_q:
            url = f"{config.NEWS_RSS}?q={urllib.parse.quote(clean_q)}&hl=en-US&gl=US&ceid=US:en"
        else:
            url = f"{config.NEWS_TOP_STORIES_RSS}?hl=en-US&gl=US&ceid=US:en"

        headers = {"User-Agent": self._get_ua()}
        posts = []

        try:
            resp = requests.get(url, headers=headers, timeout=8)
            if resp.status_code == 200:
                feed = feedparser.parse(resp.text)
                for entry in feed.entries[:limit]:
                    raw_summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
                    cleaned_summary = clean_text(raw_summary)
                    if len(cleaned_summary) > 1000:
                        cleaned_summary = cleaned_summary[:1000] + "..."

                    source_name = ""
                    if hasattr(entry, "source") and isinstance(entry.source, dict):
                        source_name = entry.source.get("title", "")
                    elif hasattr(entry, "source") and hasattr(entry.source, "title"):
                        source_name = getattr(entry.source, "title", "")

                    raw_title = getattr(entry, "title", "")
                    if not source_name and " - " in raw_title:
                        parts = raw_title.rsplit(" - ", 1)
                        title_str = clean_text(parts[0])
                        source_name = clean_text(parts[1])
                    else:
                        title_str = clean_text(raw_title)

                    pub_date = getattr(entry, "published", getattr(entry, "updated", None))

                    posts.append({
                        "id": getattr(entry, "id", getattr(entry, "link", "")),
                        "title": title_str,
                        "text": cleaned_summary,
                        "author": source_name or "Google News",
                        "url": getattr(entry, "link", ""),
                        "score": 1.0,
                        "created_utc": format_timestamp(pub_date)
                    })
        except Exception:
            pass

        self.cache.set(cache_key, posts)
        return posts
