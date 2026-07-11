/**
 * agent-claude — Claude Sonnet 4.5 Adapter
 * Lovable Edge Function: /functions/v1/agent-claude
 */
import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { validateCBMessage, type CBMessage } from "../_shared/cb-schema.ts";
import { runAdapter } from "../_shared/agent-adapter.ts";

const cors = {
  "Access-Control-Allow-Origin":  "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });

  const raw = await req.json().catch(() => null);
  const v = validateCBMessage(raw);
  if (!v.valid) return new Response(JSON.stringify({ error: v.errors }), { status: 422, headers: cors });

  const msg = raw as CBMessage;
  const result = await runAdapter(msg, {
    source: "claude",
    model:  "claude-sonnet-4-5",
    system: "You are the K-9 cognitive agent Claude — specialized in reasoning, code analysis, and governance decisions.",
  });

  return new Response(JSON.stringify(result.response_message), {
    status: 200,
    headers: { ...cors, "Content-Type": "application/json" },
  });
});
