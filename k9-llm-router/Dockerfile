# K-9 LLM Router — Dockerfile
# Sprint 4 — full economic stack
# Targets: WSL2 Fedora, Hetzner VPS, RunPod/Vast.ai GPU

FROM python:3.11-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Core router
COPY main.py .

# Economic layer — Sprint 3/4
COPY k9_paymaster.py .
COPY k9_mcp_manager.py .
COPY k9_orchestrator.py .
COPY k9_task_queue.py .
COPY k9_worker.py .
COPY k9-swarm-agent.py .

# Launch tooling
COPY launch-economic-stack.sh .
RUN chmod +x launch-economic-stack.sh

# Sub-packages
COPY src/ ./src/
COPY packai-node/ ./packai-node/
COPY n8n-workflows/ ./n8n-workflows/

# Config template (never commit .env itself)
COPY .env.example .env.example

# Health check (main router)
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -f http://localhost:${ROUTER_PORT:-8765}/swarm/health || exit 1

# Default: expose all K-9 ports
EXPOSE 8765 8744 9002 3030 8766

# Default entrypoint: main router only
# Use launch-economic-stack.sh for full stack
CMD ["python", "main.py"]
