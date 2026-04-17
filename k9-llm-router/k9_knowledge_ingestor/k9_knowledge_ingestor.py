"""
k9-knowledge-ingestor — Sprint 6
WSL2 Python service — Port :8767

Closes the RAG loop by:
  1. Polling Supabase tables (trading_signals, cross_module_events, memory_events)
  2. Generating local embeddings via sentence-transformers (all-mpnet-base-v2, 768-dim)
  3. Upserting into ict_pattern_knowledge (embedding_local column)
  4. Optionally watching a local Obsidian vault for .md file changes

Tables polled:
  - trading_signals       → pattern_type = 'trading_signal'
  - cross_module_events   → pattern_type = 'cross_module_event'
  - memory_events (Mongo) → pattern_type = 'memory_event'

Supabase target:
  - Table: ict_pattern_knowledge
  - Column: embedding_local vector(768)
  - Function: search_similar_patterns_local(query_embedding, threshold, count)

Run:
  python k9_knowledge_ingestor.py

Env:
  SUPABASE_URL           — https://ziqenqqgnqxqrazmjohs.supabase.co
  SUPABASE_SERVICE_KEY   — service role key
  MONGO_URL              — mongodb://localhost:27017 (PackAI leader DB)
  OBSIDIAN_VAULT_PATH    — optional: /home/novum/obsidian-vault
  INGEST_INTERVAL_SECS   — default 300 (5 min)
  EMBEDDING_MODEL        — default all-mpnet-base-v2
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

SUPABASE_URL        = os.getenv("SUPABASE_URL", "https://ziqenqqgnqxqrazmjohs.supabase.co")
SUPABASE_KEY        = os.getenv("SUPABASE_SERVICE_KEY", os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""))
MONGO_URL           = os.getenv("MONGO_URL", "mongodb://localhost:27017")
OBSIDIAN_VAULT      = os.getenv("OBSIDIAN_VAULT_PATH", "")
INTERVAL            = int(os.getenv("INGEST_INTERVAL_SECS", "300"))
MODEL_NAME          = os.getenv("EMBEDDING_MODEL", "all-mpnet-base-v2")
LOOKBACK_MINUTES    = int(os.getenv("LOOKBACK_MINUTES", "60"))  # how far back to fetch on each cycle

log = logging.getLogger("k9-ingestor")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

# ── Embedder ──────────────────────────────────────────────────────────────────

class LocalEmbedder:
    """Lazy-loaded sentence-transformers embedder (768-dim, free, local)"""

    def __init__(self, model_name: str = MODEL_NAME):
        self._model_name = model_name
        self._model = None

    def _load(self):
        if self._model is None:
            log.info("Loading embedding model: %s", self._model_name)
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self._model_name)
            log.info("Model loaded ✅ dim=%d", self._model.get_sentence_embedding_dimension())

    def embed(self, text: str) -> list[float]:
        self._load()
        vec = self._model.encode(text, show_progress_bar=False)
        return vec.tolist()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self._load()
        vecs = self._model.encode(texts, show_progress_bar=False, batch_size=32)
        return [v.tolist() for v in vecs]


embedder = LocalEmbedder()

# ── Supabase helpers ───────────────────────────────────────────────────────────

def _supa_headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }

async def supa_fetch(client: httpx.AsyncClient, table: str, params: dict) -> list[dict]:
    r = await client.get(
        f"{SUPABASE_URL}/rest/v1/{table}",
        params=params,
        headers=_supa_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()

async def supa_upsert(client: httpx.AsyncClient, rows: list[dict]) -> bool:
    """Upsert rows into ict_pattern_knowledge"""
    r = await client.post(
        f"{SUPABASE_URL}/rest/v1/ict_pattern_knowledge",
        json=rows,
        headers={**_supa_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
        timeout=30,
    )
    if r.status_code not in (200, 201, 204):
        log.warning("Upsert failed: %d %s", r.status_code, r.text[:300])
        return False
    return True

# ── Dedup helper ──────────────────────────────────────────────────────────────

def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:24]

# ── Ingest: trading_signals ───────────────────────────────────────────────────

async def ingest_trading_signals(client: httpx.AsyncClient) -> int:
    since = (datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)).isoformat()
    rows = await supa_fetch(client, "trading_signals", {
        "select": "id,signal_type,asset,confidence,reasoning,fed_stance,risk_level,metadata,created_at",
        "created_at": f"gte.{since}",
        "order": "created_at.desc",
        "limit": "100",
    })

    if not rows:
        return 0

    texts, patterns = [], []
    for row in rows:
        text = (
            f"Signal: {row.get('signal_type','?')} {row.get('asset','?')} "
            f"Confidence: {row.get('confidence','?')}% "
            f"Reasoning: {row.get('reasoning','?')} "
            f"FedStance: {row.get('fed_stance','?')} "
            f"Risk: {row.get('risk_level','?')}"
        )
        texts.append(text)
        patterns.append({
            "pattern_name": f"signal_{row.get('asset','?')}_{row.get('signal_type','?')}_{row['id'][:8]}",
            "pattern_type": "trading_signal",
            "symbols": [row.get("asset", "UNKNOWN")],
            "regime": row.get("fed_stance", "NEUTRAL"),
            "outcome": "active",
            "confidence_score": (row.get("confidence") or 50) / 100,
            "metadata": {
                "source_id": row["id"],
                "source_table": "trading_signals",
                "risk_level": row.get("risk_level"),
                **(row.get("metadata") or {}),
            },
            "description": text[:500],
        })

    embeddings = embedder.embed_batch(texts)
    upsert_rows = []
    for p, emb in zip(patterns, embeddings):
        upsert_rows.append({**p, "embedding_local": emb})

    ok = await supa_upsert(client, upsert_rows)
    count = len(upsert_rows) if ok else 0
    log.info("trading_signals → %d patterns upserted", count)
    return count

# ── Ingest: cross_module_events ───────────────────────────────────────────────

async def ingest_cross_module_events(client: httpx.AsyncClient) -> int:
    since = (datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)).isoformat()
    rows = await supa_fetch(client, "cross_module_events", {
        "select": "id,source_module,target_module,event_type,payload,created_at",
        "created_at": f"gte.{since}",
        "order": "created_at.desc",
        "limit": "200",
    })

    if not rows:
        return 0

    # Batch by source_module + event_type (avoid embedding noise for every minor event)
    # Only embed meaningful event types
    EMBED_TYPES = {
        "SIGNAL_GENERATED", "FED_ALIGNMENT_SIGNAL", "voice.command",
        "REGIME_CHANGED", "PATTERN_DETECTED", "ALERT", "K9_ACTION",
    }
    rows = [r for r in rows if r.get("event_type") in EMBED_TYPES]
    if not rows:
        return 0

    texts, patterns = [], []
    for row in rows:
        payload_str = json.dumps(row.get("payload") or {}, default=str)[:400]
        text = (
            f"Event: {row.get('event_type','?')} "
            f"from {row.get('source_module','?')} "
            f"to {row.get('target_module','?')} "
            f"payload: {payload_str}"
        )
        texts.append(text)
        patterns.append({
            "pattern_name": f"event_{row.get('event_type','?')}_{row['id'][:8]}",
            "pattern_type": "cross_module_event",
            "symbols": [],
            "outcome": "logged",
            "confidence_score": 0.60,
            "metadata": {
                "source_id": row["id"],
                "source_table": "cross_module_events",
                "source_module": row.get("source_module"),
                "event_type": row.get("event_type"),
            },
            "description": text[:500],
        })

    embeddings = embedder.embed_batch(texts)
    upsert_rows = [{**p, "embedding_local": emb} for p, emb in zip(patterns, embeddings)]
    ok = await supa_upsert(client, upsert_rows)
    count = len(upsert_rows) if ok else 0
    log.info("cross_module_events → %d events upserted", count)
    return count

# ── Ingest: PackAI memory_events (MongoDB) ────────────────────────────────────

async def ingest_memory_events(client: httpx.AsyncClient) -> int:
    """
    Reads memory_events from PackAI leader MongoDB,
    generates embeddings, and upserts to ict_pattern_knowledge.
    Requires motor (async MongoDB driver).
    """
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
        mongo = AsyncIOMotorClient(MONGO_URL, serverSelectionTimeoutMS=3000)
        db = mongo.packai_db
        since = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)
        cursor = db.memory_events.find(
            {"timestamp": {"$gte": since}, "embedding": None}
        ).limit(100)
        rows = await cursor.to_list(length=100)
    except Exception as e:
        log.debug("MongoDB unavailable (normal if PackAI not running): %s", e)
        return 0

    if not rows:
        return 0

    texts, patterns, ids = [], [], []
    for row in rows:
        text = f"MemoryEvent: {row.get('event_type','?')} — {row.get('text','')[:400]}"
        texts.append(text)
        ids.append(row["_id"])
        patterns.append({
            "pattern_name": f"memory_{row.get('event_type','?')}_{str(row['_id'])[:8]}",
            "pattern_type": "memory_event",
            "symbols": [],
            "outcome": "logged",
            "confidence_score": 0.65,
            "metadata": {
                "source_id": str(row["_id"]),
                "source_table": "memory_events",
                "event_type": row.get("event_type"),
                **(row.get("metadata") or {}),
            },
            "description": text[:500],
        })

    embeddings = embedder.embed_batch(texts)
    upsert_rows = [{**p, "embedding_local": emb} for p, emb in zip(patterns, embeddings)]
    ok = await supa_upsert(client, upsert_rows)
    count = len(upsert_rows) if ok else 0

    if ok:
        # Back-patch MongoDB records with embedding flag
        try:
            from motor.motor_asyncio import AsyncIOMotorClient
            mongo = AsyncIOMotorClient(MONGO_URL, serverSelectionTimeoutMS=3000)
            db = mongo.packai_db
            await db.memory_events.update_many(
                {"_id": {"$in": ids}},
                {"$set": {"embedding": "indexed", "embedding_indexed_at": datetime.now(timezone.utc)}},
            )
        except Exception:
            pass

    log.info("memory_events → %d events upserted", count)
    return count

# ── Ingest: Obsidian vault markdown files ─────────────────────────────────────

MARKDOWN_CHUNK_RE = re.compile(r"^#{1,2} .+", re.MULTILINE)

def _chunk_markdown(text: str, max_tokens: int = 512) -> list[str]:
    """Split markdown by H1/H2 headers, max ~512 words per chunk"""
    sections = MARKDOWN_CHUNK_RE.split(text)
    headers = MARKDOWN_CHUNK_RE.findall(text)
    chunks = []
    for i, section in enumerate(sections):
        if not section.strip():
            continue
        header = headers[i - 1] if i > 0 and i - 1 < len(headers) else ""
        words = section.split()
        # Sub-chunk if too large
        for j in range(0, max(1, len(words)), max_tokens):
            chunk_words = words[j:j + max_tokens]
            chunks.append(f"{header}\n{' '.join(chunk_words)}".strip())
    return chunks or [text[:2000]]

async def ingest_obsidian(client: httpx.AsyncClient, vault_path: str) -> int:
    vault = Path(vault_path)
    if not vault.exists():
        log.debug("Obsidian vault not found: %s", vault_path)
        return 0

    md_files = list(vault.rglob("*.md"))
    if not md_files:
        return 0

    log.info("Obsidian: %d markdown files found", len(md_files))

    all_texts, all_patterns = [], []
    for md_file in md_files:
        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception:
            continue

        chunks = _chunk_markdown(content)
        for i, chunk in enumerate(chunks):
            if len(chunk.strip()) < 50:
                continue
            h = _content_hash(chunk)
            all_texts.append(chunk)
            all_patterns.append({
                "pattern_name": f"obsidian_{md_file.stem}_{i}_{h}",
                "pattern_type": "operator_knowledge",
                "symbols": [],
                "outcome": "reference",
                "confidence_score": 0.75,
                "metadata": {
                    "source": "obsidian_vault",
                    "file": md_file.name,
                    "chunk_index": i,
                    "content_hash": h,
                },
                "description": chunk[:500],
            })

    if not all_texts:
        return 0

    embeddings = embedder.embed_batch(all_texts)
    upsert_rows = [{**p, "embedding_local": emb} for p, emb in zip(all_patterns, embeddings)]
    ok = await supa_upsert(client, upsert_rows)
    count = len(upsert_rows) if ok else 0
    log.info("Obsidian vault → %d chunks upserted", count)
    return count

# ── Main loop ─────────────────────────────────────────────────────────────────

async def run_cycle() -> dict[str, int]:
    totals: dict[str, int] = {}
    async with httpx.AsyncClient() as client:
        totals["trading_signals"]     = await ingest_trading_signals(client)
        totals["cross_module_events"] = await ingest_cross_module_events(client)
        totals["memory_events"]       = await ingest_memory_events(client)
        if OBSIDIAN_VAULT:
            totals["obsidian"] = await ingest_obsidian(client, OBSIDIAN_VAULT)
    return totals

async def main():
    log.info("🧠 k9-knowledge-ingestor starting — interval=%ds model=%s", INTERVAL, MODEL_NAME)
    log.info("Supabase: %s", SUPABASE_URL)
    if OBSIDIAN_VAULT:
        log.info("Obsidian vault: %s", OBSIDIAN_VAULT)
    else:
        log.info("Obsidian vault: not configured (set OBSIDIAN_VAULT_PATH to enable)")

    while True:
        start = time.monotonic()
        try:
            totals = await run_cycle()
            elapsed = time.monotonic() - start
            log.info(
                "✅ Cycle complete in %.1fs — signals:%d events:%d memory:%d obsidian:%s",
                elapsed,
                totals.get("trading_signals", 0),
                totals.get("cross_module_events", 0),
                totals.get("memory_events", 0),
                totals.get("obsidian", "off"),
            )
        except Exception as e:
            log.error("Cycle error: %s", e, exc_info=True)

        await asyncio.sleep(INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
