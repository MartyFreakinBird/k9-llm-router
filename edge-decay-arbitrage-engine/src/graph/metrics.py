"""
Global swarm metrics: Velocity of Convergence (VoC) and KL-Divergence.
These are the inputs to the adaptive router selector.
"""
import math
import json
from typing import List
import redis.asyncio as aioredis
from src.core.config import settings
from src.core import state


async def get_global_voc(redis: aioredis.Redis) -> float:
    """
    Average VoC across all active edges.
    High VoC = swarm is stampeding a set of paths.
    """
    edges = list(state.graph_edges.values())
    if not edges:
        return 0.0
    return sum(abs(e.velocity_of_convergence) for e in edges) / len(edges)


async def get_kl_divergence(redis: aioredis.Redis) -> float:
    """
    KL divergence of recent path-algorithm distribution vs uniform.
    Low KL = everyone using same algo (herd) → trigger anti-crowd Yen's.
    High KL = healthy diversity.
    """
    raw = await redis.lrange("path_history", 0, settings.path_history_size - 1)
    if len(raw) < 10:
        return 1.0  # Not enough data → assume diversity

    algo_counts: dict[str, int] = {}
    for item in raw:
        try:
            record = json.loads(item)
            algo = record.get("algorithm", "unknown")
            algo_counts[algo] = algo_counts.get(algo, 0) + 1
        except Exception:
            continue

    n = sum(algo_counts.values())
    if n == 0:
        return 1.0

    k = len(algo_counts)
    uniform_p = 1.0 / max(k, 1)
    kl = 0.0
    for count in algo_counts.values():
        p = count / n
        if p > 0:
            kl += p * math.log(p / uniform_p)
    return max(0.0, kl)


async def get_top_decaying_edges(redis: aioredis.Redis, n: int = 5) -> list:
    edges = sorted(
        state.graph_edges.values(),
        key=lambda e: e.velocity_of_convergence,
        reverse=True
    )
    return [
        {
            "edge": f"{e.source} → {e.target}",
            "voc": round(e.velocity_of_convergence, 4),
            "crowding": round(e.crowding_penalty, 4),
        }
        for e in edges[:n]
    ]


async def detect_negative_cycle(redis: aioredis.Redis) -> bool:
    return state.negative_cycle_active
