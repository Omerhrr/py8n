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

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user
from ..db import get_db
from ..models import Py8nSystem
from ..services import system_deployment as deploy_svc
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
