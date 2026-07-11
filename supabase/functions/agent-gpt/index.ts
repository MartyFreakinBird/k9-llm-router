/**
 * agent-gpt — GPT-5-mini Adapter
 * Lovable Edge Function: /functions/v1/agent-gpt
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
  const result = await runAdapter(msg, { source: "gpt", model: "gpt-5-mini" });

  return new Response(JSON.stringify(result.response_message), {
    status: 200,
    headers: { ...cors, "Content-Type": "application/json" },
  });
});
