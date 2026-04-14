"""
K-9 Orchestrator — command dispatch smoke tests
"""
import pytest
import os

os.environ.setdefault("K9_PAYMASTER_ENABLE", "false")
os.environ.setdefault("K9_PAYMASTER_URL", "http://localhost:9002")
os.environ.setdefault("LLM_ROUTER_URL", "http://localhost:8765")
os.environ.setdefault("K9_MCP_MANAGER_URL", "http://localhost:3030")

from httpx import AsyncClient, ASGITransport
from k9_orchestrator import app


@pytest.mark.asyncio
async def test_orchestrator_root():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_orchestrator_health():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/swarm/health")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_task_queue_health_check():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/orchestrator/command", json={
            "command": "health_check",
            "params": {},
            "source": "pytest"
        })
    assert r.status_code in (200, 202)
