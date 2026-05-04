/**
 * K-9 Agent API — Lovable caller
 * Sprint INT-1
 *
 * Used by Base44 / Vectos agent layer to call Lovable apps.
 * Routes through MCP Manager at :3030 by default.
 * Falls back to direct HTTP if MCP is unavailable.
 *
 * Usage (in any K-9 service or Base44 backend function):
 *   import { callLovable, callLovableDirect } from "./callLovable";
 *   const result = await callLovable("lovable.packai-ui", "what is my portfolio value?");
 */

import type { TaskRequest, TaskResponse, MCPToolCall, MCPToolResult } from "../shared-types/task";

const MCP_URL     = process.env.MCP_URL       ?? "http://localhost:3030";
const PACKAI_URL  = process.env.PACKAI_UI_URL  ?? "https://packai.lovable.app";

// ─────────────────────────────────────────────────────────────────────────────
// PRIMARY: Route through MCP Manager (preferred — tracked, fallback-capable)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Call a Lovable app via K-9 MCP Manager.
 * MCP handles health checks, fallbacks, cost tracking, and AEG proof generation.
 */
export async function callLovable(
  toolId: string,
  input: string,
  context?: Record<string, any>
): Promise<TaskResponse> {
  const body: MCPToolCall = {
    tool_id: toolId,
    method:  "tool.lovable.execute",
    params:  { input, context },
    caller:  "vectos",
  };

  const res = await fetch(`${MCP_URL}/tool/call`, {
    method:  "POST",
    headers: { "Content-Type": "application/json", "X-K9-Source": "vectos-agent" },
    body:    JSON.stringify(body),
    signal:  AbortSignal.timeout(30_000),
  });

  if (!res.ok) {
    throw new Error(`MCP call failed: HTTP ${res.status} — ${await res.text()}`);
  }

  const mcpResult: MCPToolResult = await res.json();

  if (!mcpResult.success) {
    throw new Error(`MCP tool error: ${mcpResult.error}`);
  }

  return mcpResult.result as TaskResponse;
}

// ─────────────────────────────────────────────────────────────────────────────
// FALLBACK: Direct HTTP call to Lovable app (bypass MCP)
// Use only when MCP is down or for testing
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Call a Lovable app directly (no MCP routing).
 * WARNING: Bypasses cost tracking, health fallback, and AEG proof generation.
 */
export async function callLovableDirect(
  baseUrl: string,
  input: string,
  context?: Record<string, any>,
  authToken?: string
): Promise<TaskResponse> {
  const body: TaskRequest = {
    task_type: "ui_action",
    input,
    context,
    source: "vectos",
  };

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "X-K9-Source":  "vectos-agent-direct",
  };

  if (authToken) {
    headers["Authorization"] = `Bearer ${authToken}`;
  }

  const res = await fetch(`${baseUrl}/api/task`, {
    method: "POST",
    headers,
    body:   JSON.stringify(body),
    signal: AbortSignal.timeout(30_000),
  });

  if (!res.ok) {
    throw new Error(`Lovable direct call failed: HTTP ${res.status} — ${await res.text()}`);
  }

  return res.json() as Promise<TaskResponse>;
}

// ─────────────────────────────────────────────────────────────────────────────
// CONVENIENCE WRAPPERS (specific app targets)
// ─────────────────────────────────────────────────────────────────────────────

/** Call PackAI UI specifically */
export async function callPackAIUI(
  input: string,
  context?: Record<string, any>
): Promise<TaskResponse> {
  return callLovable("lovable.packai-ui", input, context);
}

// ─────────────────────────────────────────────────────────────────────────────
// ROUTE TASK (full K-9 task pipeline — use this for agent-originated tasks)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Route a full TaskRequest through the K-9 orchestrator at :8744.
 * Orchestrator handles: paymaster gate → MCP → execution → AEG proof
 */
export async function routeTask(task: TaskRequest): Promise<TaskResponse> {
  const ORCH_URL = process.env.K9_ORCH_URL ?? "http://localhost:8744";

  const res = await fetch(`${ORCH_URL}/execute`, {
    method:  "POST",
    headers: {
      "Content-Type": "application/json",
      "X-K9-Source":  "vectos-agent",
    },
    body:   JSON.stringify(task),
    signal: AbortSignal.timeout(60_000),
  });

  if (!res.ok) {
    throw new Error(`Orchestrator task failed: HTTP ${res.status} — ${await res.text()}`);
  }

  return res.json() as Promise<TaskResponse>;
}
