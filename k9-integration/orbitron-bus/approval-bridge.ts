/**
 * approval-bridge — Supabase Edge Function
 * 
 * CB-9 Approval Bridge: Lovable UI → Supabase cb_messages
 * 
 * When the operator approves/rejects from the Lovable ApprovalQueue component,
 * this edge function writes the decision back to cb_messages as a CB v1 envelope.
 * The k9-llm-router polls cb_messages for approval decisions and processes them.
 * 
 * Flow:
 *   Lovable UI → POST /approval-bridge → write decision to cb_messages
 *   k9-llm-router → polls cb_messages for decision updates → processes
 * 
 * Deploy:
 *   supabase functions deploy approval-bridge --project-ref ziqenqqgnqxqrazmjohs
 * 
 * Env (set in Supabase dashboard > Edge Functions > Secrets):
 *   SUPABASE_URL           — https://ziqenqqgnqxqrazmjohs.supabase.co
 *   SUPABASE_SERVICE_KEY   — service role key (for writing to cb_messages)
 *   APPROVAL_INTEGRATION_KEY — shared secret for auth (optional, checked in x-integration-key header)
 */

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type, x-integration-key",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

interface ApprovalRequest {
  approval_id: string;
  action: "approve" | "reject";
  approved_by: string;
  note?: string;
}

serve(async (req: Request) => {
  // Handle CORS preflight
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: corsHeaders });
  }

  if (req.method !== "POST") {
    return new Response(JSON.stringify({ error: "Method not allowed" }), {
      status: 405,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  // Optional auth check
  const integrationKey = Deno.env.get("APPROVAL_INTEGRATION_KEY");
  if (integrationKey) {
    const provided = req.headers.get("x-integration-key");
    if (provided !== integrationKey) {
      return new Response(JSON.stringify({ error: "Unauthorized" }), {
        status: 403,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }
  }

  // Parse request body
  let body: ApprovalRequest;
  try {
    body = await req.json();
  } catch {
    return new Response(JSON.stringify({ error: "Invalid JSON body" }), {
      status: 400,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  // Validate
  if (!body.approval_id || !body.action) {
    return new Response(JSON.stringify({ error: "approval_id and action required" }), {
      status: 400,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  if (body.action !== "approve" && body.action !== "reject") {
    return new Response(JSON.stringify({ error: "action must be 'approve' or 'reject'" }), {
      status: 400,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  // Get Supabase credentials
  const supabaseUrl = Deno.env.get("SUPABASE_URL") || "https://ziqenqqgnqxqrazmjohs.supabase.co";
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_KEY");

  if (!serviceKey) {
    return new Response(JSON.stringify({ error: "Server not configured — missing SUPABASE_SERVICE_KEY" }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  // Build CB v1 envelope for the decision
  const now = new Date().toISOString();
  const messageId = crypto.randomUUID();
  const traceId = crypto.randomUUID();

  const envelope = {
    spec: "cb.v1",
    message_id: messageId,
    trace_id: traceId,
    source: "lovable-approval-ui",
    target: "k9-approval",
    type: "execution_request",
    ontology_tags: [body.action, "cb-9", "human-decision"],
    confidence: 1.0,
    payload: {
      approval_id: body.approval_id,
      action: body.action,
      approved_by: body.approved_by || "lovable-ui",
      note: body.note || "",
      decided_at: now,
    },
    timestamp: now,
    signature: undefined, // Lovable UI is not a signing authority — k9-llm-router validates
  };

  try {
    // Write to cb_messages
    const insertRes = await fetch(`${supabaseUrl}/rest/v1/cb_messages`, {
      method: "POST",
      headers: {
        "apikey": serviceKey,
        "Authorization": `Bearer ${serviceKey}`,
        "Content-Type": "application/json",
        "Prefer": "return=representation",
      },
      body: JSON.stringify({
        message_id: messageId,
        trace_id: traceId,
        source: "lovable-approval-ui",
        type: "execution_request",
        ontology_tags: [body.action, "cb-9", "human-decision"],
        confidence: 1.0,
        payload: envelope.payload,
        created_at: now,
        spec: "cb.v1",
      }),
    });

    if (!insertRes.ok) {
      const errText = await insertRes.text();
      return new Response(JSON.stringify({ 
        error: "Failed to write to cb_messages", 
        detail: errText,
        status: insertRes.status,
      }), {
        status: 502,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const inserted = await insertRes.json();

    return new Response(JSON.stringify({
      ok: true,
      approval_id: body.approval_id,
      action: body.action,
      message_id: messageId,
      trace_id: traceId,
      written_at: now,
    }), {
      status: 200,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });

  } catch (e) {
    return new Response(JSON.stringify({ 
      error: "Supabase write failed", 
      detail: String(e),
    }), {
      status: 502,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
