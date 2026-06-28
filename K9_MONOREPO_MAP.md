# K-9 Ecosystem — Monorepo Map

> Last updated: 2026-04-14
> Operator: VectOS Carbon

## Repository Index

| Repo | Type | Port | Status | GitHub |
|---|---|---|---|---|
| `k9-llm-router` | Core router + economic stack | :8765 :8744 :9002 :3030 | ✅ Active | [link](https://github.com/MartyFreakinBird/k9-llm-router) |
| `AlexaMobileWeb` | PackAI multi-agent web platform | :5000 | ✅ Normalized | [link](https://github.com/MartyFreakinBird/AlexaMobileWeb) |
| `MapPackManager` | Automotive LAM + Transmission UI | :5000 | ✅ Normalized | [link](https://github.com/MartyFreakinBird/MapPackManager) |
| `orbitron-integrator` | Orbitron trading UI (React+Vite) | — | 🔒 Private | — |
| `fed-whisperer` | FedWhisperer UI + Supabase functions | — | 🔒 Private | — |
| `ai-yield-whisperer` | DeFi AI platform + PackAI Leader | — | 🔒 Private | — |

## Folder Convention (per repo)

```
/
├── client/              # React/TSX frontend
│   └── src/
│       ├── components/  # UI components
│       ├── pages/       # Route pages
│       └── hooks/       # Custom hooks
├── server/              # Express backend
├── chromebook-agent/    # Python edge agent (AlexaMobileWeb only)
├── edge-deployments/    # Edge node deployment scripts
├── docs/                # Architecture specs and integration contracts
├── .github/workflows/   # CI/CD pipelines
├── Dockerfile           # Production container
├── README.md            # Service overview
└── .gitignore           # K-9 standard ignores
```

## K-9 Swarm — Port Registry

```
:8765  k9-llm-router        LLM routing + n8n webhooks
:8744  k9-orchestrator      L3 Coordination (9 commands)
:9002  k9-paymaster         Economic agent + CLOB L1
:3030  k9-mcp-manager       Tool registry (14 tools)
:11434 Ollama               Local LLM inference
:5678  n8n                  Workflow automation
:5000  AlexaMobileWeb       PackAI web platform
:5000  MapPackManager       Automotive LAM dashboard (different host)
```

## Import Pipeline

New Replit services → run `import_replit_service.yaml`
Wallpaper/UI updates → run `deploy_wallpaper_bundle.yaml`
Both pipelines → run `k9_pipeline_meta.sh`

## Pending Imports (identified from GitHub Desktop "Other" section)

These local repos still need to be published and normalized:
- Any additional repos in `C:\users\timau\onedrive\documents\github\`

---

