# K-9 Lovable Platform Integration Audit
**Date:** 2026-04-16  
**Platforms:** orbitron-integrator, ai-yield-whisperer, fed-whisperer (all live on Lovable)  
**Scope:** Integration surface with WSL2 K-9 stack — what's wired, what's missing, what to build

---

## ARCHITECTURE REALITY

These three platforms are **cloud-native Lovable apps** — they do NOT need Replit detachment (no Express server, no replitAuth). They run entirely as:
- React/Vite frontend (Lovable CDN)
- Supabase edge functions (Deno runtime, deployed to Supabase)
- Shared Supabase database (`ziqenqqgnqxqrazmjohs.supabase.co`)

The K-9 WSL2 stack communicates with them **via Supabase only** — not via direct HTTP.

```
WSL2 K-9 Stack
  └── orbitron_client.py
        ├── POST → platform-sync edge fn   (event bus)
        ├── POST → external-integration fn (heartbeat, register)
        └── GET  → Supabase REST API       (read shared tables)

Lovable Platforms
  └── useK9Action hook
        └── invoke → k9-action-executor edge fn
              └── invoke → k9-orchestrator edge fn  (quant, Black-Scholes)
```

---

## PLATFORM 1 — orbitron-integrator (Command Center)

### Status: STRONG — most complete K-9 integration

**What's wired ✅:**
- `useK9Action` hook — 16 actions across 9 categories (auth, quant, deploy, monitor, sync, voice, osint, defi, trading)
- `k9-action-executor` edge fn — full action router, logs to `cross_module_events` table
- `k9-orchestrator` edge fn — Black-Scholes, Monte Carlo, vol surface, regime analysis (all running in Deno, no WSL2 needed)
- `K9CommandPalette` — Cmd+K UI for all 16 actions, real-time result display
- `K9QuantDashboard` — quant output visualization
- `K9InfrastructureInstaller` — node provisioning UI
- `NodeKeyProvisioning` edge fn — generates/rotates API keys for K-9 nodes
- Real-time `cross_module_events` subscription — action log updates live
- `platform-sync` event bus — 60+ event types, K9_AGENT + K9_EDGE registered as valid platforms

**What's MISSING / BROKEN 🔴:**
1. **`k9-action-executor` → `signal-aggregator` edge fn** — `trading.signal_scan` calls `signal-aggregator` which does NOT exist in orbitron-integrator's function list. Will return 404.
2. **`k9-action-executor` → `sentiment-analysis` edge fn** — `osint.scan` calls `sentiment-analysis` which also does NOT exist.
3. **`k9-action-executor` → `voice-command-router` edge fn** — `voice.transcribe` calls `voice-command-router` which does NOT exist.
4. **`k9-orchestrator` quant engine is cloud-only** — Black-Scholes runs in Deno. Good for cloud. But there's no relay to WSL2 `:9001` k9-quant-engine for live TradingView signals. `quant.tv_signals` reads from `module_shared_data` table — **WSL2 must WRITE signals there** for this to work.
5. **`openbb-bridge` edge fn** references `OPENBB_LOCAL_ENDPOINT=http://localhost:5002` — local OpenBB instance. Not in any K-9 service map. Either add to stack or stub it.

**Fix plan:**
- Create `signal-aggregator`, `sentiment-analysis`, `voice-command-router` as stub edge functions
- Wire WSL2 `k9_orchestrator.py` to write TradingView signals to `module_shared_data` table
- Add OpenBB port `:5002` to K-9 service map or create fallback stub

---

## PLATFORM 2 — ai-yield-whisperer (Spot Pack Dog / DeFi)

### Status: PARTIAL — PackAI integration built but endpoint unconfigured

**What's wired ✅:**
- `PackAIIntegration` class — connects to a "PackAI Leader" URL stored in `api_configurations` table
- Calls: `/api/packdogs/register`, `/api/models/trained`, `/api/memory/event`, `/api/rag/query`, `/api/memory/prune`
- If configured: syncs models, logs memory events, queries RAG
- Supabase `api_configurations` table stores encrypted `leaderUrl || apiKey` pair
- Multiple edge functions: `ai-chat`, `defi-ai-insights`, `entropy-engine`, `cryptoforge-engine`, `cross-module-ai`, `cross-module-status`
- Platform registered as `AI_YIELD_WHISPERER` in Orbitron event bus

**What's MISSING / BROKEN 🔴:**
1. **`leaderUrl` is empty by default** — `PackAIIntegration` constructor sets `leaderUrl: ''`. Without the right value saved in `api_configurations`, ALL PackAI calls silently no-op. The "Leader" it expects is AlexaMobileWeb (PackAI platform).
2. **AlexaMobileWeb is not publicly reachable** — it runs on WSL2 `:5000`. ai-yield-whisperer (running in browser on Lovable) cannot call `http://localhost:5000` from cloud. **Needs a tunnel** (Tailscale Funnel, ngrok, or Cloudflare tunnel) to expose AlexaMobileWeb to the browser.
3. **`ai_engine.py`** (packai-leader/) uses stub inference — `EMERGENT_LLM_API_KEY` env var, simulated pattern recognition. Not wired to k9-llm-router.
4. **`cross-module-status` edge fn** — checks status of other modules via `module_shared_data`. Works only if modules write their status there (WSL2 stack currently doesn't).

**Fix plan:**
- Expose AlexaMobileWeb via Tailscale Funnel → set as `leaderUrl` in ai-yield-whisperer's `api_configurations`
- Wire `ai_engine.py` inference to `http://localhost:8765/llm/route` (k9-llm-router)
- Have WSL2 stack write service health to `module_shared_data` on 60s interval

---

## PLATFORM 3 — fed-whisperer (FedWhisperer / Macro Signal Engine)

### Status: STANDALONE — minimal K-9 integration, runs independently

**What's wired ✅:**
- Full FRED economic data pipeline (fetch-economic-data, calculate-regime, fed-crypto-synthesis)
- `generate-trading-signals` edge fn — Taylor Rule calculation → BUY/SELL/HOLD signals
- `external-integration` edge fn — same structure as Orbitron, accepts K9_AGENT registration
- Signals stored in Supabase `trading_signals` table
- `fed_whisperer_bridge.py` in k9-llm-router already polls this (`FedWhispererBridge` — Sprint 3)

**What's MISSING / BROKEN 🔴:**
1. **`trading_signals` table is EMPTY** (confirmed in memory #4) — FedWhisperer edge functions aren't being triggered on schedule. No cron automation exists in the Supabase config.
2. **`fed_whisperer_bridge.py` polls but gets nothing** — because the signals table is empty. Fallback to FRED direct is active but that's a degraded mode.
3. **`check-bot-health` edge fn** — monitors bot health but no bot is actually running autonomously.
4. **Signal → K-9 relay missing** — when FedWhisperer generates a signal, it should fire a `platform-sync` event with type `SIGNAL_GENERATED` to notify K9_AGENT. This broadcast is not implemented.

**Fix plan:**
- Deploy a Supabase cron job to trigger `execute-daily-update` + `generate-trading-signals` daily at market open
- Add `platform-sync` broadcast at end of `generate-trading-signals` (notify K9_AGENT)
- WSL2 `fed_whisperer_bridge.py` already handles `DATA_SYNC` — just needs signals flowing

---

## INTEGRATION GAP MAP

```
WSL2 K-9 Stack → Lovable Platforms
  orbitron_client.py  → platform-sync      ✅ wired (heartbeat works)
  orbitron_client.py  → external-integr.  ✅ registered
  k9_orchestrator.py  → module_shared_data 🔴 NOT writing signals/status
  fed_whisperer_bridge→ trading_signals    🔴 table empty (no cron)

Lovable Platforms → WSL2 K-9 Stack
  k9-action-executor  → k9-orchestrator   ✅ quant works (Deno-local)
  k9-action-executor  → signal-aggregator 🔴 edge fn missing
  k9-action-executor  → sentiment-analysis🔴 edge fn missing
  ai-yield-whisperer  → AlexaMobileWeb    🔴 no public tunnel
  ai-yield-whisperer  → k9-llm-router     🔴 ai_engine.py not wired
```

---

## PRIORITY ACTION LIST

| # | Platform | Severity | Fix |
|---|---|---|---|
| 1 | fed-whisperer | 🔴 BLOCKER | Supabase cron → `generate-trading-signals` daily at 9:30 AM ET |
| 2 | fed-whisperer | 🔴 BLOCKER | Add `platform-sync` broadcast at end of signal generation |
| 3 | orbitron | 🔴 HIGH | Create `signal-aggregator` stub edge fn |
| 4 | orbitron | 🔴 HIGH | Create `sentiment-analysis` stub edge fn |
| 5 | orbitron | 🔴 HIGH | Create `voice-command-router` stub edge fn |
| 6 | orbitron | 🟡 MED | Wire k9_orchestrator.py to write TV signals → `module_shared_data` |
| 7 | ai-yield | 🟡 MED | Expose AlexaMobileWeb via Tailscale Funnel → configure leaderUrl |
| 8 | ai-yield | 🟡 MED | Wire ai_engine.py inference → k9-llm-router `:8765` |
| 9 | all | 🟢 LOW | WSL2 stack writes service status → `module_shared_data` every 60s |

---

## SPRINT 5 — RECOMMENDED

**Goal:** Close the event loop. Signals flow from FedWhisperer → Supabase → K-9 → back to Orbitron.

**Deliverables:**
1. Supabase cron on fed-whisperer (daily 9:30 AM ET)
2. `platform-sync` broadcast appended to `generate-trading-signals`
3. Three missing edge fn stubs in orbitron-integrator (`signal-aggregator`, `sentiment-analysis`, `voice-command-router`)
4. `k9_orchestrator.py` patch — write TV signals to `module_shared_data` via orbitron_client
5. Tailscale Funnel setup guide for AlexaMobileWeb public exposure
