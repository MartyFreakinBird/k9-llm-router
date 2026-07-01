# k9-gemini-agent Dockerfile — Sprint 10
# Mirrors k9-llm-router conventions:
#   python:3.11-slim base, non-root, HEALTHCHECK, config-driven startup
#
# Build:   docker build -t k9-gemini-agent:latest k9_gemini_agent/
# Run:     docker run -d --name k9-gemini-agent \
#            -e GEMINI_API_KEY=$GEMINI_API_KEY \
#            -e K9_PAYMASTER_URL=http://host-gateway:9002 \
#            --add-host=host-gateway:host-gateway \
#            -p 8770:8770 \
#            k9-gemini-agent:latest

FROM python:3.11-slim

WORKDIR /app

# System deps — curl only (healthcheck + keep minimal)
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && apt-get clean \
    && find /var/lib/apt/lists -type f -delete

# Python deps first (cache-friendly layer order)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY k9_gemini_agent.py .

# Non-root user — matches k9-llm-router convention
RUN useradd -m k9agent
USER k9agent

# Config — all overridable at runtime via -e flags
ENV PYTHONUNBUFFERED=1
ENV K9_GEMINI_PORT=8770
ENV GEMINI_API_KEY=""
ENV GEMINI_MODEL="gemini-2.5-computer-use-preview"
ENV GEMINI_TASK_MODEL="gemini-2.5-pro"
ENV K9_PAYMASTER_URL="http://localhost:9002"

EXPOSE 8770

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
  CMD curl -sf http://localhost:${K9_GEMINI_PORT}/health || exit 1

CMD ["python", "k9_gemini_agent.py"]
