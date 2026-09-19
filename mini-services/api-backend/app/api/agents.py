"""Agent inventory + agent modules (v34 / v108).

GET /api/v1/agents - every workflow that contains an ai_agent node, with a
summary of the tools each agent can call (name + kind), its memory mode and
chat/webhook reachability. Read-only convenience view; the heavy lifting
stays in /workflows + /chat.

v108 - AGENT MODULES: the agent as its own resource instead of a node
trapped inside a graph. A module carries a system prompt, a provider
(sandbox bridge or an OpenAI-compatible credential), the SAME tool kit the
ai_agent node speaks (workflow / http / knowledge / dataset / code) and
per-session memory - and runs directly over POST /agents/modules/{id}/run
with no workflow document at all. The runtime is the node's own machinery
(see services/agent_modules); this router is the doors.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user, own_or_404, scope_rows
from ..db import get_db
from ..models import AgentModule, Workflow
from ..services import agent_modules as mod_svc
from ..services.agent_modules import AgentModuleError

router = APIRouter(prefix="/agents", tags=["agents"])

AGENT_NODE_TYPES = {"ai_agent"}


def _summarize(wf: Workflow) -> dict[str, Any] | None:
    graph = wf.graph or {}
    agents = [n for n in graph.get("nodes", []) if n.get("type") in AGENT_NODE_TYPES]
    if not agents:
        return None
    tools: list[dict[str, str]] = []
    memory_sessions: list[str] = []
    for node in agents:
        params = node.get("parameters") or {}
        for t in params.get("tools") or []:
            if isinstance(t, dict) and t.get("name"):
                tools.append({"name": str(t["name"]), "kind": str(t.get("kind", "knowledge"))})
        if params.get("memory", "none") == "buffer":
            memory_sessions.append(str(params.get("session_key") or "default"))
    return {
        "id": wf.id,
        "name": wf.name,
        "description": wf.description,
        "active": bool(wf.is_active),
        "agent_nodes": [n.get("name") or n.get("id") for n in agents],
        "tools": tools,
        "tool_kinds": sorted({t["kind"] for t in tools}),
        "memory_sessions": memory_sessions,
        "node_count": len(graph.get("nodes", [])),
        "updated_at": wf.updated_at.isoformat() if wf.updated_at else None,
    }


@router.get("")
async def list_agents(user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    rows = (await db.execute(select(Workflow).order_by(Workflow.updated_at.desc()))).scalars().all()
    summarized = (_summarize(wf) for wf in scope_rows(rows, user))  # v37
    return [a for a in summarized if a]


# ---------------------------------------------------------------------------
# v108 - agent modules: the agent as its own resource
# ---------------------------------------------------------------------------

_ALLOWED_PROVIDERS = {"sandbox_bridge", "openai_compatible"}
_ALLOWED_MEMORY = {"none", "buffer"}


class ModuleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field("", max_length=500)
    system_prompt: str = Field(
        "You are a precise operations agent. Use the tools when they help, then answer.",
        max_length=8000)
    provider: str = Field("sandbox_bridge", description="sandbox_bridge | openai_compatible")
    model: str = Field("", max_length=120)
    credential_id: str | None = Field(None, max_length=36)
    temperature: float = Field(0.4, ge=0, le=2)
    max_iterations: int = Field(5, ge=1, le=10)
    memory: str = Field("none", description="none | buffer")
    max_history_turns: int = Field(5, ge=1, le=50)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    is_active: bool = True


class ModuleUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    description: str | None = Field(None, max_length=500)
    system_prompt: str | None = Field(None, max_length=8000)
    provider: str | None = None
    model: str | None = Field(None, max_length=120)
    credential_id: str | None = None
    temperature: float | None = Field(None, ge=0, le=2)
    max_iterations: int | None = Field(None, ge=1, le=10)
    memory: str | None = None
    max_history_turns: int | None = Field(None, ge=1, le=50)
    tools: list[dict[str, Any]] | None = None
    is_active: bool | None = None


class ModuleRun(BaseModel):
    message: str = Field(..., min_length=1, max_length=20000)
    session_key: str = Field("default", max_length=120)


def _check_provider_memory(provider: str | None, memory: str | None) -> None:
    if provider is not None and provider not in _ALLOWED_PROVIDERS:
        raise HTTPException(status_code=400, detail="provider must be sandbox_bridge or openai_compatible")
    if memory is not None and memory not in _ALLOWED_MEMORY:
        raise HTTPException(status_code=400, detail="memory must be none or buffer")


def module_out(m: AgentModule) -> dict[str, Any]:
    tools = m.tools or []
    return {
        "id": m.id,
        "name": m.name,
        "description": m.description,
        "system_prompt": m.system_prompt,
        "provider": m.provider,
        "model": m.model,
        "credential_id": m.credential_id,
        "temperature": m.temperature,
        "max_iterations": m.max_iterations,
        "memory": m.memory,
        "max_history_turns": m.max_history_turns,
        "tools": tools,
        "tool_kinds": sorted({str(t.get("kind", "knowledge")) for t in tools if isinstance(t, dict)}),
        "is_active": bool(m.is_active),
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }


async def _get_module(db: AsyncSession, module_id: str, user) -> AgentModule:
    row = await db.get(AgentModule, module_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Agent module not found")
    own_or_404(row.owner_id, user)
    return row


@router.get("/modules")
async def list_modules(user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    rows = (await db.execute(select(AgentModule).order_by(AgentModule.updated_at.desc()))).scalars().all()
    return [module_out(m) for m in scope_rows(rows, user)]


@router.post("/modules", status_code=201)
async def create_module(
    body: ModuleCreate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    _check_provider_memory(body.provider, body.memory)
    try:
        tools = mod_svc.validate_tools(body.tools)
    except AgentModuleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if body.provider == "openai_compatible" and not body.credential_id:
        raise HTTPException(status_code=400, detail="openai_compatible provider requires a credential_id")
    row = AgentModule(
        owner_id=user.id if user else None,
        name=body.name.strip(),
        description=body.description,
        system_prompt=body.system_prompt,
        provider=body.provider,
        model=body.model.strip(),
        credential_id=body.credential_id,
        temperature=body.temperature,
        max_iterations=body.max_iterations,
        memory=body.memory,
        max_history_turns=body.max_history_turns,
        tools=tools,
        is_active=body.is_active,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return module_out(row)


@router.get("/modules/{module_id}")
async def get_module(module_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return module_out(await _get_module(db, module_id, user))


@router.patch("/modules/{module_id}")
async def update_module(
    module_id: str, body: ModuleUpdate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    row = await _get_module(db, module_id, user)
    _check_provider_memory(body.provider, body.memory)
    data = body.model_dump(exclude_unset=True)
    if "tools" in data:
        try:
            data["tools"] = mod_svc.validate_tools(data["tools"])
        except AgentModuleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if data.get("provider") == "openai_compatible" and not (data.get("credential_id") or row.credential_id):
        raise HTTPException(status_code=400, detail="openai_compatible provider requires a credential_id")
    for key, value in data.items():
        setattr(row, key, value)
    await db.commit()
    await db.refresh(row)
    return module_out(row)


@router.delete("/modules/{module_id}")
async def delete_module(module_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    row = await _get_module(db, module_id, user)
    await db.delete(row)
    await db.commit()
    return {"deleted": row.id}


@router.post("/modules/{module_id}/run")
async def run_module(
    module_id: str, body: ModuleRun, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    row = await _get_module(db, module_id, user)
    owner_id = user.id if user else None
    try:
        return await mod_svc.run_module(row, body.message, body.session_key, owner_id=owner_id)
    except AgentModuleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/modules/{module_id}/sessions")
async def module_sessions(
    module_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)
) -> list[dict[str, Any]]:
    row = await _get_module(db, module_id, user)
    return await mod_svc.list_sessions(row, owner_id=user.id if user else None)


@router.delete("/modules/{module_id}/sessions/{session_key}")
async def clear_module_session(
    module_id: str, session_key: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    row = await _get_module(db, module_id, user)
    gone = await mod_svc.clear_session(row, session_key, owner_id=user.id if user else None)
    return {"cleared": bool(gone), "session_key": session_key}
