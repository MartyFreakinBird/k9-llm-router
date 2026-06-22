"""
hybrid_rag.py — K-9 Hybrid RAG Query Engine
─────────────────────────────────────────────────────────────────────────────
Sprint CB-2 · k9-knowledge-ingestor upgrade

Upgrades the single-vector retrieval to:
  Dense retrieval    → Supabase pgvector (existing ict_pattern_knowledge)
  Sparse retrieval   → BM25 (rank-bm25, persisted to local file)
  Fusion             → Reciprocal Rank Fusion (RRF, k=60)
  Reranking          → Cohere rerank-english-v3.0 (optional — degrades gracefully)
  Answer generation  → routed through k9-llm-router /route (no direct LLM calls)

Integrates with the existing k9-knowledge-ingestor service at :8767:
  POST /query          — hybrid RAG query (this module)
  POST /rag/build-bm25 — rebuild BM25 index from current corpus

Dependencies (add to requirements.txt):
  rank-bm25>=0.2.2
  cohere>=5.5.0 (optional)
  nltk>=3.8.0

Env vars:
  SUPABASE_URL           — existing
  SUPABASE_SERVICE_KEY   — existing
  BM25_INDEX_PATH        — default ./bm25_index.pkl
  COHERE_API_KEY         — optional — skip reranking if absent
  RAG_TOP_K_RETRIEVAL    — default 20
  RAG_TOP_K_RERANK       — default 5
  RAG_LLM_ROUTER_URL     — default http://localhost:8765
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import pickle
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

log = logging.getLogger("k9-hybrid-rag")

# ── Config ────────────────────────────────────────────────────────────────────

SUPABASE_URL      = os.getenv("SUPABASE_URL", "")
SUPABASE_SVC_KEY  = os.getenv("SUPABASE_SERVICE_KEY", "")
BM25_INDEX_PATH   = os.getenv("BM25_INDEX_PATH", "./bm25_index.pkl")
COHERE_API_KEY    = os.getenv("COHERE_API_KEY", "")
TOP_K_RETRIEVAL   = int(os.getenv("RAG_TOP_K_RETRIEVAL", "20"))
TOP_K_RERANK      = int(os.getenv("RAG_TOP_K_RERANK", "5"))
LLM_ROUTER_URL    = os.getenv("RAG_LLM_ROUTER_URL", "http://localhost:8765")

# Supabase vector search function name (already exists in ict_pattern_knowledge)
VECTOR_SEARCH_FN  = "search_similar_patterns_local"
EMBED_DIM         = 768   # all-mpnet-base-v2


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class RAGDocument:
    id:         str
    content:    str
    source:     str           # table or file origin
    pattern_type: str
    metadata:   dict = field(default_factory=dict)
    score:      float = 0.0   # final reranked score


@dataclass
class RAGResult:
    answer:     str
    citations:  list[dict]    # [{text, source, score}]
    documents:  list[RAGDocument]
    query:      str
    latency_ms: float
    method:     str           # "hybrid" | "dense-only" | "bm25-only"
    cache_hit:  bool = False


@dataclass
class RRFScore:
    doc_id: str
    score:  float


# ── BM25 Index Manager ────────────────────────────────────────────────────────

class BM25Index:
    """
    Manages the BM25 sparse index.
    Built from the full text corpus in ict_pattern_knowledge.
    Persisted to disk so it survives restarts without rebuild.
    """

    def __init__(self, path: str = BM25_INDEX_PATH) -> None:
        self.path      = path
        self._bm25     = None
        self._docs: list[RAGDocument] = []
        self._built_at: float = 0.0

    def is_ready(self) -> bool:
        return self._bm25 is not None and len(self._docs) > 0

    def load(self) -> bool:
        """Load persisted index from disk."""
        try:
            if not os.path.exists(self.path):
                return False
            with open(self.path, "rb") as f:
                data = pickle.load(f)
            self._bm25     = data["bm25"]
            self._docs     = data["docs"]
            self._built_at = data.get("built_at", 0.0)
            log.info("[rag] BM25 index loaded: %d docs from %s", len(self._docs), self.path)
            return True
        except Exception as e:
            log.warning("[rag] BM25 load failed: %s", e)
            return False

    def build(self, docs: list[RAGDocument]) -> None:
        """Build BM25 index from document list and persist to disk."""
        try:
            from rank_bm25 import BM25Okapi
            import nltk
            try:
                nltk.data.find("tokenizers/punkt")
            except LookupError:
                nltk.download("punkt", quiet=True)
                nltk.download("punkt_tab", quiet=True)
            from nltk.tokenize import word_tokenize

            tokenized = [word_tokenize(d.content.lower()) for d in docs]
            self._bm25     = BM25Okapi(tokenized)
            self._docs     = docs
            self._built_at = time.time()

            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(self.path, "wb") as f:
                pickle.dump({"bm25": self._bm25, "docs": docs, "built_at": self._built_at}, f)

            log.info("[rag] BM25 index built and saved: %d docs → %s", len(docs), self.path)
        except ImportError as e:
            log.warning("[rag] BM25 build failed — rank-bm25 or nltk missing: %s", e)

    def search(self, query: str, top_k: int = TOP_K_RETRIEVAL) -> list[tuple[RAGDocument, float]]:
        """BM25 sparse search. Returns (doc, score) tuples."""
        if not self.is_ready():
            return []
        try:
            from nltk.tokenize import word_tokenize
            import numpy as np
            tokens = word_tokenize(query.lower())
            scores = self._bm25.get_scores(tokens)
            top_idx = np.argsort(scores)[-top_k:][::-1]
            return [(self._docs[i], float(scores[i])) for i in top_idx if scores[i] > 0]
        except Exception as e:
            log.warning("[rag] BM25 search error: %s", e)
            return []


# ── Supabase dense retrieval ──────────────────────────────────────────────────

async def dense_search(
    query_embedding: list[float],
    top_k: int = TOP_K_RETRIEVAL,
) -> list[RAGDocument]:
    """Call Supabase pgvector RPC to get dense nearest neighbors."""
    if not SUPABASE_URL or not SUPABASE_SVC_KEY:
        log.warning("[rag] Supabase not configured — dense search unavailable")
        return []

    payload = {
        "query_embedding": query_embedding,
        "similarity_threshold": 0.6,
        "count": top_k,
    }
    headers = {
        "apikey":        SUPABASE_SVC_KEY,
        "Authorization": f"Bearer {SUPABASE_SVC_KEY}",
        "Content-Type":  "application/json",
    }
    url = f"{SUPABASE_URL}/rest/v1/rpc/{VECTOR_SEARCH_FN}"

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            rows = resp.json()
            return [
                RAGDocument(
                    id=r.get("id", ""),
                    content=r.get("raw_text", r.get("content", "")),
                    source=r.get("source_table", "supabase"),
                    pattern_type=r.get("pattern_type", "unknown"),
                    metadata=r,
                    score=float(r.get("similarity", 0.0)),
                )
                for r in (rows or [])
                if r.get("raw_text") or r.get("content")
            ]
        except Exception as e:
            log.warning("[rag] Dense search failed: %s", e)
            return []


# ── Embed via ingestor ────────────────────────────────────────────────────────

async def embed_query(text: str) -> Optional[list[float]]:
    """Get query embedding from the local ingestor service at :8767."""
    async with httpx.AsyncClient(timeout=8.0) as client:
        try:
            resp = await client.post(
                "http://localhost:8767/embed",
                json={"text": text},
            )
            resp.raise_for_status()
            return resp.json().get("embedding")
        except Exception as e:
            log.warning("[rag] Embed query failed: %s", e)
            return None


# ── Reciprocal Rank Fusion ────────────────────────────────────────────────────

def reciprocal_rank_fusion(
    ranked_lists: list[list[tuple[RAGDocument, float]]],
    k: int = 60,
) -> list[RAGDocument]:
    """
    Merge multiple ranked lists using RRF.
    RRF score = sum(1 / (k + rank)) across all lists.
    """
    scores: dict[str, float] = {}
    doc_map: dict[str, RAGDocument] = {}

    for ranked in ranked_lists:
        for rank, (doc, _) in enumerate(ranked):
            doc_id = doc.id or hashlib.sha256(doc.content[:100].encode()).hexdigest()[:12]
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
            doc_map[doc_id] = doc

    sorted_ids = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    result = []
    for doc_id, score in sorted_ids:
        doc = doc_map[doc_id]
        doc.score = score
        result.append(doc)
    return result


# ── Cohere reranking ──────────────────────────────────────────────────────────

async def cohere_rerank(
    query: str,
    docs: list[RAGDocument],
    top_k: int = TOP_K_RERANK,
) -> list[RAGDocument]:
    """Rerank with Cohere rerank-english-v3.0. Degrades to identity if unavailable."""
    if not COHERE_API_KEY or not docs:
        return docs[:top_k]

    try:
        import cohere as cohere_lib
        co = cohere_lib.Client(api_key=COHERE_API_KEY)

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: co.rerank(
                query=query,
                documents=[d.content for d in docs],
                top_n=top_k,
                model="rerank-english-v3.0",
            )
        )
        reranked = []
        for item in result.results:
            doc = docs[item.index]
            doc.score = item.relevance_score
            reranked.append(doc)
        log.info("[rag] Cohere reranked %d → %d docs", len(docs), len(reranked))
        return reranked

    except ImportError:
        log.warning("[rag] cohere not installed — skipping rerank")
        return docs[:top_k]
    except Exception as e:
        log.warning("[rag] Cohere rerank failed: %s — using RRF order", e)
        return docs[:top_k]


# ── Answer generation via k9-llm-router ──────────────────────────────────────

async def generate_answer(
    query: str,
    docs: list[RAGDocument],
    task_type: str = "rag_query",
) -> str:
    """
    Route answer generation through k9-llm-router /route.
    Never calls LLM APIs directly.
    """
    context_parts = [
        f"[{i+1}] Source: {doc.source} | Type: {doc.pattern_type}\n{doc.content}"
        for i, doc in enumerate(docs)
    ]
    context = "\n\n---\n\n".join(context_parts)

    prompt = (
        f"Answer the following question using ONLY the provided context. "
        f"Cite each claim with [N] where N is the source number. "
        f"If the context is insufficient, say so clearly.\n\n"
        f"Context:\n{context}\n\n"
        f"Question: {query}\n\n"
        f"Answer:"
    )

    payload = {
        "task_type":   task_type,
        "component":   "k9-hybrid-rag",
        "messages":    [{"role": "user", "content": prompt}],
        "max_tokens":  800,
        "temperature": 0.2,  # low temp for factual RAG answers
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(f"{LLM_ROUTER_URL}/route", json=payload)
            resp.raise_for_status()
            return resp.json().get("content", "No answer generated.")
        except Exception as e:
            log.warning("[rag] LLM router call failed: %s", e)
            return f"Answer generation failed: {e}"


# ── Citation extraction ───────────────────────────────────────────────────────

def extract_citations(answer: str, docs: list[RAGDocument]) -> list[dict]:
    """Extract [N] citations from answer text and map to source documents."""
    citations = []
    for i, doc in enumerate(docs):
        if f"[{i+1}]" in answer:
            citations.append({
                "text":    doc.content[:200],
                "source":  doc.source,
                "type":    doc.pattern_type,
                "score":   round(doc.score, 4),
                "index":   i + 1,
            })
    return citations


# ── Main query engine ─────────────────────────────────────────────────────────

class HybridRAGEngine:
    """
    Main RAG query engine. Wire into k9-knowledge-ingestor FastAPI app.

    Usage:
        engine = HybridRAGEngine()
        engine.initialize()    # loads BM25 index at startup

        result = await engine.query("What are the current ICT patterns for BTC?")
    """

    def __init__(self) -> None:
        self.bm25 = BM25Index()
        self._queries = 0
        self._hits    = 0
        self._errors  = 0

    def initialize(self) -> None:
        """Load BM25 index from disk. Call at service startup."""
        loaded = self.bm25.load()
        if not loaded:
            log.info("[rag] No BM25 index on disk — will build on first /rag/build-bm25 call")

    async def build_bm25_index(self) -> dict:
        """
        Fetch all documents from Supabase ict_pattern_knowledge and build BM25.
        Call this endpoint when corpus changes (or schedule daily).
        """
        if not SUPABASE_URL:
            return {"error": "Supabase not configured"}

        headers = {
            "apikey":        SUPABASE_SVC_KEY,
            "Authorization": f"Bearer {SUPABASE_SVC_KEY}",
        }
        url = f"{SUPABASE_URL}/rest/v1/ict_pattern_knowledge?select=id,raw_text,source_table,pattern_type&limit=5000"

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                rows = resp.json()
                docs = [
                    RAGDocument(
                        id=r.get("id", ""),
                        content=r.get("raw_text", ""),
                        source=r.get("source_table", "supabase"),
                        pattern_type=r.get("pattern_type", "unknown"),
                    )
                    for r in rows if r.get("raw_text")
                ]
                self.bm25.build(docs)
                return {"built": True, "doc_count": len(docs)}
            except Exception as e:
                return {"error": str(e)}

    async def query(
        self,
        question:    str,
        task_type:   str = "rag_query",
        use_rerank:  bool = True,
    ) -> RAGResult:
        """
        Full hybrid RAG pipeline:
        embed → dense + sparse → RRF → rerank → generate → cite
        """
        start = time.time()
        self._queries += 1

        # 1. Embed query (local model via ingestor)
        embedding = await embed_query(question)
        method    = "hybrid"

        # 2. Dense retrieval
        dense_results: list[tuple[RAGDocument, float]] = []
        if embedding:
            dense_docs = await dense_search(embedding, top_k=TOP_K_RETRIEVAL)
            dense_results = [(d, d.score) for d in dense_docs]
        else:
            log.warning("[rag] No embedding — dense retrieval skipped")
            method = "bm25-only"

        # 3. Sparse (BM25) retrieval
        bm25_results = self.bm25.search(question, top_k=TOP_K_RETRIEVAL)
        if not dense_results and not bm25_results:
            self._errors += 1
            return RAGResult(
                answer="No relevant documents found.",
                citations=[],
                documents=[],
                query=question,
                latency_ms=(time.time() - start) * 1000,
                method="none",
            )

        if not bm25_results:
            method = "dense-only"

        # 4. RRF fusion
        ranked_lists = [l for l in [dense_results, bm25_results] if l]
        fused_docs   = reciprocal_rank_fusion(ranked_lists)[:TOP_K_RERANK * 2]

        # 5. Cohere rerank
        if use_rerank:
            final_docs = await cohere_rerank(question, fused_docs, top_k=TOP_K_RERANK)
        else:
            final_docs = fused_docs[:TOP_K_RERANK]

        # 6. Generate answer
        answer = await generate_answer(question, final_docs, task_type)

        # 7. Extract citations
        citations = extract_citations(answer, final_docs)
        self._hits += 1

        return RAGResult(
            answer    = answer,
            citations = citations,
            documents = final_docs,
            query     = question,
            latency_ms = (time.time() - start) * 1000,
            method    = method,
        )

    def stats(self) -> dict:
        return {
            "queries":      self._queries,
            "hits":         self._hits,
            "errors":       self._errors,
            "bm25_ready":   self.bm25.is_ready(),
            "bm25_docs":    len(self.bm25._docs),
            "bm25_built":   self.bm25._built_at,
            "cohere":       bool(COHERE_API_KEY),
            "llm_router":   LLM_ROUTER_URL,
        }


# ── Module singleton ──────────────────────────────────────────────────────────
rag_engine = HybridRAGEngine()
