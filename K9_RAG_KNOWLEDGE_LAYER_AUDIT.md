# K-9 RAG + Knowledge Layer — Obsidian/Notable Integration Audit
**Date:** 2026-04-17
**Context:** Evaluate Obsidian + Notable as layered note-taking tools feeding the existing K-9 RAG system

---

## WHAT WE ACTUALLY HAVE (vs. what the proposal assumes)

The proposal assumes a RAG system that needs a knowledge base. We have something more specific:

### Existing RAG Infrastructure

**Supabase pgvector store (`ict_pattern_knowledge`):**
- 1536-dim vector embeddings (OpenAI ada-002 dimension)
- `search_similar_patterns()` stored function (cosine similarity, IVFFlat index)
- Tables: `ict_pattern_knowledge`, `pattern_performance` (continuous learning via `update_pattern_confidence`)
- Seeded with 5 ICT pattern archetypes

**Embedding Pipeline:**
- `generate-embeddings` edge fn — uses Lovable AI (Gemini 2.5 Flash) to generate vectors
- `rag-trading-signals` edge fn — full RAG loop: embed → search → augment → LLM response
- PackAI leader (MongoDB via Motor): `memory_events` collection, embedding field = None (background job placeholder)

**`sentence-transformers` in requirements.txt** — installed but not wired to any endpoint yet. Intended for local embedding generation on WSL2.

### The Gap

PackAI leader's `memory_events` embeddings are `None` — no background job runs embeddings. The local embedding path exists in theory but isn't executing. This is the biggest RAG gap.

---

## VERDICT ON OBSIDIAN + NOTABLE

### What they bring:
| Feature | K-9 Relevance |
|---|---|
| Markdown storage | We already use markdown (GitHub repos, CLAUDE.md files) |
| Bidirectional links | Useful for knowledge graph — NOT the bottleneck right now |
| Local-first vault | We're already WSL2-local-first |
| Git integration | Already have GitHub Actions CI on all 5 repos |
| Tagging + querying | Partially solved by Supabase queries + module_shared_data |

### What they don't bring:
- Vector embeddings (need external pipeline regardless)
- Supabase/pgvector integration (custom build required)
- Trading signal awareness (they're generic note tools)
- Real-time K-9 event memory (would need polling/webhooks)

### Conclusion:
**Obsidian and Notable are input tools, not RAG tools.** They need a pipeline to become queryable. We already have the pipeline. What we're missing is not the note tool — it's the ingestion path from K-9 events into the vector store.

---

## THE RIGHT ARCHITECTURE FOR THIS SYSTEM

```
K-9 Event Sources                  Ingestion Layer              Vector Store
─────────────────                  ───────────────              ────────────
trading_signals (Supabase)  ─┐
module_shared_data          ─┼──→ k9-knowledge-ingestor   ──→ ict_pattern_knowledge
cross_module_events         ─┤     (WSL2 Python service)       (pgvector, Supabase)
PackAI memory_events (Mongo)─┘     Uses sentence-transformers       │
                                                                      │
Obsidian/Notable (optional)                                           ▼
  → export markdown files ──────→ same ingestor ──────────→ search_similar_patterns()
  → .md vault in WSL2                                               │
                                                                     ▼
                                                       rag-trading-signals edge fn
                                                       ai-chat edge fn
                                                       PackAI /rag/query endpoint
```

**Obsidian/Notable can slot in as the OPERATOR KNOWLEDGE LAYER** — 
VectOS Carbon writes strategy notes, market theses, operator decisions → these get embedded → become retrievable context during trade analysis. High value, low complexity to add once ingestor exists.

---

## WHAT TO BUILD: k9-knowledge-ingestor

A single WSL2 Python service that:
1. Polls `trading_signals` + `cross_module_events` + `memory_events` on interval
2. Generates embeddings via `sentence-transformers` (local, zero API cost)
3. Upserts to `ict_pattern_knowledge` table via Supabase REST
4. Optionally: watches a local Obsidian vault directory for `.md` file changes → embed + upsert

### Chunking strategy (from proposal, applied to our context):
- Trading signals: each signal = 1 chunk (asset + reasoning + regime context)
- Memory events: each event = 1 chunk
- Markdown notes (Obsidian): chunk by H2 section, max 512 tokens per chunk
- Cross-module events: batch by source_module + time window (5-min buckets)

### Embedding model: `all-MiniLM-L6-v2` (sentence-transformers)
- Already in requirements.txt
- 384-dim output (smaller than 1536, need to check pgvector column dim — currently 1536)
- Option A: pad to 1536 with zeros (hacky)
- Option B: add a new column `embedding_384 vector(384)` + new index
- Option C: use `all-mpnet-base-v2` (768-dim) — better quality, still free
- **Recommended: add `embedding_local vector(768)` column, keep `embedding vector(1536)` for Lovable AI path**

---

## PRIORITY ACTION LIST

| # | Action | Value | Complexity |
|---|---|---|---|
| 1 | Build `k9-knowledge-ingestor` service (WSL2) | 🔴 HIGH | Medium |
| 2 | Add `embedding_local vector(768)` column to `ict_pattern_knowledge` | 🔴 HIGH | Low |
| 3 | Add `search_similar_patterns_local()` Supabase fn for 768-dim | 🔴 HIGH | Low |
| 4 | Wire PackAI `memory_events` background embedding job | 🟡 MED | Medium |
| 5 | Obsidian vault → ingestor watch dir (optional operator layer) | 🟢 LOW | Low |
| 6 | Notable → not needed (Obsidian covers the use case) | ❌ DROP | — |

**Notable is dropped.** Obsidian alone is sufficient for the operator knowledge layer. Two tools for the same job is waste — Musk Protocol step 2.

---

## SPRINT 6 RECOMMENDATION

**Goal:** Close the PackAI RAG loop. Knowledge ingestor running. Memory events getting embedded.

**Deliverables:**
1. `k9-knowledge-ingestor` Python service (WSL2, port :8767)
   - Polls Supabase tables on 5-min interval
   - Embeds via sentence-transformers locally
   - Upserts to ict_pattern_knowledge
   - Optional: watches ~/obsidian-vault for .md changes
2. Supabase migration: `embedding_local vector(768)` + `search_similar_patterns_local()` fn
3. PackAI `ai_engine.py`: wire `query_rag()` to call `search_similar_patterns_local` via Supabase REST
4. `launch-economic-stack.sh`: add k9-knowledge-ingestor to startup sequence

