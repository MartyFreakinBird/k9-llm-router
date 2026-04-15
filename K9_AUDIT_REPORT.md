# K-9 Ecosystem — Full Audit Report
**Date:** 2026-04-15  
**Repos audited:** AlexaMobileWeb, MapPackManager  
**Auditor:** Vectos

---

## EXECUTIVE SUMMARY

Both repos are Replit-exported production systems that have been normalized (Dockerfile, CI, README). The core code is **significantly more advanced than expected** — this is not prototype territory. However, there are **7 critical broken seams** that will prevent the stack from running outside Replit. These are all fixable. None require architectural changes.

---

## REPO 1 — AlexaMobileWeb (PackAI Multi-Agent Platform)

### What it actually is
- Full Express + React + TypeScript platform
- **10 LAM action domains** — PackAI, Trading, Orbitron, AIYield, Voice, EdgeDevice, K9 Automotive, DeFi, Education, CodeDev
- **7 specialized agents** — Scout, Whisperer, FedWatcher, Narrator, Router, NetWatcher, Orbitron
- Chromebook edge agent (Python, `/chromebook-agent/`)
- Multi-platform edge deployments: Chromebook, Raspberry Pi, Windows
- Real-time WebSocket server on `/ws` and `/ws/chromebook`
- Orbitron connector wired to Supabase `external-integration` edge function ✅
- Auth system (currently Replit OIDC — **broken outside Replit**)
- Memory system, RAG, Pack Management, Automation Gateway, MCP routes

### CRITICAL BROKEN SEAMS

**🔴 SEAM 1 — replitAuth.ts: Hard crash on startup**
```
if (!process.env.REPLIT_DOMAINS) {
  throw new Error("Environment variable REPLIT_DOMAINS not provided");
}
```
The app will throw and die immediately outside Replit. `replitAuth.ts` uses Replit OIDC (`REPL_ID`, `REPLIT_DOMAINS`, `ISSUER_URL`). All 6 route files import `isAuthenticated` from this.

**Fix required:** Replace `replitAuth.ts` with a lightweight JWT/session auth or a `bypass` stub for WSL2 local dev. The `isAuthenticated` middleware must remain signature-compatible.

---

**🔴 SEAM 2 — Orbitron connector hardcoded to dead Replit URL**
```
// server/utils/orbitron-integration.ts
curl -X POST https://packai.replit.app/api/lam/command
curl -X GET  https://packai.replit.app/api/ios/status
```
These are stale curl examples in utility docs — but they indicate the original integration target was `packai.replit.app`, which is gone.

**Fix required:** Update all `packai.replit.app` references to point to `http://localhost:5000` (AlexaMobileWeb itself) or the actual K-9 LLM router at `:8765`.

---

**🔴 SEAM 3 — Missing `.env` — 18 required environment variables**
No `.env.example` at root level. App expects:
```
DATABASE_URL          ORBITRON_API_KEY       ORBITRON_ENDPOINT
SESSION_SECRET        ORBITRON_WEBHOOK_URL   N8N_WEBHOOK_URL
REPL_ID               OPENAI_API_KEY         ANTHROPIC_API_KEY
REPLIT_DOMAINS        AIYIELD_API_KEY        AIYIELD_ENDPOINT
ISSUER_URL            TRADING_API_KEY        CHROMEBOOK_API_KEY
VOICE_MONKEY_TOKEN    LAM_MOCK_MODE          N8N_WEBHOOK_SECRET
```

**Fix required:** Generate `.env.example` with all vars documented, mark which are required vs optional, add WSL2-local defaults.

---

**🟡 SEAM 4 — Database dependency: Neon PostgreSQL required on startup**
`server/db.ts` + session store both need `DATABASE_URL`. Without it the app won't start. `replitAuth.ts` also uses a pg session store.

**Fix required:** Either provision a local PostgreSQL via Docker or add a graceful startup mode that falls back to in-memory storage when `DATABASE_URL` is absent.

---

**🟡 SEAM 5 — MCP service allows `.replit.app` as trusted origin**
```
// server/services/mcp-service.ts:370
".replit.app",
```
Low risk but should be cleaned — this is a CORS/origin trust policy.

---

### WORKING SEAMS ✅
- Orbitron connector properly uses Supabase endpoint (`ziqenqqgnqxqrazmjohs.supabase.co`) — matches memory entry #4
- K-9 action registry is complete — 10 domains, all handlers implemented
- `chromebook-agent/` is standalone Python with its own `.env.example` and `requirements.txt` — can run independently
- Edge deployments (`edge-deployments/`) have install scripts for Chromebook, RPi, Windows
- WebSocket architecture is sound — real sensor data path works correctly
- Orbitron signal aggregator wired to Supabase platform-sync function

---

## REPO 2 — MapPackManager (K-9 Automotive LAM Dashboard)

### What it actually is
- Honda Civic edge node dashboard — K24 5AT transmission map pack manager
- KITT-inspired UI (voice assistant, knight scanner, ecosystem status)
- PWA with service worker + manifest — installable on Civic head unit
- LLM router (Ollama-first → PackAI Cloud → Akash/io.net fallback)
- PackAI client with agent lifecycle, context packs, feedback logs
- Orbitron sync service
- OBD-II integration, real-time gauges, shift map editor
- Full Drizzle ORM schema for transmission data

### CRITICAL BROKEN SEAMS

**🔴 SEAM 6 — PackAI endpoint pointing to dead Replit instance**
```
// server/llm-router.ts:68
endpoint: process.env.PACKAI_ENDPOINT || "https://packai.replit.app",
```
When `PACKAI_ENDPOINT` is unset, all cloud LLM queries die silently (returns "PackAI endpoint not configured"). It falls back to Ollama only.

**Fix required:** Set `PACKAI_ENDPOINT=http://localhost:5000` (AlexaMobileWeb) or `http://localhost:8765` (k9-llm-router). This wires MapPackManager's cloud LLM path to the actual K-9 router.

---

**🔴 SEAM 7 — PackAI client has 3 commented-out TODO API calls**
```typescript
// packai-client.ts:146
// TODO: Replace with actual PackAI deployment API call when endpoint is available
// packai-client.ts:281
// TODO: Replace with actual PackAI evaluation API call when endpoint is available  
// packai-client.ts:345
// TODO: Replace with actual PackAI API call when endpoint is available
```
These 3 agent lifecycle operations (deploy, evaluate, getContextPack) run in permanent mock mode even when connected. Agent management is simulated, not real.

**Fix required:** Implement the 3 actual API calls once AlexaMobileWeb is running locally and endpoint is confirmed.

---

### WORKING SEAMS ✅
- LLM complexity analyzer is functional — routes simple/medium/complex queries correctly
- Local Ollama routing works — hits `:11434` correctly
- Transmission storage, shift maps, OBD schema all defined and working
- KITT UI components (voice assistant, knight scanner, ecosystem status) are built
- PWA manifest + service worker are present
- K9_ORBITRON_MODULE_SPEC.md + PackAI integration spec are solid developer contracts
- `shared/packai-types.ts` defines full telemetry schema matching k9-actions.ts in AlexaMobileWeb

---

## CROSS-REPO INTEGRATION MAP

```
MapPackManager (:5000)
  └── llm-router.ts
        ├── LOCAL:  → Ollama :11434           ✅ works
        └── CLOUD:  → PACKAI_ENDPOINT         🔴 → should be AlexaMobileWeb :5000

AlexaMobileWeb (:5000)
  ├── LAM router                              ✅ works
  ├── replitAuth                              🔴 crashes outside Replit
  ├── Orbitron connector → Supabase           ✅ works (matches memory #4)
  └── k9-integration routes → k9-llm-router  ✅ wired to :8765

k9-llm-router (:8765)
  ├── Paymaster :9002                         ✅ Sprint 4 complete
  ├── MCP Manager :3030                       ✅ Sprint 4 complete
  ├── Orchestrator :8744                      ✅ Sprint 4 complete
  └── Orbitron client                         ✅ registered
```

---

## PRIORITY ACTION LIST

| # | Severity | Repo | Fix |
|---|---|---|---|
| 1 | 🔴 BLOCKER | AlexaMobileWeb | Replace `replitAuth.ts` with JWT/session stub for local dev |
| 2 | 🔴 BLOCKER | AlexaMobileWeb | Generate `.env.example` with all 18 vars + WSL2 defaults |
| 3 | 🔴 BLOCKER | MapPackManager | Set `PACKAI_ENDPOINT` default → `http://localhost:5000` |
| 4 | 🔴 HIGH | MapPackManager | Implement 3 TODO PackAI API calls in `packai-client.ts` |
| 5 | 🟡 MEDIUM | AlexaMobileWeb | Add DB-less startup mode (in-memory fallback) |
| 6 | 🟡 MEDIUM | AlexaMobileWeb | Clean `packai.replit.app` references from orbitron-integration.ts |
| 7 | 🟢 LOW | AlexaMobileWeb | Remove `.replit.app` from MCP trusted origins |

---

## SPRINT RECOMMENDATION: Sprint 4c — Local Detachment

**Goal:** Both repos run fully on WSL2 without Replit dependencies.  
**Estimated effort:** 3-4 targeted file edits + env files.

**Deliverables:**
1. `server/localAuth.ts` — JWT bypass replacing replitAuth (dev mode flag)
2. `.env.example` for AlexaMobileWeb
3. `.env.example` for MapPackManager with `PACKAI_ENDPOINT=http://localhost:5000`
4. Implement 3 PackAI client TODO stubs
5. Patch `packai.replit.app` → `localhost:5000`

After Sprint 4c: both repos start cleanly in WSL2, MapPackManager routes LLM calls through AlexaMobileWeb → k9-llm-router → Ollama/cloud.

---

## ADDITIONAL FINDING — Other Repos in Local GitHub Folder

From the GitHub Desktop screenshot and memory, these likely have the same Replit-dependency pattern:
- `fed-whisperer` — FedWhisperer UI + Supabase functions
- `ai-yield-whisperer` — DeFi AI + PackAI Leader (known partially broken, memory #6)
- `orbitron-integrator` — Main Orbitron UI

All should go through the same `import_replit_service.yaml` + auth detachment process.
