"""
CB-6 Tests — JEPA target encoder + handover engine
"""

import pytest
import math
from src.k9_mcts.jepa_target_encoder import (
    JEPATargetEncoder, OutcomeRecord, LatentVector, _hash_to_vector, EMA_MOMENTUM
)
from src.k9_mcts.handover_engine import (
    HandoverEngine, HandoverDecision, RiskLevel,
    classify_risk, classify_task,
)


# ── LatentVector tests ──────────────────────────────────────────────────────

class TestLatentVector:
    def test_cosine_similarity_identical(self):
        v = LatentVector(values=[1.0, 0.0, 0.0])
        assert v.cosine_similarity(v) == pytest.approx(1.0, abs=1e-6)

    def test_cosine_similarity_orthogonal(self):
        v1 = LatentVector(values=[1.0, 0.0])
        v2 = LatentVector(values=[0.0, 1.0])
        assert v1.cosine_similarity(v2) == pytest.approx(0.0, abs=1e-6)

    def test_cosine_similarity_zero_vector(self):
        v = LatentVector(values=[0.0, 0.0, 0.0])
        assert v.cosine_similarity(v) == 0.0

    def test_ema_update_converges(self):
        v = LatentVector(values=[0.0, 0.0, 0.0])
        target = LatentVector(values=[1.0, 1.0, 1.0])
        # After many updates with momentum 0.999, should converge toward target
        for _ in range(10000):
            v.ema_update(target, momentum=0.9)  # faster convergence for test
        assert all(abs(x - 1.0) < 0.01 for x in v.values)

    def test_ema_momentum_constant(self):
        """The EMA momentum constant is locked at 0.999 per user instruction."""
        assert EMA_MOMENTUM == 0.999

    def test_hash_to_vector_is_deterministic(self):
        v1 = _hash_to_vector("sanction check")
        v2 = _hash_to_vector("sanction check")
        assert v1.values == v2.values

    def test_hash_to_vector_different_inputs_different_vectors(self):
        v1 = _hash_to_vector("is this wallet sanctioned?")
        v2 = _hash_to_vector("transfer all funds to bridge")
        assert v1.cosine_similarity(v2) < 0.99  # not identical


# ── JEPA Target Encoder tests ────────────────────────────────────────────────

class TestJEPATargetEncoder:
    def test_initial_state(self):
        enc = JEPATargetEncoder()
        assert enc.total_updates == 0
        assert len(enc.proven_classes) == 0
        stats = enc.stats()
        assert stats["ema_momentum"] == 0.999

    def test_observe_updates_embeddings(self):
        enc = JEPATargetEncoder(confidence_threshold=0.8)
        outcome = OutcomeRecord(
            trace_id="t1", hypothesis="check wallet compliance",
            task_class="compliance", confidence=0.95, critique_score=0.9,
            iterations=3, elapsed_ms=100, evidence_count=5,
        )
        enc.observe(outcome)
        assert enc.total_updates == 1
        assert enc.stats()["good_embedding_norm"] > 0.0

    def test_observe_bad_outcome_updates_bad_embedding(self):
        enc = JEPATargetEncoder(confidence_threshold=0.8)
        outcome = OutcomeRecord(
            trace_id="t2", hypothesis="risky transfer",
            task_class="execution", confidence=0.3, critique_score=0.2,
            iterations=5, elapsed_ms=200, evidence_count=2,
        )
        enc.observe(outcome)
        assert enc.stats()["bad_embedding_norm"] > 0.0

    def test_predict_branch_prior_neutral_before_data(self):
        enc = JEPATargetEncoder()
        prior = enc.predict_branch_prior("any hypothesis", "forensic")
        assert prior["prior_score"] == 0.5
        assert prior["recommended"] is True  # neutral = don't starve the tree

    def test_predict_branch_prior_after_learning(self):
        enc = JEPATargetEncoder(confidence_threshold=0.8)
        # Feed 5 good outcomes with similar hypothesis text
        for i in range(5):
            enc.observe(OutcomeRecord(
                trace_id=f"good{i}", hypothesis="check wallet compliance status",
                task_class="compliance", confidence=0.95, critique_score=0.9,
                iterations=3, elapsed_ms=100, evidence_count=5,
            ))
        # Feed 5 bad outcomes with different text
        for i in range(5):
            enc.observe(OutcomeRecord(
                trace_id=f"bad{i}", hypothesis="transfer all funds to bridge",
                task_class="execution", confidence=0.3, critique_score=0.2,
                iterations=5, elapsed_ms=200, evidence_count=2,
            ))
        # Similar to good → should have higher prior
        prior_good = enc.predict_branch_prior("check wallet compliance", "compliance")
        prior_bad = enc.predict_branch_prior("transfer all funds to bridge", "execution")
        assert prior_good["prior_score"] != prior_bad["prior_score"]

    def test_class_promotion_to_proven(self):
        enc = JEPATargetEncoder(confidence_threshold=0.8)
        enc.min_proven_samples = 5  # speed up test
        for i in range(5):
            enc.observe(OutcomeRecord(
                trace_id=f"t{i}", hypothesis="compliance check",
                task_class="compliance", confidence=0.95, critique_score=0.9,
                iterations=3, elapsed_ms=100, evidence_count=5,
            ))
        assert "compliance" in enc.proven_classes
        assert enc.is_proven("compliance") is True
        assert enc.is_proven("forensic") is False

    def test_class_stats_tracking(self):
        enc = JEPATargetEncoder(confidence_threshold=0.8)
        for i in range(3):
            enc.observe(OutcomeRecord(
                trace_id=f"t{i}", hypothesis="check compliance",
                task_class="compliance", confidence=0.9, critique_score=0.8,
                iterations=2, elapsed_ms=50, evidence_count=3,
            ))
        stats = enc.get_class_stats("compliance")
        assert stats["total"] == 3
        assert stats["successes"] == 3
        assert stats["mean_confidence"] == pytest.approx(0.9, abs=0.01)

    def test_history_bounded(self):
        enc = JEPATargetEncoder()
        enc.max_history = 5
        for i in range(10):
            enc.observe(OutcomeRecord(
                trace_id=f"t{i}", hypothesis="test",
                task_class="test", confidence=0.5, critique_score=0.5,
                iterations=1, elapsed_ms=10, evidence_count=1,
            ))
        assert len(enc.history) == 5

    def test_no_backprop_allowed(self):
        """The target encoder only uses EMA — no gradient-based updates."""
        enc = JEPATargetEncoder()
        assert not hasattr(enc, 'backward') or not callable(getattr(enc, 'backward', None))
        assert not hasattr(enc, 'parameters') or not callable(getattr(enc, 'parameters', None))


# ── Handover Engine tests ────────────────────────────────────────────────────

class TestRiskClassification:
    def test_read_only(self):
        assert classify_risk("check wallet compliance status") == RiskLevel.READ_ONLY

    def test_idempotent(self):
        assert classify_risk("refresh cache and reindex") == RiskLevel.IDEMPOTENT

    def test_state_changing(self):
        assert classify_risk("update the risk params") == RiskLevel.STATE_CHANGING

    def test_high_risk_transfer(self):
        assert classify_risk("transfer all funds to bridge") == RiskLevel.HIGH_RISK

    def test_high_risk_swap(self):
        assert classify_risk("swap tokens on the DEX") == RiskLevel.HIGH_RISK

    def test_default_read_only(self):
        assert classify_risk("what is the meaning of this?") == RiskLevel.READ_ONLY


class TestTaskClassification:
    def test_compliance(self):
        assert classify_task("is this wallet sanctioned?") == "compliance"

    def test_execution(self):
        assert classify_task("swap tokens for profit") == "execution"

    def test_analytics(self):
        assert classify_task("count all transactions in last 7 days") == "analytics"

    def test_forensic_default(self):
        assert classify_task("trace the origin of these funds") == "forensic"


class TestHandoverEngine:
    def test_reject_below_threshold(self):
        he = HandoverEngine(confidence_threshold=0.92)
        result = he.evaluate(
            question="check compliance", answer="looks fine",
            confidence=0.5, iterations=1, elapsed_ms=100, evidence_count=2,
        )
        assert result.decision == HandoverDecision.REJECT

    def test_high_risk_always_human_review(self):
        he = HandoverEngine(confidence_threshold=0.92)
        result = he.evaluate(
            question="transfer all funds to bridge", answer="executing",
            confidence=0.98, iterations=3, elapsed_ms=200, evidence_count=5,
        )
        assert result.decision == HandoverDecision.HUMAN_REVIEW
        assert result.risk_level == RiskLevel.HIGH_RISK
        assert result.cooldown_seconds > 0

    def test_state_changing_always_human_review(self):
        he = HandoverEngine(confidence_threshold=0.92)
        result = he.evaluate(
            question="update the risk params", answer="done",
            confidence=0.98, iterations=3, elapsed_ms=200, evidence_count=5,
        )
        assert result.decision == HandoverDecision.HUMAN_REVIEW
        assert result.risk_level == RiskLevel.STATE_CHANGING

    def test_read_only_unproven_goes_to_human_review(self):
        he = HandoverEngine(confidence_threshold=0.92)
        result = he.evaluate(
            question="check wallet compliance", answer="clean",
            confidence=0.95, iterations=3, elapsed_ms=150, evidence_count=4,
        )
        assert result.decision == HandoverDecision.HUMAN_REVIEW
        assert "not yet proven" in result.reason

    def test_read_only_proven_auto_executes(self):
        # Reset global singletons for isolation
        import src.k9_mcts.jepa_target_encoder as jepa_mod
        import src.k9_mcts.handover_engine as he_mod
        jepa_mod._target_encoder = None
        he_mod._handover = None
        he = HandoverEngine(confidence_threshold=0.8)
        he.encoder.min_proven_samples = 3
        # Train the encoder with 3 successful compliance outcomes
        for i in range(3):
            he.evaluate(
                question="check wallet compliance", answer=f"clean {i}",
                confidence=0.9, iterations=3, elapsed_ms=100, evidence_count=4,
            )
        # Now it should be proven and auto-execute
        result = he.evaluate(
            question="check wallet compliance", answer="clean",
            confidence=0.9, iterations=3, elapsed_ms=100, evidence_count=4,
        )
        assert result.decision == HandoverDecision.AUTO_EXECUTE
        assert result.risk_level == RiskLevel.READ_ONLY
        assert result.execution_envelope["type"] == "execution_request"

    def test_envelope_is_cb_v1_compliant(self):
        he = HandoverEngine(confidence_threshold=0.8)
        he.encoder.min_proven_samples = 2
        for i in range(2):
            he.evaluate(question="check compliance", answer="ok",
                confidence=0.9, iterations=1, elapsed_ms=50, evidence_count=2)
        result = he.evaluate(question="check compliance", answer="ok",
            confidence=0.9, iterations=1, elapsed_ms=50, evidence_count=2)
        env = result.execution_envelope
        assert env["spec"] == "cb.v1"
        assert env["source"] == "k9-handover"
        assert "trace_id" in env
        assert "confidence" in env
        assert "timestamp" in env
        assert "payload" in env

    def test_stats_returns_counts(self):
        he = HandoverEngine(confidence_threshold=0.92)
        he.evaluate(question="check", answer="ok", confidence=0.5,
            iterations=1, elapsed_ms=10, evidence_count=1)  # reject
        he.evaluate(question="transfer all", answer="ok", confidence=0.95,
            iterations=3, elapsed_ms=100, evidence_count=3)  # human review
        s = he.stats()
        assert s["reject_count"] == 1
        assert s["human_review_count"] == 1
        assert s["auto_execute_count"] == 0
