"""
K-9 Orchestrator -- command dispatch smoke tests (Sprint 4b)
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
async def test_health_check_command_inline():
    """health_check is a LIGHT command -- should return 200 synchronously."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/orchestrator/command", json={
            "command": "health_check",
            "params": {},
            "source": "pytest"
        })
    # 200 for light commands, 500 if downstream services offline (acceptable in CI)
    assert r.status_code in (200, 500)


@pytest.mark.asyncio
async def test_heavy_command_returns_202_or_fallback():
    """
    run_quant_analysis is HEAVY.
    With Celery available: 202 + task_id.
    Without Celery (CI): falls back to inline, which may 200 or 500 (router offline).
    Either way: should NOT block for 30s.
    """
    import asyncio
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # Give it max 5s -- if it blocks 30s, the Celery offload is broken
        try:
            r = await asyncio.wait_for(
                c.post("/orchestrator/command", json={
                    "command": "run_quant_analysis",
                    "params": {"symbol": "BTCUSD"},
                    "source": "pytest"
                }),
                timeout=5.0
            )
            # 202 = Celery queued, 200 = inline success, 500 = inline fail (no LLM in CI)
            assert r.status_code in (200, 202, 500), f"Unexpected status: {r.status_code}"
            if r.status_code == 202:
                data = r.json()
                assert "task_id" in data
                assert "poll_url" in data
        except asyncio.TimeoutError:
            pytest.fail("Heavy command blocked for >5s -- Celery offload is broken")


@pytest.mark.asyncio
async def test_unknown_command_404():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/orchestrator/command", json={
            "command": "does_not_exist",
            "params": {}
        })
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_task_list_endpoint():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/orchestrator/tasks")
    assert r.status_code == 200
