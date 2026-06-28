# K-9 Command Surface — Wallpaper Update Prompt
> Feed this file to Claude Code in `~/k9-wallpaper/` or `~/k9-llm-router/wallpaper/`
> Claude will read `wallpaper.html`, apply the changes below, and write the file back.

---

## Context

`wallpaper.html` is the K-9 Command Surface — a single-file Octos/Sucrose desktop wallpaper (4854–5100 lines). It has:
- A `<style>` block at the top
- A `<div class="shell">` HTML body with named zones (`.hz-left`, `.hz-center`, `.hz-right1/2/3`, `.hz-bottom`)
- A `<script>` section at the bottom with all JS

**DO NOT** change layout CSS, background image paths, or any section you are not explicitly told to change below.
**Always** write the full file back — do not truncate.

---

## Current Port / Service Map (Sprint AEG-3 — as of 2026-05-03)

| Port  | Service                | Health path       |
|-------|------------------------|-------------------|
| 9002  | k9-paymaster           | /health           |
| 3030  | k9-mcp-manager         | /health           |
| 8744  | k9-orchestrator        | /health           |
| 8765  | k9-llm-router          | /swarm/health     |
| 8767  | k9-knowledge-ingestor  | /health           |
| 8769  | k9-control-plane       | /health           |
| 8770  | k9-gemini-agent        | /health           |
| 8768  | aeg-node-adapter       | /health           |
| 11434 | ollama                 | /api/tags         |
| 5678  | n8n                    | /healthz          |
| 47990 | sunshine-stream        | /                 |

**Stale ports that must NOT appear in HB_SERVICES:** 8080, 8888, 4000, 3000, 9001, 5001, 8123, 8088, 4953

---

## How to Apply a New Sprint Update

When the K-9 stack changes between sprints, apply this diff pattern to `wallpaper.html`:

### 1. `HB_SERVICES` array (in `<script>`)
Find `const HB_SERVICES = [` and replace the entire array with the new port/path/group map.
Each entry: `{port:NNNN, name:'service-name', path:'/health', group:'core'|'ai'|'aeg'|'infra'}`

### 2. Heartbeat DOM rows (`id="heartbeatList"`)
Find `<div class="mod-status" id="heartbeatList">` and replace all `.ms-row` children to match HB_SERVICES exactly.
Pattern per row:
```html
<div class="ms-row" id="hb-PORT"><div class="dot dd" id="hb-dot-PORT"></div><span class="ms-name">SERVICE</span><span class="ms-val ms-off" id="hb-val-PORT">—</span></div>
```
AEG adapter row gets `style="color:var(--purple)"` on `.ms-name`.

### 3. Quick Launch grid (`class="ql-grid"`)
Find the first `<div class="ql-grid">` (in the dashboard view) and update `<a>` hrefs to match current ports.
AEG adapter link gets `style="border-color:rgba(167,139,250,.3);color:var(--purple)"`.

### 4. Home left panel pills (`class="hz hz-left"`)
Find `<div class="hz hz-left">` and update the pill `id` and text content to match the service map.
Pills are `id="hp-SERVICE"` and updated live by `runHomeStackPoll()`.
AEG pill (`id="hp-aeg"`) always gets `border:1px solid rgba(167,139,250,.3)`.

### 5. `HOME_PILL_MAP` and `HOME_PILL_LABEL` objects (in `<script>`)
Keep these in sync with HB_SERVICES — maps `port → DOM id` and `port → display label`.

### 6. `K9_LAUNCH_CMDS` object
Update paths if the launcher script moves. Current:
```js
start:   'cd ~/k9-llm-router && ./launch-economic-stack.sh start',
stop:    'cd ~/k9-llm-router && ./launch-economic-stack.sh stop',
restart: 'cd ~/k9-llm-router && ./launch-economic-stack.sh restart',
status:  'cd ~/k9-llm-router && ./launch-economic-stack.sh status',
compose: 'cd ~/k9-llm-router && docker compose --env-file .env.compose up -d',
logs:    'cd ~/k9-llm-router && ./launch-economic-stack.sh logs k9-llm-router',
aeg:     'cd ~/aeg-protocol && npm install && AEG_NODE_ID=$AEG_NODE_ID AEG_OPERATOR_KEY=$AEG_OPERATOR_KEY npx tsx sdk/aegAdapter.ts',
```

### 7. Terminal command blocks (in `const TERM = {` or `TERM_CMDS`)
Blocks by key: `tmux-k9`, `wsl-start`, `docker-stack`, `aeg-start`, `git-pr`
Update port numbers in shell command strings inside `lines:[...]` arrays when ports change.
Add a new key for any new service: `'SERVICE-start':{badge:'SERVICE · :PORT', blocks:[...]}`

### 8. Project cards (`id="projList"`)
Each `<div class="proj-card">` has `data-cmd` (maps to TERM key) and `data-proj`.
Update `.proj-port` text to show current port when ports change.
Add new cards for new services. AEG card gets `style="border-color:rgba(167,139,250,.3)"`.

### 9. Sprint badge / AEG right panel (`class="hz hz-right3"`)
`id="home-aeg-sprint"` text should show current sprint: e.g. `SPRINT AEG-4 ✓` after AEG-4 completes.
Add a next-sprint shortcut pill with `onclick="copyToClip('...')"`.

### 10. Footer bar (`class="cell-footer"`)
Update `.f-val` text for LLM and AEG labels to reflect current sprint.

---

## Validation Checklist (run after every edit)

Confirm these are true in the final file:

- [ ] `const HB_SERVICES` has no ports 8080, 8888, 4000, 3000
- [ ] `id="heartbeatList"` has rows matching every HB_SERVICES entry
- [ ] `HOME_PILL_MAP` keys match HB_SERVICES ports
- [ ] Quick launch `<a>` hrefs match current ports
- [ ] `hp-aeg` pill exists in `.hz-left`
- [ ] `home-launch-btn` exists in `.hz-bottom` with `onclick="homeLaunchStack()"`
- [ ] `runHomeStackPoll()` is called at boot + `setInterval(runHomeStackPoll, 30000)`
- [ ] `checkPort(port, path)` accepts 2 args and uses `path||'/health'`
- [ ] `checkPort(svc.port, svc.path)` called inside `runHeartbeat()`
- [ ] `aeg-start` key exists in TERM_CMDS
- [ ] `aeg-node-adapter` project card exists in `#projList`
- [ ] File writes back without truncation (line count should be ≥ 5000)

---

## AEG-4 Sprint Additions (next update)

When AEG-4 (ERC-20 + Governor + Treasury deploy) completes, additionally:

1. Update `home-aeg-sprint` text → `SPRINT AEG-4 ✓`
2. Add pill: `POA CONTRACT · Base Sepolia ✓` with green styling
3. Update `aeg-start` TERM block to include the deployed contract address in the curl examples
4. Add `POA_CONTRACT_ADDRESS` to the environment notes in the wsl-start block
5. Update the "AEG-4 next" pill in hz-right3 → "AEG-5 next: staking UI"

---

## File Locations

- Primary: `~/k9-wallpaper/wallpaper.html`
- Mirror: `~/k9-llm-router/wallpaper/wallpaper.html`
- After editing: copy primary → mirror, then `git add -A && git commit && git push`
- Commit message format: `feat(wallpaper): Sprint [NAME] — [one-line summary]`
