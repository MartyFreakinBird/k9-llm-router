"""
k9_tx_adapter.py — TX Blockchain (Coreum post-merger) Surveillance Adapter

Role: K9-GL monitoring node. READ-ONLY. Never signs, never executes.
Sits parallel to aeg_signal_router.py — does not touch the EVM/Solidity
Sentry Kernel track (Base Sepolia AEG-4/6/7/9).

Queries:
  - Bank balances (standard Cosmos SDK)
  - AssetFT (Smart Token) compliance features: minting, freezing, whitelisting
  - Frozen / whitelisted balance status per account+denom

Output: Findings are packaged as K9-CB v1 "observation" envelopes and
forwarded to orbitron-bus, exactly like any other K-9 sensor module —
no direct DB writes, no execution requests.

FastAPI service on :9005 (k9-tx-adapter) — consistent with the existing
K-9 port map (see memory: Orbitron/PackAI K-9 Ecosystem Full Stack Map).
"""

import os
import time
import uuid
import httpx
from typing import Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────────
LCD_ENDPOINT = os.getenv("TX_LCD_ENDPOINT", "https://full-node.mainnet-1.coreum.dev:1317")
CHAIN_ID     = os.getenv("TX_CHAIN_ID", "coreum-mainnet-1")
PREFIX       = os.getenv("TX_ADDRESS_PREFIX", "core")
ORBITRON_BUS_URL = os.getenv("ORBITRON_BUS_URL", "")  # posts CB-v1 observations here


class K9TXChainAdapter:
    """Read-only Cosmos SDK / CosmWasm query client for the TX (Coreum) chain."""

    def __init__(self, lcd_endpoint: str = LCD_ENDPOINT, chain_id: str = CHAIN_ID, prefix: str = PREFIX):
        self.lcd = lcd_endpoint.rstrip("/")
        self.chain_id = chain_id
        self.prefix = prefix
        self.client = httpx.Client(timeout=10.0)

    # ── Standard Cosmos SDK bank module ─────────────────────────────────────
    def query_bank_balances(self, address: str) -> dict:
        """GET /cosmos/bank/v1beta1/balances/{address}"""
        url = f"{self.lcd}/cosmos/bank/v1beta1/balances/{address}"
        try:
            r = self.client.get(url)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            return {"error": f"bank balance query failed: {e}"}

    # ── Coreum native AssetFT (Smart Token) module ──────────────────────────
    def query_ft_token(self, denom: str) -> dict:
        """GET /coreum/asset/ft/v1/tokens/{denom} — returns issuer, features bitmask"""
        url = f"{self.lcd}/coreum/asset/ft/v1/tokens/{denom}"
        try:
            r = self.client.get(url)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            return {"error": f"ft token query failed: {e}"}

    def query_ft_frozen_balance(self, account: str, denom: str) -> dict:
        """GET /coreum/asset/ft/v1/tokens/{denom}/frozen-balance/{account}"""
        url = f"{self.lcd}/coreum/asset/ft/v1/tokens/{denom}/frozen-balance/{account}"
        try:
            r = self.client.get(url)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            return {"error": f"frozen balance query failed: {e}"}

    def query_ft_whitelisted_balance(self, account: str, denom: str) -> dict:
        """GET /coreum/asset/ft/v1/tokens/{denom}/whitelisted-balance/{account}"""
        url = f"{self.lcd}/coreum/asset/ft/v1/tokens/{denom}/whitelisted-balance/{account}"
        try:
            r = self.client.get(url)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            return {"error": f"whitelist balance query failed: {e}"}

    # ── Composite compliance check ──────────────────────────────────────────
    def monitor_compliance_rules(self, account_address: str, denom: str) -> dict:
        """
        Aggregates AssetFT compliance state for one account+denom pair:
        issuer info, freeze status, whitelist status, KYC-relevant features.
        This is what feeds K-9's typology detection (mixer usage, large
        transfers, compliance flags).
        """
        token_info  = self.query_ft_token(denom)
        frozen      = self.query_ft_frozen_balance(account_address, denom)
        whitelisted = self.query_ft_whitelisted_balance(account_address, denom)
        balances    = self.query_bank_balances(account_address)

        features = token_info.get("token", {}).get("features", []) if "token" in token_info else []

        return {
            "account": account_address,
            "denom": denom,
            "chain_id": self.chain_id,
            "token_features": features,
            "is_frozen": "error" not in frozen and frozen.get("frozen_balance") is not None,
            "is_whitelisted": "error" not in whitelisted,
            "current_balances": balances.get("balances", []) if "error" not in balances else [],
            "queried_at": time.time(),
        }

    # ── Package as K9-CB v1 observation envelope ────────────────────────────
    def to_cb_envelope(self, finding: dict, source: str = "k9-tx-adapter") -> dict:
        return {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": source,
            "type": "observation",
            "ontology_tags": ["tx-chain", "compliance", "smart-token", finding.get("denom", "unknown")],
            "confidence": 1.0,  # direct chain query, not inference
            "payload": finding,
            "trace_id": str(uuid.uuid4()),
        }

    def forward_to_bus(self, envelope: dict) -> dict:
        if not ORBITRON_BUS_URL:
            return {"forwarded": False, "reason": "ORBITRON_BUS_URL not configured"}
        try:
            r = self.client.post(ORBITRON_BUS_URL, json=envelope, timeout=8.0)
            return {"forwarded": True, "status": r.status_code}
        except Exception as e:
            return {"forwarded": False, "error": str(e)}


# ── FastAPI service :9005 ────────────────────────────────────────────────────
app = FastAPI(title="k9-tx-adapter", version="1.0.0")
adapter = K9TXChainAdapter()


class ComplianceQuery(BaseModel):
    account_address: str
    denom: str
    forward: bool = True


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "k9-tx-adapter",
        "chain_id": CHAIN_ID,
        "lcd_endpoint": LCD_ENDPOINT,
        "role": "READ-ONLY surveillance — never signs, never executes",
    }


@app.get("/balance/{address}")
def get_balance(address: str):
    return adapter.query_bank_balances(address)


@app.get("/ft/{denom}")
def get_ft_token(denom: str):
    return adapter.query_ft_token(denom)


@app.post("/compliance/check")
def check_compliance(q: ComplianceQuery):
    finding = adapter.monitor_compliance_rules(q.account_address, q.denom)
    envelope = adapter.to_cb_envelope(finding)
    result = {"finding": finding, "envelope": envelope}
    if q.forward:
        result["forward_result"] = adapter.forward_to_bus(envelope)
    return result


if __name__ == "__main__":
    import uvicorn
    print(f"K-9 TX Surveillance Node — chain_id={CHAIN_ID} lcd={LCD_ENDPOINT}")
    print("Role: READ-ONLY. Additive to K9-GL. Does not touch EVM/AEG track.")
    uvicorn.run(app, host="0.0.0.0", port=9005)
