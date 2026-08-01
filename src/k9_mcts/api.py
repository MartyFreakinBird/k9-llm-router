"""
k9-mcts — api.py
FastAPI router — mounts at /reason in main.py

CB-6: Added /reason/autonomous and /reason/handover/stats endpoints
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
import asyncio

from .orchestrator import K9MCTSOrchestrator
from .jepa_target_encoder import get_target_encoder
from .handover_engine import get_handover_engine, HandoverDecision
from .execution_dispatcher import get_dispatcher, DispatchResult
from .observability import get_journal
from .journal_persistence import get_persistence
from .monitoring import get_monitor

router = APIRouter(prefix="/reason", tags=["MCTS Reasoning"])


class ReasonRequest(BaseModel):
    question: str = Field(..., description="Forensic or analytical query")
    context: Optional[dict] = Field(default=None, description="Optional context dict")
    timeout_s: float = Field(default=30.0, ge=5.0, le=120.0)


class ReasonResponse(BaseModel):
    trace_id: str
    question: str
    answer: str
    confidence: float
    committed: bool
    iterations: int
    elapsed_ms: float
    reasoning_trace: list
    evidence: list


class AutonomousReasonResponse(BaseModel):
    trace_id: str
    question: str
    answer: str
    confidence: float
    committed: bool
    iterations: int
    elapsed_ms: float
    reasoning_trace: list
    evidence: list
    handover: dict


@router.post("", response_model=ReasonResponse)
async def reason(req: ReasonRequest):
    """Run MCTS reasoning loop. Returns committed answer + reasoning trace."""
    orc = K9MCTSOrchestrator(timeout_s=req.timeout_s)
    result = await orc.reason(req.question, req.context)
    return ReasonResponse(**{k: result[k] for k in ReasonResponse.__fields__})


@router.post("/autonomous", response_model=AutonomousReasonResponse)
async def reason_autonomous(req: ReasonRequest):
    """
    CB-6: Run MCTS + handover engine evaluation.
    Returns the committed answer, reasoning trace, AND the handover decision
    (auto_execute, human_review, or reject) with risk classification.
    """
    orc = K9MCTSOrchestrator(timeout_s=req.timeout_s)
    result = await orc.reason(req.question, req.context)
    return AutonomousReasonResponse(
        trace_id=result["trace_id"],
        question=result["question"],
        answer=result["answer"],
        confidence=result["confidence"],
        committed=result["committed"],
        iterations=result["iterations"],
        elapsed_ms=result["elapsed_ms"],
        reasoning_trace=result["reasoning_trace"],
        evidence=result["evidence"],
        handover=result.get("handover", {}),
    )


@router.get("/health")
async def mcts_health():
    return {
        "status": "online",
        "module": "k9-mcts",
        "threshold": 0.92,
        "max_depth": 5,
        "n_branches": 3,
        "cb_version": 6,
    }


@router.get("/handover/stats")
async def handover_stats():
    """CB-6: Handover engine statistics (auto-execute vs human-review counts)."""
    return get_handover_engine().stats()


@router.get("/jepa/stats")
async def jepa_stats():
    """CB-6: JEPA target encoder statistics (proven classes, embedding norms)."""
    return get_target_encoder().stats()


@router.get("/jepa/classes")
async def jepa_classes():
    """CB-6: Per-task-class statistics from the target encoder."""
    return get_target_encoder().get_class_stats()


@router.get("/dispatch/stats")
async def dispatch_stats():
    """CB-6: Execution dispatcher statistics (dispatch count, latency)."""
    return get_dispatcher().stats()


# ── CB-7: Observability & Decision Journal ──────────────────────────────────

@router.get("/journal")
async def journal_recent(
    limit: int = 20,
    decision: str = "",
    risk_level: str = "",
    task_class: str = "",
):
    """CB-7: Recent decisions in the journal, filterable."""
    entries = get_journal().recent(
        limit=limit, decision=decision, risk_level=risk_level, task_class=task_class
    )
    return {
        "count": len(entries),
        "entries": [e.to_dict() for e in entries],
    }


@router.get("/journal/summary")
async def journal_summary():
    """CB-7: Aggregate decision statistics (auto/human/reject counts, risk distribution)."""
    return get_journal().summary()


@router.get("/journal/auto")
async def journal_auto(limit: int = 50):
    """CB-7: Only auto-executed decisions (human audit of autonomous actions)."""
    entries = get_journal().auto_executed(limit=limit)
    return {
        "count": len(entries),
        "entries": [e.to_dict() for e in entries],
    }


@router.get("/journal/human")
async def journal_human(limit: int = 50):
    """CB-7: Only human-review decisions (pending human action)."""
    entries = get_journal().human_reviews(limit=limit)
    return {
        "count": len(entries),
        "entries": [e.to_dict() for e in entries],
    }


@router.get("/journal/export")
async def journal_export():
    """CB-7: Export full journal as JSON array (for backup/analysis)."""
    return get_journal().export()


@router.get("/persistence/stats")
async def persistence_stats():
    """CB-7.5: Supabase persistence statistics (persisted count, queue, errors)."""
    return get_persistence().stats()


@router.post("/persistence/flush")
async def persistence_flush():
    """CB-7.5: Manually trigger a flush of queued entries to Supabase."""
    p = get_persistence()
    await p._flush()
    return p.stats()


# ── CB-8: Real-time Monitoring & Alerting ────────────────────────────────────

@router.get("/monitor/status")
async def monitor_status():
    """CB-8: Full monitoring state — decision counts, error rate, alerts, circuit breaker."""
    return get_monitor().status()


@router.get("/monitor/alerts")
async def monitor_alerts(level: str = "", limit: int = 50):
    """CB-8: Recent alerts, optionally filtered by level (info/warning/critical/emergency)."""
    return {
        "count": len(get_monitor().alerts(level=level, limit=limit)),
        "alerts": get_monitor().alerts(level=level, limit=limit),
    }


@router.get("/monitor/circuit-breaker")
async def circuit_breaker_status():
    """CB-8: Circuit breaker state (tripped/consecutive_errors/total_trips)."""
    return get_monitor()._circuit_breaker.to_dict()


@router.post("/monitor/circuit-breaker/reset")
async def circuit_breaker_reset():
    """CB-8: Manually reset the circuit breaker to resume auto-execution."""
    return get_monitor().reset_circuit_breaker()


@router.get("/journal/{trace_id}")
async def journal_entry(trace_id: str):
    """CB-7: Full decision detail by trace ID."""
    entry = get_journal().get(trace_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"trace_id {trace_id} not found")
    return entry.to_dict()
