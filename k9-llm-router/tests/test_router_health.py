"""
K-9 LLM Router — Health endpoint smoke tests
Run: pytest tests/ -v --asyncio-mode=auto
"""
import pytest
from httpx import AsyncClient, ASGITransport
import os

os.environ.setdefault("ROUTER_MODE", "cloud")
os.environ.setdefault("K9_PAYMASTER_ENABLE", "false")

from main import app


@pytest.mark.asyncio
async def test_root():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/")
    assert r.status_code == 200
    assert "k9-llm-router" in r.json()["service"]


@pytest.mark.asyncio
async def test_swarm_health():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/swarm/health")
    assert r.status_code == 200
    data = r.json()
    assert "status" in data


@pytest.mark.asyncio
async def test_models_endpoint():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/models")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
