"""The harness doors (v109 runtime, v110 builder) - the system's own
agentic runtime.

GET  /harness/tools                          the toolchest (sensitivity marked)
POST /harness/sessions                       open a session (the SAME brain
                                             providers the node/modules speak)
GET  /harness/sessions                       the estate's harness sessions
GET  /harness/sessions/{id}                  one session + its turn list
PATCH /harness/sessions/{id}                 rename / prompt / active / memory
DELETE /harness/sessions/{id}
POST /harness/sessions/{id}/turns            ONE message -> the loop runs;
                                             completes, exhausts, or pauses
                                             at the approval gate
GET  /harness/sessions/{id}/turns            the transcript
GET  /harness/turns/{id}                     one turn with its full trace
GET  /harness/approvals                      the decision queue (sweep runs)
POST /harness/approvals/{id}/approve         run the move NOW + resume
POST /harness/approvals/{id}/reject          refuse + resume with the refusal
GET  /harness/health                         harness health + guard config
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user, own_or_404
from ..db import get_db
from ..models import HarnessSession, HarnessTurn
from ..services import harness
from ..services.harness import tools as harness_tools
from ..services.harness.service import HarnessError

router = APIRouter(prefix="/harness", tags=["harness"])

_ALLOWED_PROVIDERS = {"sandbox_bridge", "openai_compatible"}
_ALLOWED_MEMORY = {"none", "buffer"}


def _check(provider: str | None, memory: str | None) -> None:
    if provider is not None and provider not in _ALLOWED_PROVIDERS:
        raise HTTPException(status_code=400,
                            detail="provider must be sandbox_bridge or openai_compatible")
    if memory is not None and memory not in _ALLOWED_MEMORY:
        raise HTTPException(status_code=400, detail="memory must be none or buffer")


class SessionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field("", max_length=500)
    system_prompt: str = Field(
        "You are the operations harness. You can read the whole estate and "
        "you may move the business, but every sensitive move waits for a "
        "human decision first. Answer with what the tools returned.",
        max_length=8000)
    provider: str = Field("sandbox_bridge")
    model: str = Field("", max_length=120)
    credential_id: str | None = Field(None, max_length=36)
    temperature: float = Field(0.4, ge=0, le=2)
    memory: str = Field("buffer")
    max_history_turns: int = Field(5, ge=1, le=50)
    is_active: bool = True


class SessionUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    description: str | None = Field(None, max_length=500)
    system_prompt: str | None = Field(None, max_length=8000)
    provider: str | None = None
    model: str | None = Field(None, max_length=120)
    credential_id: str | None = None
    temperature: float | None = Field(None, ge=0, le=2)
    memory: str | None = None
    max_history_turns: int | None = Field(None, ge=1, le=50)
    is_active: bool | None = None


class TurnCreate(BaseModel):
    message: str = Field(..., min_length=1, max_length=20000)


def session_out(s: HarnessSession) -> dict[str, Any]:
    return {
        "id": s.id, "name": s.name, "description": s.description,
        "system_prompt": s.system_prompt, "provider": s.provider,
        "model": s.model, "credential_id": s.credential_id,
        "temperature": s.temperature, "memory": s.memory,
        "max_history_turns": s.max_history_turns,
        "is_active": bool(s.is_active),
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


def turn_out(t: HarnessTurn, *, full: bool = False) -> dict[str, Any]:
    out = {
        "id": t.id, "session_id": t.session_id,
        "user_message": t.user_message, "status": t.status,
        "reply": t.reply, "iterations": t.iterations,
        "guard_blocks": t.guard_blocks,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }
    if full:
        out.update({
            "tool_calls": t.tool_calls or [],
            "trace": t.trace or [],
            "error": t.error,
            "waiting": t.status == "waiting_approval",
        })
    return out


async def _own_session(db: AsyncSession, session_id: str, user) -> HarnessSession:
    try:
        row = await harness.get_session(db, session_id)
    except HarnessError as exc:
        raise _map_error(exc) from exc
    own_or_404(row.owner_id, user)
    return row


def _map_error(exc: HarnessError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


# ---------------------------------------------------------------------------
# the toolchest + health
# ---------------------------------------------------------------------------

@router.get("/tools")
async def list_tools() -> list[dict[str, Any]]:
    return harness_tools.tool_catalogue(harness_tools.build_registry())


@router.get("/health")
async def health(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await harness.harness_health(db)


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------

@router.get("/sessions")
async def list_sessions(user=Depends(get_optional_user),
                        db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    rows = await harness.list_sessions(db, user.id if user else None)
    return [session_out(s) for s in rows]


@router.post("/sessions", status_code=201)
async def create_session(body: SessionCreate, user=Depends(get_optional_user),
                         db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    _check(body.provider, body.memory)
    if body.provider == "openai_compatible" and not body.credential_id:
        raise HTTPException(status_code=400,
                            detail="openai_compatible provider requires a credential_id")
    row = await harness.create_session(
        db, owner_id=user.id if user else None, name=body.name.strip(),
        description=body.description, system_prompt=body.system_prompt,
        provider=body.provider, model=body.model.strip(),
        credential_id=body.credential_id, temperature=body.temperature,
        memory=body.memory, max_history_turns=body.max_history_turns,
        is_active=body.is_active)
    return session_out(row)


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, user=Depends(get_optional_user),
                      db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    row = await _own_session(db, session_id, user)
    turns = await harness.list_turns(db, row.id)
    out = session_out(row)
    out["turns"] = [turn_out(t) for t in turns]
    return out


@router.patch("/sessions/{session_id}")
async def update_session(session_id: str, body: SessionUpdate,
                         user=Depends(get_optional_user),
                         db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    row = await _own_session(db, session_id, user)
    _check(body.provider, body.memory)
    data = body.model_dump(exclude_unset=True)
    if data.get("provider") == "openai_compatible" and \
            not (data.get("credential_id") or row.credential_id):
        raise HTTPException(status_code=400,
                            detail="openai_compatible provider requires a credential_id")
    for key, value in data.items():
        setattr(row, key, value)
    await db.commit()
    await db.refresh(row)
    return session_out(row)


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, user=Depends(get_optional_user),
                         db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    row = await _own_session(db, session_id, user)
    await db.delete(row)
    await db.commit()
    return {"deleted": row.id}


# ---------------------------------------------------------------------------
# turns
# ---------------------------------------------------------------------------

@router.post("/sessions/{session_id}/turns")
async def create_turn(session_id: str, body: TurnCreate,
                      user=Depends(get_optional_user),
                      db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    session = await _own_session(db, session_id, user)
    try:
        turn = await harness.start_turn(db, session, body.message)
    except HarnessError as exc:
        raise _map_error(exc) from exc
    return turn_out(turn, full=True)


@router.get("/sessions/{session_id}/turns")
async def list_session_turns(session_id: str, user=Depends(get_optional_user),
                             db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    await _own_session(db, session_id, user)
    turns = await harness.list_turns(db, session_id)
    return [turn_out(t, full=True) for t in turns]


@router.get("/turns/{turn_id}")
async def get_turn(turn_id: str, user=Depends(get_optional_user),
                   db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    try:
        turn = await harness.get_turn(db, turn_id)
    except HarnessError as exc:
        raise _map_error(exc) from exc
    own_or_404(turn.owner_id, user)
    return turn_out(turn, full=True)


# ---------------------------------------------------------------------------
# the decision queue
# ---------------------------------------------------------------------------

@router.get("/approvals")
async def list_approvals(status: str | None = None,
                         user=Depends(get_optional_user),
                         db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    rows = await harness.list_approvals(db, user.id if user else None, status=status)
    return [{
        "id": a.id, "turn_id": a.turn_id, "session_id": a.session_id,
        "tool": a.tool, "arguments": a.arguments or {}, "status": a.status,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "decided_at": a.decided_at.isoformat() if a.decided_at else None,
        "decided_by": a.decided_by,
    } for a in rows]


@router.post("/approvals/{approval_id}/approve")
async def approve(approval_id: str, user=Depends(get_optional_user),
                  db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    slip = await _own_approval(db, approval_id, user)
    try:
        turn = await harness.decide(db, slip, approve=True,
                                    decided_by=user.id if user else None)
    except HarnessError as exc:
        raise _map_error(exc) from exc
    return turn_out(turn, full=True)


@router.post("/approvals/{approval_id}/reject")
async def reject(approval_id: str, user=Depends(get_optional_user),
                 db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    slip = await _own_approval(db, approval_id, user)
    try:
        turn = await harness.decide(db, slip, approve=False,
                                    decided_by=user.id if user else None)
    except HarnessError as exc:
        raise _map_error(exc) from exc
    return turn_out(turn, full=True)


async def _own_approval(db: AsyncSession, approval_id: str, user):
    try:
        slip = await harness.get_approval(db, approval_id)
    except HarnessError as exc:
        raise _map_error(exc) from exc
    own_or_404(slip.owner_id, user)
    return slip
