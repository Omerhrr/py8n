"""AI System Builder API (v59) - the Describe -> Clarify -> Design -> Build loop.

* ``POST   /builder/systems``                      - describe what you want; get a SystemSpec
* ``GET    /builder/systems``                      - the builder drafts list
* ``GET    /builder/systems/{id}``                 - spec + transcript + built refs
* ``POST   /builder/systems/{id}/answers``         - fold interview answers into the spec
* ``POST   /builder/systems/{id}/components``      - tick/untick a component (dependency-safe)
* ``POST   /builder/systems/{id}/build``           - translate the spec into REAL primitives
* ``DELETE /builder/systems/{id}``                 - drop a draft (built objects stay)
"""

from __future__ import annotations

import copy

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user, own_or_404
from ..db import get_db
from ..models import SystemDraft
from ..services.scheduler import resync_report_jobs, resync_workflow_jobs
from ..services.system_builder import (
    apply_answers,
    build_system,
    enhance_spec_with_llm,
    synthesize_spec,
    toggle_component,
)

router = APIRouter(prefix="/builder/systems", tags=["builder"])


class SystemCreate(BaseModel):
    description: str = Field(..., min_length=8, max_length=4000)
    use_llm: bool = Field(default=False, description="Refine the spec with the sandbox-bridge LLM (fail-soft)")


class AnswersIn(BaseModel):
    answers: dict[str, str] = Field(..., min_length=1, max_length=20)


class ComponentToggle(BaseModel):
    component_id: str = Field(..., min_length=1, max_length=60)
    selected: bool


def _out(draft: SystemDraft) -> dict:
    return {
        "id": draft.id,
        "name": draft.name,
        "description": draft.description,
        "persona": draft.persona,
        "status": draft.status,
        "spec": draft.spec_json,
        "messages": draft.messages_json,
        "built": draft.built_json,
        "created_at": draft.created_at.isoformat() if draft.created_at else None,
        "updated_at": draft.updated_at.isoformat() if draft.updated_at else None,
    }


async def _get_draft(db: AsyncSession, draft_id: str, user) -> SystemDraft:
    draft = await db.get(SystemDraft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="System draft not found")
    own_or_404(draft.owner_id, user)
    return draft


@router.post("", status_code=201)
async def create_system_draft(body: SystemCreate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Describe -> Discover: synthesize the SystemSpec from plain language."""
    try:
        spec = synthesize_spec(body.description)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if body.use_llm:
        spec = await enhance_spec_with_llm(spec, body.description)
    draft = SystemDraft(
        name=spec.get("title") or "System",
        description=body.description,
        persona=spec.get("persona") or "business",
        spec_json=spec,
        messages_json=[{"role": "user", "text": body.description, "ts": spec.pop("_ts", None)}],
    )
    draft.owner_id = user.id if user else None
    db.add(draft)
    await db.commit()
    await db.refresh(draft)
    return _out(draft)


@router.get("")
async def list_system_drafts(user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    q = select(SystemDraft).order_by(SystemDraft.updated_at.desc()).limit(50)
    if user:
        q = q.where(SystemDraft.owner_id.in_([user.id, None]))
    rows = (await db.execute(q)).scalars().all()
    return [
        {
            "id": d.id, "name": d.name, "persona": d.persona, "status": d.status,
            "description": d.description[:200],
            "selected": sum(1 for c in (d.spec_json or {}).get("components", []) if c.get("selected")),
            "built_refs": _built_summary(d.built_json),
            "updated_at": d.updated_at.isoformat() if d.updated_at else None,
        }
        for d in rows
    ]


def _built_summary(built: dict | None) -> dict:
    if not built:
        return {}
    return {
        "workflow_id": built.get("workflow_id"),
        "dataset_id": built.get("dataset_id"),
        "dashboard_id": built.get("dashboard_id"),
        "report_id": built.get("report_id"),
    }


@router.get("/{draft_id}")
async def get_system_draft(draft_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    draft = await _get_draft(db, draft_id, user)
    return _out(draft)


@router.post("/{draft_id}/answers")
async def answer_questions(draft_id: str, body: AnswersIn, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Clarify: fold interview answers back into the SystemSpec."""
    draft = await _get_draft(db, draft_id, user)
    if draft.status == "built":
        raise HTTPException(status_code=400, detail="this system is already built - edit the built objects directly")
    # deep copy - same reason as the toggle endpoint (shared nested state would
    # make old and new compare equal, so the UPDATE would never fire)
    spec = apply_answers(copy.deepcopy(draft.spec_json or {}), body.answers)
    messages = list(draft.messages_json or [])
    messages.append({"role": "user", "kind": "answers", "answers": body.answers, "ts": _ts()})
    draft.spec_json = spec
    draft.messages_json = messages
    draft.name = spec.get("title") or draft.name
    await db.commit()
    await db.refresh(draft)
    return _out(draft)


@router.post("/{draft_id}/components")
async def toggle_system_component(draft_id: str, body: ComponentToggle, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Design: tick/untick a component (dependency-validated)."""
    draft = await _get_draft(db, draft_id, user)
    if draft.status == "built":
        raise HTTPException(status_code=400, detail="this system is already built - edit the built objects directly")
    try:
        # deep copy: the toggle mutates nested component dicts in place - with a
        # shared structure the re-assigned spec compares EQUAL to the old value
        # and SQLAlchemy would skip the UPDATE entirely
        spec = toggle_component(copy.deepcopy(draft.spec_json or {}), body.component_id, body.selected)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    messages = list(draft.messages_json or [])
    messages.append({"role": "user", "kind": "toggle", "component": body.component_id,
                     "selected": body.selected, "ts": _ts()})
    draft.spec_json = spec
    draft.messages_json = messages
    import sys as _sys
    print("DBG dirty:", [(type(a).__name__, id(a), (getattr(a, "built_json", {}) or {}).get("system_id")) for a in db.dirty], "| draft id:", id(draft), file=_sys.stderr)
    await db.commit()
    from sqlalchemy import text as _text
    _row = (await db.execute(_text("SELECT built_json FROM system_drafts WHERE id = :i"), {"i": draft.id})).first()
    import json as _j; _b = _j.loads(_row[0]) if isinstance(_row[0], str) else _row[0]
    print("DBG raw DB after commit system_id:", (_b or {}).get("system_id"), file=_sys.stderr)
    await db.refresh(draft)
    print("DBG after refresh system_id:", (draft.built_json or {}).get("system_id"), file=_sys.stderr)
    return _out(draft)


class BuildRequest(BaseModel):
    as_system: bool = Field(default=False, description="v61: also create a Py8n System binding everything the build created")


@router.post("/{draft_id}/build")
async def build_system_draft(draft_id: str, body: BuildRequest | None = None, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Build: translate the SELECTED components into real primitives."""
    draft = await _get_draft(db, draft_id, user)
    if draft.status == "built":
        raise HTTPException(status_code=400, detail="this system is already built")
    built = await build_system(db, draft)
    draft.status = "built"
    messages = list(draft.messages_json or [])
    messages.append({"role": "system", "kind": "built", "refs": _built_summary(built), "ts": _ts()})

    # v61 bridge: optionally bind everything the build created into a System.
    # NOTE: build a NEW dict here and never mutate the one that gets assigned
    # to built_json - the ORM keeps a REFERENCE to the value it flushed as its
    # committed-state snapshot, so in-place mutations make a later assignment
    # compare EQUAL and the UPDATE silently disappears.
    if body and body.as_system:
        from uuid import uuid4 as _uuid4

        from ..models import Py8nSystem, SystemComponent

        sys_row = Py8nSystem(
            id=str(_uuid4()),  # explicit: column defaults apply only at flush
            name=f"{draft.name} system",
            description=f"Built by the AI System Builder from: {(draft.description or '')[:300]}",
            icon="wand-2", color="#ec4899",
        )
        sys_row.owner_id = draft.owner_id
        db.add(sys_row)
        built = {**built, "system_id": sys_row.id}  # sys_row.id is uuid-generated client-side
        for kind, ref in (
            ("workflow", built.get("workflow_id")),
            ("dataset", built.get("dataset_id")),
            ("dashboard", built.get("dashboard_id")),
            ("report", built.get("report_id")),
        ):
            if ref:
                db.add(SystemComponent(system_id=sys_row.id, kind=kind, ref_id=ref))
        messages.append({"role": "system", "kind": "system_created", "ref": sys_row.id, "ts": _ts()})
    draft.built_json = built
    draft.messages_json = messages
    await db.commit()
    await db.refresh(draft)
    # keep APScheduler in sync with anything the build created
    if built.get("workflow_id"):
        try:
            await resync_workflow_jobs(built["workflow_id"])
        except Exception:
            pass
    if built.get("report_id"):
        try:
            await resync_report_jobs(built["report_id"])
        except Exception:
            pass
    return _out(draft)


@router.delete("/{draft_id}", status_code=204)
async def delete_system_draft(draft_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    draft = await _get_draft(db, draft_id, user)
    await db.delete(draft)
    await db.commit()


def _ts() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
