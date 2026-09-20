# K-9 Ecosystem — Full Audit Report
**Date:** 2026-05-01
**Scope:** All 6 repos post Sprint 6

---

## REPO STATE

| Repo | Last Sprint Commit | Status |
|---|---|---|
| k9-llm-router | Sprint 6 — ingestor + launch script | ✅ Clean |
| ai-yield-whisperer | Sprint 6 — RAG loop closed | ✅ Clean |
| fed-whisperer | Sprint 5 — generate-trading-signals | ✅ Clean |
| orbitron-integrator | Post-Sprint: 7 new edge fns + vibe update | ✅ Clean |
| AlexaMobileWeb | Sprint 4c — Replit detached | ✅ Clean |
| MapPackManager | Sprint 4c — Replit detached | ✅ Clean |

---

## CONFIRMED WORKING

- ✅ FedWhisperer → trading_signals persisted + broadcast (Sprint 5)
- ✅ PackAI ai_engine.py → query_rag() wired to search_similar_patterns_local()
- ✅ PackAI main.py → /memory/event embeds immediately via asyncio.create_task()
- ✅ k9-knowledge-ingestor service file exists (:8767)
- ✅ launch-economic-stack.sh — all 5 services defined with correct cmds
- ✅ AlexaMobileWeb — replitAuth.ARCHIVED.ts confirmed, no Replit URLs in server/
- ✅ orbitron-integrator 7 new fns registered in config.toml
- ✅ auto-sync.sh + ecosystem-up.sh exist (references k9-llm-router launch script)
- ✅ Sprint 6 migration file committed and staged

---

## OPEN SEAMS (Ranked by Priority)

### 🔴 CRITICAL

**S1 — Sprint 6 migration NOT applied to Supabase**
- File: `ai-yield-whisperer/supabase/migrations/20260417000000_sprint6_local_embeddings.sql`
- Adds `embedding_local vector(768)` column + `search_similar_patterns_local()` fn
- Until applied: `query_rag()` will 500 and `k9-knowledge-ingestor` upserts will fail
- Fix: `cd ~/ai-yield-whisperer && supabase db push`

**S2 — k9-llm-router/requirements.txt missing ingestor deps**
- Router requirements only has: fastapi, uvicorn, httpx, python-dotenv, pydantic
- Ingestor needs: sentence-transformers, motor, pymongo, torch, transformers
- These live in `k9_knowledge_ingestor/requirements.txt` (separate) — OK for Docker
- Risk: if running from a single venv, ingestor will fail to import
- Fix: WSL2 install path should `pip install -r k9_knowledge_ingestor/requirements.txt` separately

### 🟡 MEDIUM

**S3 — ecosystem-up.sh path is wrong**
- Line: `cd ../k9-llm-router/k9-llm-router && ./launch-economic-stack.sh start`
- Double-nests the dir: `k9-llm-router/k9-llm-router` — will fail to cd
- Fix: `cd ../k9-llm-router && ./launch-economic-stack.sh start`

**S4 — Tailscale Funnel (AlexaMobileWeb → ai-yield-whisperer)**
- TAILSCALE_FUNNEL_SETUP.md exists and documents the steps
- Not yet executed on WSL2 (manual step, requires Tailscale auth)
- Blocks AlexaMobileWeb from reaching PackAI leader over the internet
- Fix: Follow TAILSCALE_FUNNEL_SETUP.md on WSL2

**S5 — orbitron-integrator new edge fns not wired to k9-action-executor**
- 7 post-Sprint-5 edge fns (signal-forge, great-rebalancing-signals, three-body-calibration,
  alpha-lifecycle-engine, listing-sniper, recovery-intelligence-agent, venue-operations-agent)
- k9-action-executor only routes to original 16 actions — these new fns are callable
  directly but not reachable via the K-9 action dispatch system
- Fix: Add action routes to k9-action-executor switch block

### 🟢 LOW / FUTURE

**S6 — k9_paymaster_dir / k9_mcp_dir / k9_orch_dir env vars**
- launch-economic-stack.sh falls back to `$HOME/k9-paymaster` etc.
- These dirs may not exist on WSL2 yet (services built separately)
- Fix: Set env vars pointing to actual service dirs, or build the services

**S7 — PaymasterLedger on-chain (Sprint 10)**
- Paymaster currently writes to in-memory/local only
- On-chain signing via HSM wallet deferred to Sprint 16

**S8 — auto-sync.sh hourly polling**
- Runs `git pull` every hour — no restart logic implemented
- If k9-llm-router updates while running, tmux session won't reload
- Fix: Add tmux restart signal after relevant file changes

---

## NEW CAPABILITIES SINCE SPRINT 6 AUDIT

orbitron-integrator added 7 new edge functions (post Sprint 5, not tracked in memory):
- `signal-forge` — signal generation + backtesting (Zod-validated inputs)
- `great-rebalancing-signals` — portfolio rebalancing signal engine
- `three-body-calibration` — 3-body regime calibration (BTC/ETH/DXY)
- `alpha-lifecycle-engine` — edge tracking (Sharpe, hit rate, decay)
- `listing-sniper` — new token listing opportunity detection
- `recovery-intelligence-agent` — drawdown recovery intelligence
- `venue-operations-agent` — exchange venue ops

ai-yield-whisperer added 4 new tables (Apr 2026):
- `entropy_readings` — market entropy/regime analysis
- `chain_discoveries` — chain discovery scoring
- `exchange_reserves` — exchange reserve tracking
- `narrative_signals` — market narrative detection

---

## ACTION SUMMARY

| Priority | Action | Repo | Effort |
|---|---|---|---|
| 🔴 | `supabase db push` (Sprint 6 migration) | ai-yield-whisperer | 1 cmd |
| 🔴 | Install ingestor deps in WSL2 venv | k9-llm-router | 1 cmd |
| 🟡 | Fix ecosystem-up.sh path | orbitron-integrator | 1 line |
| 🟡 | Execute Tailscale Funnel setup | AlexaMobileWeb | 10 min |
| 🟡 | Wire 7 new Orbitron fns to k9-action-executor | orbitron-integrator | Sprint 7 |
| 🟢 | Set K9_*_DIR env vars on WSL2 | local | env config |


---

## RESOLUTION 2026-09-20 — Duplicate entrypoint removed

The stale nested copy `k9-llm-router/k9-llm-router/` (last touched 2026-07-06, containing the
pre-patch unsafe CORS, broken Gemini registration, silent paymaster handling, and missing DoS
guards) has been removed from the repo. The root `main.py` (patched 2026-09-01) is the single
canonical entrypoint.

Deployment sanity check confirming root file is what production launches:
- `Dockerfile`: `CMD ["python", "main.py"]`, build context `.` → root main.py
- `render.yaml`: `startCommand: python -m src.signal_generator` → root src package
- `docker-compose.yml`: all build contexts are root-relative (`.` / `k9_gemini_agent`)

Unique July-era files (k9_orchestrator.py, k9_paymaster.py, k9_task_queue.py, k9_worker.py,
k9-swarm-agent.py, k9_mcp_manager.py, networks.yaml, n8n workflows, 3 test files) remain
recoverable from git history. Test suite: 130/130 passing after removal.
