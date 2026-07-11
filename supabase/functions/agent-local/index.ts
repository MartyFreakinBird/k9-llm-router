/**
 * agent-local — Sovereign Local LLM Adapter
 * Lovable Edge Function: /functions/v1/agent-local
 *
 * Routes to sovereign Ollama/vLLM via ORBITRON_WEBHOOK_URL first.
 * Falls back to claude-sonnet-4-5 via Lovable AI Gateway if sovereign is offline.
 *
 * This is the privacy-first path — payload should contain no PII when routing local.
 */
import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { validateCBMessage, type CBMessage } from "../_shared/cb-schema.ts";
import { runAdapter } from "../_shared/agent-adapter.ts";

const cors = {
  "Access-Control-Allow-Origin":  "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const SOVEREIGN_URL = Deno.env.get("SOVEREIGN_LLM_URL") ?? ""; // e.g. http://tailscale-ip:8765/route

async function tryLocalRoute(msg: CBMessage): Promise<CBMessage | null> {
  if (!SOVEREIGN_URL) return null;
  try {
    const resp = await fetch(`${SOVEREIGN_URL}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task_type: "general",
        component: "agent-local",
        messages:  [{ role: "user", content: JSON.stringify(msg.payload) }],
      }),
      signal: AbortSignal.timeout(8000),
    });
    if (!resp.ok) return null;
    const data = await resp.json();
    const text = data.content ?? data.result ?? JSON.stringify(data);

    const { createAgentResponse } = await import("../_shared/cb-schema.ts");
    return createAgentResponse(
      "local-llm",
      { content: text, model_used: "sovereign-local", input_trace: msg.message_id },
      msg.message_id,
      msg.trace_id,
      0.75,
      msg.ontology_tags
    );
  } catch {
    return null;
  }
}

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });

  const raw = await req.json().catch(() => null);
  const v = validateCBMessage(raw);
  if (!v.valid) return new Response(JSON.stringify({ error: v.errors }), { status: 422, headers: cors });

  const msg = raw as CBMessage;

  // Try sovereign local first
  const localResult = await tryLocalRoute(msg);
  if (localResult) {
    return new Response(JSON.stringify(localResult), {
      status: 200,
      headers: { ...cors, "Content-Type": "application/json" },
    });
  }

  // Fallback to Lovable AI Gateway (Claude) with local-llm source label
  const result = await runAdapter(msg, {
    source: "local-llm",
    model:  "local-llama",  // agent-adapter.ts maps this → claude-sonnet-4-5 fallback
    system: "You are the K-9 sovereign local agent — prioritize privacy, conciseness, and on-device reasoning patterns.",
  });

  return new Response(JSON.stringify(result.response_message), {
    status: 200,
    headers: { ...cors, "Content-Type": "application/json" },
  });
});
