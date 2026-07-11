/**
 * _shared/agent-adapter.ts — Universal Agent Adapter Base
 * ─────────────────────────────────────────────────────────────────────────────
 * Lovable Edge Function shared module — imported by all four agent adapters:
 *   agent-gpt, agent-claude, agent-gemini, agent-local
 *
 * Contract:
 *   - Takes a cb.v1 CBMessage (type: "user_intent")
 *   - Calls the appropriate model via Lovable AI Gateway
 *   - Returns a cb.v1 CBMessage (type: "agent_response")
 *   - NEVER calls exchange/wallet APIs — inference only
 *
 * All keys are managed via Lovable AI Gateway — no raw API keys here.
 * ─────────────────────────────────────────────────────────────────────────────
 */

import {
  createAgentResponse,
  type CBMessage,
  type CBSource,
  type CBOntologyTag,
} from "./cb-schema.ts";

// ── Lovable AI Gateway config ──────────────────────────────────────────────────
// Keys injected by Lovable platform — no .env management needed

const AI_GATEWAY_URL = Deno.env.get("AI_GATEWAY_URL") ?? "https://api.lovable.dev/ai";

export type AdapterModel =
  | "gpt-5-mini"       // OpenAI — agent-gpt
  | "claude-sonnet-4-5" // Anthropic — agent-claude
  | "gemini-3-flash"   // Google — agent-gemini
  | "local-llama"      // Sovereign Ollama — agent-local (placeholder until online)
  | string;

export interface AdapterConfig {
  source:     CBSource;
  model:      AdapterModel;
  system?:    string;
  maxTokens?: number;
}

export interface AdapterResult {
  response_message: CBMessage;
  model_used:       string;
  latency_ms:       number;
  tokens_used?:     number;
}

// ── System prompt template ─────────────────────────────────────────────────────

function buildSystemPrompt(model: AdapterModel, customSystem?: string): string {
  const base = `You are a K-9 Cognitive Bus agent (${model}).
You receive user intents and produce structured analytical responses.
Rules:
- Never suggest executing trades or wallet transactions directly
- Always flag if confidence is below 0.6
- Respond in the context of the payload's ontology_tags
- Keep responses concise and actionable`;

  return customSystem ? `${base}\n\n${customSystem}` : base;
}

// ── Lovable AI Gateway call ────────────────────────────────────────────────────

async function callGateway(
  model: AdapterModel,
  systemPrompt: string,
  userContent: string,
  maxTokens: number = 500
): Promise<{ text: string; tokens: number; latency_ms: number }> {
  const start = Date.now();

  // Normalize model names to gateway format
  const modelMap: Record<string, string> = {
    "gpt-5-mini":        "openai/gpt-4o-mini",
    "claude-sonnet-4-5": "anthropic/claude-sonnet-4-5",
    "gemini-3-flash":    "google/gemini-2.0-flash",
    "local-llama":       "anthropic/claude-sonnet-4-5", // placeholder fallback
  };
  const gatewayModel = modelMap[model] ?? model;

  const resp = await fetch(`${AI_GATEWAY_URL}/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      // Lovable AI Gateway handles auth via platform context
    },
    body: JSON.stringify({
      model: gatewayModel,
      messages: [
        { role: "system", content: systemPrompt },
        { role: "user",   content: userContent },
      ],
      max_tokens: maxTokens,
    }),
  });

  if (!resp.ok) {
    const err = await resp.text();
    throw new Error(`AI Gateway error ${resp.status}: ${err}`);
  }

  const data = await resp.json();
  const text = data.choices?.[0]?.message?.content
    ?? data.content?.[0]?.text
    ?? data.text
    ?? "";

  return {
    text,
    tokens:     data.usage?.total_tokens ?? 0,
    latency_ms: Date.now() - start,
  };
}

// ── Confidence estimation ──────────────────────────────────────────────────────

function estimateConfidence(text: string, model: AdapterModel): number {
  // Basic heuristic — sovereign kernel will apply AEG scoring on top
  const hasHedging = /uncertain|unclear|unsure|cannot|i don't know/i.test(text);
  const hasAssertion = /recommend|suggest|signal|buy|sell|hold|expect/i.test(text);

  if (hasHedging)   return 0.5;
  if (hasAssertion) return 0.78;
  return 0.65;
}

// ── Main adapter function ──────────────────────────────────────────────────────

export async function runAdapter(
  inbound: CBMessage,
  config: AdapterConfig
): Promise<AdapterResult> {
  const { source, model, system, maxTokens } = config;

  // Extract user-readable content from payload
  const userContent = typeof inbound.payload.content === "string"
    ? inbound.payload.content
    : JSON.stringify(inbound.payload);

  const systemPrompt = buildSystemPrompt(model, system);

  const { text, tokens, latency_ms } = await callGateway(
    model, systemPrompt, userContent, maxTokens
  );

  const confidence = estimateConfidence(text, model);

  const response_message = createAgentResponse(
    source,
    {
      content:     text,
      model_used:  model,
      tokens_used: tokens,
      latency_ms,
      input_trace: inbound.message_id,
    },
    inbound.message_id,   // causation_id
    inbound.trace_id,     // same trace chain
    confidence,
    inbound.ontology_tags // inherit tags from intent
  );

  return { response_message, model_used: model, latency_ms, tokens_used: tokens };
}
