# Vercel Deployment Audit
**Date:** 2026-05-05 | **Operator:** VectOS Carbon

---

## Summary

| Project | URL | HTTP | Root Issue | Fix |
|---------|-----|------|------------|-----|
| alexa-mobile-web | alexa-mobile-web.vercel.app | 200 ⚠️ | Serving raw bundled server JS (not HTML SPA) | vercel.json with outputDirectory + rewrites |
| map-pack-manager | map-pack-manager.vercel.app | 200 ⚠️ | Same — serving server bundle at root | vercel.json with outputDirectory + rewrites |
| k9-llm-router | k9-llm-router.vercel.app | 404 🔴 | Repo root has NO web-servable files — Python FastAPI app, Vercel can't serve it | vercel.json static redirect OR remove from Vercel |

---

## Findings

### 1. alexa-mobile-web — HTTP 200 but WRONG content
- **What Vercel is serving:** `dist/index.js` — the compiled Express server bundle (Node.js code)
- **What it should serve:** `dist/public/index.html` — the Vite-built React SPA
- **Root cause:** No `vercel.json` → Vercel guesses the output, picks the wrong file
- **Side effect:** `/api/health` returns 404 (Express server isn't running — Vercel doesn't run `node dist/index.js`)
- **Fix:** `vercel.json` that points `outputDirectory` to `dist/public`, adds SPA rewrite, and exposes `/api/*` as serverless functions

### 2. map-pack-manager — HTTP 200 but WRONG content
- **Identical issue** to alexa-mobile-web
- Same Express + Vite stack, same missing vercel.json
- Serving raw Node.js bundle instead of SPA HTML

### 3. k9-llm-router — HTTP 404
- **Root cause:** Vercel project points at repo root which contains only: `.agents/`, `incoming_files/`, `k9-integration/`, `k9-llm-router/`
- None of these are web-servable to Vercel (no index.html, no static assets, no Next.js app)
- The actual app (`k9-llm-router/k9-llm-router/`) is a Python FastAPI service — **Vercel cannot run Python FastAPI**
- **Fix options:**
  - A) Add a `vercel.json` with a static landing page pointing users to the WSL2 service
  - B) Remove the Vercel project entirely — this service runs locally on :8765

---

## Fixes Applied
