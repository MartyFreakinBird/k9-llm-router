"""
k9-mcts — tree.py
Monte Carlo Tree Search over LLM reasoning branches.

Each node is a hypothesis about a query (e.g. "is this wallet mixing?").
Scoring is driven by:
  - k9-jepa-engine confidence (prediction quality)
  - k9-llm-router adversarial critique score
  - governance_engine approval gate (blocks commit on human_approval_required)
"""

from __future__ import annotations
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MCTSNode:
    thought: str                      # The hypothesis / reasoning step
    depth: int = 0
    parent: Optional["MCTSNode"] = None
    children: list["MCTSNode"] = field(default_factory=list)

    # Scoring (updated during backprop)
    visits: int = 0
    value: float = 0.0               # cumulative score
    confidence: float = 0.0          # latest confidence from JEPA / LLM
    critique_score: float = 0.0      # adversarial critic score (0-1, higher = survived)

    # Provenance trace (maps to CbMessage.provenance.evidence[])
    evidence: list[str] = field(default_factory=list)
    trace_id: str = field(default_factory=lambda: f"mcts_{uuid.uuid4().hex[:8]}")
    created_at: float = field(default_factory=time.time)
    committed: bool = False           # True when this branch is the selected answer

    def uct_score(self, exploration: float = 1.41) -> float:
        """Upper Confidence Bound for Trees — balances exploit vs explore."""
        if self.visits == 0:
            return float("inf")
        exploit = self.value / self.visits
        explore = exploration * math.sqrt(math.log(self.parent.visits + 1) / self.visits)
        return exploit + explore

    def mean_confidence(self) -> float:
        return self.value / self.visits if self.visits > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "trace_id":       self.trace_id,
            "depth":          self.depth,
            "thought":        self.thought[:200],
            "visits":         self.visits,
            "confidence":     round(self.confidence, 4),
            "critique_score": round(self.critique_score, 4),
            "mean_conf":      round(self.mean_confidence(), 4),
            "evidence":       self.evidence,
            "committed":      self.committed,
            "children":       len(self.children),
        }


class MCTSTree:
    """
    Full MCTS cycle: Selection → Expansion → Rollout → Backpropagation.
    Wired to external scorers via async callbacks injected at runtime.
    """

    def __init__(
        self,
        root_question: str,
        max_depth: int = 5,
        confidence_threshold: float = 0.92,
        n_branches: int = 3,
        exploration: float = 1.41,
    ):
        self.root = MCTSNode(thought=root_question, depth=0)
        self.max_depth = max_depth
        self.threshold = confidence_threshold
        self.n_branches = n_branches
        self.exploration = exploration
        self.best_node: Optional[MCTSNode] = None
        self.iteration = 0
        self.committed = False

    def select(self) -> MCTSNode:
        node = self.root
        while node.children and not node.committed:
            node = max(node.children, key=lambda c: c.uct_score(self.exploration))
        return node

    def expand(self, parent: MCTSNode, thoughts: list[str]) -> list[MCTSNode]:
        children = []
        for t in thoughts:
            child = MCTSNode(thought=t, depth=parent.depth + 1, parent=parent)
            parent.children.append(child)
            children.append(child)
        return children

    def backprop(self, node: MCTSNode, score: float):
        n = node
        while n is not None:
            n.visits += 1
            n.value  += score
            n = n.parent

    def commit_best(self) -> Optional[MCTSNode]:
        leaves = self._collect_leaves(self.root)
        if not leaves:
            return None
        best = max(leaves, key=lambda n: n.mean_confidence())
        best.committed = True
        self.best_node = best
        self.committed = True
        return best

    def _collect_leaves(self, node: MCTSNode) -> list[MCTSNode]:
        if not node.children:
            return [node]
        leaves = []
        for c in node.children:
            leaves.extend(self._collect_leaves(c))
        return leaves

    def reasoning_trace(self) -> list[dict]:
        if not self.best_node:
            return []
        path, n = [], self.best_node
        while n is not None:
            path.append(n.to_dict())
            n = n.parent
        return list(reversed(path))

    def all_nodes(self) -> list[dict]:
        result = []
        def walk(n):
            result.append(n.to_dict())
            for c in n.children:
                walk(c)
        walk(self.root)
        return result
