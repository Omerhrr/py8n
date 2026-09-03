"""Automation Operations Center API (v57) - the control-plane endpoints.

GET /ops/overview             - whole-environment rollup (SYSTEM verdict)
GET /ops/incidents/{exec_id}  - the full incident drilldown chain
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user
from ..db import get_db
from ..services.ops import incident_chain, ops_overview

router = APIRouter(prefix="/ops", tags=["ops"])


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
