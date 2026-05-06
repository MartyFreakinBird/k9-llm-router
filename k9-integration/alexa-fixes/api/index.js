/**
 * api/index.js — Vercel Serverless Function
 *
 * Catches all /api/* requests and forwards them to the Express server.
 * Built from server/vercel.ts → dist/vercel.js by the build script.
 *
 * vercel.json routes: /api/(.*) → /api/index
 */

let handlerPromise;

function getHandler() {
  if (!handlerPromise) {
    handlerPromise = import("../dist/vercel.js")
      .then((mod) => mod.default)
      .catch((err) => {
        console.error("[Vercel] Failed to load handler:", err.message);
        return null;
      });
  }
  return handlerPromise;
}

module.exports = async (req, res) => {
  const handler = await getHandler();
  if (!handler) {
    return res.status(503).json({
      error: "Server unavailable",
      message: "API server failed to initialize. Check build logs."
    });
  }
  return handler(req, res);
};
