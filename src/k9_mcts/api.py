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
from .approval_workflow import get_approval_workflow, ApprovalState
from .security_reinforcement import get_security_gate
from .forensic_analyzer import get_forensic_analyzer, ThreatAssessment, AttackClassification, RecommendedAction
from .handover_engine import HandoverDecision
from .execution_dispatcher import get_dispatcher

router = APIRouter(prefix="/reason", tags=["MCTS Reasoning"])


# ── CB-9: Start Supabase approval decision poller ─────────────────────────────
@router.on_event("startup")
async def _start_supabase_poller():
    """Start background polling of Supabase cb_messages for Lovable UI approval decisions."""
    try:
        wf = get_approval_workflow()
        wf.start_supabase_poller(interval=3.0)
    except Exception as e:
        import logging
        logging.getLogger("k9.approval").debug(f"[APPROVAL] Supabase poller not started: {e}")


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


# ── CB-9: Human-in-the-Loop Approval Workflow ──────────────────────────────

@router.get("/approvals")
async def approvals_pending(limit: int = 20):
    """CB-9: Pending human-review approval tasks awaiting operator action."""
    tasks = get_approval_workflow().pending(limit=limit)
    return {
        "count": len(tasks),
        "pending": [t.to_dict() for t in tasks],
    }


@router.get("/approvals/recent")
async def approvals_recent(limit: int = 50, state: str = ""):
    """CB-9: Recent approval tasks, optionally filtered by state."""
    tasks = get_approval_workflow().recent(limit=limit, state=state)
    return {
        "count": len(tasks),
        "tasks": [t.to_dict() for t in tasks],
    }


@router.get("/approvals/stats")
async def approval_stats():
    """CB-9: Aggregate approval statistics."""
    return get_approval_workflow().stats()




# -- CB-9 SECURITY: Security reinforcement endpoints ----------------------------

@router.get("/security/status")
async def security_status():
    return get_security_gate().status()


@router.post("/security/kill-switch/activate")
async def kill_switch_activate(reason: str = "manual"):
    return get_security_gate().kill_switch.activate(reason, "api-operator")


@router.post("/security/kill-switch/deactivate")
async def kill_switch_deactivate(authorized_by: str = "operator"):
    return get_security_gate().kill_switch.deactivate(authorized_by)


@router.post("/security/behavioral/halt")
async def behavioral_halt(reason: str = "manual"):
    get_security_gate().behavior.halt(reason)
    return {"halted": True, "reason": reason}


@router.post("/security/behavioral/resume")
async def behavioral_resume():
    get_security_gate().behavior.resume()
    return {"halted": False}


@router.get("/security/forensics/export")
async def forensics_export(limit: int = 100):
    return {
        "records": get_security_gate().forensics.export(limit),
        "stats": get_security_gate().forensics.stats(),
    }


@router.get("/security/anomalies")
async def security_anomalies(limit: int = 20):
    return {
        "anomalies": get_security_gate().behavior.recent_anomalies(limit),
        "baseline": get_security_gate().behavior.baseline_stats(),
    }


@router.get("/security/egress")
async def egress_status():
    return get_security_gate().egress.list_services()


@router.post("/security/egress/{service_name}/remove")
async def egress_remove_service(service_name: str):
    get_security_gate().egress.remove_service(service_name)
    return {"removed": service_name, "remaining": list(get_security_gate().egress.list_services().keys())}



# -- CB-9 FORENSIC ANALYZER: LLM-powered forensic analysis -----------------------

@router.post("/security/forensics/analyze")
async def forensics_analyze(limit: int = 50):
    """Run LLM forensic analysis on captured records. Uses local Ollama model."""
    gate = get_security_gate()
    analyzer = get_forensic_analyzer()
    records = gate.forensics.export(limit)
    assessment = await analyzer.analyze(records)
    return assessment.to_dict()


@router.get("/security/forensics/analysis")
async def forensics_last_analysis():
    """Get the last forensic analysis result."""
    analyzer = get_forensic_analyzer()
    last = analyzer._last_analysis
    if not last:
        return {"error": "no analysis performed yet", "status": analyzer.status()}
    return last.to_dict()


@router.get("/security/forensics/analyzer-status")
async def forensics_analyzer_status():
    """Get forensic analyzer configuration and status."""
    return get_forensic_analyzer().status()


@router.post("/security/forensics/auto-analyze")
async def forensics_toggle_auto(enable: bool = True):
    """Toggle auto-analysis mode. When enabled, analyzes records every 5 min."""
    analyzer = get_forensic_analyzer()
    if enable and not analyzer._auto_mode:
        gate = get_security_gate()
        await analyzer.start_auto_analysis(
            get_records_fn=lambda: gate.forensics.export(50),
        )
    elif not enable and analyzer._auto_mode:
        await analyzer.stop_auto_analysis()
    return analyzer.status()
@router.get("/journal/{trace_id}")
async def journal_entry(trace_id: str):
    """CB-7: Full decision detail by trace ID."""
    entry = get_journal().get(trace_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"trace_id {trace_id} not found")
    return entry.to_dict()


@router.post("/approvals/{approval_id}/approve")
async def approval_approve(approval_id: str, approved_by: str = "operator", note: str = ""):
    """
    CB-9: Approve a pending human-review task.
    If the circuit breaker is NOT tripped, immediately dispatches via the executor.
    """
    wf = get_approval_workflow()
    task = wf.approve(approval_id, approved_by=approved_by, note=note)
    if not task:
        raise HTTPException(status_code=404, detail=f"approval {approval_id} not found or not pending")

    # Attempt dispatch if circuit breaker allows
    monitor = get_monitor()
    if not monitor.can_auto_execute():
        return {
            "approval": task.to_dict(),
            "dispatch": {"result": "circuit_breaker_tripped", "error": "circuit breaker active — manual reset required"},
        }

    # Dispatch via the execution dispatcher
    from .handover_engine import get_handover_engine, HandoverDecision, RiskLevel, HandoverResult
    handover = get_handover_engine()

    # Reconstruct a minimal HandoverResult for the dispatcher
    risk = RiskLevel(task.risk_level)
    hr = HandoverResult(
        decision=HandoverDecision.AUTO_EXECUTE,
        risk_level=risk,
        task_class=task.task_class,
        confidence=task.confidence,
        reason="human-approved",
    )

    dispatcher = get_dispatcher()
    outcome = await dispatcher.dispatch(
        handover=hr,
        question=task.question,
        answer=task.answer,
        trace_id=task.trace_id,
    )

    # Record the dispatch result
    wf.mark_executed(
        approval_id,
        dispatch_result=outcome.dispatch_result.value,
        dispatch_service=outcome.service_called,
        dispatch_latency_ms=outcome.latency_ms,
        dispatch_error=outcome.error,
    )

    return {
        "approval": wf.get(approval_id).to_dict(),
        "dispatch": {
            "result": outcome.dispatch_result.value,
            "service": outcome.service_called,
            "latency_ms": round(outcome.latency_ms, 1),
            "error": outcome.error,
        },
    }


@router.post("/approvals/{approval_id}/reject")
async def approval_reject(approval_id: str, approved_by: str = "operator", note: str = ""):
    """CB-9: Reject a pending human-review task. Feeds negative outcome to JEPA encoder."""
    task = get_approval_workflow().reject(approval_id, approved_by=approved_by, note=note)
    if not task:
        raise HTTPException(status_code=404, detail=f"approval {approval_id} not found or not pending")
    return task.to_dict()


@router.get("/approvals/{approval_id}")
async def approval_detail(approval_id: str):
    """CB-9: Full approval task detail by ID."""
    task = get_approval_workflow().get(approval_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"approval {approval_id} not found")
    return task.to_dict()
