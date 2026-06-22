/**
 * agent-gemini — Gemini 3 Flash Adapter
 * Lovable Edge Function: /functions/v1/agent-gemini
 */
import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { validateCBMessage, type CBMessage } from "../shared-types/cb-schema.ts";
import { runAdapter } from "./agent-adapter.ts";

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
    source: "gemini",
    model:  "gemini-3-flash",
    system: "You are the K-9 cognitive agent Gemini — specialized in multimodal analysis, market data, and real-time pattern recognition.",
  });

  return new Response(JSON.stringify(result.response_message), {
    status: 200,
    headers: { ...cors, "Content-Type": "application/json" },
  });
});
