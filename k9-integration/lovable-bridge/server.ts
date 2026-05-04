/**
 * Lovable Bridge — Fastify HTTP server
 * Sprint INT-1
 *
 * Exposes:
 *   GET  /health           — liveness
 *   GET  /registry         — list registered Lovable apps
 *   POST /call             — call a Lovable app via toolId
 *   GET  /health/apps      — health check all registered apps
 *
 * Port: 8780
 */

import Fastify from "fastify";
import { LovableBridge, LOVABLE_APP_REGISTRY } from "./index";

const PORT   = parseInt(process.env.PORT ?? "8780");
const server = Fastify({ logger: true });
const bridge = new LovableBridge();

// ── Startup: register all Lovable apps ────────────────────────────────────────
server.addHook("onReady", async () => {
  await bridge.registerAll();
  server.log.info("Lovable Bridge ready — registered %d apps", LOVABLE_APP_REGISTRY.length);
});

// ── Routes ────────────────────────────────────────────────────────────────────

server.get("/health", async () => ({
  status:    "online",
  component: "lovable-bridge",
  sprint:    "INT-1",
  apps:      bridge.list().length,
  timestamp: new Date().toISOString(),
}));

server.get("/registry", async () => ({
  apps: bridge.list(),
}));

server.post<{
  Body: { tool_id: string; input: string; context?: Record<string, any> };
}>("/call", async (req, reply) => {
  const { tool_id, input, context } = req.body;

  if (!tool_id || !input) {
    return reply.code(400).send({ error: "tool_id and input are required" });
  }

  const result = await bridge.call(tool_id, "ui_action", { input, context });

  if (!result.success) {
    return reply.code(502).send(result);
  }

  return result;
});

server.get("/health/apps", async () => {
  const results = await bridge.healthCheck();
  return { apps: results };
});

// ── Start ─────────────────────────────────────────────────────────────────────
try {
  await server.listen({ port: PORT, host: "0.0.0.0" });
  console.log(`
╔══════════════════════════════════════════════════╗
║    K-9 LOVABLE BRIDGE  ·  Sprint INT-1          ║
╚══════════════════════════════════════════════════╝
  Port     : ${PORT}
  Registry : http://0.0.0.0:${PORT}/registry
  Call     : POST http://0.0.0.0:${PORT}/call
  Apps     : ${LOVABLE_APP_REGISTRY.length} registered
`);
} catch (err) {
  server.log.error(err);
  process.exit(1);
}
