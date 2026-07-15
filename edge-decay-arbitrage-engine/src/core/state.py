"""
Global app state: graph, swarm metrics, current router selection.
All mutable state goes through this module — never import graph
structures directly in route handlers.
"""
from typing import Dict, Optional
from src.core.models import Edge, Node

# Live graph — adjacency list
graph_nodes: Dict[str, Node] = {}
graph_edges: Dict[str, Edge] = {}   # key = f"{source}::{target}"

# Current swarm read
current_router: str = "ara_star"
last_voc: float = 0.0
last_kl: float = 1.0
negative_cycle_active: bool = False


def get_edge(source: str, target: str) -> Optional[Edge]:
    return graph_edges.get(f"{source}::{target}")


def set_edge(edge: Edge) -> None:
    graph_edges[f"{edge.source}::{edge.target}"] = edge


def get_neighbors(node_id: str) -> list[Edge]:
    prefix = f"{node_id}::"
    return [e for k, e in graph_edges.items() if k.startswith(prefix) and e.is_active]


def adjacency_dict() -> Dict[str, list[Edge]]:
    adj: Dict[str, list[Edge]] = {}
    for edge in graph_edges.values():
        if edge.is_active:
            adj.setdefault(edge.source, []).append(edge)
    return adj
