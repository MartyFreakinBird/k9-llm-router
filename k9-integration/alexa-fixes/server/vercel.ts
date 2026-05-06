/**
 * server/vercel.ts — Vercel Serverless Entry Point
 *
 * Builds and exports the Express app WITHOUT calling server.listen().
 * Used by api/index.js for Vercel serverless function deployment.
 *
 * The main server/index.ts still handles local development with listen().
 */

import express, { type Request, Response, NextFunction } from "express";
import { registerRoutes } from "./routes";

const app = express();

app.use(express.json({
  verify: (req: any, _res, buf) => {
    req.rawBody = buf;
  }
}));
app.use(express.urlencoded({ extended: false }));

// Request logger (API only)
app.use((req, res, next) => {
  const start = Date.now();
  const path = req.path;

  res.on("finish", () => {
    const duration = Date.now() - start;
    if (path.startsWith("/api")) {
      console.log(`${req.method} ${path} ${res.statusCode} in ${duration}ms`);
    }
  });
  next();
});

// Initialize routes (async — must be awaited before export)
let appReady: Promise<void>;

function ensureReady() {
  if (!appReady) {
    appReady = (async () => {
      await registerRoutes(app);

      try {
        const { registerAllActions } = await import("./lam/register-actions");
        await registerAllActions();
      } catch (e: any) {
        console.warn("[Vercel] LAM actions skipped:", e.message);
      }

      app.use((err: any, _req: Request, res: Response, _next: NextFunction) => {
        const status = err.status || err.statusCode || 500;
        const message = err.message || "Internal Server Error";
        res.status(status).json({ message });
      });
    })();
  }
  return appReady;
}

// Vercel serverless handler — ensures app is initialized before each request
export default async function handler(req: Request, res: Response) {
  await ensureReady();
  return app(req, res);
}

// Also export app for testing
export { app };
