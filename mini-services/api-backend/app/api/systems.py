"""Py8n Systems API (v61 + v62 governance + v81 runtime) - the operating unit above workflows.

* ``POST   /systems``                        - create a system
* ``GET    /systems``                        - cards with component counts + verdict
* ``GET    /systems/templates``              - role-specific starter kits (v62)
* ``POST   /systems/templates/{slug}/instantiate`` - create system from a kit
* ``GET    /systems/dependencies``           - cross-system dependency graph (v62)
* ``GET    /systems/{id}``                   - detail: grouped components + health
* ``PUT    /systems/{id}``                   - rename / redescribe / restyle
* ``POST   /systems/{id}/components``        - bind workflow|dataset|app|dashboard|model|report|model_system|voice_agent|queue|meeting
* ``DELETE /systems/{id}/components/{cid}``  - unbind
* ``DELETE /systems/{id}``                   - dissolve (member objects are untouched)
* ``GET/POST /systems/{id}/members``         - v62 role management
* ``PUT/DELETE /systems/{id}/members/{uid}`` - v62 change / remove a member

v81 SYSTEM RUNTIME - the operating environment verbs:

* ``POST /systems/{id}/start|stop|pause|resume`` - the lifecycle. The gate
  holds the system's workflows on every reactive path (event triggers,
  schedule ticks, webhooks) without touching their own ``is_active``;
  ``start`` may carry ``activate_workflows: true`` to turn pack-installed
  inactive workflows on, loudly and on the record.
* ``GET /systems/{id}/state``      - the runtime snapshot (gate, live interactions)
* ``GET /systems/{id}/metrics``    - derived counters over a window
* ``GET /systems/{id}/operations`` - the durable operations log
* ``GET /systems/{id}/events``     - the system's event thread + component events
* ``POST /systems/{id}/upgrade``   - re-apply the source solution's pack

Every attach is resolved against the live table with owner scoping, so a
system can never reference a foreign or nonexistent object. The health
verdict is derived from the members at read time (nothing stored); the
operations log is the deliberate exception - it IS traffic state.

v62 ROLES: the creator is the single owner (``owner_id``); invited members
hold ``editor`` (bind/unbind/edit/operate) or ``viewer`` (read-only) roles. A
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
    health_overview,
    resolve_component,
    system_health,
    system_summary,
)
from ..services import system_deployment as deploy_svc
from ..services.solutions import finalize_pack_dataset_names
from ..services import system_runtime
from ..services.operators import chains_for_system  # v93: the chains LIVE on the installed system
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


class LifecycleAction(BaseModel):
    """Body for start/stop/pause/resume. ``activate_workflows`` only means
    something on start: turn every bound workflow's is_active ON (the loud,
    on-the-record door for pack installs that land inactive)."""
    activate_workflows: bool = Field(default=False)


class DeploymentPut(BaseModel):
    """v102: create-or-patch the system's deployment identity. An empty
    ``domain`` string CLEARS the custom domain (a real move); omit the
    field to leave it untouched. Branding only patches the keys present."""
    domain: str | None = Field(default=None, max_length=253,
                               description="custom hostname; empty string clears it")
    environment: str | None = Field(default=None, description="staging | production")
    branding: dict | None = Field(default=None, description="accent | tagline | login_headline | logo")


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


# ------------------------------------------------------------------ v102
# the estate health overview - "is my business actually healthy?"
@router.get("/health/overview")
async def estate_health(user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """One row per visible system: the status dot (running / attention /
    hold), the 7d success rate, overdue instances, failed workflows and
    open escalations - composed from what the platform already keeps,
    nothing stored. The deployment identity rides each row."""
    return await health_overview(db, user)


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
        # v93: the cross-department chains this system's machines sit in,
        # drawn with the LIVE instance counts per node and per leg
        "chains": await chains_for_system(db, s),
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
    await db.flush()
    # v81: the runtime sees the membership change - on the record, on the wire
    op = await system_runtime.record_operation(
        db, s, "component_added", getattr(user, "id", None) or "system",
        {"kind": body.kind, "ref_id": body.ref_id, "component_id": comp.id})
    await system_runtime._emit_system_event(
        db, s, "system.component_added",
        {"operation_id": op.id, "kind": body.kind, "ref_id": body.ref_id})
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
    detail = {"kind": comp.kind, "ref_id": comp.ref_id, "component_id": comp.id}
    await db.delete(comp)
    op = await system_runtime.record_operation(
        db, s, "component_removed", getattr(user, "id", None) or "system", detail)
    await system_runtime._emit_system_event(
        db, s, "system.component_removed", {"operation_id": op.id, **detail})
    await db.commit()


@router.delete("/{system_id}", status_code=204)
async def delete_system(system_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    s, _role = await _get_system(db, system_id, user, min_role="owner")
    await db.delete(s)  # components cascade; member objects are untouched
    await db.commit()


# ------------------------------------------------------------------ v81
# the system runtime - lifecycle verbs, runtime reads


async def _apply_verb(system_id: str, verb: str, body: LifecycleAction | None,
                      user, db: AsyncSession) -> dict:
    s, _role = await _get_system(db, system_id, user, min_role="editor")
    try:
        result = await system_runtime.apply_lifecycle(
            db, s, verb, actor=getattr(user, "id", None) or "system",
            activate_workflows=bool(body.activate_workflows) if body else False)
    except system_runtime.SystemRuntimeError as exc:
        msg = str(exc)
        raise HTTPException(status_code=409 if "cannot" in msg or "corrupted" in msg else 400,
                            detail=msg) from exc
    await db.commit()
    fresh = await _reload_summary(db, s.id)
    return {**result, "lifecycle": fresh["lifecycle"]}


@router.post("/{system_id}/start")
async def start_system(system_id: str, body: LifecycleAction | None = None,
                       user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Open the gate: the system's workflows react again (events, schedules,
    webhooks). activate_workflows=true also flips their is_active ON."""
    return await _apply_verb(system_id, "start", body, user, db)


@router.post("/{system_id}/stop")
async def stop_system(system_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Close the gate: every reactive path holds. is_active flags are
    untouched; live interactions are not killed - they finish honestly."""
    return await _apply_verb(system_id, "stop", None, user, db)


@router.post("/{system_id}/pause")
async def pause_system(system_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """A temporary hold - same gate as stop, but resume (not start) reopens."""
    return await _apply_verb(system_id, "pause", None, user, db)


@router.post("/{system_id}/resume")
async def resume_system(system_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Reopen a paused system."""
    return await _apply_verb(system_id, "resume", None, user, db)


@router.get("/{system_id}/state")
async def state(system_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """The runtime snapshot: the gate, the workflows it holds, live
    interactions, last activity. Derived at read time."""
    s, my_role = await _get_system(db, system_id, user)
    return {**(await system_runtime.system_state(db, s)), "my_role": my_role}


@router.get("/{system_id}/metrics")
async def metrics(system_id: str, hours: int = 24,
                  user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Derived counters over a window (executions by status, lifecycle
    events, operations)."""
    s, _role = await _get_system(db, system_id, user)
    return await system_runtime.system_metrics(db, s, hours=hours)


@router.get("/{system_id}/operations")
async def operations(system_id: str, limit: int = 50,
                     user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """The durable operations log - every verb the runtime accepted."""
    s, _role = await _get_system(db, system_id, user)
    return {"operations": await system_runtime.list_operations(db, s, limit=limit)}


@router.get("/{system_id}/events")
async def system_events_view(system_id: str, limit: int = 100,
                             user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """The system's event view: lifecycle events on the system's correlation
    thread + component events whose target is a bound object."""
    s, _role = await _get_system(db, system_id, user)
    return {"events": await system_runtime.system_scoped_events(db, s, limit=limit)}


@router.post("/{system_id}/upgrade")
async def upgrade(system_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Re-apply the source solution's pack and reconcile components. New
    workflows/datasets bind; same-name objects are reported and left
    untouched - a running system is never rewritten under itself."""
    s, _role = await _get_system(db, system_id, user, min_role="editor")
    try:
        result = await system_runtime.upgrade_from_solution(
            db, s, actor=getattr(user, "id", None) or "system")
    except system_runtime.SystemRuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    return result


# ------------------------------------------------------------------ v102
# the deployed-system identity - the commercial bridge

@router.get("/{system_id}/deployment")
async def get_deployment(system_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """The system's deployment identity (domain, environment, status,
    branding) - null when the system was never deployed."""
    s, _role = await _get_system(db, system_id, user)
    row = await deploy_svc.get_deployment(db, s.id)
    return {"deployment": deploy_svc.deployment_out(row)}


@router.put("/{system_id}/deployment")
async def put_deployment(system_id: str, body: DeploymentPut,
                         user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Create-or-patch the deployment record: the custom domain (empty
    string clears it), the environment, the branding. Every change lands
    in the operations log."""
    s, _role = await _get_system(db, system_id, user, min_role="editor")
    try:
        row = await deploy_svc.upsert_deployment(
            db, s, domain=body.domain, environment=body.environment,
            branding=body.branding, actor=getattr(user, "id", None) or "system")
    except deploy_svc.DeploymentError as exc:
        status = 409 if "already answers" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    await db.commit()
    await db.refresh(row)
    return {"deployment": deploy_svc.deployment_out(row)}


@router.post("/{system_id}/deployment/{verb}")
async def deployment_verb(system_id: str, verb: str,
                          user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Move the deployment status - deploy (offline/paused -> live),
    pause (live -> paused), retire (-> offline). Loud verbs only: every
    move writes the operations log and emits on the system's thread."""
    s, _role = await _get_system(db, system_id, user, min_role="editor")
    try:
        row = await deploy_svc.apply_verb(db, s, verb, actor=getattr(user, "id", None) or "system")
    except deploy_svc.DeploymentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    await db.refresh(row)
    return {"deployment": deploy_svc.deployment_out(row)}


# v102: the update lifecycle - preview -> apply -> see changes -> accept/rollback

@router.get("/{system_id}/update/preview")
async def update_preview(system_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """"What's going to change if I upgrade this system?" - the shared
    reconcile plan read-only, plus any applied-but-unruled changeset
    (pending) the Accept / Rollback verbs rule on."""
    s, _role = await _get_system(db, system_id, user)
    return await system_runtime.preview_update(db, s)


@router.post("/{system_id}/update/accept")
async def update_accept(system_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Accept a pending upgrade - the changes become settled history."""
    s, _role = await _get_system(db, system_id, user, min_role="editor")
    try:
        result = await system_runtime.accept_update(db, s, actor=getattr(user, "id", None) or "system")
    except system_runtime.SystemRuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return result


@router.post("/{system_id}/update/rollback")
async def update_rollback(system_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Roll a pending upgrade back - unbind exactly what it bound; the
    imported objects stay in the estate, unbound."""
    s, _role = await _get_system(db, system_id, user, min_role="editor")
    try:
        result = await system_runtime.rollback_update(db, s, actor=getattr(user, "id", None) or "system")
    except system_runtime.SystemRuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return result


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
