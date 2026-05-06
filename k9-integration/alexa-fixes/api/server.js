/**
 * api/server.js — Vercel Serverless Entry for AlexaMobileWeb Express Server
 *
 * Wraps the Express app as a Vercel serverless function.
 * All /api/* requests are routed here by vercel.json rewrites.
 *
 * Vercel expects: module.exports = handler(req, res)
 */

// Dynamic import for ESM-compiled server
let appPromise;

function getApp() {
  if (!appPromise) {
    appPromise = import("../dist/index.js")
      .then((mod) => mod.default || mod.app || mod)
      .catch((err) => {
        console.error("[Vercel] Failed to load server:", err.message);
        throw err;
      });
  }
  return appPromise;
}

module.exports = async (req, res) => {
  try {
    const app = await getApp();
    // Express app as handler
    return app(req, res);
  } catch (err) {
    console.error("[Vercel] Server error:", err.message);
    res.status(500).json({ error: "Server initialization failed", message: err.message });
  }
};
