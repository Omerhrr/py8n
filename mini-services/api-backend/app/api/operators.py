"""Marketplace operators API (v83) - "Install a business operator".

The shelf above solutions: GET /operators lists the curated business
operators with the exact topology each install builds, GET
/operators/{slug} shows the install plan, POST /operators/{slug}/install
composes the whole business (datasets, event-reactive workflows, agent,
rooms, queues, campaign, staff dashboard) into a RUNNING Py8nSystem.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..services.operators import OperatorError, operator_catalog, operator_detail
from ..services.operators import install_operator
from .auth import get_optional_user
from ..db import get_db

router = APIRouter(prefix="/operators", tags=["operators"])


class OperatorInstallRequest(BaseModel):
    """Install options - the credentials are the installer's to bind."""

    brain: str = "scaffold"
    llm_credential_id: str | None = None
    note: str = ""


@router.get("")
async def list_operators():
    """The curated operator shelf - businesses, not workflow templates."""
    return operator_catalog()


@router.get("/{slug}")
async def operator_ship(slug: str):
    """The install plan for one operator - everything it will build."""
    try:
        return operator_detail(slug)
    except OperatorError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{slug}/install")
async def install(slug: str, body: OperatorInstallRequest | None = None,
                  user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Install the operator: compose the business, bind it RUNNING."""
    from ..services.operators import OPERATORS_BY_SLUG

    if slug not in OPERATORS_BY_SLUG:
        raise HTTPException(status_code=404, detail=f"unknown operator {slug!r}")
    owner = user.id if user else None
    b = body or OperatorInstallRequest()
    try:
        built = await install_operator(
            db, slug, owner_id=owner,
            llm_credential_id=(b.llm_credential_id or "").strip() or None,
            brain=b.brain, note=b.note or "")
    except OperatorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    return built
