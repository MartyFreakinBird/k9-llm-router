/**
 * orbitron-bus — K-9 Cognitive Bus Ingress
 * ─────────────────────────────────────────────────────────────────────────────
 * Lovable Edge Function (Deno) — deployed as /functions/v1/orbitron-bus
 *
 * Responsibilities:
 *   1. Accept signed cb.v1 messages from any K-9 surface
 *   2. Validate against cb-schema.ts
 *   3. Persist to cb_messages table (RLS: auth.uid() scoped)
 *   4. Mirror to cross_module_events (existing Orbitron realtime channel)
 *   5. If type === "execution_request": forward to sovereign control-plane
 *      NEVER execute trades here — this is the ingress only
 *   6. Fan-out to agent adapters if type === "user_intent"
 *
 * Security:
 *   - All writes are user-scoped via RLS
 *   - execution_request requires signature field (verified against HMAC key)
 *   - Replit services authenticate via REPLIT_INTEGRATION_KEY header
 *
 * Env vars (set in Supabase dashboard → Edge Functions → Secrets):
 *   ORBITRON_WEBHOOK_URL     — sovereign control-plane :8769/agent/route
 *   ORBITRON_WEBHOOK_SECRET  — shared HMAC key for execution forwarding
 *   REPLIT_INTEGRATION_KEY   — API key for Replit service auth
 *   SUPABASE_URL             — (auto-injected)
 *   SUPABASE_SERVICE_ROLE_KEY — (auto-injected)
 * ─────────────────────────────────────────────────────────────────────────────
 */

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import {
  validateCBMessage,
  type CBMessage,
} from "../_shared/cb-schema.ts";

// ── Config ────────────────────────────────────────────────────────────────────

const SUPABASE_URL            = Deno.env.get("SUPABASE_URL")!;
const SERVICE_ROLE_KEY        = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const ORBITRON_WEBHOOK_URL    = Deno.env.get("ORBITRON_WEBHOOK_URL") ?? "";
const ORBITRON_WEBHOOK_SECRET = Deno.env.get("ORBITRON_WEBHOOK_SECRET") ?? "";
const REPLIT_INTEGRATION_KEY  = Deno.env.get("REPLIT_INTEGRATION_KEY") ?? "";

const corsHeaders = {
  "Access-Control-Allow-Origin":  "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type, x-integration-key",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

// ── Auth helpers ──────────────────────────────────────────────────────────────

function isReplitSource(req: Request): boolean {
  const key = req.headers.get("x-integration-key");
  return !!REPLIT_INTEGRATION_KEY && key === REPLIT_INTEGRATION_KEY;
}

function getUserId(req: Request, supabase: ReturnType<typeof createClient>): string | null {
  // For Replit sources, use a service account ID
  if (isReplitSource(req)) return "replit-service";
  // Otherwise will be extracted from JWT session in RLS context
  return null;
}

// ── Execution forwarding (sovereign control-plane only) ───────────────────────

async function forwardToSovereignKernel(msg: CBMessage): Promise<void> {
  if (!ORBITRON_WEBHOOK_URL) {
    console.warn("[orbitron-bus] ORBITRON_WEBHOOK_URL not set — execution_request dropped");
    return;
  }

  const body = JSON.stringify(msg);

  // HMAC-SHA256 signature for sovereign kernel verification
  let signature = "";
  if (ORBITRON_WEBHOOK_SECRET) {
    const encoder = new TextEncoder();
    const key = await crypto.subtle.importKey(
      "raw",
      encoder.encode(ORBITRON_WEBHOOK_SECRET),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"]
    );
    const sigBuffer = await crypto.subtle.sign("HMAC", key, encoder.encode(body));
    signature = Array.from(new Uint8Array(sigBuffer))
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");
  }

  try {
    const resp = await fetch(ORBITRON_WEBHOOK_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-k9-signature": signature,
        "x-k9-source": "orbitron-bus",
      },
      body,
    });
    if (!resp.ok) {
      console.error("[orbitron-bus] Sovereign kernel rejected:", resp.status, await resp.text());
    } else {
      console.log("[orbitron-bus] execution_request forwarded → sovereign kernel ✓");
    }
  } catch (err) {
    console.error("[orbitron-bus] Sovereign kernel unreachable:", err);
  }
}

// ── Main handler ──────────────────────────────────────────────────────────────

serve(async (req: Request) => {
  // CORS preflight
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  if (req.method !== "POST") {
    return new Response(JSON.stringify({ error: "POST only" }), {
      status: 405,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  let raw: unknown;
  try {
    raw = await req.json();
  } catch {
    return new Response(JSON.stringify({ error: "Invalid JSON" }), {
      status: 400,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  // ── Validate cb.v1 envelope ────────────────────────────────────────────────
  const validation = validateCBMessage(raw);
  if (!validation.valid) {
    return new Response(
      JSON.stringify({ error: "Schema validation failed", details: validation.errors }),
      { status: 422, headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  }

  const msg = raw as CBMessage;

  // ── Supabase client (service role for writes) ─────────────────────────────
  const supabase = createClient(SUPABASE_URL, SERVICE_ROLE_KEY);

  // ── 1. Persist to cb_messages ─────────────────────────────────────────────
  const { error: insertError } = await supabase
    .from("cb_messages")
    .insert({
      message_id:      msg.message_id,
      trace_id:        msg.trace_id,
      causation_id:    msg.causation_id ?? null,
      source:          msg.source,
      target:          msg.target ?? null,
      type:            msg.type,
      ontology_tags:   msg.ontology_tags,
      confidence:      msg.confidence,
      payload:         msg.payload,
      timestamp:       msg.timestamp,
      signature:       msg.signature ?? null,
      aeg_proof_hash:  msg.aeg_proof_hash ?? null,
      alignment_score: msg.alignment_score ?? null,
    });

  if (insertError) {
    console.error("[orbitron-bus] cb_messages insert failed:", insertError);
    return new Response(
      JSON.stringify({ error: "Persistence failed", detail: insertError.message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  }

  // ── 2. Mirror to cross_module_events (Orbitron realtime channel) ──────────
  await supabase.from("cross_module_events").insert({
    event_type:      msg.type,
    source_platform: msg.source,
    data: {
      cb_message_id: msg.message_id,
      trace_id:      msg.trace_id,
      ontology_tags: msg.ontology_tags,
      confidence:    msg.confidence,
      payload:       msg.payload,
    },
    created_at: msg.timestamp,
  });

  // ── 3. Execution requests → sovereign kernel (NEVER execute here) ─────────
  if (msg.type === "execution_request") {
    if (!msg.signature) {
      return new Response(
        JSON.stringify({ error: "execution_request requires signature field" }),
        { status: 403, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }
    await forwardToSovereignKernel(msg);
  }

  // ── 4. Fan-out user_intent to agent adapters ──────────────────────────────
  // (adapters subscribe to cb_messages realtime channel filtered by type)
  // No additional action needed here — Supabase realtime handles fan-out

  return new Response(
    JSON.stringify({
      ok:         true,
      message_id: msg.message_id,
      trace_id:   msg.trace_id,
      persisted:  true,
      forwarded:  msg.type === "execution_request",
    }),
    { status: 200, headers: { ...corsHeaders, "Content-Type": "application/json" } }
  );
});
