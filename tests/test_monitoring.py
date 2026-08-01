"""
CB-8 Monitoring + Circuit Breaker + Persistence tests
"""

import pytest
import time
from unittest.mock import AsyncMock, patch, MagicMock

from src.k9_mcts.monitoring import (
    MonitoringEngine, Alert, AlertType, AlertLevel, CircuitBreaker,
    get_monitor,
)
from src.k9_mcts.observability import JournalEntry, DecisionJournal
from src.k9_mcts.journal_persistence import JournalPersistence

import src.k9_mcts.monitoring as mon_mod
import src.k9_mcts.observability as obs_mod
import src.k9_mcts.journal_persistence as persist_mod
import src.k9_mcts.jepa_target_encoder as jepa_mod
import src.k9_mcts.handover_engine as he_mod
import src.k9_mcts.execution_dispatcher as disp_mod


@pytest.fixture(autouse=True)
def reset_singletons():
    jepa_mod._target_encoder = None
    he_mod._handover = None
    disp_mod._dispatcher = None
    obs_mod._journal = None
    mon_mod._monitor = None
    persist_mod._persistence = None
    yield
    jepa_mod._target_encoder = None
    he_mod._handover = None
    disp_mod._dispatcher = None
    obs_mod._journal = None
    mon_mod._monitor = None
    persist_mod._persistence = None


def _make_entry(
    trace_id="t1",
    decision="auto_execute",
    risk_level="read_only",
    task_class="compliance",
    confidence=0.95,
    dispatch_result="executed",
    timestamp=None,
):
    return JournalEntry(
        trace_id=trace_id,
        timestamp=timestamp or time.time(),
        question="check compliance",
        answer="clean",
        confidence=confidence,
        iterations=3,
        elapsed_ms=100.0,
        reasoning_trace=[],
        evidence=["evidence"],
        task_class=task_class,
        decision=decision,
        risk_level=risk_level,
        handover_reason="test",
        cooldown_seconds=0,
        dispatch_result=dispatch_result,
        dispatch_service="tx-adapter" if dispatch_result == "executed" else "",
        dispatch_latency_ms=50.0 if dispatch_result == "executed" else 0.0,
        dispatch_error="" if dispatch_result == "executed" else "timeout",
        jepa_total_updates=10,
        jepa_proven_classes=["compliance"],
        jepa_class_success_rate=0.9,
        envelope_id="env-1",
    )


# ── CircuitBreaker tests ────────────────────────────────────────────────────

class TestCircuitBreaker:
    def test_initial_state_not_tripped(self):
        cb = CircuitBreaker()
        assert cb.tripped is False
        assert cb.consecutive_errors == 0

    def test_success_resets_error_streak(self):
        cb = CircuitBreaker()
        cb.consecutive_errors = 5
        cb.record_dispatch(True)
        assert cb.consecutive_errors == 0
        assert cb.tripped is False

    def test_trips_after_threshold(self):
        cb = CircuitBreaker()
        for _ in range(10):
            cb.record_dispatch(False)
        assert cb.tripped is True
        assert cb.consecutive_errors == 10

    def test_does_not_trip_below_threshold(self):
        cb = CircuitBreaker()
        for _ in range(9):
            cb.record_dispatch(False)
        assert cb.tripped is False

    def test_trip_blocks_auto_execute(self):
        cb = CircuitBreaker()
        cb.trip("test")
        assert cb.record_dispatch(True) is False  # blocked
        assert cb.record_dispatch(False) is False  # still blocked

    def test_reset_clears_state(self):
        cb = CircuitBreaker()
        cb.trip("test")
        cb.reset()
        assert cb.tripped is False
        assert cb.consecutive_errors == 0

    def test_total_trips_incremented(self):
        cb = CircuitBreaker()
        cb.trip("reason1")
        cb.reset()
        cb.trip("reason2")
        assert cb.total_trips == 2

    def test_to_dict_has_all_fields(self):
        cb = CircuitBreaker()
        d = cb.to_dict()
        assert "tripped" in d
        assert "consecutive_errors" in d
        assert "threshold" in d
        assert "total_trips" in d


# ── MonitoringEngine tests ──────────────────────────────────────────────────

class TestMonitoringEngine:
    def test_initial_state(self):
        m = MonitoringEngine()
        s = m.status()
        assert s["total_evaluations"] == 0
        assert s["monitoring_active"] is True
        assert s["circuit_breaker"]["tripped"] is False

    def test_evaluate_records_counters(self):
        m = MonitoringEngine()
        m.evaluate(_make_entry(decision="auto_execute", dispatch_result="executed"))
        m.evaluate(_make_entry(decision="human_review", dispatch_result=""))
        m.evaluate(_make_entry(decision="reject", dispatch_result=""))
        s = m.status()
        assert s["decision_counts"]["auto_execute"] == 1
        assert s["decision_counts"]["human_review"] == 1
        assert s["decision_counts"]["reject"] == 1

    def test_error_rate_alert_triggers(self):
        m = MonitoringEngine()
        # 5 errors out of 6 = 83% error rate (threshold 30%)
        for i in range(5):
            m.evaluate(_make_entry(
                trace_id=f"err{i}", dispatch_result="error", decision="auto_execute",
            ))
        m.evaluate(_make_entry(trace_id="ok1", dispatch_result="executed", decision="auto_execute"))
        alerts = m.alerts()
        error_alerts = [a for a in alerts if a["alert_type"] == "error_rate_spike"]
        assert len(error_alerts) > 0

    def test_confidence_drift_alert(self):
        m = MonitoringEngine()
        # Build high baseline
        for i in range(15):
            m.evaluate(_make_entry(trace_id=f"hi{i}", confidence=0.95))
        # Sudden drop
        m.evaluate(_make_entry(trace_id="drop1", confidence=0.50))
        alerts = m.alerts()
        drift_alerts = [a for a in alerts if a["alert_type"] == "confidence_drift"]
        assert len(drift_alerts) > 0

    def test_no_alert_when_confidence_stable(self):
        m = MonitoringEngine()
        for i in range(20):
            m.evaluate(_make_entry(trace_id=f"stable{i}", confidence=0.93))
        drift_alerts = [a for a in m.alerts() if a["alert_type"] == "confidence_drift"]
        assert len(drift_alerts) == 0

    def test_circuit_breaker_trips_on_error_streak(self):
        m = MonitoringEngine()
        for i in range(10):
            m.evaluate(_make_entry(
                trace_id=f"err{i}", dispatch_result="error", decision="auto_execute",
            ))
        assert m._circuit_breaker.tripped is True
        assert m.can_auto_execute() is False

    def test_circuit_breaker_prevents_dispatch(self):
        m = MonitoringEngine()
        for i in range(10):
            m.evaluate(_make_entry(
                trace_id=f"err{i}", dispatch_result="error", decision="auto_execute",
            ))
        # Next auto_execute should be blocked
        assert m.can_auto_execute() is False

    def test_reset_circuit_breaker(self):
        m = MonitoringEngine()
        m._circuit_breaker.trip("test")
        result = m.reset_circuit_breaker()
        assert result["tripped"] is False
        assert m.can_auto_execute() is True

    def test_alerts_filtered_by_level(self):
        m = MonitoringEngine()
        # Trigger circuit breaker (emergency level)
        for i in range(10):
            m.evaluate(_make_entry(
                trace_id=f"err{i}", dispatch_result="error", decision="auto_execute",
            ))
        emergency_alerts = m.alerts(level="emergency")
        assert len(emergency_alerts) > 0
        warning_alerts = m.alerts(level="warning")
        # May or may not have warnings, but emergency alerts exist
        assert all(a["level"] == "emergency" for a in emergency_alerts)

    def test_human_review_backlog_alert(self):
        m = MonitoringEngine()
        # Create old human-review entries in the journal
        journal = obs_mod.get_journal()
        old_ts = time.time() - 3700  # > 1hr SLA
        for i in range(3):
            entry = _make_entry(
                trace_id=f"old_hr_{i}",
                decision="human_review",
                dispatch_result="",
                timestamp=old_ts,
            )
            journal.record(entry)
            m.evaluate(entry)
        backlog_alerts = [a for a in m.alerts() if a["alert_type"] == "human_review_backlog"]
        assert len(backlog_alerts) > 0

    def test_status_returns_all_sections(self):
        m = MonitoringEngine()
        m.evaluate(_make_entry())
        s = m.status()
        assert "monitoring_active" in s
        assert "decision_counts" in s
        assert "dispatch_stats" in s
        assert "confidence" in s
        assert "circuit_breaker" in s
        assert "thresholds" in s
        assert "active_alerts" in s

    def test_auto_rate_anomaly_alert(self):
        m = MonitoringEngine()
        # 15 auto-executes, then stop auto-executing
        for i in range(15):
            m.evaluate(_make_entry(trace_id=f"auto{i}", decision="auto_execute",
                                   dispatch_result="executed"))
        # Now 10 rejects (auto ratio drops to 15/25 = 60%, but we need <5%)
        # Actually we need total >= 20 and auto_ratio < 0.05 with auto_count > 5
        # That's hard to trigger with these counts. Let's do more rejects.
        for i in range(300):
            m.evaluate(_make_entry(trace_id=f"rej{i}", decision="reject",
                                   dispatch_result=""))
        # auto ratio = 15/315 = 4.7% < 5%, and auto_count=15 > 5
        anomaly_alerts = [a for a in m.alerts() if a["alert_type"] == "auto_rate_anomaly"]
        assert len(anomaly_alerts) > 0


# ── JournalPersistence tests ─────────────────────────────────────────────────

class TestJournalPersistence:
    def test_enqueue_adds_to_queue(self):
        p = JournalPersistence()
        p.enqueue(_make_entry())
        assert len(p._queue) == 1

    def test_entry_to_row_mapping(self):
        p = JournalPersistence()
        entry = _make_entry()
        row = p._entry_to_row(entry)
        assert row["trace_id"] == "t1"
        assert row["type"] == "auto_execute"
        assert row["source"] == "k9-mcts"
        assert row["confidence"] == 0.95
        assert "payload" in row
        assert "question" in row["payload"]
        assert "dispatch" in row["payload"]
        assert "jepa" in row["payload"]

    def test_entry_to_row_returns_none_for_empty_trace(self):
        p = JournalPersistence()
        entry = _make_entry()
        entry.trace_id = ""
        row = p._entry_to_row(entry)
        assert row is None

    def test_stats_disabled_without_api_key(self):
        p = JournalPersistence()
        p._api_key = ""
        s = p.stats()
        assert s["enabled"] is False
        assert "not configured" in s["supabase_url"]

    def test_stats_enabled_with_api_key(self):
        p = JournalPersistence()
        p._api_key = "test-key"
        s = p.stats()
        assert s["enabled"] is False  # not running
        assert s["queue_size"] >= 0

    @patch('src.k9_mcts.journal_persistence.httpx.AsyncClient')
    def test_flush_sends_batch(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        p = JournalPersistence()
        p._api_key = "test-key"
        p._headers["Authorization"] = "Bearer test-key"
        p._headers["apikey"] = "test-key"
        for i in range(3):
            p.enqueue(_make_entry(trace_id=f"t{i}"))

        import asyncio
        asyncio.get_event_loop().run_until_complete(p._flush())
        assert p.total_persisted == 3
        assert len(p._queue) == 0

    @patch('src.k9_mcts.journal_persistence.httpx.AsyncClient')
    def test_flush_requeues_on_error(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "server error"
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        p = JournalPersistence()
        p._api_key = "test-key"
        p._headers["Authorization"] = "Bearer test-key"
        p._headers["apikey"] = "test-key"
        for i in range(3):
            p.enqueue(_make_entry(trace_id=f"t{i}"))

        import asyncio
        asyncio.get_event_loop().run_until_complete(p._flush())
        assert p.total_errors >= 1
        # Should be re-queued
        assert len(p._queue) > 0

    def test_singleton_returns_same_instance(self):
        m1 = get_monitor()
        m2 = get_monitor()
        assert m1 is m2
