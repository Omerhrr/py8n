"""The PUBLIC system-identity resolver (v102) - the custom-domain door.

This router is registered WITHOUT the global auth dependency on
purpose: a login surface must be able to show "you are signing in to
Acme Operations" BEFORE the visitor authenticates. One route:

    GET /systems/by-domain/{domain}

resolving a deployed system's custom hostname back to its identity -
name, icon, color, branding, environment - and 404 unless a deployment
exists AND the status is ``live`` (an offline or paused system does not
present a login surface; honesty at this door beats a pretty error).

When the caller DOES carry a valid token, the answer includes
``my_role`` - "system-specific login" is the platform login resolved
against the domain's system with the member's role in the answer.
Membership itself is v62's ``system_members`` (owner / editor /
viewer); nothing about users is stored here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user
from ..config import settings
from ..db import get_db
from ..models import Py8nSystem, SystemComponent
from ..services import business_processes as bp_svc
from ..services import system_deployment as deploy_svc
from ..services import system_keys as keys_svc
from ..services.system_governance import member_role

router = APIRouter(prefix="/systems/by-domain", tags=["systems"])


@router.get("/{domain}")
async def by_domain(domain: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """The public identity a custom domain resolves to (live deployments
    only). With a token, the caller's role on the system rides along."""
    try:
        row = await deploy_svc.get_by_domain(db, domain)
    except deploy_svc.DeploymentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="No system answers on this domain")
    system = await db.get(Py8nSystem, row.system_id)
    if system is None:
        raise HTTPException(status_code=404, detail="No system answers on this domain")
    identity = deploy_svc.public_identity(row, system)
    if identity is None:
        raise HTTPException(
            status_code=404,
            detail="this system's deployment is not live - the identity surface is dark")
    identity["my_role"] = await member_role(db, system, user)
    return identity


@router.get("/{domain}/work")
async def domain_work(domain: str, request: Request,
                      user=Depends(get_optional_user),
                      db: AsyncSession = Depends(get_db)):
    """v105: the system's own WORK SURFACE - the front door stops being a
    hallway and becomes the workplace. The same live deployment the
    identity door resolves, now carrying the pending work on the
    machines the system binds: every machine with its live operation
    (open / stuck) and the attention rows (open instances past their
    SLA, most-overdue first, the escalation book per row).

    The authority is the system's own:
    * the system's key (``py8n_sys_``) reads its own system's work - a
      key on a foreign system looks nonexistent (404), never a
      fall-through to the anonymous owner;
    * the system's people (any v62 member role) read it with their token;
    * the enforced-mode anonymous caller is told to sign in (401); a
      signed-in stranger is honestly refused (403); auth-off's anonymous
      single-operator caller is the owner (the platform convention).
    """
    try:
        row = await deploy_svc.get_by_domain(db, domain)
    except deploy_svc.DeploymentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="No system answers on this domain")
    system = await db.get(Py8nSystem, row.system_id)
    if system is None:
        raise HTTPException(status_code=404, detail="No system answers on this domain")
    identity = deploy_svc.public_identity(row, system)
    if identity is None:
        raise HTTPException(
            status_code=404,
            detail="this system's deployment is not live - the work surface is dark")
    key_info = getattr(request.state, "py8n_system_key", None)
    if key_info is not None:
        if key_info.get("system_id") != row.system_id:
            raise HTTPException(status_code=404,
                                detail="No system answers on this domain")
        my_role = keys_svc.role_for(key_info.get("scopes"))
    else:
        if user is None and settings.require_auth:
            raise HTTPException(status_code=401,
                                detail="sign in to see this system's work")
        my_role = await member_role(db, system, user)
        if my_role is None:
            raise HTTPException(status_code=403,
                                detail="you are not a member of this system")
    machine_ids = (await db.execute(
        select(SystemComponent.ref_id).where(
            SystemComponent.system_id == row.system_id,
            SystemComponent.kind == "process"))).scalars().all()
    work = await bp_svc.system_work_surface(db, list(machine_ids))
    # v106: the deployment's liveness rides the surface - the people on
    # this address can see their own door is being watched (the last
    # probe's evidence, or an honest null when it has never been asked).
    return {**identity, "my_role": my_role, "work": work,
            "liveness": deploy_svc.liveness(row)}
