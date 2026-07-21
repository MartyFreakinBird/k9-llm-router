"""
k9-mcts — orchestrator.py

Full async MCTS loop wired into the K-9 stack:
  - Hypothesis generation  → k9-llm-router :8765/route  (LLM, temp=0.9)
  - Adversarial critique   → k9-llm-router :8765/route  (LLM, temp=0.2, critic role)
  - TX graph rollout       → k9_tx_adapter :9005/query  (Coreum RPC read-only)
  - JEPA confidence score  → aeg_token_model :9003/score
  - Governance gate        → SessionKeyWallet risk params (drawdown/pool_share guard)
  - CB v1 publish          → orbitron_client → Supabase cb_messages

Lifecycle:
  question → expand 3 hypotheses → rollout each on TX graph →
  critique → backprop → select by UCT → re-expand if below threshold →
  commit when confidence >= 0.92 OR max_depth reached →
  emit CB v1 "inference" envelope with full reasoning trace
"""

from __future__ import annotations
import asyncio
import json
import logging
import time
import uuid
from typing import Any, Optional

import httpx

from .tree import MCTSNode, MCTSTree

logger = logging.getLogger("k9.mcts")

# ── Service endpoints (override via env in launch-economic-stack.sh) ─────────
import os
LLM_ROUTER_URL     = os.getenv("K9_JEPA_ENDPOINT",    "http://localhost:8765/route")
TX_ADAPTER_URL     = os.getenv("K9_TX_ADAPTER_URL",   "http://localhost:9005/query")
AEG_SCORE_URL      = os.getenv("AEG_SCORE_URL",       "http://localhost:9003/score")
ORBITRON_URL       = os.getenv("ORBITRON_BUS_URL",     "http://localhost:8769")
CONFIDENCE_THRESH  = float(os.getenv("MCTS_THRESHOLD", "0.92"))
MAX_DEPTH          = int(os.getenv("MCTS_MAX_DEPTH",   "5"))
N_BRANCHES         = int(os.getenv("MCTS_N_BRANCHES",  "3"))


# ── Prompts ───────────────────────────────────────────────────────────────────

HYPOTHESIS_PROMPT = """\
You are a DeFi forensic reasoning engine. Given a question about blockchain activity,
generate {n} distinct investigative hypotheses. Each must be a different approach.
Return JSON array of strings only.

Question: {question}
Context so far: {context}
"""

CRITIQUE_PROMPT = """\
You are an adversarial critic evaluating a DeFi forensic hypothesis.
Score this hypothesis from 0.0 to 1.0 based on:
- Logical coherence (does the reasoning chain hold?)
- Completeness (does it account for proxy contracts, bridge hops, temporal gaps?)
- Falsifiability (can it be proven wrong by evidence?)

Return JSON: {{"score": float, "weakness": "string", "strength": "string"}}

Hypothesis: {hypothesis}
Evidence: {evidence}
"""

REFLECT_PROMPT = """\
This forensic hypothesis failed with the following critique:
{critique}

The evidence gathered was: {evidence}

Generate a refined hypothesis that addresses the weakness.
Return a single string (the improved hypothesis).
"""


class K9MCTSOrchestrator:
    """
    Runs the full MCTS reasoning loop for a forensic or analytical query.
    Designed to be called from main.py's POST /reason endpoint.
    """

    def __init__(self, timeout_s: float = 30.0):
        self.timeout = httpx.Timeout(timeout_s)

    # ── Public entry point ────────────────────────────────────────────────────

    async def reason(self, question: str, context: dict | None = None) -> dict:
        """
        Run MCTS and return the committed answer + reasoning trace.
        """
        start = time.monotonic()
        trace_id = f"mcts_{uuid.uuid4().hex[:8]}"
        ctx = context or {}

        tree = MCTSTree(
            root_question=question,
            max_depth=MAX_DEPTH,
            confidence_threshold=CONFIDENCE_THRESH,
            n_branches=N_BRANCHES,
        )

        logger.info(f"[MCTS:{trace_id}] START  q={question[:80]}")

        for iteration in range(MAX_DEPTH * N_BRANCHES):
            tree.iteration = iteration

            # 1. Selection
            node = tree.select()
            if node.depth >= tree.max_depth:
                break

            # 2. Expansion — generate N hypotheses from current node
            thoughts = await self._generate_hypotheses(
                question=node.thought,
                context=json.dumps(ctx),
                n=N_BRANCHES,
            )
            children = tree.expand(node, thoughts)

            # 3. Rollout — execute each child concurrently
            rollout_tasks = [self._rollout(child, question) for child in children]
            scores = await asyncio.gather(*rollout_tasks, return_exceptions=True)

            # 4. Backpropagation
            for child, score in zip(children, scores):
                if isinstance(score, Exception):
                    score = 0.1
                tree.backprop(child, float(score))

            # 5. Check early termination
            best = tree.commit_best()
            if best and best.mean_confidence() >= CONFIDENCE_THRESH:
                logger.info(
                    f"[MCTS:{trace_id}] COMMIT iter={iteration} "
                    f"conf={best.mean_confidence():.3f}"
                )
                break

        # Force commit best available if threshold not met
        if not tree.committed:
            tree.commit_best()

        elapsed_ms = (time.monotonic() - start) * 1000
        result = self._build_result(tree, trace_id, elapsed_ms)

        # Publish to Orbitron bus (fire-and-forget)
        asyncio.create_task(self._publish_cb(result, trace_id))

        return result

    # ── Rollout ───────────────────────────────────────────────────────────────

    async def _rollout(self, node: MCTSNode, original_question: str) -> float:
        """
        Execute one branch: TX graph query → critique → JEPA score.
        Returns composite confidence score [0, 1].
        """
        # Step A: TX graph traversal (read-only)
        evidence = await self._tx_query(node.thought)
        node.evidence = evidence[:5] if isinstance(evidence, list) else [str(evidence)]

        # Step B: Adversarial critique
        critique = await self._critique(node.thought, node.evidence)
        node.critique_score = critique.get("score", 0.5)

        # Step C: JEPA alignment score
        jepa_score = await self._jepa_score(node.thought, node.evidence)
        node.confidence = jepa_score

        # Composite: weighted blend — critique 40%, JEPA 60%
        composite = 0.4 * node.critique_score + 0.6 * node.confidence

        # If critique reveals weakness → reflect and refine thought
        if node.critique_score < 0.4 and node.depth < MAX_DEPTH - 1:
            refined = await self._reflect(node.thought, critique, node.evidence)
            if refined:
                node.thought = refined  # Mutate thought in-place (backtrack)

        return composite

    # ── LLM calls ─────────────────────────────────────────────────────────────

    async def _generate_hypotheses(self, question: str, context: str, n: int) -> list[str]:
        prompt = HYPOTHESIS_PROMPT.format(question=question, context=context, n=n)
        raw = await self._llm_call(prompt, temperature=0.9)
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(h) for h in parsed[:n]]
        except Exception:
            pass
        # Fallback: split on newlines
        lines = [l.strip("- •1234567890.").strip() for l in raw.split("\n") if len(l.strip()) > 20]
        return lines[:n] or [question]

    async def _critique(self, hypothesis: str, evidence: list[str]) -> dict:
        prompt = CRITIQUE_PROMPT.format(
            hypothesis=hypothesis,
            evidence="\n".join(evidence[:3]),
        )
        raw = await self._llm_call(prompt, temperature=0.2)
        try:
            return json.loads(raw)
        except Exception:
            return {"score": 0.5, "weakness": "parse_error", "strength": "unknown"}

    async def _reflect(self, hypothesis: str, critique: dict, evidence: list[str]) -> Optional[str]:
        prompt = REFLECT_PROMPT.format(
            critique=json.dumps(critique),
            evidence="\n".join(evidence[:3]),
        )
        raw = await self._llm_call(prompt, temperature=0.7)
        return raw.strip() if raw.strip() else None

    async def _llm_call(self, prompt: str, temperature: float = 0.7) -> str:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    LLM_ROUTER_URL,
                    json={"prompt": prompt, "temperature": temperature, "max_tokens": 512},
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("response", data.get("text", ""))
        except Exception as e:
            logger.warning(f"[MCTS] LLM call failed: {e}")
            return ""

    # ── TX graph rollout ──────────────────────────────────────────────────────

    async def _tx_query(self, hypothesis: str) -> list[str]:
        """
        Extracts wallet address from hypothesis and queries TX adapter.
        Returns list of evidence strings.
        """
        # Extract any hex/bech32 address from hypothesis (simple heuristic)
        import re
        addrs = re.findall(r'(0x[a-fA-F0-9]{40}|core1[a-z0-9]{38,})', hypothesis)
        if not addrs:
            return ["no_address_extracted"]

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    TX_ADAPTER_URL,
                    json={"address": addrs[0], "query_type": "compliance_check"},
                )
                resp.raise_for_status()
                data = resp.json()
                # Flatten to list of strings for evidence
                return [
                    f"frozen={data.get('frozen', False)}",
                    f"whitelisted={data.get('whitelisted', False)}",
                    f"tx_count={data.get('tx_count', 'unknown')}",
                    f"balance={data.get('balance', 'unknown')}",
                ]
        except Exception as e:
            return [f"tx_query_error: {str(e)[:80]}"]

    # ── JEPA scoring ──────────────────────────────────────────────────────────

    async def _jepa_score(self, hypothesis: str, evidence: list[str]) -> float:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    AEG_SCORE_URL,
                    json={"input": hypothesis, "evidence": evidence},
                )
                resp.raise_for_status()
                return float(resp.json().get("score", 0.5))
        except Exception:
            return 0.5

    # ── Orbitron publish ──────────────────────────────────────────────────────

    async def _publish_cb(self, result: dict, trace_id: str):
        """Emit CB v1 'inference' envelope to Orbitron bus."""
        import time as _time
        envelope = {
            "spec": "cb.v1",
            "message_id": str(uuid.uuid4()),
            "trace_id": trace_id,
            "source": "k9-mcts",
            "target": "orbitron-bus",
            "type": "inference",
            "ontology_tags": ["mcts", "forensic", "reasoning"],
            "confidence": result.get("confidence", 0.0),
            "payload": {
                "question":         result.get("question", ""),
                "answer":           result.get("answer", ""),
                "iterations":       result.get("iterations", 0),
                "elapsed_ms":       result.get("elapsed_ms", 0),
                "reasoning_trace":  result.get("reasoning_trace", []),
            },
            "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                await client.post(
                    f"{ORBITRON_URL}/cb",
                    json=envelope,
                    headers={"Content-Type": "application/json"},
                )
        except Exception as e:
            logger.warning(f"[MCTS] Orbitron publish failed (non-fatal): {e}")

    # ── Result builder ────────────────────────────────────────────────────────

    def _build_result(self, tree: MCTSTree, trace_id: str, elapsed_ms: float) -> dict:
        best = tree.best_node
        return {
            "trace_id":       trace_id,
            "question":       tree.root.thought,
            "answer":         best.thought if best else "no_answer",
            "confidence":     round(best.mean_confidence(), 4) if best else 0.0,
            "committed":      tree.committed,
            "iterations":     tree.iteration,
            "elapsed_ms":     round(elapsed_ms, 1),
            "reasoning_trace": tree.reasoning_trace(),
            "all_nodes":       tree.all_nodes(),
            "evidence":       best.evidence if best else [],
        }
