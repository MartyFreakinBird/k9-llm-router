"""
K-9 Paymaster — unit tests for gate logic
"""
import pytest
import os

os.environ.setdefault("K9_PAYMASTER_ENABLE", "true")
os.environ.setdefault("K9_DAILY_BUDGET_USD", "10.0")
os.environ.setdefault("K9_AUTO_APPROVE_UNDER", "0.10")

from httpx import AsyncClient, ASGITransport
from k9_paymaster import PaymasterAgent


@pytest.mark.asyncio
async def test_paymaster_gate_approved():
    agent = PaymasterAgent(port=9002)
    async with AsyncClient(transport=ASGITransport(agent.app), base_url="http://test") as c:
        r = await c.post("/paymaster/gate/inference", json={
            "model": "ollama/llama3",
            "tokens_est": 100,
            "cost_usd": 0.0001,
            "agent_id": "test-agent"
        })
    assert r.status_code == 200
    assert r.json()["approved"] is True


@pytest.mark.asyncio
async def test_paymaster_summary():
    agent = PaymasterAgent(port=9002)
    async with AsyncClient(transport=ASGITransport(agent.app), base_url="http://test") as c:
        r = await c.get("/paymaster/summary")
    assert r.status_code == 200
    data = r.json()
    assert "daily_budget_usd" in data
