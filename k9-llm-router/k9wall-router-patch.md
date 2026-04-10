# K-9 Wall — LLM Router Integration Patch

## What to change in wallpaper.html

### 1. Fix the port (line ~8449)
The Wall currently polls k9-llm-router on port 8888.
Router now runs on **8765**. Update in `HB_SERVICES`:

```js
// BEFORE:
{port:8888,name:'k9-llm-router'},

// AFTER:
{port:8765,name:'k9-llm-router'},
```

---

### 2. Add router model status to DASHBOARD view
Find the left panel of the dashboard view (`id="v-dashboard"`).
Add this block inside the agent status section:

```html
<!-- LLM Router Status Panel -->
<div class="panel-block" id="router-status-panel" style="padding:10px 12px;border-bottom:1px solid var(--border)">
  <div class="lbl" style="font-size:9px;letter-spacing:2px;color:var(--muted);margin-bottom:8px">LLM ROUTER</div>
  <div id="router-mode-badge" style="font-size:10px;color:var(--cyan);margin-bottom:6px">MODE: —</div>
  <div id="router-models" style="display:flex;flex-direction:column;gap:4px">
    <!-- populated by pollRouterStatus() -->
    <div style="font-size:10px;color:var(--dim)">checking models...</div>
  </div>
</div>
```

---

### 3. Add pollRouterStatus() to the script block
Add this function near the other poll functions (e.g. after `agentHeartbeat()`):

```js
/* ══════════════════════════════════════
   LLM ROUTER STATUS PANEL
   Polls k9-llm-router /health and /models
   Updates dashboard router panel.
══════════════════════════════════════ */
const ROUTER_BASE = 'http://localhost:8765';

async function pollRouterStatus() {
  const modeBadge  = document.getElementById('router-mode-badge');
  const modelsDiv  = document.getElementById('router-models');
  if (!modeBadge || !modelsDiv) return;

  try {
    const [healthRes, modelsRes] = await Promise.all([
      fetch(`${ROUTER_BASE}/health`, { signal: AbortSignal.timeout(2500) }),
      fetch(`${ROUTER_BASE}/models`, { signal: AbortSignal.timeout(2500) }),
    ]);

    if (healthRes.ok) {
      const h = await healthRes.json();
      const modeColor = { local: 'var(--green)', cloud: 'var(--amber)', hybrid: 'var(--cyan)' }[h.mode] || 'var(--muted)';
      modeBadge.textContent = `MODE: ${h.mode?.toUpperCase() || '—'}`;
      modeBadge.style.color = modeColor;
    }

    if (modelsRes.ok) {
      const models = await modelsRes.json();
      modelsDiv.innerHTML = Object.entries(models).map(([key, m]) => {
        const dot   = m.healthy ? '●' : '○';
        const color = m.healthy ? 'var(--green)' : 'var(--red)';
        const ms    = m.latency_ms > 0 ? ` · ${Math.round(m.latency_ms)}ms` : '';
        const src   = m.provider === 'ollama' ? '⚙' : '☁';
        return `<div style="display:flex;align-items:center;gap:6px;font-size:10px">
          <span style="color:${color}">${dot}</span>
          <span style="color:var(--text)">${src} ${key}</span>
          <span style="color:var(--muted);margin-left:auto">${m.provider}${ms}</span>
        </div>`;
      }).join('');
    }

  } catch (e) {
    modeBadge.textContent = 'MODE: offline';
    modeBadge.style.color = 'var(--red)';
    modelsDiv.innerHTML = '<div style="font-size:10px;color:var(--red)">router unreachable</div>';
  }
}

// Poll every 15s
setInterval(pollRouterStatus, 15_000);
// Initial call on load
setTimeout(pollRouterStatus, 2000);
```

---

### 4. Add router URL to command shortcuts (TERMINAL / COMMANDS tab)
In the terminal/commands section, add:

```html
<div class="cmd-row" onclick="tapCmd(this)">
  <span class="cmd-code">curl http://localhost:8765/models | python -m json.tool</span>
  <span class="cmd-hint">router models</span>
  <span class="cmd-copy-icon">⎘</span>
</div>
<div class="cmd-row" onclick="tapCmd(this)">
  <span class="cmd-code">curl http://localhost:8765/swarm/health | python -m json.tool</span>
  <span class="cmd-hint">router health</span>
  <span class="cmd-copy-icon">⎘</span>
</div>
<div class="cmd-row" onclick="tapCmd(this)">
  <span class="cmd-code">curl -X POST http://localhost:8765/route -H "Content-Type: application/json" -d '{"task_type":"scout_tutor","messages":[{"role":"user","content":"test"}]}'</span>
  <span class="cmd-hint">test route</span>
  <span class="cmd-copy-icon">⎘</span>
</div>
```

---

## WSL2 install + run sequence

```bash
# 1. Create venv
python3.11 -m venv ~/k9-workspace/venvs/k9-llm-router
source ~/k9-workspace/venvs/k9-llm-router/bin/activate

# 2. Clone / copy repo
cd ~/k9-workspace/core
git clone https://github.com/YOUR_ORG/k9-llm-router
cd k9-llm-router

# 3. Install deps
pip install -r requirements.txt --break-system-packages

# 4. Configure
cp .env.example .env
# edit .env: set LOCAL_MODEL_URL, ANTHROPIC_API_KEY if using hybrid

# 5. Start in tmux
tmux new -s k9-llm-router
python main.py

# 6. Verify
curl http://localhost:8765/swarm/health
curl http://localhost:8765/models

# K-9 Wall will now auto-detect on next poll (2s after load)
```

## Ollama model pull (run once on WSL2 or Mac Mini)

```bash
# Priority pulls for K-9 workloads:
ollama pull qwen2.5:72b          # Qwen 3.5 proxy — swarms, desktop, math
ollama pull deepseek-coder-v2    # DeepSeek V4 proxy — finance, trading
ollama pull mistral              # Mistral — reasoning, coding
ollama pull llama3.3:70b         # Llama 4 proxy — fallback
# Optional (if VRAM allows):
ollama pull glm4                 # GLM-5 proxy — SCOUT tutor
```

## Tailscale remote (Mac Mini → WSL2)

If Ollama runs on Mac Mini via Tailscale, set in .env:
```
LOCAL_MODEL_URL=http://100.x.x.x:11434   # replace with Mac Mini Tailscale IP
ROUTER_MODE=local
```
The router handles the rest — WSL2 app → Tailscale → Mac Mini inference.
