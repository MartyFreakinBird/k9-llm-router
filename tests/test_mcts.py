import math
import pytest
from src.k9_mcts.tree import MCTSNode, MCTSTree

def test_uct_unvisited_is_inf():
    root = MCTSNode(thought="root")
    root.visits = 1
    child = MCTSNode(thought="child", parent=root)
    assert child.uct_score() == float("inf")

def test_uct_balances():
    root = MCTSNode(thought="root")
    root.visits = 10
    child = MCTSNode(thought="child", parent=root)
    child.visits = 5
    child.value = 4.0
    exploit = 4.0 / 5
    explore = 1.41 * math.sqrt(math.log(11) / 5)
    assert abs(child.uct_score() - (exploit + explore)) < 1e-6

def test_expand_backprop():
    tree = MCTSTree("is wallet mixing?", max_depth=3, n_branches=2)
    node = tree.select()
    children = tree.expand(node, ["hypothesis A", "hypothesis B"])
    assert len(children) == 2
    assert children[0].depth == 1
    tree.backprop(children[0], 0.8)
    assert tree.root.visits == 1
    assert tree.root.value == 0.8

def test_commit_best():
    tree = MCTSTree("test", max_depth=2, n_branches=2)
    children = tree.expand(tree.root, ["low", "high"])
    tree.backprop(children[0], 0.3)
    tree.backprop(children[1], 0.9)
    best = tree.commit_best()
    assert best is children[1]
    assert best.committed is True

def test_reasoning_trace():
    tree = MCTSTree("root q", max_depth=3, n_branches=2)
    c1 = tree.expand(tree.root, ["c1"])[0]
    c2 = tree.expand(c1, ["c2"])[0]
    tree.backprop(c2, 0.95)
    tree.best_node = c2
    tree.committed = True
    trace = tree.reasoning_trace()
    assert len(trace) == 3
    assert trace[0]["thought"] == "root q"

def test_to_dict():
    node = MCTSNode(thought="test", depth=2)
    node.visits = 5
    node.value = 3.5
    d = node.to_dict()
    assert "trace_id" in d
    assert d["mean_conf"] == pytest.approx(0.7, abs=0.001)
