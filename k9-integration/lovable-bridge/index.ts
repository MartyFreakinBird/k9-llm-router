/**
 * K-9 Lovable Bridge — Sprint INT-1
 *
 * Wraps any Lovable-deployed app as a K-9 compatible service.
 * Registers each app into the MCP tool registry at :3030.
 *
 * Usage:
 *   import { LovableBridge } from "./lovable-bridge";
 *   const bridge = new LovableBridge();
 *   await bridge.register(PACKAI_UI_CONFIG);
 *   const result = await bridge.call("lovable.packai-ui", "ui_action", { input: "..." });
 *
 * Wired into MCP Manager via:
 *   POST http://localhost:3030/tool/register
 *   POST http://localhost:3030/tool/call
 */

import type {
  LovableAppConfig,
  TaskRequest,
  TaskResponse,
  MCPToolResult,
  ServiceRegistryEntry,
} from "../shared-types/task";

// ─────────────────────────────────────────────────────────────────────────────
// KNOWN LOVABLE APPS
// Add new apps here — they auto-register with MCP on startup
// ─────────────────────────────────────────────────────────────────────────────

export const LOVABLE_APP_REGISTRY: LovableAppConfig[] = [
  {
    app_id:      "packai-ui",
    name:        "PackAI UI",
    base_url:    process.env.PACKAI_UI_URL ?? "https://packai.lovable.app",
    mcp_tool_id: "lovable.packai-ui",
    capabilities: ["inference", "ui_action"],
    auth_header: "PACKAI_UI_API_KEY",
    endpoints: [
      {
        path:    "/api/task",
        method:  "POST",
        purpose: "Execute a UI-level task (inference, display, interaction)",
        input:   "TaskRequest",
        output:  "TaskResponse",
      },
      {
        path:    "/api/health",
        method:  "GET",
        purpose: "Health check",
        input:   "none",
        output:  "HealthResponse",
      },
    ],
  },
  // Add additional Lovable apps here:
  // {
  //   app_id:      "orbitron-dashboard",
  //   name:        "Orbitron Dashboard",
  //   base_url:    process.env.ORBITRON_UI_URL ?? "https://orbitron.lovable.app",
  //   mcp_tool_id: "lovable.orbitron-dashboard",
  //   capabilities: ["market_data", "trading_signal", "portfolio_analysis"],
  //   endpoints: [...],
  // },
];

// ─────────────────────────────────────────────────────────────────────────────
// BRIDGE CLASS
// ─────────────────────────────────────────────────────────────────────────────

export class LovableBridge {
  private registry = new Map<string, LovableAppConfig>();
  private mcpUrl: string;

  constructor(mcpUrl = process.env.MCP_URL ?? "http://localhost:3030") {
    this.mcpUrl = mcpUrl;
  }

  /** Register a Lovable app config locally + push to MCP Manager */
  async register(config: LovableAppConfig): Promise<void> {
    this.registry.set(config.mcp_tool_id, config);
    await this.pushToMCP(config);
    console.log(`[lovable-bridge] Registered: ${config.mcp_tool_id} → ${config.base_url}`);
  }

  /** Register all apps in LOVABLE_APP_REGISTRY */
  async registerAll(): Promise<void> {
    for (const app of LOVABLE_APP_REGISTRY) {
      await this.register(app);
    }
  }

  /** Call a Lovable app endpoint via its MCP tool ID */
  async call(
    toolId: string,
    taskType: string,
    input: Omit<TaskRequest, "task_type">
  ): Promise<MCPToolResult> {
    const config = this.registry.get(toolId);
    if (!config) {
      return {
        tool_id: toolId,
        method: taskType,
        success: false,
        error: `Unknown tool: ${toolId}. Is it registered?`,
        duration_ms: 0,
      };
    }

    const endpoint = config.endpoints.find(e => e.method === "POST");
    if (!endpoint) {
      return {
        tool_id: toolId,
        method: taskType,
        success: false,
        error: `No POST endpoint found for ${toolId}`,
        duration_ms: 0,
      };
    }

    const start = Date.now();
    const url = `${config.base_url}${endpoint.path}`;

    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      "X-K9-Source": "lovable-bridge",
    };

    if (config.auth_header) {
      const token = process.env[config.auth_header];
      if (token) headers["Authorization"] = `Bearer ${token}`;
    }

    const body: TaskRequest = {
      task_type: taskType as any,
      source: "lovable",
      ...input,
    };

    try {
      const res = await fetch(url, {
        method: "POST",
        headers,
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(30_000),
      });

      if (!res.ok) {
        throw new Error(`HTTP ${res.status}: ${await res.text()}`);
      }

      const data: TaskResponse = await res.json();

      return {
        tool_id: toolId,
        method: taskType,
        success: true,
        result: data,
        duration_ms: Date.now() - start,
      };
    } catch (err: any) {
      return {
        tool_id: toolId,
        method: taskType,
        success: false,
        error: err.message,
        duration_ms: Date.now() - start,
      };
    }
  }

  /** Push app config to K-9 MCP Manager tool registry */
  private async pushToMCP(config: LovableAppConfig): Promise<void> {
    const entry: ServiceRegistryEntry = {
      id:           config.mcp_tool_id,
      type:         "lovable-app",
      name:         config.name,
      base_url:     config.base_url,
      capabilities: config.capabilities,
      status:       "unknown",
      registered_at: new Date().toISOString(),
    };

    try {
      const res = await fetch(`${this.mcpUrl}/tool/register`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tool_id:     config.mcp_tool_id,
          name:        config.name,
          tier:        "cheap",
          base_url:    config.base_url,
          methods:     config.endpoints.map(e => `tool.lovable.${config.app_id}${e.path.replace(/\//g, ".")}`),
          description: `Lovable UI app: ${config.name}`,
        }),
        signal: AbortSignal.timeout(5_000),
      });

      if (!res.ok) {
        console.warn(`[lovable-bridge] MCP register failed for ${config.mcp_tool_id}: HTTP ${res.status}`);
      }
    } catch (err: any) {
      // MCP may not be up yet — not fatal, bridge still works standalone
      console.warn(`[lovable-bridge] MCP unreachable (${err.message}) — ${config.mcp_tool_id} registered locally only`);
    }
  }

  /** Health check all registered Lovable apps */
  async healthCheck(): Promise<Record<string, "healthy" | "unreachable">> {
    const results: Record<string, "healthy" | "unreachable"> = {};

    for (const [toolId, config] of this.registry) {
      const healthEndpoint = config.endpoints.find(e => e.path.includes("health") && e.method === "GET");
      const url = healthEndpoint
        ? `${config.base_url}${healthEndpoint.path}`
        : `${config.base_url}/api/health`;

      try {
        const res = await fetch(url, { signal: AbortSignal.timeout(5_000) });
        results[toolId] = res.ok ? "healthy" : "unreachable";
      } catch {
        results[toolId] = "unreachable";
      }
    }

    return results;
  }

  list(): LovableAppConfig[] {
    return [...this.registry.values()];
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// MCP TOOL DEFINITION (drop into MCP Manager TOOL_REGISTRY)
// ─────────────────────────────────────────────────────────────────────────────

export function makeLovableMCPTool(config: LovableAppConfig, bridge: LovableBridge) {
  return {
    name:        config.mcp_tool_id,
    description: `Lovable UI app: ${config.name} at ${config.base_url}`,
    execute: async (input: string, context?: Record<string, any>) => {
      return bridge.call(config.mcp_tool_id, "ui_action", { input, context });
    },
  };
}
