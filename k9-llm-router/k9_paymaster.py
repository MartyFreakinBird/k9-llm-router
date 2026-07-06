"""
k9_paymaster.py — K-9 PAYMASTER Economic Agent (port :9002)

Responsibilities: budget gate, cost tracking, MoonPay rails,
wallet ops, AutonomyGate feed for DeFi Level 1/2 routing.

MCP tools exposed:
  tool.budget.gate(cost_usd, category, agent_id)
  tool.payment.quote(amount_usd, currency)
  tool.payment.execute(amount, currency, recipient)
  tool.wallet.balance()
  tool.wallet.history()

Run: python k9_paymaster.py --port 9002
Deps: pip install fastapi uvicorn httpx python-dotenv --break-system-packages
"""
from __future__ import annotations
import asyncio, json, logging, os, time, uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()
log = logging.getLogger("k9-paymaster")

# ── CONFIG
MOONPAY_API_KEY    = os.getenv("MOONPAY_API_KEY", "")
MOONPAY_SECRET_KEY = os.getenv("MOONPAY_SECRET_KEY", "")
MOONPAY_BASE_URL   = os.getenv("MOONPAY_BASE_URL", "https://api.moonpay.com")
MOONPAY_SANDBOX    = os.getenv("MOONPAY_SANDBOX", "true").lower() == "true"
DAILY_BUDGET_USD   = float(os.getenv("K9_DAILY_BUDGET_USD",   "10.00"))
TASK_CAP_USD       = float(os.getenv("K9_TASK_CAP_USD",       "1.00"))
PAYMENT_CAP_USD    = float(os.getenv("K9_PAYMENT_CAP_USD",    "25.00"))
AUTO_APPROVE_UNDER = float(os.getenv("K9_AUTO_APPROVE_UNDER", "0.10"))
LEDGER_PATH = Path(os.getenv("K9_LEDGER_PATH", "~/.k9/paymaster_ledger.jsonl")).expanduser()

class PaymentStatus(str, Enum):
    PENDING="pending"; APPROVED="approved"; DENIED="denied"; EXECUTED="executed"; FAILED="failed"

class CostCategory(str, Enum):
    INFERENCE="inference"; LOCAL_LLM="local_llm"; DEFI="defi"; PAYMENT="payment"
    COMPUTE="compute"; STORAGE="storage"; MCP_TOOL="mcp_tool"; MISC="misc"

class AutonomyLevel(str, Enum):
    L1_ADVISORY="L1"; L2_AUTONOMOUS="L2"

@dataclass
class CostEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    agent_id: str = ""
    category: CostCategory = CostCategory.MISC
    amount_usd: float = 0.0
    description: str = ""
    approved: bool = False
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict = field(default_factory=dict)

@dataclass
class PaymentRequest:
    request_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    from_agent: str = ""; to_agent: str = ""
    amount_usd: float = 0.0; currency: str = "USD"; purpose: str = ""
    status: PaymentStatus = PaymentStatus.PENDING
    autonomy: AutonomyLevel = AutonomyLevel.L1_ADVISORY
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    executed_at: str | None = None; tx_hash: str | None = None
    moonpay_tx: dict = field(default_factory=dict)

@dataclass
class BudgetGateResult:
    approved: bool; reason: str; remaining_usd: float
    daily_spent_usd: float; autonomy_level: AutonomyLevel
    request_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

@dataclass
class WalletState:
    usd_balance: float = 0.0; eth_balance: float = 0.0
    sol_balance: float = 0.0; usdc_balance: float = 0.0
    last_updated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    address: str = ""; network: str = "mainnet"

class PaymasterLedger:
    """Append-only JSONL ledger. Sprint 10: replace with on-chain audit trail."""
    def __init__(self, path=LEDGER_PATH):
        self.path = path; self.path.parent.mkdir(parents=True, exist_ok=True)
        self._events: list[CostEvent] = []
        if path.exists():
            for line in path.read_text().splitlines():
                try: self._events.append(CostEvent(**json.loads(line)))
                except: pass
        log.info("Ledger: %d events", len(self._events))

    def append(self, e: CostEvent):
        self._events.append(e)
        with self.path.open("a") as f: f.write(json.dumps(asdict(e)) + "\n")

    def daily_spent(self, cat=None):
        today = datetime.now(timezone.utc).date().isoformat()
        return round(sum(e.amount_usd for e in self._events
            if e.timestamp.startswith(today) and e.approved
            and (cat is None or e.category == cat)), 4)

    def recent(self, n=20): return [asdict(e) for e in self._events[-n:]]

    def summary(self):
        spent = self.daily_spent()
        return {
            "today_spent_usd": spent, "daily_budget_usd": DAILY_BUDGET_USD,
            "remaining_usd": round(DAILY_BUDGET_USD - spent, 4),
            "total_events": len(self._events), "auto_approve_under": AUTO_APPROVE_UNDER,
            "by_category": {c.value: round(self.daily_spent(c), 4) for c in CostCategory},
        }

class BudgetGate:
    def __init__(self, ledger: PaymasterLedger): self.ledger = ledger

    def gate(self, cost_usd, category, agent_id, description=""):
        daily = self.ledger.daily_spent(); remaining = DAILY_BUDGET_USD - daily
        if cost_usd > TASK_CAP_USD:
            return BudgetGateResult(False, f"Cost ${cost_usd:.4f} > task cap ${TASK_CAP_USD}", remaining, daily, AutonomyLevel.L1_ADVISORY)
        if remaining <= 0:
            return BudgetGateResult(False, f"Daily budget exhausted", 0.0, daily, AutonomyLevel.L1_ADVISORY)
        if cost_usd > remaining:
            return BudgetGateResult(False, f"Cost ${cost_usd:.4f} > remaining ${remaining:.4f}", remaining, daily, AutonomyLevel.L1_ADVISORY)
        autonomy = AutonomyLevel.L2_AUTONOMOUS if cost_usd <= AUTO_APPROVE_UNDER else AutonomyLevel.L1_ADVISORY
        approved = autonomy == AutonomyLevel.L2_AUTONOMOUS
        if approved:
            self.ledger.append(CostEvent(agent_id=agent_id, category=category, amount_usd=cost_usd, description=description, approved=True))
        return BudgetGateResult(
            approved, "Auto-approved" if approved else f"L1 advisory — requires human (>${AUTO_APPROVE_UNDER:.2f})",
            remaining - cost_usd if approved else remaining,
            daily + cost_usd if approved else daily, autonomy
        )

class MoonPayClient:
    """MoonPay fiat<>crypto rails. Sandbox by default until HSM signing (Sprint 16)."""
    MOCK_RATES = {"eth": 0.000295, "sol": 0.0062, "btc": 0.0000094, "usdc": 1.0}

    def __init__(self):
        self.api_key = MOONPAY_API_KEY; self.base_url = MOONPAY_BASE_URL
        if not self.api_key: log.warning("MOONPAY_API_KEY not set — payments mocked")

    async def get_quote(self, amount_usd, currency="eth"):
        if not self.api_key: return self._mock_quote(amount_usd, currency)
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{self.base_url}/v3/currencies/{currency}/quote",
                    params={"apiKey": self.api_key, "baseCurrencyAmount": amount_usd, "baseCurrencyCode": "usd"})
                r.raise_for_status(); return r.json()
        except Exception as e:
            log.warning("MoonPay quote error: %s", e); return self._mock_quote(amount_usd, currency)

    async def execute_payment(self, req: PaymentRequest):
        if MOONPAY_SANDBOX or not self.api_key:
            log.info("SANDBOX payment $%.4f → %s", req.amount_usd, req.to_agent)
            return {"id": f"mock_{req.request_id}", "status": "completed", "amount": req.amount_usd,
                    "recipient": req.to_agent, "mock": True, "timestamp": datetime.now(timezone.utc).isoformat()}
        log.warning("Live MoonPay — HSM signing not integrated (Sprint 16)")
        return {"id": f"pending_{req.request_id}", "status": "pending_hsm"}

    def _mock_quote(self, amount, currency):
        rate = self.MOCK_RATES.get(currency.lower(), 1.0)
        return {"baseCurrencyAmount": amount, "quoteCurrencyAmount": round(amount * rate, 8),
                "quoteCurrencyCode": currency, "feeAmount": round(amount * 0.015, 4), "mock": True}

class PaymasterAgent:
    def __init__(self, port=9002):
        self.port = port; self.ledger = PaymasterLedger()
        self.gate_engine = BudgetGate(self.ledger); self.moonpay = MoonPayClient()
        self.wallet = WalletState(); self._pending: dict[str, PaymentRequest] = {}
        self.app = self._build_api()

    def _build_api(self):
        app = FastAPI(title="K-9 Paymaster", version="1.0.0")
        app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

        @app.post("/paymaster/gate")
        async def gate(p: dict):
            try:
                r = self.gate_engine.gate(float(p.get("cost_usd",0)), CostCategory(p.get("category","misc")),
                    p.get("agent_id","unknown"), p.get("description",""))
                return asdict(r)
            except Exception as e: raise HTTPException(400, str(e))

        @app.get("/paymaster/summary")
        async def budget_summary(): return self.ledger.summary()

        @app.post("/paymaster/payment/quote")
        async def quote(p: dict):
            return await self.moonpay.get_quote(float(p.get("amount_usd",0)), p.get("currency","eth"))

        @app.post("/paymaster/payment/exec")
        async def exec_payment(p: dict):
            amount = float(p.get("amount_usd", 0))
            if amount > PAYMENT_CAP_USD:
                raise HTTPException(403, f"${amount:.2f} exceeds payment cap ${PAYMENT_CAP_USD:.2f}")
            gate_r = self.gate_engine.gate(amount, CostCategory.PAYMENT, p.get("from_agent","?"), p.get("purpose",""))
            req = PaymentRequest(from_agent=p.get("from_agent","?"), to_agent=p.get("to_agent",""),
                amount_usd=amount, purpose=p.get("purpose",""), autonomy=gate_r.autonomy_level)
            if not gate_r.approved:
                req.status = PaymentStatus.DENIED; self._pending[req.request_id] = req
                return {"approved": False, "reason": gate_r.reason, "request_id": req.request_id, "pending": True}
            tx = await self.moonpay.execute_payment(req)
            req.status = PaymentStatus.EXECUTED; req.executed_at = datetime.now(timezone.utc).isoformat()
            req.moonpay_tx = tx; self._pending[req.request_id] = req
            self.ledger.append(CostEvent(agent_id=req.from_agent, category=CostCategory.PAYMENT,
                amount_usd=amount, description=req.purpose, approved=True, metadata={"tx": tx}))
            return {"approved": True, "tx": tx, "request_id": req.request_id}

        @app.get("/paymaster/wallet")
        async def wallet(): return asdict(self.wallet)

        @app.get("/paymaster/wallet/history")
        async def history():
            return {"moonpay": await self.moonpay.get_quote(0, "eth"),
                    "k9_events": self.ledger.recent(20)}

        @app.get("/paymaster/pending")
        async def pending(): return {"count": len(self._pending),
            "payments": [asdict(p) for p in self._pending.values()]}

        @app.post("/paymaster/pending/{rid}/approve")
        async def approve(rid: str):
            req = self._pending.get(rid)
            if not req: raise HTTPException(404, "Not found")
            tx = await self.moonpay.execute_payment(req)
            req.status = PaymentStatus.EXECUTED; req.moonpay_tx = tx
            self.ledger.append(CostEvent(agent_id=req.from_agent, category=CostCategory.PAYMENT,
                amount_usd=req.amount_usd, description=req.purpose, approved=True,
                metadata={"manual_approval": True, "tx": tx}))
            return {"approved": True, "tx": tx}

        @app.get("/paymaster/ledger")
        async def ledger(): return self.ledger.recent(50)

        @app.post("/paymaster/gate/inference")
        async def gate_inference(p: dict):
            """
            Convenience endpoint for k9-llm-router.
            POST {model: str, tokens_est: int, cost_usd: float, agent_id: str}
            Returns {approved: bool, reason: str, remaining_usd: float}
            """
            cost = float(p.get("cost_usd", 0.001))
            r = self.gate_engine.gate(
                cost, CostCategory.INFERENCE, p.get("agent_id", "llm-router"),
                f"LLM inference: {p.get('model','?')} ~{p.get('tokens_est',0)} tokens"
            )
            return asdict(r)

        @app.post("/paymaster/ledger/record")
        async def record_cost(p: dict):
            """Record a completed cost event (called by router after inference)."""
            e = CostEvent(
                agent_id=p.get("agent_id","?"),
                category=CostCategory(p.get("category","inference")),
                amount_usd=float(p.get("amount_usd",0)),
                description=p.get("description",""),
                approved=True,
                metadata=p.get("metadata",{}),
            )
            self.ledger.append(e)
            return {"recorded": True, "event_id": e.event_id}


        # ── CLOB SIGNING PATHWAY ─────────────────────────────────────────────
        # Level-1 Advisory: All CLOB order requests require manual approval.
        # No order is signed or submitted autonomously.
        # Approval via POST /paymaster/clob/{rid}/sign after human review.

        @app.post("/paymaster/clob/request")
        async def clob_request(p: dict):
            """
            Submit a CLOB order for L1 human review.

            POST body:
              {
                "agent_id":   "k9-orchestrator",
                "symbol":     "BTCUSDT",
                "side":       "buy" | "sell",
                "order_type": "limit" | "market",
                "quantity":   0.01,
                "price":      84500.0,
                "exchange":   "binance" | "kraken" | "coinbase",
                "strategy":   "ICT_BOS_OB",
                "signal_id":  "n8n_1234567890"
              }

            Returns: {request_id, status: "pending_l1_review", advisory}

            # L1 ADVISORY — DO NOT remove this gate without Security Auditor sign-off.
            # Wallet signing requires HSM (Sprint 16). This endpoint queues for human approval.
            """
            rid = str(uuid.uuid4())[:12]
            clob_rec = {
                "request_id":  rid,
                "type":        "clob_order",
                "agent_id":    p.get("agent_id", "unknown"),
                "symbol":      p.get("symbol", "UNKNOWN"),
                "side":        p.get("side", "unknown"),
                "order_type":  p.get("order_type", "limit"),
                "quantity":    float(p.get("quantity", 0)),
                "price":       float(p.get("price", 0)),
                "exchange":    p.get("exchange", "unknown"),
                "strategy":    p.get("strategy", ""),
                "signal_id":   p.get("signal_id", ""),
                "status":      "pending_l1_review",
                "created_at":  datetime.now(timezone.utc).isoformat(),
                "advisory":    "L1: Awaiting human sign-off. No order submitted.",
            }
            # Estimate notional value
            notional = clob_rec["quantity"] * clob_rec["price"]
            clob_rec["notional_usd"] = round(notional, 2)

            # Always queue for L1 review — never auto-approve CLOB
            self._pending[rid] = PaymentRequest(
                request_id=rid,
                from_agent=clob_rec["agent_id"],
                to_agent=clob_rec["exchange"],
                amount_usd=notional,
                currency="usd",
                purpose=f"CLOB {clob_rec['side'].upper()} {clob_rec['quantity']} "
                        f"{clob_rec['symbol']} @ {clob_rec['price']} on {clob_rec['exchange']}",
                metadata=clob_rec,
            )

            log.warning(
                "[L1-ADVISORY] CLOB request queued: %s %s %s qty=%.4f @ %.2f (rid=%s)",
                clob_rec["side"].upper(), clob_rec["quantity"], clob_rec["symbol"],
                clob_rec["quantity"], clob_rec["price"], rid,
            )

            return {
                "request_id": rid,
                "status":     "pending_l1_review",
                "notional_usd": clob_rec["notional_usd"],
                "advisory":   "L1: Order queued for human review. Not submitted. Approve via POST /paymaster/clob/{rid}/sign",
                "review_url": f"/paymaster/clob/{rid}",
            }

        @app.get("/paymaster/clob/pending")
        async def clob_pending():
            """List all CLOB orders pending L1 human review."""
            clob_items = [
                {**asdict(v), "metadata": v.metadata}
                for v in self._pending.values()
                if v.metadata.get("type") == "clob_order"
            ]
            return {
                "count": len(clob_items),
                "orders": clob_items,
                "advisory": "L1: Review each order before signing. Verify exchange, symbol, quantity, price.",
            }

        @app.get("/paymaster/clob/{rid}")
        async def clob_get(rid: str):
            """Get a specific CLOB order by request_id."""
            req = self._pending.get(rid)
            if not req or req.metadata.get("type") != "clob_order":
                raise HTTPException(404, f"CLOB request {rid} not found")
            return {**asdict(req), "advisory": "L1: Human review required before signing."}

        @app.post("/paymaster/clob/{rid}/sign")
        async def clob_sign(rid: str, body: dict = {}):
            """
            Sign and submit a CLOB order after human approval.

            # L1 ADVISORY: This endpoint EXECUTES an order.
            # Only call after explicit human review of /paymaster/clob/pending.
            # HSM signing not yet integrated (Sprint 16) — uses mock signing stub.

            POST body (optional):
              {"confirm": true, "note": "Manually reviewed — proceed"}
            """
            req = self._pending.get(rid)
            if not req or req.metadata.get("type") != "clob_order":
                raise HTTPException(404, f"CLOB request {rid} not found")

            if req.status == PaymentStatus.EXECUTED:
                return {"status": "already_executed", "request_id": rid}

            confirm = body.get("confirm", False)
            if not confirm:
                raise HTTPException(400, "confirm=true required in body. This executes a live order.")

            # Sprint 16: replace with HSM wallet signing
            # For now: mock sign (safe — no live exchange connection)
            mock_tx = {
                "order_id":   f"mock_{rid}",
                "symbol":     req.metadata.get("symbol"),
                "side":       req.metadata.get("side"),
                "quantity":   req.metadata.get("quantity"),
                "price":      req.metadata.get("price"),
                "exchange":   req.metadata.get("exchange"),
                "signed_by":  "mock_hsm_stub",
                "status":     "mock_submitted",
                "note":       "Sprint 16: replace with real HSM signing + exchange API submission",
                "timestamp":  datetime.now(timezone.utc).isoformat(),
                "advisory":   "L1: Mock submission only. No real order placed until Sprint 16 HSM.",
            }

            req.status     = PaymentStatus.EXECUTED
            req.moonpay_tx = mock_tx

            self.ledger.append(CostEvent(
                agent_id=req.from_agent,
                category=CostCategory.PAYMENT,
                amount_usd=req.amount_usd,
                description=req.purpose,
                approved=True,
                metadata={"manual_approval": True, "l1_signed": True, "tx": mock_tx},
            ))

            log.warning(
                "[L1-SIGNED] CLOB order signed (mock): %s rid=%s",
                req.purpose, rid,
            )

            return {
                "signed":      True,
                "request_id":  rid,
                "tx":          mock_tx,
                "advisory":    "L1: Mock signed. Sprint 16 HSM required for live exchange submission.",
            }

        @app.post("/paymaster/clob/{rid}/reject")
        async def clob_reject(rid: str, body: dict = {}):
            """Reject a CLOB order and remove from pending queue."""
            req = self._pending.get(rid)
            if not req:
                raise HTTPException(404, f"CLOB request {rid} not found")
            reason = body.get("reason", "Human rejected")
            del self._pending[rid]
            log.info("[L1-REJECTED] CLOB request %s rejected: %s", rid, reason)
            return {"rejected": True, "request_id": rid, "reason": reason}

        return app

    def run(self):
        import uvicorn
        print(f"""
╔══════════════════════════════════════════════════════╗
║         K-9 PAYMASTER — Economic Agent              ║
╚══════════════════════════════════════════════════════╝
  Port           : {self.port}
  Daily budget   : ${DAILY_BUDGET_USD:.2f}
  Auto-approve ≤ : ${AUTO_APPROVE_UNDER:.2f}
  Task cap       : ${TASK_CAP_USD:.2f}  |  Payment cap: ${PAYMENT_CAP_USD:.2f}
  MoonPay        : {"SANDBOX" if MOONPAY_SANDBOX else "LIVE"} | API key: {"set" if MOONPAY_API_KEY else "NOT SET — mocked"}
  Ledger         : {LEDGER_PATH}

  Endpoints:
    POST /paymaster/gate            tool.budget.gate()
    POST /paymaster/payment/quote   tool.payment.quote()
    POST /paymaster/payment/exec    tool.payment.execute()
    GET  /paymaster/wallet          tool.wallet.balance()
    GET  /paymaster/summary         dashboard feed
    GET  /paymaster/pending         L1 review queue
""")
        uvicorn.run(self.app, host="0.0.0.0", port=self.port, log_level="warning")

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(); p.add_argument("--port", type=int, default=9002)
    PaymasterAgent(port=p.parse_args().port).run()
