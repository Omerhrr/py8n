"""Py8n Systems API (v61 + v62 governance) - the operating unit above workflows.

* ``POST   /systems``                        - create a system
* ``GET    /systems``                        - cards with component counts + verdict
* ``GET    /systems/templates``              - role-specific starter kits (v62)
* ``POST   /systems/templates/{slug}/instantiate`` - create system from a kit
* ``GET    /systems/dependencies``           - cross-system dependency graph (v62)
* ``GET    /systems/{id}``                   - detail: grouped components + health
* ``PUT    /systems/{id}``                   - rename / redescribe / restyle
* ``POST   /systems/{id}/components``        - bind workflow|dataset|app|dashboard|model|report|model_system
* ``DELETE /systems/{id}/components/{cid}``  - unbind
* ``DELETE /systems/{id}``                   - dissolve (member objects are untouched)
* ``GET/POST /systems/{id}/members``         - v62 role management
* ``PUT/DELETE /systems/{id}/members/{uid}`` - v62 change / remove a member

Every attach is resolved against the live table with owner scoping, so a
system can never reference a foreign or nonexistent object. The health
verdict is derived from the members at read time (nothing stored).

v62 ROLES: the creator is the single owner (``owner_id``); invited members
hold ``editor`` (bind/unbind/edit) or ``viewer`` (read-only) roles. A
system you are not part of looks nonexistent (404); an action above your
role is 403. Auth-off installs (user None) keep full control.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..api.packs import PackDocument, _import_pack_doc
from ..auth import get_optional_user
from ..db import get_db
from ..models import Py8nSystem, SystemComponent, Workflow
from ..services.py8n_systems import (
    COMPONENT_KINDS,
    KIND_TABLES,
    architecture_layers,
    resolve_component,
    system_health,
    system_summary,
)
from ..services.solutions import finalize_pack_dataset_names
from ..services.system_governance import (
    RoleDenied,
    get_template,
    instantiate_template,
    invite_member,
    member_list,
    member_role,
    remove_member,
    require_role,
    set_member_role,
    template_summaries,
    TEMPLATE_ROLES,
)
from ..services import system_dependencies as deps_svc

router = APIRouter(prefix="/systems", tags=["systems"])


class SystemCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=140)
    description: str = Field(default="", max_length=2000)
    icon: str = Field(default="boxes", max_length=60)
    color: str = Field(default="#f97316", max_length=20)


class SystemUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=140)
    description: str | None = Field(default=None, max_length=2000)
    icon: str | None = Field(default=None, max_length=60)
    color: str | None = Field(default=None, max_length=20)


class ComponentAttach(BaseModel):
    kind: str = Field(..., description=f"one of: {', '.join(COMPONENT_KINDS)}")
    ref_id: str = Field(..., min_length=1, max_length=36)


class MemberInvite(BaseModel):
    email: str = Field(..., max_length=200)
    role: str = Field("viewer", description="editor | viewer")


class MemberRoleChange(BaseModel):
    role: str = Field(..., description="editor | viewer")


async def _get_system(db: AsyncSession, system_id: str, user, min_role: str = "viewer") -> tuple[Py8nSystem, str]:
    """Load + role-check. Eager-load components (async lazy loads are a
    MissingGreenlet trap). Returns (system, my_role)."""
    row = (
        await db.execute(
            select(Py8nSystem).options(selectinload(Py8nSystem.components)).where(Py8nSystem.id == system_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="System not found")
    try:
        role = await require_role(db, row, user, min_role)
    except RoleDenied as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    return row, role


async def _reload_summary(db: AsyncSession, system_id: str) -> dict:
    """Post-commit summary fetch: re-select with components eagerly loaded."""
    row = (
        await db.execute(
            select(Py8nSystem).options(selectinload(Py8nSystem.components)).where(Py8nSystem.id == system_id)
        )
    ).scalar_one()
    return system_summary(row)


@router.post("", status_code=201)
async def create_system(body: SystemCreate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = Py8nSystem(name=body.name.strip(), description=body.description.strip(),
                     icon=body.icon, color=body.color)
    row.owner_id = user.id if user else None
    db.add(row)
    await db.commit()
    summary = await _reload_summary(db, row.id)
    summary["my_role"] = "owner"
    return summary


# ------------------------------------------------------------------ v62
# templates + dependencies: registered BEFORE /{system_id} so the static
# segments always win the route match
@router.get("/templates")
async def list_templates(role: str = "", user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    if role and role not in TEMPLATE_ROLES:
        raise HTTPException(status_code=400, detail=f"unknown role {role!r} (allowed: {', '.join(TEMPLATE_ROLES)})")
    return {"templates": template_summaries(role), "roles": list(TEMPLATE_ROLES)}


@router.post("/templates/{slug}/instantiate", status_code=201)
async def instantiate(slug: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Create a system from a role template: pack import (same machinery as
    marketplace installs - workflows land INACTIVE, datasets with sample
    rows) + everything bound + the role's dashboard/report."""
    try:
        template = get_template(slug)
    except LookupError:
        raise HTTPException(status_code=404, detail=f"No template {slug!r}") from None
    owner = user.id if user else None
    pack_dict = await finalize_pack_dataset_names(db, template["pack"])
    pack = PackDocument.model_validate(pack_dict)
    result = await _import_pack_doc(pack, owner, db)
    built = await instantiate_template(db, template, owner, result)
    await db.commit()
    summary = await _reload_summary(db, built["system_id"])
    health = await system_health(db, await _fresh_system(db, built["system_id"]))
    return {
        **summary,
        "my_role": "owner",
        "verdict": health["verdict"],
        "created": built["created"],
        "warnings": result.get("warnings", []),
    }


async def _fresh_system(db: AsyncSession, system_id: str) -> Py8nSystem:
    return (
        await db.execute(
            select(Py8nSystem).options(selectinload(Py8nSystem.components)).where(Py8nSystem.id == system_id)
        )
    ).scalar_one()


@router.get("/dependencies")
async def dependencies(system_id: str = "", user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Cross-system dependency graph - shared objects, data flows and
    model flows between the systems you can read. Derived, never stored."""
    graph = await deps_svc.dependency_graph(db, user, system_id=system_id or None)
    return graph


# ------------------------------------------------------------------ core
@router.get("")
async def list_systems(user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    q = (
        select(Py8nSystem)
        .options(selectinload(Py8nSystem.components))
        .order_by(Py8nSystem.updated_at.desc())
        .limit(100)
    )
    rows = (await db.execute(q)).scalars().unique().all()
    out = []
    for s in rows:
        my_role = await member_role(db, s, user)
        if my_role is None:
            continue  # not visible: foreign system + no membership
        summary = system_summary(s)
        summary["verdict"] = (await system_health(db, s))["verdict"]
        summary["my_role"] = my_role
        out.append(summary)
    return out


@router.get("/{system_id}")
async def system_detail(system_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    s, my_role = await _get_system(db, system_id, user)
    grouped: dict[str, list] = {k: [] for k in COMPONENT_KINDS}
    for c in s.components or []:
        model = KIND_TABLES.get(c.kind)
        name = None
        if model is not None:
            row = await db.get(model, c.ref_id)
            name = getattr(row, "name", None) if row is not None else None
        grouped[c.kind].append({
            "component_id": c.id,
            "kind": c.kind,
            "ref_id": c.ref_id,
            "name": name or c.ref_id[:8],
            "added_at": c.added_at.isoformat() if c.added_at else None,
        })
    # v67: derived medallion layers over the bound workflows (staging /
    # curated / dead_letter, classified from dataset_write node targets)
    wf_rows: dict[str, Workflow] = {}
    for c in s.components or []:
        if c.kind == "workflow" and c.ref_id not in wf_rows:
            wf = await db.get(Workflow, c.ref_id)
            if wf is not None:
                wf_rows[c.ref_id] = wf
    return {
        **system_summary(s),
        "my_role": my_role,
        "grouped": grouped,
        "architecture": architecture_layers(s, wf_rows),
        "health": await system_health(db, s),
    }


@router.put("/{system_id}")
async def update_system(system_id: str, body: SystemUpdate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    s, _role = await _get_system(db, system_id, user, min_role="editor")
    if body.name is not None:
        s.name = body.name.strip()
    if body.description is not None:
        s.description = body.description.strip()
    if body.icon is not None:
        s.icon = body.icon
    if body.color is not None:
        s.color = body.color
    await db.commit()
    return await _reload_summary(db, s.id)


@router.post("/{system_id}/components", status_code=201)
async def attach_component(system_id: str, body: ComponentAttach, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    s, _role = await _get_system(db, system_id, user, min_role="editor")
    # Members assemble the OWNER's estate: the ref resolves against the
    # system owner's ownership boundary (never the caller's), so an editor
    # can bind the owner's dataset but a stranger's object still 404s.
    scope_owner = s.owner_id or (user.id if user else None)
    try:
        await resolve_component(db, body.kind, body.ref_id, scope_owner)
    except ValueError as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc) else 400, detail=str(exc)) from exc
    dup = (
        await db.execute(
            select(SystemComponent).where(
                SystemComponent.system_id == s.id,
                SystemComponent.kind == body.kind,
                SystemComponent.ref_id == body.ref_id,
            )
        )
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(status_code=409, detail="that object is already bound to this system")
    comp = SystemComponent(system_id=s.id, kind=body.kind, ref_id=body.ref_id)
    db.add(comp)
    await db.commit()
    await db.refresh(comp)
    fresh = await _reload_summary(db, s.id)
    return {"component_id": comp.id, "kind": comp.kind, "ref_id": comp.ref_id,
            "system_total": fresh["total_components"]}


@router.delete("/{system_id}/components/{component_id}", status_code=204)
async def detach_component(system_id: str, component_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    s, _role = await _get_system(db, system_id, user, min_role="editor")
    comp = await db.get(SystemComponent, component_id)
    if comp is None or comp.system_id != s.id:
        raise HTTPException(status_code=404, detail="Component not found")
    await db.delete(comp)
    await db.commit()


@router.delete("/{system_id}", status_code=204)
async def delete_system(system_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    s, _role = await _get_system(db, system_id, user, min_role="owner")
    await db.delete(s)  # components cascade; member objects are untouched
    await db.commit()


# ------------------------------------------------------------------ v62
# members
@router.get("/{system_id}/members")
async def list_members(system_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    s, _role = await _get_system(db, system_id, user, min_role="viewer")
    return {"members": await member_list(db, s), "my_role": _role}


@router.post("/{system_id}/members", status_code=201)
async def add_member(system_id: str, body: MemberInvite, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    s, _role = await _get_system(db, system_id, user, min_role="owner")
    try:
        member = await invite_member(db, s, body.email, body.role)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        status = 409 if "already" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    await db.commit()
    return {"user_id": member.user_id, "role": member.role, "system_total": len(await member_list(db, s))}


@router.put("/{system_id}/members/{user_id}")
async def change_member(system_id: str, user_id: str, body: MemberRoleChange, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    s, _role = await _get_system(db, system_id, user, min_role="owner")
    try:
        await set_member_role(db, s, user_id, body.role)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    return {"user_id": user_id, "role": body.role}


@router.delete("/{system_id}/members/{user_id}", status_code=204)
async def kick_member(system_id: str, user_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    s, _role = await _get_system(db, system_id, user, min_role="owner")
    try:
        await remove_member(db, s, user_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
