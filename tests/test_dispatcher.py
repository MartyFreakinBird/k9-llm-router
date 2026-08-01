"""
CB-6 Execution Dispatcher tests
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

from src.k9_mcts.execution_dispatcher import (
    ExecutionDispatcher, ExecutionOutcome, DispatchResult, get_dispatcher
)
from src.k9_mcts.handover_engine import HandoverResult, HandoverDecision, RiskLevel
from src.k9_mcts.jepa_target_encoder import JEPATargetEncoder, OutcomeRecord

import src.k9_mcts.jepa_target_encoder as jepa_mod
import src.k9_mcts.handover_engine as he_mod
import src.k9_mcts.execution_dispatcher as disp_mod


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset all CB-6 singletons for test isolation."""
    jepa_mod._target_encoder = None
    he_mod._handover = None
    disp_mod._dispatcher = None
    yield
    jepa_mod._target_encoder = None
    he_mod._handover = None
    disp_mod._dispatcher = None


def _make_handover(decision=HandoverDecision.AUTO_EXECUTE, task_class="compliance",
                   risk=RiskLevel.READ_ONLY, confidence=0.95):
    return HandoverResult(
        decision=decision, risk_level=risk, task_class=task_class,
        confidence=confidence, reason="test", cooldown_seconds=0,
    )


class TestExecutionDispatcher:
    def test_reject_non_auto_execute(self):
        """Dispatcher must NEVER execute when decision is not AUTO_EXECUTE."""
        d = ExecutionDispatcher()
        handover = _make_handover(decision=HandoverDecision.HUMAN_REVIEW)
        result = asyncio.get_event_loop().run_until_complete(
            d.dispatch(handover, "question", "answer")
        )
        assert result.dispatch_result == DispatchResult.REJECTED
        assert d.noop_count == 1

    def test_reject_below_threshold(self):
        d = ExecutionDispatcher()
        handover = _make_handover(decision=HandoverDecision.REJECT)
        result = asyncio.get_event_loop().run_until_complete(
            d.dispatch(handover, "q", "a")
        )
        assert result.dispatch_result == DispatchResult.REJECTED

    @patch('src.k9_mcts.execution_dispatcher.httpx.AsyncClient')
    def test_dispatch_compliance_calls_tx_adapter(self, mock_client_cls):
        """Compliance + read_only → TX adapter :9005/compliance/check."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"finding": {"frozen": False}}
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        d = ExecutionDispatcher()
        handover = _make_handover(task_class="compliance", risk=RiskLevel.READ_ONLY)
        result = asyncio.get_event_loop().run_until_complete(
            d.dispatch(handover, "check compliance for 0xabc", "clean")
        )
        assert result.dispatch_result == DispatchResult.EXECUTED
        assert result.service_called == "tx-adapter:compliance"
        mock_client.post.assert_called_once()

    @patch('src.k9_mcts.execution_dispatcher.httpx.AsyncClient')
    def test_dispatch_forensic_calls_tx_adapter_balance(self, mock_client_cls):
        """Forensic + read_only → TX adapter :9005/balance/{addr}."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"balance": "100"}
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        d = ExecutionDispatcher()
        handover = _make_handover(task_class="forensic", risk=RiskLevel.READ_ONLY)
        result = asyncio.get_event_loop().run_until_complete(
            d.dispatch(handover, "analyze 0xabc123def456789012345678901234567890abcd", "found")
        )
        assert result.dispatch_result == DispatchResult.EXECUTED
        assert result.service_called == "tx-adapter:balance"

    def test_dispatch_forensic_no_address_errors(self):
        """Forensic query with no extractable address → error."""
        d = ExecutionDispatcher()
        handover = _make_handover(task_class="forensic", risk=RiskLevel.READ_ONLY)
        result = asyncio.get_event_loop().run_until_complete(
            d.dispatch(handover, "analyze the situation", "no address here")
        )
        assert result.dispatch_result == DispatchResult.ERROR
        assert "no address" in result.error.lower()

    @patch('src.k9_mcts.execution_dispatcher.httpx.AsyncClient')
    def test_dispatch_idempotent_calls_signal_router(self, mock_client_cls):
        """Execution + idempotent → AEG signal router :9004/batch."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"accepted": 1}
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        d = ExecutionDispatcher()
        handover = _make_handover(task_class="execution", risk=RiskLevel.IDEMPOTENT)
        result = asyncio.get_event_loop().run_until_complete(
            d.dispatch(handover, "refresh cache", "done")
        )
        assert result.dispatch_result == DispatchResult.EXECUTED
        assert result.service_called == "aeg-signal-router:batch"

    def test_no_op_for_unmapped_combination(self):
        """Unknown task_class/risk combination → NO_OP."""
        d = ExecutionDispatcher()
        handover = _make_handover(task_class="unknown", risk=RiskLevel.READ_ONLY)
        result = asyncio.get_event_loop().run_until_complete(
            d.dispatch(handover, "q", "a")
        )
        assert result.dispatch_result == DispatchResult.NO_OP
        assert d.noop_count == 1

    def test_error_counted_on_exception(self):
        """Exceptions are caught and counted."""
        d = ExecutionDispatcher()
        handover = _make_handover(task_class="compliance", risk=RiskLevel.READ_ONLY)
        # No mock → httpx will fail to connect
        result = asyncio.get_event_loop().run_until_complete(
            d.dispatch(handover, "check 0xabc", "answer")
        )
        assert result.dispatch_result == DispatchResult.ERROR
        assert d.error_count == 1

    def test_stats_returned(self):
        d = ExecutionDispatcher()
        d.dispatch_count = 5
        d.error_count = 1
        d.noop_count = 2
        d.total_latency_ms = 500.0
        s = d.stats()
        assert s["dispatch_count"] == 5
        assert s["error_count"] == 1
        assert s["noop_count"] == 2
        assert s["avg_latency_ms"] == 100.0

    def test_outcome_recorded_to_encoder(self):
        """Dispatcher feeds execution outcome back to the target encoder."""
        d = ExecutionDispatcher()
        d._record_outcome(
            _make_handover(task_class="compliance"),
            ExecutionOutcome(
                dispatch_result=DispatchResult.EXECUTED,
                service_called="tx-adapter",
                raw_response={"frozen": False},
                latency_ms=50.0,
            ),
            "clean",
            "trace1",
        )
        assert d._encoder.total_updates == 1

    def test_failed_outcome_recorded_with_low_confidence(self):
        """Failed dispatch → low confidence in the target encoder."""
        d = ExecutionDispatcher()
        d._record_outcome(
            _make_handover(confidence=0.95, task_class="compliance"),
            ExecutionOutcome(
                dispatch_result=DispatchResult.ERROR,
                service_called="tx-adapter",
                error="timeout",
            ),
            "failed",
            "trace2",
        )
        stats = d._encoder.get_class_stats("compliance")
        # confidence should be 0.3 (failure mapping)
        assert stats["mean_confidence"] == pytest.approx(0.3, abs=0.01)


class TestAddressExtraction:
    def test_extract_hex_address(self):
        addr = "0xabc123def456789012345678901234567890abcd"
        assert ExecutionDispatcher._extract_address(f"check {addr}") == addr

    def test_extract_bech32_address(self):
        addr = "core1abcdefghijklmnopqrstuvwxyz1234567890abcd"
        assert ExecutionDispatcher._extract_address(f"trace {addr}") == addr

    def test_extract_no_address(self):
        assert ExecutionDispatcher._extract_address("no address here") == ""

    def test_extract_picks_first_if_multiple(self):
        addr1 = "0xabc123def456789012345678901234567890abcd"
        addr2 = "0xdef123abc456789012345678901234567890abcd"
        result = ExecutionDispatcher._extract_address(f"{addr1} and {addr2}")
        assert result == addr1
