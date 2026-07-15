"""
Anytime Repairing A* (ARA*).
Returns the best path found within max_runtime_ms.
Epsilon starts high (fast, suboptimal) and decays toward 1 (optimal).
Anti-pathfinding heuristic: penalise high-VoC edges.
"""
import heapq
import time
import math
from typing import Dict, List, Optional, Tuple
from src.core.config import settings
from src.core import state
from src.core.models import PathRequest


def _heuristic(node_id: str, end_node: str) -> float:
    """Admissible h: abs difference of price levels (0 if unknown)."""
    n = state.graph_nodes.get(node_id)
    e = state.graph_nodes.get(end_node)
    if n and e:
        return abs(n.price_level - e.price_level) * 1e-5
    return 0.0


def _effective_cost(edge) -> float:
    """
    Anti-crowd cost: base_cost + crowding_penalty + VoC penalty.
    High VoC edges are expensive → naturally avoided.
    """
    return (
        edge.base_cost
        + edge.crowding_penalty
        + abs(edge.velocity_of_convergence) * 0.01
    )


def ara_star(request: PathRequest) -> Tuple[List[str], float, str]:
    """
    Returns (path, total_cost, algo_label).
    Gracefully returns best-so-far if max_runtime_ms exceeded.
    """
    start = request.start_node
    goal = request.end_node
    deadline = time.monotonic() + request.max_runtime_ms / 1000.0

    epsilon = settings.ara_epsilon_start
    best_path: List[str] = []
    best_cost: float = math.inf

    while time.monotonic() < deadline and epsilon >= 1.0:
        path, cost = _weighted_astar(start, goal, epsilon, deadline)
        if path and cost < best_cost:
            best_path = path
            best_cost = cost
        epsilon = max(1.0, epsilon * settings.ara_epsilon_decay)

    label = "ara_star" if best_path else "ara_star_timeout"
    return best_path, best_cost, label


def _weighted_astar(
    start: str, goal: str, epsilon: float, deadline: float
) -> Tuple[List[str], float]:
    open_heap: List[Tuple[float, str]] = []
    g: Dict[str, float] = {start: 0.0}
    came_from: Dict[str, Optional[str]] = {start: None}

    h_start = epsilon * _heuristic(start, goal)
    heapq.heappush(open_heap, (h_start, start))

    while open_heap and time.monotonic() < deadline:
        f, current = heapq.heappop(open_heap)

        if current == goal:
            return _reconstruct(came_from, goal), g[goal]

        for edge in state.get_neighbors(current):
            cost = _effective_cost(edge)
            new_g = g[current] + cost
            if new_g < g.get(edge.target, math.inf):
                g[edge.target] = new_g
                came_from[edge.target] = current
                f_score = new_g + epsilon * _heuristic(edge.target, goal)
                heapq.heappush(open_heap, (f_score, edge.target))

    return [], math.inf


def _reconstruct(came_from: Dict[str, Optional[str]], goal: str) -> List[str]:
    path = []
    node: Optional[str] = goal
    while node is not None:
        path.append(node)
        node = came_from.get(node)
    return list(reversed(path))
