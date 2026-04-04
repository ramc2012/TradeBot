"""Agent routes — proposals, approvals, chat."""
from __future__ import annotations
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter(prefix="/api/agent", tags=["agent"])


class ChatRequest(BaseModel):
    message: str


class ScanRequest(BaseModel):
    symbols: Optional[List[str]] = None


def _get_trading_agent():
    from agent import trading_agent

    return trading_agent


@router.get("/proposals")
async def get_proposals():
    return _get_trading_agent().get_pending_proposals()


@router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(proposal_id: str):
    success = await _get_trading_agent().approve_proposal(proposal_id)
    if not success:
        raise HTTPException(404, "Proposal not found or already acted on")
    return {"status": "approved", "proposal_id": proposal_id}


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(proposal_id: str):
    success = await _get_trading_agent().reject_proposal(proposal_id)
    if not success:
        raise HTTPException(404, "Proposal not found or already acted on")
    return {"status": "rejected", "proposal_id": proposal_id}


@router.post("/run-scan")
async def run_scan(req: ScanRequest):
    proposals = await _get_trading_agent().run_scan(req.symbols)
    return {
        "proposals_generated": len(proposals),
        "proposals": [
            {
                "id": p.id,
                "symbol": p.symbol,
                "strategy": p.strategy,
                "confidence": p.confidence,
            }
            for p in proposals
        ],
    }


@router.get("/agent-log")
async def get_agent_log(limit: int = Query(50)):
    return _get_trading_agent().get_agent_logs(limit)


@router.post("/chat")
async def chat(req: ChatRequest):
    response = await _get_trading_agent().chat(req.message)
    return {"response": response}


@router.get("/rules-status")
async def rules_status():
    return _get_trading_agent().rules_engine.get_status()


@router.post("/rules/{rule_name}")
async def toggle_rule(rule_name: str, enabled: bool = Query(True)):
    _get_trading_agent().rules_engine.set_rule(rule_name, enabled)
    return {"rule": rule_name, "enabled": enabled}


@router.post("/set-mode")
async def set_agent_mode(mode: str = Query(..., regex="^(paper|live)$")):
    _get_trading_agent().mode = mode
    return {"mode": mode}
