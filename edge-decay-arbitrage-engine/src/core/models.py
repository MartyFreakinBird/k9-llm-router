from pydantic import BaseModel, Field
from typing import Optional, List, Dict
from enum import Enum
from datetime import datetime


class Granularity(str, Enum):
    MICRO = "micro"           # < 100ms (HFT tick-level)
    STRUCTURAL = "structural" # > 1min (daily crowding)


class Node(BaseModel):
    id: str                   # e.g. "BTCUSDT_45000"
    symbol: str
    price_level: float
    timestamp: datetime


class Edge(BaseModel):
    source: str
    target: str
    base_cost: float                     # Slippage + spread
    crowding_penalty: float = 0.0        # Incremented per active path
    velocity_of_convergence: float = 0.0 # ΔCrowding/Δt
    is_active: bool = True


class PathRequest(BaseModel):
    start_node: str
    end_node: str
    granularity: Granularity = Granularity.MICRO
    max_runtime_ms: int = Field(default=50, ge=1, le=5000)


class PathResult(BaseModel):
    path: List[str]
    total_cost: float
    algorithm_used: str
    execution_time_ms: float
    congestion_score: float  # Sum of crowding_penalties along path


class SwarmMetrics(BaseModel):
    voc: float
    kl_divergence: float
    active_algo: str
    top_decay_edges: List[Dict]
    negative_cycle_detected: bool = False
