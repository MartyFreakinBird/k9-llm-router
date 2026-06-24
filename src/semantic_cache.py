"""
semantic_cache.py — K-9 Semantic Cache Layer
─────────────────────────────────────────────────────────────────────────────
Sprint CB-2 · k9-llm-router

Reduces Anthropic/Gemini/OpenAI cost and latency by caching semantically
similar prompts. Sits between the /route handler and model backends.

Strategy:
  - Embed incoming prompt with a local sentence-transformer (no API cost)
  - Search FAISS index for cosine similarity ≥ threshold (default 0.92)
  - On hit: return cached response instantly (no LLM call)
  - On miss: call LLM, store result in Redis + FAISS
  - FAISS index is rebuilt from Redis on startup (survives restarts)

Dependencies (add to requirements.txt):
  faiss-cpu>=1.8.0
  sentence-transformers>=3.0.0
  redis>=5.0.1
  numpy>=1.26.0

Env vars:
  REDIS_HOST         default: localhost
  REDIS_PORT         default: 6379
  CACHE_THRESHOLD    cosine similarity threshold, default: 0.92
  CACHE_TTL          seconds, default: 3600
  CACHE_ENABLED      "true" | "false", default: true
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("k9-semantic-cache")

# ── Config ────────────────────────────────────────────────────────────────────

REDIS_HOST      = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT      = int(os.getenv("REDIS_PORT", "6379"))
CACHE_THRESHOLD = float(os.getenv("CACHE_THRESHOLD", "0.92"))
CACHE_TTL       = int(os.getenv("CACHE_TTL", "3600"))
CACHE_ENABLED   = os.getenv("CACHE_ENABLED", "true").lower() == "true"

# Embedding dim for all-MiniLM-L6-v2
EMBED_DIM = 384


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class CacheEntry:
    prompt_hash:  str
    prompt:       str
    response:     str
    model_used:   str
    task_type:    str
    latency_ms:   float
    tokens_used:  int
    hits:         int = 0
    created_at:   float = field(default_factory=time.time)


@dataclass
class CacheResult:
    hit:         bool
    response:    Optional[str]   = None
    model_used:  Optional[str]   = None
    latency_ms:  Optional[float] = None
    similarity:  Optional[float] = None
    cache_key:   Optional[str]   = None
    saved_ms:    Optional[float] = None   # estimated latency saved


# ── Lazy imports (avoid hard failure if deps not installed) ───────────────────

def _import_deps():
    """Returns (faiss, np, SentenceTransformer) or raises ImportError with install hint."""
    try:
        import faiss
        import numpy as np
        from sentence_transformers import SentenceTransformer
        return faiss, np, SentenceTransformer
    except ImportError as e:
        raise ImportError(
            f"Semantic cache deps missing: {e}. "
            "Run: pip install faiss-cpu sentence-transformers numpy redis"
        ) from e


def _import_redis():
    try:
        import redis
        return redis
    except ImportError as e:
        raise ImportError(f"Redis client missing: {e}. Run: pip install redis") from e


# ── Cache implementation ──────────────────────────────────────────────────────

class SemanticCache:
    """
    FAISS + Redis semantic cache for LLM responses.

    Usage:
        cache = SemanticCache()
        cache.initialize()    # call once at app startup

        result = cache.get(prompt, task_type)
        if result.hit:
            return result.response

        # ... call LLM ...
        cache.set(prompt, response, model_used, task_type, latency_ms, tokens)
    """

    def __init__(
        self,
        threshold: float = CACHE_THRESHOLD,
        ttl: int = CACHE_TTL,
        enabled: bool = CACHE_ENABLED,
    ) -> None:
        self.threshold = threshold
        self.ttl       = ttl
        self.enabled   = enabled
        self._ready    = False

        # Initialized in initialize()
        self._faiss      = None
        self._np         = None
        self._encoder    = None
        self._redis      = None
        self._index      = None
        self._id_map: list[str] = []   # FAISS slot → prompt_hash

        # Stats
        self._hits    = 0
        self._misses  = 0
        self._sets    = 0
        self._errors  = 0

    def initialize(self) -> None:
        """Load deps and rebuild FAISS index from Redis. Call at app startup."""
        if not self.enabled:
            log.info("[cache] Semantic cache disabled via CACHE_ENABLED=false")
            return
        try:
            faiss, np, SentenceTransformer = _import_deps()
            redis_lib = _import_redis()

            self._faiss   = faiss
            self._np      = np
            self._encoder = SentenceTransformer("all-MiniLM-L6-v2")
            self._redis   = redis_lib.Redis(
                host=REDIS_HOST, port=REDIS_PORT,
                decode_responses=False,   # binary for embeddings
                socket_connect_timeout=3,
            )
            self._redis.ping()

            # Inner product index (for normalized vectors = cosine similarity)
            self._index  = faiss.IndexFlatIP(EMBED_DIM)
            self._id_map = []

            self._rebuild_index_from_redis()
            self._ready = True
            log.info(
                "[cache] ✅ Semantic cache ready — %d entries, threshold=%.2f, TTL=%ds",
                len(self._id_map), self.threshold, self.ttl
            )
        except Exception as e:
            self._ready = False
            log.warning("[cache] ⚠️  Semantic cache unavailable: %s — routing will bypass cache", e)

    def _rebuild_index_from_redis(self) -> None:
        """On startup, reload all cached embeddings from Redis into FAISS."""
        pattern = b"k9cache:emb:*"
        cursor = 0
        loaded = 0
        while True:
            cursor, keys = self._redis.scan(cursor, match=pattern, count=200)
            for key in keys:
                raw = self._redis.get(key)
                if raw:
                    try:
                        emb = self._np.frombuffer(raw, dtype="float32")
                        prompt_hash = key.decode().replace("k9cache:emb:", "")
                        self._index.add(emb.reshape(1, -1))
                        self._id_map.append(prompt_hash)
                        loaded += 1
                    except Exception:
                        pass
            if cursor == 0:
                break
        log.info("[cache] Rebuilt FAISS index from Redis: %d entries", loaded)

    def _embed(self, text: str) -> "np.ndarray":
        """Embed text and L2-normalize for cosine similarity via inner product."""
        emb = self._encoder.encode([text], normalize_embeddings=True)[0]
        return emb.astype("float32")

    def _prompt_hash(self, prompt: str, task_type: str) -> str:
        return hashlib.sha256(f"{task_type}:{prompt}".encode()).hexdigest()[:16]

    def get(self, prompt: str, task_type: str = "default") -> CacheResult:
        """Look up a prompt. Returns CacheResult with hit=True if found."""
        if not self._ready or not self.enabled:
            return CacheResult(hit=False)

        try:
            emb = self._embed(prompt)

            if self._index.ntotal == 0:
                self._misses += 1
                return CacheResult(hit=False)

            D, I = self._index.search(emb.reshape(1, -1), 1)
            similarity = float(D[0][0])
            idx        = int(I[0][0])

            if similarity < self.threshold:
                self._misses += 1
                return CacheResult(hit=False, similarity=similarity)

            prompt_hash = self._id_map[idx]
            raw = self._redis.get(f"k9cache:resp:{prompt_hash}")
            if not raw:
                # Entry expired or evicted — clean up FAISS slot
                self._misses += 1
                return CacheResult(hit=False, similarity=similarity)

            entry_data = json.loads(raw.decode())

            # Increment hit counter
            try:
                self._redis.hincrby(f"k9cache:stats:{prompt_hash}", "hits", 1)
            except Exception:
                pass

            self._hits += 1
            log.info(
                "[cache] HIT similarity=%.4f hash=%s model=%s",
                similarity, prompt_hash, entry_data.get("model_used", "?")
            )

            return CacheResult(
                hit        = True,
                response   = entry_data["response"],
                model_used = entry_data.get("model_used", "cached"),
                latency_ms = entry_data.get("latency_ms", 0.0),
                similarity = similarity,
                cache_key  = prompt_hash,
                saved_ms   = entry_data.get("latency_ms", 800.0),  # estimated saving
            )

        except Exception as e:
            self._errors += 1
            log.warning("[cache] GET error: %s", e)
            return CacheResult(hit=False)

    def set(
        self,
        prompt:     str,
        response:   str,
        model_used: str,
        task_type:  str   = "default",
        latency_ms: float = 0.0,
        tokens:     int   = 0,
    ) -> bool:
        """Store a new response. Returns True on success."""
        if not self._ready or not self.enabled:
            return False

        try:
            prompt_hash = self._prompt_hash(prompt, task_type)
            emb = self._embed(prompt)

            # Check if already indexed (avoid duplicates)
            if self._index.ntotal > 0:
                D, I = self._index.search(emb.reshape(1, -1), 1)
                if float(D[0][0]) >= 0.999:
                    # Already exists — just refresh TTL
                    self._redis.expire(f"k9cache:resp:{prompt_hash}", self.ttl)
                    return True

            # Store response JSON in Redis
            entry = {
                "prompt_hash": prompt_hash,
                "response":    response,
                "model_used":  model_used,
                "task_type":   task_type,
                "latency_ms":  latency_ms,
                "tokens_used": tokens,
                "created_at":  time.time(),
            }
            self._redis.setex(
                f"k9cache:resp:{prompt_hash}",
                self.ttl,
                json.dumps(entry).encode()
            )

            # Store embedding as binary for index rebuild on restart
            self._redis.setex(
                f"k9cache:emb:{prompt_hash}",
                self.ttl + 60,     # slightly longer than response TTL
                emb.tobytes()
            )

            # Add to live FAISS index
            self._index.add(emb.reshape(1, -1))
            self._id_map.append(prompt_hash)

            self._sets += 1
            log.info(
                "[cache] SET hash=%s model=%s task=%s tokens=%d",
                prompt_hash, model_used, task_type, tokens
            )
            return True

        except Exception as e:
            self._errors += 1
            log.warning("[cache] SET error: %s", e)
            return False

    def invalidate(self, prompt_hash: str) -> bool:
        """Manually invalidate a specific cache entry."""
        if not self._ready:
            return False
        try:
            self._redis.delete(
                f"k9cache:resp:{prompt_hash}",
                f"k9cache:emb:{prompt_hash}",
                f"k9cache:stats:{prompt_hash}",
            )
            # Remove from FAISS id_map (mark as tombstone — rebuilt on next restart)
            if prompt_hash in self._id_map:
                self._id_map[self._id_map.index(prompt_hash)] = "__deleted__"
            return True
        except Exception:
            return False

    def stats(self) -> dict:
        return {
            "enabled":      self.enabled,
            "ready":        self._ready,
            "index_size":   self._index.ntotal if self._index else 0,
            "hits":         self._hits,
            "misses":       self._misses,
            "sets":         self._sets,
            "errors":       self._errors,
            "hit_rate":     round(self._hits / max(1, self._hits + self._misses), 3),
            "threshold":    self.threshold,
            "ttl_seconds":  self.ttl,
            "redis_host":   REDIS_HOST,
        }

    def flush(self) -> int:
        """Flush all cache entries. Returns count deleted."""
        if not self._ready:
            return 0
        try:
            cursor, count = 0, 0
            while True:
                cursor, keys = self._redis.scan(cursor, match=b"k9cache:*", count=200)
                if keys:
                    self._redis.delete(*keys)
                    count += len(keys)
                if cursor == 0:
                    break
            # Reset FAISS
            self._index = self._faiss.IndexFlatIP(EMBED_DIM)
            self._id_map = []
            log.info("[cache] Flushed %d entries", count)
            return count
        except Exception as e:
            log.warning("[cache] Flush error: %s", e)
            return 0


# ── Module-level singleton (imported by main.py) ──────────────────────────────
semantic_cache = SemanticCache()
