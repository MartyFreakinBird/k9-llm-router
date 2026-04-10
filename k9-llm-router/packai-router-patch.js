/**
 * packai-router-patch.js
 * ─────────────────────────────────────────────────────────────────────────────
 * Drop-in replacement for callScout() and callFinanceCoach() in
 * packai-k9-unified.jsx.
 *
 * Routes inference through k9-llm-router instead of hitting Anthropic directly.
 * Supports: local Ollama (via router), cloud fallback, hybrid.
 *
 * USAGE: Replace the two async functions in packai-k9-unified.jsx with these.
 * Set LLM_ROUTER_URL to your k9-llm-router endpoint.
 *
 * Local WSL2:    http://localhost:8765
 * Tailscale:     http://100.x.x.x:8765
 * Replit edge:   https://k9-llm-router.your-replit-username.repl.co
 * ─────────────────────────────────────────────────────────────────────────────
 */

// ── CONFIG ────────────────────────────────────────────────────────────────────

const LLM_ROUTER_URL = (() => {
  // Priority: env var → window config → localhost default
  if (typeof process !== "undefined" && process.env?.VITE_LLM_ROUTER_URL)
    return process.env.VITE_LLM_ROUTER_URL;
  if (typeof window !== "undefined" && window.__K9_ROUTER_URL)
    return window.__K9_ROUTER_URL;
  return "http://localhost:8765";
})();

const ROUTER_TIMEOUT_MS = 30_000;

// ── CORE ROUTE FUNCTION ───────────────────────────────────────────────────────

/**
 * Route an inference request through k9-llm-router.
 * Falls back to direct Anthropic call if router is unreachable.
 */
async function routeInference({ taskType, messages, system, maxTokens = 1000, component = "packai" }) {
  const controller = new AbortController();
  const tid = setTimeout(() => controller.abort(), ROUTER_TIMEOUT_MS);

  try {
    const res = await fetch(`${LLM_ROUTER_URL}/route`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      body: JSON.stringify({
        task_type: taskType,
        messages,
        system: system || null,
        max_tokens: maxTokens,
        component,
      }),
    });
    clearTimeout(tid);

    if (!res.ok) throw new Error(`Router returned ${res.status}`);
    const data = await res.json();
    console.debug(`[K9-Router] ${taskType} → ${data.model_used} (${data.latency_ms}ms, ${data.backend})`);
    return data.content;

  } catch (err) {
    clearTimeout(tid);
    console.warn(`[K9-Router] Router unreachable (${err.message}) — falling back to direct Anthropic`);
    return null; // caller handles fallback
  }
}

// ── PACKAI REPLACEMENTS ───────────────────────────────────────────────────────

/**
 * SCOUT AI Tutor — replaces callScout() in packai-k9-unified.jsx
 * Routes: scout_tutor → GLM-5 (local) or Claude (cloud fallback)
 */
async function callScout(messages, ctx) {
  const system = SCOUT_SYSTEM(ctx); // existing function — unchanged

  // Try router first
  const routerResult = await routeInference({
    taskType: "scout_tutor",
    messages,
    system,
    maxTokens: 1000,
    component: "packai-scout",
  });

  if (routerResult) return routerResult;

  // Direct Anthropic fallback (existing behavior)
  const res = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model: "claude-sonnet-4-20250514",
      max_tokens: 1000,
      system,
      messages,
    }),
  });
  const data = await res.json();
  return data.content?.find((b) => b.type === "text")?.text
    || "Let me think about that... 🤔 What do YOU think the first step is?";
}

/**
 * Finance Coach — replaces callFinanceCoach() in packai-k9-unified.jsx
 * Routes: finance_coach → DeepSeek V4 (local) or Claude (cloud fallback)
 */
async function callFinanceCoach(week, gross, net, rewardFund) {
  const completedA = ACADEMIC_TASKS.filter((t) => week.academic[t.id]).map((t) => t.label);
  const completedC = CHORE_TASKS.filter((t) => week.chores[t.id]).map((t) => t.label);

  const prompt = `You are a sharp financial literacy coach for a teenager using a gamified "Total Compensation Package" system.

This week: Gross $${gross}, CoL deduction $${WEEKLY_COL}, Net reward pay $${net}.
Cumulative reward fund: $${rewardFund}.
Academic tasks: ${completedA.join(", ") || "none"}.
Operations tasks: ${completedC.join(", ") || "none"}.

Give a SHORT (2-3 sentence), motivating, real-world financial insight tied to their exact numbers. Reference one real career/finance concept. Be direct and specific. One emoji at the very start only.`;

  const messages = [{ role: "user", content: prompt }];

  // Try router first
  const routerResult = await routeInference({
    taskType: "finance_coach",
    messages,
    maxTokens: 1000,
    component: "packai-finance",
  });

  if (routerResult) return routerResult;

  // Direct Anthropic fallback
  const res = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model: "claude-sonnet-4-20250514",
      max_tokens: 1000,
      messages,
    }),
  });
  const data = await res.json();
  return data.content?.[0]?.text || "Keep pushing — every dollar earned builds the habit.";
}

// ── ROUTER HEALTH UTIL (optional — use in K9 Wall status panel) ──────────────

async function getRouterHealth() {
  try {
    const res = await fetch(`${LLM_ROUTER_URL}/swarm/health`, {
      signal: AbortSignal.timeout(3000),
    });
    return res.ok ? await res.json() : null;
  } catch {
    return null;
  }
}

async function getRouterModels() {
  try {
    const res = await fetch(`${LLM_ROUTER_URL}/models`, {
      signal: AbortSignal.timeout(3000),
    });
    return res.ok ? await res.json() : null;
  } catch {
    return null;
  }
}

export { callScout, callFinanceCoach, getRouterHealth, getRouterModels, LLM_ROUTER_URL };
