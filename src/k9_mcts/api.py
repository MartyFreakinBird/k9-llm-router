"""
k9-mcts — api.py
FastAPI router — mounts at /reason in main.py
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
import asyncio

from .orchestrator import K9MCTSOrchestrator

router = APIRouter(prefix="/reason", tags=["MCTS Reasoning"])
_orchestrator = K9MCTSOrchestrator()


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


@router.post("", response_model=ReasonResponse)
async def reason(req: ReasonRequest):
    """
    Run MCTS reasoning loop over a question.
    Returns the committed answer + full reasoning trace.
    Blocks until confidence >= 0.92 OR max_depth reached.
    """
    orc = K9MCTSOrchestrator(timeout_s=req.timeout_s)
    result = await orc.reason(req.question, req.context)
    return ReasonResponse(**{k: result[k] for k in ReasonResponse.__fields__})


@router.get("/health")
async def mcts_health():
    return {
        "status": "online",
        "module": "k9-mcts",
        "threshold": 0.92,
        "max_depth": 5,
        "n_branches": 3,
    }
