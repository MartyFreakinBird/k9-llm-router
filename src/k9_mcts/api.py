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
