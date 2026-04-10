# K-9 LLM Router — Dockerfile
# Targets: WSL2 Fedora, Replit (edge), Hetzner VPS, RunPod/Vast.ai GPU

FROM python:3.11-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App
COPY main.py .
COPY .env.example .env.example

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:${ROUTER_PORT:-8765}/swarm/health || exit 1

EXPOSE 8765

CMD ["python", "main.py"]
