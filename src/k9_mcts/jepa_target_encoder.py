"""
k9-mcts — jepa_target_encoder.py

JEPA Target Encoder — learns representations of successful MCTS outcomes.
Steers branch expansion by predicting which hypotheses are likely to succeed.

Architecture (per K-9 JEPA mapping, memory entry 25):
  - Context Encoder  = k9-llm-router (encodes question -> latent z)
  - Target Encoder  = THIS MODULE (encodes "ideal outcome" -> latent y)
  - Latent Predictor = k9-orchestrator (predicts y from z — the MCTS loop itself)

Training rule (LOCKED — user instruction):
  - Target encoder weights updated via EMA with 0.999 momentum.
  - Backpropagation through the target encoder is STRICTLY PROHIBITED.
  - The target encoder is a slow-moving anchor, not a gradient sink.
"""

from __future__ import annotations

import math
import time
import logging
from dataclasses import dataclass, field
from typing import Optional
from collections import defaultdict

logger = logging.getLogger("k9.jepa.target")

EMA_MOMENTUM = 0.999
LATENT_DIM = 64


@dataclass
class OutcomeRecord:
    """A single MCTS outcome for the target encoder to learn from."""
    trace_id: str
    hypothesis: str
    task_class: str
    confidence: float
    critique_score: float
    iterations: int
    elapsed_ms: float
    evidence_count: int
    auto_executed: bool = False
    human_overridden: bool = False
    timestamp: float = field(default_factory=time.time)


@dataclass
class LatentVector:
    values: list = field(default_factory=lambda: [0.0] * LATENT_DIM)

    def cosine_similarity(self, other: "LatentVector") -> float:
        dot = sum(a * b for a, b in zip(self.values, other.values))
        na = math.sqrt(sum(a * a for a in self.values))
        nb = math.sqrt(sum(b * b for b in other.values))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    def ema_update(self, target: "LatentVector", momentum: float = EMA_MOMENTUM):
        self.values = [
            momentum * old + (1 - momentum) * new
            for old, new in zip(self.values, target.values)
        ]


def _hash_to_vector(text: str, dim: int = LATENT_DIM) -> LatentVector:
    vec = [0.0] * dim
    for i, ch in enumerate(text):
        pos = (ord(ch) * (i + 1)) % dim
        vec[pos] += ord(ch) / 256.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return LatentVector(values=vec)


class JEPATargetEncoder:
    def __init__(self, confidence_threshold: float = 0.92):
        self.threshold = confidence_threshold
        self.good_outcome_embedding = LatentVector()
        self.bad_outcome_embedding = LatentVector()
        self.class_stats = defaultdict(lambda: {
            "total": 0, "successes": 0, "auto_executed": 0, "human_fallback": 0,
            "mean_confidence": 0.0, "mean_iterations": 0.0, "mean_elapsed_ms": 0.0,
            "ema_confidence": 0.0,
        })
        self.proven_classes = set()
        self.min_proven_samples = 10
        self.history = []
        self.max_history = 1000
        self.total_updates = 0
        self.last_update_ts = 0.0

    def observe(self, outcome: OutcomeRecord) -> None:
        self.total_updates += 1
        self.last_update_ts = time.time()
        self.history.append(outcome)
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]

        latent = _hash_to_vector(outcome.hypothesis)
        if outcome.confidence >= self.threshold:
            self.good_outcome_embedding.ema_update(latent)
        else:
            self.bad_outcome_embedding.ema_update(latent)

        s = self.class_stats[outcome.task_class]
        s["total"] += 1
        if outcome.confidence >= self.threshold:
            s["successes"] += 1
        if outcome.auto_executed:
            s["auto_executed"] += 1
        if outcome.human_overridden:
            s["human_fallback"] += 1

        n = s["total"]
        s["mean_confidence"] = ((n - 1) * s["mean_confidence"] + outcome.confidence) / n
        s["mean_iterations"] = ((n - 1) * s["mean_iterations"] + outcome.iterations) / n
        s["mean_elapsed_ms"] = ((n - 1) * s["mean_elapsed_ms"] + outcome.elapsed_ms) / n

        if s["ema_confidence"] > 0:
            s["ema_confidence"] = EMA_MOMENTUM * s["ema_confidence"] + (1 - EMA_MOMENTUM) * outcome.confidence
        else:
            s["ema_confidence"] = outcome.confidence

        if s["successes"] >= self.min_proven_samples and outcome.task_class not in self.proven_classes:
            self.proven_classes.add(outcome.task_class)
            logger.info(f"[JEPA-TARGET] class '{outcome.task_class}' promoted to proven ({s['successes']}/{s['total']})")

    def predict_branch_prior(self, hypothesis: str, task_class: str = "") -> dict:
        latent = _hash_to_vector(hypothesis)
        sim_good = self.good_outcome_embedding.cosine_similarity(latent) if self.total_updates > 0 else 0.5
        sim_bad = self.bad_outcome_embedding.cosine_similarity(latent) if self.total_updates > 0 else 0.5

        class_rate = 0.5
        if task_class and task_class in self.class_stats:
            s = self.class_stats[task_class]
            if s["total"] > 0:
                class_rate = s["successes"] / s["total"]

        if self.total_updates == 0:
            prior_score = 0.5
        else:
            sim_good_norm = (sim_good + 1) / 2
            sim_bad_norm = (sim_bad + 1) / 2
            embedding_signal = sim_good_norm * (1 - sim_bad_norm)
            prior_score = 0.4 * embedding_signal + 0.3 * class_rate + 0.3 * 0.5

        prior_score = max(0.0, min(1.0, prior_score))
        return {
            "prior_score": round(prior_score, 4),
            "similarity_to_good": round(sim_good, 4),
            "similarity_to_bad": round(sim_bad, 4),
            "class_success_rate": round(class_rate, 4),
            "recommended": prior_score >= 0.35,
        }

    def is_proven(self, task_class: str) -> bool:
        return task_class in self.proven_classes

    def get_class_stats(self, task_class: str = "") -> dict:
        if task_class:
            return dict(self.class_stats.get(task_class, {}))
        return {k: dict(v) for k, v in self.class_stats.items()}

    def stats(self) -> dict:
        return {
            "total_updates": self.total_updates,
            "last_update_ts": self.last_update_ts,
            "proven_classes": list(self.proven_classes),
            "class_count": len(self.class_stats),
            "min_proven_samples": self.min_proven_samples,
            "ema_momentum": EMA_MOMENTUM,
            "history_size": len(self.history),
            "good_embedding_norm": round(math.sqrt(sum(v*v for v in self.good_outcome_embedding.values)), 4) if self.total_updates > 0 else 0.0,
            "bad_embedding_norm": round(math.sqrt(sum(v*v for v in self.bad_outcome_embedding.values)), 4) if self.total_updates > 0 else 0.0,
        }


_target_encoder: Optional[JEPATargetEncoder] = None

def get_target_encoder() -> JEPATargetEncoder:
    global _target_encoder
    if _target_encoder is None:
        _target_encoder = JEPATargetEncoder()
    return _target_encoder
