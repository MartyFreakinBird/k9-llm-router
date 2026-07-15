"""
Builds the price-level graph from L2 order book snapshots.
Each bid/ask level becomes a Node; adjacent levels become Edges.
"""
from datetime import datetime, timezone
from typing import List, Dict, Any
from src.core.models import Node, Edge
from src.core import state


def build_from_orderbook(symbol: str, bids: List[List[float]], asks: List[List[float]]) -> int:
    """
    bids/asks: list of [price, qty] pairs (L2 snapshot).
    Returns number of edges created.
    """
    now = datetime.now(timezone.utc)
    all_levels: List[tuple[float, str]] = []

    for price, qty in bids:
        nid = f"{symbol}_{price:.2f}_bid"
        state.graph_nodes[nid] = Node(id=nid, symbol=symbol, price_level=price, timestamp=now)
        all_levels.append((price, nid))

    for price, qty in asks:
        nid = f"{symbol}_{price:.2f}_ask"
        state.graph_nodes[nid] = Node(id=nid, symbol=symbol, price_level=price, timestamp=now)
        all_levels.append((price, nid))

    # Sort by price; connect adjacent levels as directed edges (both directions)
    all_levels.sort(key=lambda x: x[0])
    edges_created = 0
    for i in range(len(all_levels) - 1):
        price_a, nid_a = all_levels[i]
        price_b, nid_b = all_levels[i + 1]
        spread = abs(price_b - price_a)
        base_cost = spread / price_a  # normalised spread as cost

        for src, tgt in [(nid_a, nid_b), (nid_b, nid_a)]:
            state.set_edge(Edge(source=src, target=tgt, base_cost=base_cost))
            edges_created += 1

    return edges_created


def seed_synthetic(symbol: str = "BTCUSDT", mid_price: float = 45_000.0, levels: int = 10) -> int:
    """Seed a synthetic order book for testing (0.01% tick spacing)."""
    tick = mid_price * 0.0001
    bids = [[mid_price - tick * i, 1.0] for i in range(1, levels + 1)]
    asks = [[mid_price + tick * i, 1.0] for i in range(1, levels + 1)]
    return build_from_orderbook(symbol, bids, asks)
