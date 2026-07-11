/**
 * K-9 Cognitive Bus Schema — cb.v1
 * ─────────────────────────────────────────────────────────────────────────────
 * The universal message envelope for ALL inter-agent communication across:
 *   - Lovable (orbitron-bus edge function)
 *   - HTML container (k9-client.ts)
 *   - Replit services (POST to /orbitron-bus)
 *   - k9-llm-router (aeg_signal_router.py, main.py)
 *   - Sovereign Rust kernel (typeshare-generated Rust types)
 *
 * VERSION: cb.v1
 * SOURCE OF TRUTH: k9-integration/shared-types/cb-schema.ts
 * Consumers import from this file — never redefine locally.
 * ─────────────────────────────────────────────────────────────────────────────
 */

// ── Core envelope ─────────────────────────────────────────────────────────────

export type CBSource =
  | "k9-agent"          // k9-llm-router / orchestrator
  | "lovable"           // Lovable edge functions
  | "replit"            // Replit scalper / sensor services
  | "html-container"    // Browser overlay / PWA
  | "vectos"            // Base44 Superagent
  | "gemini"            // Gemini adapter
  | "claude"            // Claude adapter
  | "gpt"               // GPT adapter
  | "local-llm"         // Sovereign local model (Ollama / vLLM)
  | "user";             // Direct user intent

export type CBMessageType =
  | "user_intent"          // User action from UI / overlay
  | "agent_response"       // LLM agent response
  | "trading_signal"       // Signal generated (NOT execution)
  | "execution_request"    // Execution directive → sovereign kernel only
  | "execution_result"     // Result from sovereign kernel
  | "heartbeat"            // Service liveness
  | "status_update"        // Service status change
  | "alert"                // Error or circuit-breaker event
  | "governance_vote"      // DAO / AEG governance event
  | "knowledge_update"     // RAG ingestor event
  | "system_event";        // Boot, shutdown, config change

export type CBOntologyTag =
  | "defi"
  | "trading"
  | "fed"
  | "macro"
  | "sentiment"
  | "portfolio"
  | "system"
  | "agent"
  | "governance"
  | "risk"
  | "yield"
  | string;  // extensible

export type CBConfidence = number; // 0.0 – 1.0

/**
 * cb.v1 message envelope — every agent, service, and surface speaks this.
 */
export interface CBMessage {
  // ── Envelope ──────────────────────────────────────────────────────────────
  schema_version:  "cb.v1";
  message_id:      string;           // UUID v4
  trace_id:        string;           // UUID — same across a causal chain
  causation_id?:   string;           // message_id that caused this message
  correlation_id?: string;           // for request/reply pairing

  // ── Routing ───────────────────────────────────────────────────────────────
  source:          CBSource;
  target?:         CBSource | CBSource[];  // omit = broadcast to all
  type:            CBMessageType;

  // ── Semantics ─────────────────────────────────────────────────────────────
  ontology_tags:   CBOntologyTag[];
  confidence:      CBConfidence;       // 0.0 = unknown, 1.0 = certain
  payload:         Record<string, unknown>;

  // ── Time + audit ──────────────────────────────────────────────────────────
  timestamp:       string;            // ISO 8601 UTC
  ttl_seconds?:    number;            // optional expiry
  signature?:      string;            // HMAC-SHA256 of canonical payload (sovereign kernel)

  // ── AEG alignment ─────────────────────────────────────────────────────────
  aeg_proof_hash?:    string;         // keccak256 if on-chain proof generated
  alignment_score?:   number;         // 0–10000 bps (AEG token model output)
}

// ── Convenience constructors ──────────────────────────────────────────────────

let _counter = 0;

function uuid(): string {
  // Lightweight UUID v4-ish (no crypto dep — safe for Deno edge + browser)
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

export function createCBMessage(
  partial: Omit<CBMessage, "schema_version" | "message_id" | "trace_id" | "timestamp"> & {
    trace_id?: string;
  }
): CBMessage {
  return {
    schema_version: "cb.v1",
    message_id: uuid(),
    trace_id: partial.trace_id ?? uuid(),
    timestamp: new Date().toISOString(),
    ...partial,
  };
}

export function createUserIntent(
  payload: Record<string, unknown>,
  tags: CBOntologyTag[] = [],
  traceId?: string
): CBMessage {
  return createCBMessage({
    source: "user",
    type: "user_intent",
    ontology_tags: tags,
    confidence: 1.0,
    payload,
    trace_id: traceId,
  });
}

export function createAgentResponse(
  source: CBSource,
  payload: Record<string, unknown>,
  causationId: string,
  traceId: string,
  confidence: CBConfidence = 0.8,
  tags: CBOntologyTag[] = []
): CBMessage {
  return createCBMessage({
    source,
    type: "agent_response",
    ontology_tags: tags,
    confidence,
    payload,
    causation_id: causationId,
    trace_id: traceId,
  });
}

// ── Zod-like runtime validator (no Zod dep — Deno/Node/browser safe) ──────────

export interface CBValidationResult {
  valid: boolean;
  errors: string[];
}

export function validateCBMessage(raw: unknown): CBValidationResult {
  const errors: string[] = [];
  if (typeof raw !== "object" || raw === null) {
    return { valid: false, errors: ["not an object"] };
  }
  const m = raw as Record<string, unknown>;

  if (m.schema_version !== "cb.v1")      errors.push("schema_version must be 'cb.v1'");
  if (typeof m.message_id !== "string")  errors.push("message_id must be string");
  if (typeof m.trace_id !== "string")    errors.push("trace_id must be string");
  if (typeof m.source !== "string")      errors.push("source must be string");
  if (typeof m.type !== "string")        errors.push("type must be string");
  if (!Array.isArray(m.ontology_tags))   errors.push("ontology_tags must be array");
  if (typeof m.confidence !== "number")  errors.push("confidence must be number");
  if (typeof m.payload !== "object")     errors.push("payload must be object");
  if (typeof m.timestamp !== "string")   errors.push("timestamp must be string");

  return { valid: errors.length === 0, errors };
}

// ── Python-compatible JSON schema (for k9-llm-router / sovereign kernel) ─────

export const CB_V1_JSON_SCHEMA = {
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "CBMessage",
  "description": "K-9 Cognitive Bus v1 message envelope",
  "type": "object",
  "required": ["schema_version", "message_id", "trace_id", "source", "type", "ontology_tags", "confidence", "payload", "timestamp"],
  "properties": {
    "schema_version": { "type": "string", "enum": ["cb.v1"] },
    "message_id":     { "type": "string", "format": "uuid" },
    "trace_id":       { "type": "string", "format": "uuid" },
    "causation_id":   { "type": "string", "format": "uuid" },
    "correlation_id": { "type": "string" },
    "source":         { "type": "string" },
    "target":         { "oneOf": [{ "type": "string" }, { "type": "array", "items": { "type": "string" } }] },
    "type":           { "type": "string" },
    "ontology_tags":  { "type": "array", "items": { "type": "string" } },
    "confidence":     { "type": "number", "minimum": 0, "maximum": 1 },
    "payload":        { "type": "object" },
    "timestamp":      { "type": "string", "format": "date-time" },
    "ttl_seconds":    { "type": "number" },
    "signature":      { "type": "string" },
    "aeg_proof_hash": { "type": "string" },
    "alignment_score": { "type": "number", "minimum": 0, "maximum": 10000 }
  },
  "additionalProperties": false
} as const;
