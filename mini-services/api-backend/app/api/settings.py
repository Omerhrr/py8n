"""Platform settings API (v19) — execution data retention policies."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..services import retention

router = APIRouter(prefix="/settings", tags=["settings"])


class RetentionPolicyIn(BaseModel):
    retention_days: int | None = Field(default=None, ge=0, le=3650, description="0 = keep forever")
    max_executions_per_workflow: int | None = Field(default=None, ge=0, le=100000, description="0 = unlimited")


@router.get("/retention")
async def get_retention_policy():
    return await retention.get_policy()


@router.put("/retention")
async def update_retention_policy(body: RetentionPolicyIn):
    try:
        return await retention.set_policy(body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/retention/purge")
async def purge_now():
    """Apply the policy immediately (also runs daily in the background)."""
    return await retention.purge_execution_data()
