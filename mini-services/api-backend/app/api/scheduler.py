"""Scheduler doors (v85) - the platform's own maintenance sweeps, on
demand.

The APScheduler loop runs the escalation sweep on its interval; this
door runs the SAME sweep now - deterministic for tests, live smokes and
the operator who just fixed a process and wants to see the door move.
Idempotent by construction: one escalation per state stint.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..services.business_processes import (dispatch_due_chain_reports,
                                           escalate_stuck)
from ..services import system_deployment as deploy_svc
from .auth import get_optional_user

router = APIRouter(prefix="/scheduler", tags=["scheduler"])


@router.post("/escalations/tick")
async def tick_escalations(user=Depends(get_optional_user),
                           db: AsyncSession = Depends(get_db)):
    """Sweep open business-process instances past their SLA and escalate
    each once per state stint: through the machine's own ``escalate``
    move when it defines one, otherwise as a no-move escalation on the
    record. Emits business.stuck either way. Processes bound to a
    paused/stopped system are held honestly.

    v99: after the escalation walk, every due chain-report schedule
    dispatches - the digest pattern applied to the chain-history FILE
    (one summary per window, the CSV attached, every outcome honest).
    The response carries the dispatch records under ``chain_report``.

    v104: the walk also feeds the health dot - every LIVE deployment
    whose latest liveness probe is older than
    PY8N_DEPLOY_PING_INTERVAL_SECONDS gets re-probed (stamped on the
    record; only lost/recovered TRANSITIONS land in the ops log). The
    response carries the sweep's records under ``deploy_ping``."""
    caller = getattr(user, "id", None)
    out = await escalate_stuck(db, caller, actor=caller or "scheduler")
    out["chain_report"] = await dispatch_due_chain_reports(db, caller)
    out["deploy_ping"] = await deploy_svc.ping_due_deployments(
        db, actor=caller or "scheduler")
    await db.commit()
    return out
