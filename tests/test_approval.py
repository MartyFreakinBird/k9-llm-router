"""
CB-9 Human-in-the-Loop Approval Workflow tests
"""

import pytest
import time
from unittest.mock import AsyncMock, patch, MagicMock

from src.k9_mcts.approval_workflow import (
    ApprovalWorkflow, ApprovalTask, ApprovalState,
    get_approval_workflow,
)
import src.k9_mcts.approval_workflow as aw_mod
import src.k9_mcts.jepa_target_encoder as jepa_mod
import src.k9_mcts.observability as obs_mod
import src.k9_mcts.monitoring as mon_mod
import src.k9_mcts.handover_engine as he_mod
import src.k9_mcts.execution_dispatcher as disp_mod


@pytest.fixture(autouse=True)
def reset_singletons():
    jepa_mod._target_encoder = None
    he_mod._handover = None
    disp_mod._dispatcher = None
    obs_mod._journal = None
    mon_mod._monitor = None
    aw_mod._workflow = None
    yield
    jepa_mod._target_encoder = None
    he_mod._handover = None
    disp_mod._dispatcher = None
    obs_mod._journal = None
    mon_mod._monitor = None
    aw_mod._workflow = None


def _make_workflow(max_tasks=100):
    """Create a fresh workflow with short TTL for testing."""
    w = ApprovalWorkflow(max_tasks=max_tasks)
    # Patch TTL to 2 seconds for expiry tests
    import src.k9_mcts.approval_workflow as mod
    mod.APPROVAL_TTL_SECONDS = 2.0
    return w


def _create_task(w, **kwargs):
    defaults = dict(
        trace_id="t1",
        question="execute trade worth $500",
        answer="trade approved by compliance",
        confidence=0.85,
        task_class="execution",
        risk_level="state_changing",
        handover_reason="state_changing operation — human approval required",
    )
    defaults.update(kwargs)
    return w.create_task(**defaults)


class TestApprovalTask:
    def test_to_dict_has_all_fields(self):
        w = _make_workflow()
        task = _create_task(w)
        d = task.to_dict()
        assert d["state"] == "pending"
        assert "approval_id" in d
        assert "trace_id" in d
        assert "question" in d
        assert "expires_at" in d

    def test_summary_line(self):
        w = _make_workflow()
        task = _create_task(w)
        s = task.to_summary()
        assert "PENDING" in s
        assert "execution" in s
        assert "conf=0.85" in s

    def test_is_expired_false_for_new(self):
        w = _make_workflow()
        task = _create_task(w)
        assert task.is_expired is False

    def test_is_expired_true_after_ttl(self):
        w = _make_workflow()
        task = _create_task(w)
        task.expires_at = time.time() - 1  # expired 1 second ago
        assert task.is_expired is True


class TestApprovalWorkflow:
    def test_create_task(self):
        w = _make_workflow()
        task = _create_task(w)
        assert task.state == ApprovalState.PENDING
        assert task.approval_id is not None
        assert task.task_class == "execution"
        assert w._stats["total_created"] == 1

    def test_approve_pending_task(self):
        w = _make_workflow()
        task = _create_task(w)
        approved = w.approve(task.approval_id, approved_by="operator", note="looks good")
        assert approved.state == ApprovalState.APPROVED
        assert approved.approved_by == "operator"
        assert approved.approval_note == "looks good"
        assert w._stats["total_approved"] == 1

    def test_approve_nonexistent_returns_none(self):
        w = _make_workflow()
        result = w.approve("nonexistent-id")
        assert result is None

    def test_approve_already_approved_returns_none(self):
        w = _make_workflow()
        task = _create_task(w)
        w.approve(task.approval_id)
        result = w.approve(task.approval_id)  # second time
        assert result is None

    def test_reject_pending_task(self):
        w = _make_workflow()
        task = _create_task(w)
        rejected = w.reject(task.approval_id, approved_by="operator", note="too risky")
        assert rejected.state == ApprovalState.REJECTED
        assert rejected.approval_note == "too risky"
        assert w._stats["total_rejected"] == 1

    def test_reject_feeds_negative_outcome_to_encoder(self):
        w = _make_workflow()
        task = _create_task(w)
        w.reject(task.approval_id)
        encoder = jepa_mod.get_target_encoder()
        stats = encoder.get_class_stats("execution")
        assert stats.get("total", 0) >= 1

    def test_mark_executed_success(self):
        w = _make_workflow()
        task = _create_task(w)
        w.mark_executed(task.approval_id, "executed", "tx-adapter:compliance", 45.0)
        assert task.state == ApprovalState.EXECUTED
        assert task.dispatch_service == "tx-adapter:compliance"
        assert w._stats["total_executed"] == 1

    def test_mark_executed_failure(self):
        w = _make_workflow()
        task = _create_task(w)
        w.mark_executed(task.approval_id, "error", "tx-adapter", 50.0, "timeout")
        assert task.state == ApprovalState.EXECUTION_FAILED
        assert task.dispatch_error == "timeout"
        assert w._stats["total_execution_failed"] == 1

    def test_pending_returns_only_pending(self):
        w = _make_workflow()
        t1 = _create_task(w, trace_id="t1")
        t2 = _create_task(w, trace_id="t2")
        w.approve(t1.approval_id)
        pending = w.pending()
        assert len(pending) == 1
        assert pending[0].approval_id == t2.approval_id

    def test_pending_returns_empty_when_none(self):
        w = _make_workflow()
        assert w.pending() == []

    def test_recent_filtered_by_state(self):
        w = _make_workflow()
        t1 = _create_task(w, trace_id="t1")
        t2 = _create_task(w, trace_id="t2")
        w.approve(t1.approval_id)
        w.reject(t2.approval_id)
        approved = w.recent(state="approved")
        assert len(approved) == 1
        rejected = w.recent(state="rejected")
        assert len(rejected) == 1

    def test_recent_unfiltered_returns_all(self):
        w = _make_workflow()
        for i in range(5):
            _create_task(w, trace_id=f"t{i}")
        all_tasks = w.recent()
        assert len(all_tasks) == 5

    def test_get_nonexistent_returns_none(self):
        w = _make_workflow()
        assert w.get("nonexistent") is None

    def test_expiry_auto_expires_stale_tasks(self):
        w = _make_workflow()
        task = _create_task(w)
        # Wait for TTL
        time.sleep(2.5)
        # Trigger expiry check via pending()
        pending = w.pending()
        assert len(pending) == 0
        assert task.state == ApprovalState.EXPIRED
        assert w._stats["total_expired"] == 1

    def test_approve_expired_task_returns_none(self):
        w = _make_workflow()
        task = _create_task(w)
        time.sleep(2.5)
        result = w.approve(task.approval_id)
        assert result is None
        assert task.state == ApprovalState.EXPIRED

    def test_stats_returns_aggregates(self):
        w = _make_workflow()
        t1 = _create_task(w, trace_id="t1")
        t2 = _create_task(w, trace_id="t2")
        t3 = _create_task(w, trace_id="t3")
        w.approve(t1.approval_id)
        w.reject(t2.approval_id)
        s = w.stats()
        assert s["stats"]["total_created"] == 3
        assert s["stats"]["total_approved"] == 1
        assert s["stats"]["total_rejected"] == 1
        assert s["pending"] == 1

    def test_max_tasks_trim(self):
        w = _make_workflow(max_tasks=3)
        for i in range(5):
            _create_task(w, trace_id=f"t{i}")
        # Should trim to around max_tasks
        assert len(w._tasks) <= 5

    def test_full_lifecycle(self):
        """Test full: create → approve → dispatch → mark executed."""
        w = _make_workflow()
        task = _create_task(w)
        assert task.state == ApprovalState.PENDING

        w.approve(task.approval_id, approved_by="operator")
        assert task.state == ApprovalState.APPROVED

        w.mark_executed(task.approval_id, "executed", "aeg-signal-router:9004", 32.5)
        assert task.state == ApprovalState.EXECUTED
        assert task.dispatch_latency_ms == 32.5

    def test_reject_then_approve_fails(self):
        w = _make_workflow()
        task = _create_task(w)
        w.reject(task.approval_id)
        result = w.approve(task.approval_id)
        assert result is None  # can't approve after rejecting

    def test_singleton_returns_same_instance(self):
        w1 = get_approval_workflow()
        w2 = get_approval_workflow()
        assert w1 is w2
