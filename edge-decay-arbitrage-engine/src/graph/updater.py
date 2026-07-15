"""
Dynamic edge weight updates.
Formula: W(e,t) = base_slippage + α*(dF/dt) + β*exp(γ*Crowding)
"""
import asyncio
import math
from collections import deque
from typing import Optional
import redis.asyncio as aioredis
from src.core.config import settings
from src.core import state


class GraphUpdater:
    def __init__(self, redis_client: aioredis.Redis):
        self.redis = redis_client
        self.history_window: deque[float] = deque(maxlen=100)
        self.granularity_seconds = settings.granularity_ms / 1000.0

    async def update_edge(self, source: str, target: str, new_order_flow: float) -> None:
        edge = state.get_edge(source, target)
        if edge is None:
            return

        # dCrowding/dt
        delta_crowd = new_order_flow - edge.crowding_penalty
        edge.velocity_of_convergence = delta_crowd / max(self.granularity_seconds, 1e-6)

        # Exponential crowding accumulation
        edge.crowding_penalty += edge.velocity_of_convergence * settings.crowding_decay_rate

        # Recompute effective weight
        effective_weight = (
            edge.base_cost
            + settings.alpha * abs(edge.velocity_of_convergence)
            + settings.beta * math.exp(settings.gamma * max(edge.crowding_penalty, 0.0))
        )

        state.set_edge(edge)
        self.history_window.append(effective_weight)

        # Persist to Redis for cross-process visibility
        edge_key = f"edge:{source}::{target}"
        await self.redis.hset(edge_key, mapping={
            "crowding_penalty": str(edge.crowding_penalty),
            "velocity_of_convergence": str(edge.velocity_of_convergence),
            "effective_weight": str(effective_weight),
        })

    async def decay_all_edges(self) -> None:
        """Passive decay — crowding bleeds off when no new flow arrives."""
        for edge in state.graph_edges.values():
            if edge.crowding_penalty > 0:
                edge.crowding_penalty = max(0.0, edge.crowding_penalty - 0.01)
                state.set_edge(edge)

    async def run_loop(self) -> None:
        """Background update loop — runs every granularity_ms."""
        while True:
            await self.decay_all_edges()
            await asyncio.sleep(self.granularity_seconds)
