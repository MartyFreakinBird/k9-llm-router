"""
CB-7 Observability & Decision Journal tests
"""

import pytest
import time
from src.k9_mcts.observability import (
    DecisionJournal, JournalEntry, get_journal
)
import src.k9_mcts.observability as obs_mod


@pytest.fixture(autouse=True)
def reset_journal():
    obs_mod._journal = None
    yield
    obs_mod._journal = None


def _make_entry(
    trace_id="trace_001",
    decision="auto_execute",
    risk_level="read_only",
    task_class="compliance",
    dispatch_result="executed",
    dispatch_service="tx-adapter:compliance",
):
    return JournalEntry(
        trace_id=trace_id,
        timestamp=time.time(),
        question="check wallet compliance",
        answer="wallet is clean",
        confidence=0.95,
        iterations=3,
        elapsed_ms=150.0,
        reasoning_trace=[{"thought": "step1"}],
        evidence=["frozen=false"],
        task_class=task_class,
        decision=decision,
        risk_level=risk_level,
        handover_reason="proven class + read_only",
        cooldown_seconds=0,
        dispatch_result=dispatch_result,
        dispatch_service="tx-adapter:compliance",
        dispatch_latency_ms=45.0,
        dispatch_error="",
        jepa_total_updates=15,
        jepa_proven_classes=["compliance"],
        jepa_class_success_rate=0.9,
        envelope_id="env-001",
    )


class TestJournalEntry:
    def test_to_dict_has_all_fields(self):
        entry = _make_entry()
        d = entry.to_dict()
        assert d["trace_id"] == "trace_001"
        assert d["decision"] == "auto_execute"
        assert d["confidence"] == 0.95
        assert "reasoning_trace" in d
        assert "evidence" in d

    def test_summary_line_auto_execute(self):
        entry = _make_entry(decision="auto_execute")
        line = entry.to_summary_line()
        assert "[AUTO]" in line
        assert "compliance" in line
        assert "conf=0.95" in line

    def test_summary_line_human_review(self):
        entry = _make_entry(decision="human_review", dispatch_result="")
        line = entry.to_summary_line()
        assert "[HUMAN]" in line

    def test_summary_line_reject(self):
        entry = _make_entry(decision="reject", dispatch_result="")
        line = entry.to_summary_line()
        assert "[REJECT]" in line

    def test_summary_line_includes_dispatch(self):
        entry = _make_entry(dispatch_result="executed", dispatch_service="tx-adapter:compliance")
        line = entry.to_summary_line()
        assert "tx-adapter:compliance" in line


class TestDecisionJournal:
    def test_record_and_retrieve(self):
        j = DecisionJournal()
        entry = _make_entry(trace_id="t1")
        j.record(entry)
        retrieved = j.get("t1")
        assert retrieved is not None
        assert retrieved.trace_id == "t1"
        assert retrieved.decision == "auto_execute"

    def test_recent_returns_in_reverse_order(self):
        j = DecisionJournal()
        for i in range(5):
            j.record(_make_entry(trace_id=f"t{i}"))
        recent = j.recent(limit=3)
        assert len(recent) == 3
        assert recent[0].trace_id == "t4"
        assert recent[1].trace_id == "t3"
        assert recent[2].trace_id == "t2"

    def test_recent_filter_by_decision(self):
        j = DecisionJournal()
        j.record(_make_entry(trace_id="t1", decision="auto_execute"))
        j.record(_make_entry(trace_id="t2", decision="human_review"))
        j.record(_make_entry(trace_id="t3", decision="auto_execute"))
        auto = j.recent(decision="auto_execute")
        assert len(auto) == 2
        assert all(e.decision == "auto_execute" for e in auto)

    def test_recent_filter_by_risk(self):
        j = DecisionJournal()
        j.record(_make_entry(trace_id="t1", risk_level="read_only"))
        j.record(_make_entry(trace_id="t2", risk_level="high_risk"))
        ro = j.recent(risk_level="read_only")
        assert len(ro) == 1
        assert ro[0].risk_level == "read_only"

    def test_recent_filter_by_task_class(self):
        j = DecisionJournal()
        j.record(_make_entry(trace_id="t1", task_class="compliance"))
        j.record(_make_entry(trace_id="t2", task_class="analytics"))
        comp = j.recent(task_class="compliance")
        assert len(comp) == 1
        assert comp[0].task_class == "compliance"

    def test_summary_aggregates(self):
        j = DecisionJournal()
        j.record(_make_entry(trace_id="t1", decision="auto_execute", risk_level="read_only", task_class="compliance"))
        j.record(_make_entry(trace_id="t2", decision="human_review", risk_level="high_risk", task_class="execution"))
        j.record(_make_entry(trace_id="t3", decision="reject", risk_level="read_only", task_class="compliance"))
        s = j.summary()
        assert s["total"] == 3
        assert s["by_decision"]["auto_execute"] == 1
        assert s["by_decision"]["human_review"] == 1
        assert s["by_decision"]["reject"] == 1
        assert s["by_risk"]["read_only"] == 2
        assert s["by_risk"]["high_risk"] == 1
        assert s["by_task_class"]["compliance"] == 2
        assert s["by_task_class"]["execution"] == 1

    def test_ring_buffer_evicts_oldest(self):
        j = DecisionJournal(max_entries=3)
        for i in range(5):
            j.record(_make_entry(trace_id=f"t{i}"))
        assert len(j.recent(limit=10)) == 3
        # t0 and t1 should be evicted
        assert j.get("t0") is None
        assert j.get("t1") is None
        assert j.get("t4") is not None

    def test_export_returns_all_entries(self):
        j = DecisionJournal()
        for i in range(3):
            j.record(_make_entry(trace_id=f"t{i}"))
        exported = j.export()
        assert len(exported) == 3
        assert isinstance(exported[0], dict)
        assert exported[0]["trace_id"] == "t0"

    def test_auto_executed_filter(self):
        j = DecisionJournal()
        j.record(_make_entry(trace_id="t1", decision="auto_execute"))
        j.record(_make_entry(trace_id="t2", decision="human_review"))
        j.record(_make_entry(trace_id="t3", decision="auto_execute"))
        auto = j.auto_executed()
        assert len(auto) == 2
        assert all(e.decision == "auto_execute" for e in auto)

    def test_human_reviews_filter(self):
        j = DecisionJournal()
        j.record(_make_entry(trace_id="t1", decision="auto_execute"))
        j.record(_make_entry(trace_id="t2", decision="human_review"))
        j.record(_make_entry(trace_id="t3", decision="human_review"))
        human = j.human_reviews()
        assert len(human) == 2
        assert all(e.decision == "human_review" for e in human)

    def test_get_nonexistent_returns_none(self):
        j = DecisionJournal()
        assert j.get("nonexistent") is None

    def test_summary_includes_dispatch_stats(self):
        j = DecisionJournal()
        j.record(_make_entry(trace_id="t1", dispatch_result="executed"))
        j.record(_make_entry(trace_id="t2", dispatch_result="error"))
        j.record(_make_entry(trace_id="t3", dispatch_result="executed"))
        s = j.summary()
        assert s["by_dispatch_result"]["executed"] == 2
        assert s["by_dispatch_result"]["error"] == 1

    def test_singleton_returns_same_instance(self):
        j1 = get_journal()
        j2 = get_journal()
        assert j1 is j2
