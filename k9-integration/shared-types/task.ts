/**
 * K-9 Unified Integration Layer — Shared Type Contracts
 * Sprint INT-1
 *
 * These interfaces define the API surface between:
 *   - Lovable UI apps (consumers)
 *   - Base44 / Vectos agent layer (orchestrator)
 *   - K-9 MCP Manager :3030 (tool registry)
 *   - PackAI compute node :8766 (execution)
 *
 * ALL inter-service calls MUST use these types.
 * Do not invent ad-hoc request shapes.
 */

// ─────────────────────────────────────────────────────────────────────────────
// CORE TASK CONTRACT
// ─────────────────────────────────────────────────────────────────────────────

export type TaskType =
  | "inference"
  | "trading_signal"
  | "market_data"
  | "rag_query"
  | "options_pricing"
  | "sentiment"
  | "portfolio_analysis"
  | "regime_detection"
  | "onchain_data"
  | "voice_command"
  | "ui_action";    // Lovable-originated UI tasks

export interface TaskRequest {
  task_id?:    string;               // UUID — generated if omitted
  task_type:   TaskType;
  input:       string;               // primary prompt / instruction
  context?:    Record<string, any>;  // optional structured context
  messages?:   ChatMessage[];        // chat history (inference tasks)
  system?:     string;               // system prompt override
  max_tokens?: number;               // default: 1000
  source?:     TaskSource;           // who originated this task
  priority?:   "low" | "normal" | "high";
}

export interface TaskResponse {
  task_id:      string;
  status:       "completed" | "failed" | "pending";
  result:       string;
  metadata?:    TaskMetadata;
  error?:       string;
}

export interface TaskMetadata {
  model_used?:    string;
  latency_ms?:    number;
  cost_usd?:      number;
  tool_used?:     string;
  aeg_proof?:     AEGProofRef;    // present if task generated an on-chain proof
  timestamp:      string;         // ISO 8601
}

// ─────────────────────────────────────────────────────────────────────────────
// SOURCE TRACKING
// ─────────────────────────────────────────────────────────────────────────────

export type TaskSource =
  | "lovable"      // originated from a Lovable UI app
  | "vectos"       // originated from Base44 / Vectos agent
  | "mcp"          // originated from MCP tool call
  | "packai"       // originated from PackAI compute node
  | "n8n"          // originated from n8n workflow
  | "cli"          // originated from direct CLI / script
  | "aeg-adapter"; // originated from AEG node adapter :8768

// ─────────────────────────────────────────────────────────────────────────────
// CHAT / MESSAGES
// ─────────────────────────────────────────────────────────────────────────────

export interface ChatMessage {
  role:    "user" | "assistant" | "system";
  content: string;
}

// ─────────────────────────────────────────────────────────────────────────────
// MCP TOOL INTERFACE
// ─────────────────────────────────────────────────────────────────────────────

export interface MCPToolCall {
  tool_id:  string;    // e.g. "lovable.execute", "k9.route", "ollama"
  method:   string;    // e.g. "tool.ui.execute", "tool.llm.route"
  params:   Record<string, any>;
  caller?:  TaskSource;
}

export interface MCPToolResult {
  tool_id:      string;
  method:       string;
  success:      boolean;
  result?:      any;
  error?:       string;
  duration_ms:  number;
  fallback_used?: boolean;
}

// ─────────────────────────────────────────────────────────────────────────────
// LOVABLE APP INTERFACE
// Used when Base44 / MCP calls a Lovable-deployed UI app's API
// ─────────────────────────────────────────────────────────────────────────────

export interface LovableAppConfig {
  app_id:       string;         // Lovable app identifier
  name:         string;         // human-readable
  base_url:     string;         // deployed URL (e.g. https://myapp.lovable.app)
  mcp_tool_id:  string;         // e.g. "lovable.packai-ui"
  endpoints:    LovableEndpoint[];
  capabilities: TaskType[];     // which task types this app handles
  auth_header?: string;         // optional Bearer token env var name
}

export interface LovableEndpoint {
  path:    string;              // e.g. "/api/task"
  method:  "GET" | "POST" | "PUT" | "DELETE";
  purpose: string;              // human description
  input:   string;              // TypeScript type name (from this file)
  output:  string;              // TypeScript type name
}

// ─────────────────────────────────────────────────────────────────────────────
// AEG PROOF REFERENCE
// Attached to task responses when alignment proof was generated
// ─────────────────────────────────────────────────────────────────────────────

export interface AEGProofRef {
  task_id:         string;    // bytes32 hex
  audit_hash:      string;    // bytes32 hex — keccak256(GovernanceAuditEntry)
  alignment_score: number;    // 0–10000 basis points
  on_chain:        boolean;   // true if submitProof() was called
  tx_hash?:        string;    // Base L2 tx hash if on_chain
}

// ─────────────────────────────────────────────────────────────────────────────
// HEALTH CHECK (standard across all K-9 services)
// ─────────────────────────────────────────────────────────────────────────────

export interface HealthResponse {
  status:     "online" | "degraded" | "offline";
  component:  string;
  version?:   string;
  sprint?:    string;
  uptime_s?:  number;
  timestamp:  string;
}

// ─────────────────────────────────────────────────────────────────────────────
// REGISTRY ENTRY (MCP tool registry + Lovable app registry)
// ─────────────────────────────────────────────────────────────────────────────

export interface ServiceRegistryEntry {
  id:           string;
  type:         "k9-service" | "lovable-app" | "mcp-tool" | "aeg-adapter";
  name:         string;
  base_url:     string;
  port?:        number;
  capabilities: string[];
  status:       "healthy" | "degraded" | "offline" | "unknown";
  registered_at: string;
  last_seen?:   string;
}
