"""
k9-knowledge-ingestor — Sprint 9
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

Sprint 9 additions:
  - FastAPI HTTP server on :8767 (replaces bare asyncio loop)
  - POST /embed            — embed a single text string → float[768]
  - POST /embed_batch      — embed multiple strings → float[768][]
  - GET  /health           — liveness + model status + ingest stats
  - POST /ingest/trigger   — manually trigger one ingest cycle
  - K9_LOCAL_ONLY mode     — writes embeddings to local Postgres (k9_local)
                             instead of Supabase

Run:
  python k9_knowledge_ingestor.py

Env:
  SUPABASE_URL           — https://ziqenqqgnqxqrazmjohs.supabase.co
  SUPABASE_SERVICE_KEY   — service role key
  MONGO_URL              — mongodb://localhost:27017 (PackAI leader DB)
  OBSIDIAN_VAULT_PATH    — optional: /home/novum/obsidian-vault
  INGEST_INTERVAL_SECS   — default 300 (5 min)
  EMBEDDING_MODEL        — default all-mpnet-base-v2
  K9_LOCAL_ONLY          — "true" → write to local Postgres instead of Supabase
  K9_PG_HOST             — default localhost
  K9_PG_PORT             — default 5432
  K9_PG_DB               — default k9_local
  K9_PG_USER             — default postgres
  K9_PG_PASSWORD         — required when K9_LOCAL_ONLY=true
  INGESTOR_PORT          — default 8767
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

# Sprint 9: HTTP server
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn
import threading
import psycopg2
import psycopg2.extras

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

SUPABASE_URL        = os.getenv("SUPABASE_URL", "https://ziqenqqgnqxqrazmjohs.supabase.co")
SUPABASE_KEY        = os.getenv("SUPABASE_SERVICE_KEY", os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""))
MONGO_URL           = os.getenv("MONGO_URL", "mongodb://localhost:27017")
OBSIDIAN_VAULT      = os.getenv("OBSIDIAN_VAULT_PATH", "")
INTERVAL            = int(os.getenv("INGEST_INTERVAL_SECS", "300"))
MODEL_NAME          = os.getenv("EMBEDDING_MODEL", "all-mpnet-base-v2")
LOOKBACK_MINUTES    = int(os.getenv("LOOKBACK_MINUTES", "60"))  # how far back to fetch on each cycle
INGESTOR_PORT       = int(os.getenv("INGESTOR_PORT", "8767"))
LOCAL_ONLY          = os.getenv("K9_LOCAL_ONLY", "false").lower() == "true"
PG_HOST             = os.getenv("K9_PG_HOST", "localhost")
PG_PORT             = int(os.getenv("K9_PG_PORT", "5432"))
PG_DB               = os.getenv("K9_PG_DB", "k9_local")
PG_USER             = os.getenv("K9_PG_USER", "postgres")
PG_PASSWORD         = os.getenv("K9_PG_PASSWORD", "")

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

# ── Ingest stats (for /health endpoint) ──────────────────────────────────────

class IngestStats:
    def __init__(self):
        self.last_cycle_at: str | None = None
        self.last_cycle_elapsed_s: float = 0.0
        self.last_error: str | None = None
        self.total_cycles: int = 0
        self.total_embedded: int = 0
        self.cycle_running: bool = False

stats = IngestStats()

# ── Local Postgres upsert ─────────────────────────────────────────────────────

def pg_upsert_embeddings(rows: list[dict]) -> int:
    """Write embedding rows to local k9_local.embeddings_local table."""
    if not rows:
        return 0
    conn = psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB,
        user=PG_USER, password=PG_PASSWORD
    )
    try:
        with conn:
            with conn.cursor() as cur:
                for row in rows:
                    emb_str = "[" + ",".join(str(v) for v in row["embedding_local"]) + "]"
                    cur.execute(
                        """
                        INSERT INTO embeddings_local
                          (content, pattern_type, embedding, metadata, source)
                        VALUES (%s, %s, %s::vector, %s, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        [
                            row.get("content", ""),
                            row.get("pattern_type", "unknown"),
                            emb_str,
                            json.dumps(row.get("metadata", {})),
                            row.get("source", "ingestor"),
                        ]
                    )
        return len(rows)
    finally:
        conn.close()

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

    if LOCAL_ONLY:
            ok = pg_upsert_embeddings(upsert_rows) > 0
        else:
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
    if LOCAL_ONLY:
            ok = pg_upsert_embeddings(upsert_rows) > 0
        else:
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
    if LOCAL_ONLY:
            ok = pg_upsert_embeddings(upsert_rows) > 0
        else:
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
    if LOCAL_ONLY:
            ok = pg_upsert_embeddings(upsert_rows) > 0
        else:
            ok = await supa_upsert(client, upsert_rows)
    count = len(upsert_rows) if ok else 0
    log.info("Obsidian vault → %d chunks upserted", count)
    return count

# ── Main ingest loop ─────────────────────────────────────────────────────────

async def run_cycle() -> dict[str, int]:
    totals: dict[str, int] = {}
    async with httpx.AsyncClient() as client:
        totals["trading_signals"]     = await ingest_trading_signals(client)
        totals["cross_module_events"] = await ingest_cross_module_events(client)
        totals["memory_events"]       = await ingest_memory_events(client)
        if OBSIDIAN_VAULT:
            totals["obsidian"] = await ingest_obsidian(client, OBSIDIAN_VAULT)
    return totals

async def ingest_loop():
    log.info("🧠 k9-knowledge-ingestor starting — interval=%ds model=%s port=%d",
             INTERVAL, MODEL_NAME, INGESTOR_PORT)
    log.info("Local-only mode: %s | DB: %s@%s:%d/%s", LOCAL_ONLY, PG_USER, PG_HOST, PG_PORT, PG_DB)
    if OBSIDIAN_VAULT:
        log.info("Obsidian vault: %s", OBSIDIAN_VAULT)

    while True:
        start = time.monotonic()
        stats.cycle_running = True
        try:
            totals = await run_cycle()
            elapsed = time.monotonic() - start
            stats.last_cycle_at = datetime.now(timezone.utc).isoformat()
            stats.last_cycle_elapsed_s = round(elapsed, 2)
            stats.last_error = None
            stats.total_cycles += 1
            stats.total_embedded += sum(totals.values())
            log.info(
                "✅ Cycle %d in %.1fs — signals:%d events:%d memory:%d obsidian:%s",
                stats.total_cycles, elapsed,
                totals.get("trading_signals", 0),
                totals.get("cross_module_events", 0),
                totals.get("memory_events", 0),
                totals.get("obsidian", "off"),
            )
        except Exception as e:
            stats.last_error = str(e)
            log.error("Cycle error: %s", e, exc_info=True)
        finally:
            stats.cycle_running = False
        await asyncio.sleep(INTERVAL)

# ── FastAPI HTTP server (Sprint 9) ────────────────────────────────────────────

app_http = FastAPI(title="k9-knowledge-ingestor", version="0.9.0")

class EmbedRequest(BaseModel):
    text: str

class EmbedBatchRequest(BaseModel):
    texts: list[str]

class EmbedResponse(BaseModel):
    embedding: list[float]
    dim: int
    model: str

class EmbedBatchResponse(BaseModel):
    embeddings: list[list[float]]
    count: int
    dim: int
    model: str

@app_http.post("/embed", response_model=EmbedResponse)
def embed_single(req: EmbedRequest):
    """
    Embed a single text string using local sentence-transformers model.
    Returns float[768] vector.
    Used by control-plane /rag/query to replace stub zero-vector.
    """
    if not req.text or not req.text.strip():
        raise HTTPException(status_code=400, detail="text must be non-empty")
    try:
        vec = embedder.embed(req.text.strip())
        return EmbedResponse(embedding=vec, dim=len(vec), model=MODEL_NAME)
    except Exception as e:
        log.error("Embed error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app_http.post("/embed_batch", response_model=EmbedBatchResponse)
def embed_batch(req: EmbedBatchRequest):
    """Embed multiple texts. Max 64 per request."""
    if not req.texts:
        raise HTTPException(status_code=400, detail="texts list must be non-empty")
    if len(req.texts) > 64:
        raise HTTPException(status_code=400, detail="max 64 texts per batch")
    try:
        vecs = embedder.embed_batch([t.strip() for t in req.texts if t.strip()])
        return EmbedBatchResponse(
            embeddings=vecs, count=len(vecs),
            dim=len(vecs[0]) if vecs else 0, model=MODEL_NAME
        )
    except Exception as e:
        log.error("Embed batch error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app_http.get("/health")
def health():
    """Liveness + model status + ingest stats."""
    model_loaded = embedder._model is not None
    return {
        "status": "online",
        "service": "k9-knowledge-ingestor",
        "port": INGESTOR_PORT,
        "sprint": 9,
        "model": MODEL_NAME,
        "model_loaded": model_loaded,
        "local_only": LOCAL_ONLY,
        "ingest": {
            "interval_s": INTERVAL,
            "last_cycle_at": stats.last_cycle_at,
            "last_cycle_elapsed_s": stats.last_cycle_elapsed_s,
            "last_error": stats.last_error,
            "total_cycles": stats.total_cycles,
            "total_embedded": stats.total_embedded,
            "cycle_running": stats.cycle_running,
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

@app_http.post("/ingest/trigger")
async def trigger_ingest():
    """Manually trigger one ingest cycle (non-blocking — runs in background)."""
    if stats.cycle_running:
        return {"status": "already_running", "message": "A cycle is already in progress"}
    asyncio.create_task(run_cycle())
    return {"status": "triggered", "message": "Ingest cycle started",
            "timestamp": datetime.now(timezone.utc).isoformat()}

# ── Entry point ───────────────────────────────────────────────────────────────

def run_uvicorn():
    """Run FastAPI in a background thread so asyncio ingest loop owns the event loop."""
    uvicorn.run(app_http, host="0.0.0.0", port=INGESTOR_PORT, log_level="warning")

if __name__ == "__main__":
    # Start uvicorn in a daemon thread
    t = threading.Thread(target=run_uvicorn, daemon=True)
    t.start()
    log.info("HTTP server started on :%d", INGESTOR_PORT)
    # Run ingest loop on main event loop
    asyncio.run(ingest_loop())
