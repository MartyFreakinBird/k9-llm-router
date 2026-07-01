/**
 * cb-bridge — Cognitive Bus Bridge Edge Function
 * 
 * Runs on Lovable Cloud (logkumycxztvnnatheup)
 * 
 * Subscribes to cb_messages realtime on the Cloud instance and forwards
 * relevant events to the original Orbitron instance's /external-integration
 * endpoint. Acts as the single authenticated bridge between:
 *   Cloud CB plane  (logkumycxztvnnatheup) — experimental CB traffic
 *   Sovereign plane (ziqenqqgnqxqrazmjohs) — production trading / signals
 * 
 * Architecture: Path B (recommended)
 *   Cloud Bus  → cb_messages realtime → cb-bridge → original /external-integration/event
 *   No direct DB writes to original instance — all data via edge function endpoints only
 * 
 * Env secrets required (set in Lovable Cloud dashboard):
 *   ORIGINAL_SUPABASE_URL      — https://ziqenqqgnqxqrazmjohs.supabase.co
 *   ORIGINAL_SUPABASE_ANON_KEY — publishable key for original instance
 *   ORIGINAL_INTEGRATION_KEY   — ork_live_... key from original instance api_configurations
 *                                (optional — mint via /integration-auth, then store here)
 * 
 * Deploy:
 *   supabase functions deploy cb-bridge --project-ref logkumycxztvnnatheup
 */

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { z } from "https://deno.land/x/zod@v3.22.4/mod.ts";

// ── CB v1 envelope schema (matches orbitron-bus exactly) ──────────────────────
const CBEnvelopeSchema = z.object({
  timestamp:     z.string().datetime(),
  source:        z.string().min(1),
  type:          z.enum([
    "observation", "inference", "execution_request",
    "execution_result", "heartbeat", "alert", "audit"
  ]),
  ontology_tags: z.array(z.string()).default([]),
  confidence:    z.number().min(0).max(1),
  payload:       z.record(z.unknown()),
  trace_id:      z.string().uuid(),
  causation_id:  z.string().uuid().optional(),
  signature:     z.string().optional(),
});

type CBEnvelope = z.infer<typeof CBEnvelopeSchema>;

// ── Types that get forwarded to the original instance ─────────────────────────
// execution_request always forwarded (with signature check)
// alert, audit always forwarded
// inference forwarded if confidence >= 0.65
// heartbeat, observation NOT forwarded (CB-internal only)
const FORWARD_TYPES = new Set(["execution_request", "alert", "audit"]);
const FORWARD_IF_CONFIDENT = new Set(["inference"]);
const CONFIDENCE_GATE = 0.65;

const corsHeaders = {
  "Access-Control-Allow-Origin":  "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type, x-integration-key",
};

serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  // ── Load secrets ───────────────────────────────────────────────────────────
  const ORIGINAL_URL     = Deno.env.get("ORIGINAL_SUPABASE_URL")      ?? "";
  const ORIGINAL_ANON    = Deno.env.get("ORIGINAL_SUPABASE_ANON_KEY") ?? "";
  const INTEGRATION_KEY  = Deno.env.get("ORIGINAL_INTEGRATION_KEY")   ?? "";

  if (!ORIGINAL_URL || !ORIGINAL_ANON) {
    return new Response(
      JSON.stringify({ error: "Bridge not configured — missing ORIGINAL_SUPABASE_URL or ORIGINAL_SUPABASE_ANON_KEY" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  }

  try {
    // ── Parse + validate CB envelope ──────────────────────────────────────────
    let body: unknown;
    try {
      body = await req.json();
    } catch {
      return new Response(
        JSON.stringify({ error: "Invalid JSON" }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    const parsed = CBEnvelopeSchema.safeParse(body);
    if (!parsed.success) {
      return new Response(
        JSON.stringify({ error: "CB v1 schema validation failed", issues: parsed.error.issues }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    const envelope: CBEnvelope = parsed.data;

    // ── execution_request: require signature ──────────────────────────────────
    if (envelope.type === "execution_request" && !envelope.signature) {
      return new Response(
        JSON.stringify({ error: "Unsigned execution_request rejected", trace_id: envelope.trace_id }),
        { status: 403, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    // ── Routing decision ──────────────────────────────────────────────────────
    const shouldForward =
      FORWARD_TYPES.has(envelope.type) ||
      (FORWARD_IF_CONFIDENT.has(envelope.type) && envelope.confidence >= CONFIDENCE_GATE);

    const result = {
      trace_id:       envelope.trace_id,
      type:           envelope.type,
      source:         envelope.source,
      confidence:     envelope.confidence,
      forwarded:      false,
      forward_status: null as number | null,
      forward_error:  null as string | null,
      reason:         "" as string,
    };

    if (!shouldForward) {
      result.reason = `type=${envelope.type} confidence=${envelope.confidence} — CB-internal only, not forwarded`;
      return new Response(
        JSON.stringify({ ok: true, bridge: result }),
        { status: 200, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    // ── Forward to original instance /external-integration/event ─────────────
    const forwardPayload = {
      event_type:    "cb_bridge_forward",
      source:        envelope.source,
      cb_type:       envelope.type,
      trace_id:      envelope.trace_id,
      causation_id:  envelope.causation_id,
      confidence:    envelope.confidence,
      ontology_tags: envelope.ontology_tags,
      payload:       envelope.payload,
      forwarded_at:  new Date().toISOString(),
      // execution_request: include signature for downstream verification
      ...(envelope.signature ? { signature: envelope.signature } : {}),
    };

    const forwardHeaders: Record<string, string> = {
      "Content-Type":  "application/json",
      "apikey":        ORIGINAL_ANON,
      "Authorization": `Bearer ${ORIGINAL_ANON}`,
    };

    // Add integration key if available (authenticated forward)
    if (INTEGRATION_KEY) {
      forwardHeaders["x-integration-key"] = INTEGRATION_KEY;
    }

    const forwardRes = await fetch(
      `${ORIGINAL_URL.replace(/\/$/, "")}/functions/v1/external-integration`,
      {
        method:  "POST",
        headers: forwardHeaders,
        body:    JSON.stringify({
          type:    "event",
          source:  "cb-bridge",
          data:    forwardPayload,
        }),
        signal: AbortSignal.timeout(8000), // 8s timeout
      }
    );

    result.forwarded      = true;
    result.forward_status = forwardRes.status;
    result.reason = forwardRes.ok
      ? `forwarded to original instance — ${forwardRes.status}`
      : `forward attempted but original instance returned ${forwardRes.status}`;

    if (!forwardRes.ok) {
      const errText = await forwardRes.text().catch(() => "(no body)");
      result.forward_error = errText.slice(0, 200);
      console.error(`[cb-bridge] Forward failed: ${forwardRes.status} — ${errText.slice(0, 200)}`);
    } else {
      console.log(`[cb-bridge] ✅ Forwarded ${envelope.type} trace=${envelope.trace_id} → ${forwardRes.status}`);
    }

    return new Response(
      JSON.stringify({ ok: true, bridge: result }),
      {
        status:  forwardRes.ok ? 200 : 502,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      }
    );

  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[cb-bridge] Unhandled error:", msg);
    return new Response(
      JSON.stringify({ error: "Bridge internal error", detail: msg }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  }
});
