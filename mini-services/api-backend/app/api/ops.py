"""Automation Operations Center API (v57/v58) - the control-plane endpoints.

GET  /ops/overview              - whole-environment rollup (SYSTEM verdict)
GET  /ops/incidents/{exec_id}   - the full incident drilldown chain
POST /ops/ai/investigate        - the AI-operations investigation (v58)
POST /ops/ai/apply-proposal     - apply an AI-proposed policy patch (user executes)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user, own_or_404
from ..db import get_db
from ..models import Workflow
from ..schemas import validate_execution_policy
from ..services.aiops import investigate
from ..services.devices import device_mode_report
from ..services.ops import incident_chain, ops_overview
from ..services.scheduler import resync_workflow_jobs
from ..services.versions import snapshot_workflow_version

router = APIRouter(prefix="/ops", tags=["ops"])


class InvestigateRequest(BaseModel):
    execution_id: str = Field(..., min_length=1, max_length=36)
    narrate: bool = Field(default=False, description="Ask the sandbox-bridge LLM to narrate the findings (fail-soft)")


class ApplyProposalRequest(BaseModel):
    workflow_id: str = Field(..., min_length=1, max_length=36)
    patch: dict = Field(..., description="Execution-policy patch (retries/backoff/timeout keys only)")


@router.get("/devices")
async def devices():
    """v65 device inventory - the honest GPU-execution-mode report.

    What accelerators this environment actually has, what the platform
    default device mode is, and how training nodes resolve device intent.
    py8n never claims GPU compute it does not perform.
    """
    return device_mode_report()


@router.get("/overview")
async def ops_center_overview(
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Whole-environment health: workflows, datasets, reports, agents, incidents."""
    user_id = getattr(user, "id", None)
    return await ops_overview(db, user_id)


@router.get("/incidents/{execution_id}")
async def ops_incident_drilldown(
    execution_id: str,
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """The drilldown chain: workflow -> execution -> node -> input -> error ->
    previous success -> related datasets -> impact."""
    user_id = getattr(user, "id", None)
    chain = await incident_chain(db, user_id, execution_id)
    if not chain:
        raise HTTPException(status_code=404, detail="Execution not found")
    return chain


@router.post("/ai/investigate")
async def ops_ai_investigate(
    body: InvestigateRequest,
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """The 7-step AI investigation: deterministic findings + cause +
    recommendation + a structured proposal. ``narrate`` adds optional
    LLM narration through the sandbox bridge (fail-soft)."""
    user_id = getattr(user, "id", None)
    findings = await investigate(db, user_id, body.execution_id, narrate=body.narrate)
    if not findings:
        raise HTTPException(status_code=404, detail="Execution not found")
    return findings


@router.post("/ai/apply-proposal")
async def ops_ai_apply_proposal(
    body: ApplyProposalRequest,
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Apply an AI-proposed workflow policy patch - the 'user executes' half
    of 'AI proposes; py8n/user executes'. The patch is validated with the
    same rules as the settings editor and lands as a new workflow version."""
    wf = await db.get(Workflow, body.workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    own_or_404(wf.owner_id, user)
    try:
        normalized = validate_execution_policy(body.patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not normalized:
        raise HTTPException(status_code=400, detail="patch is empty - nothing to apply")
    wf.policy_json = normalized
    await db.flush()
    await db.refresh(wf)
    snap = await snapshot_workflow_version(db, wf)
    await db.commit()  # avoid teardown-commit race
    await resync_workflow_jobs(wf.id)
    return {
        "workflow_id": wf.id,
        "policy": wf.policy_json,
        "version": snap.version,
        "note": "policy applied and snapshotted as a new workflow version",
    }
