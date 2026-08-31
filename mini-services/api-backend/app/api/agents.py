"""Agent inventory endpoint (v34) — powers the /agents console.

GET /api/v1/agents — every workflow that contains an ai_agent node, with a
summary of the tools each agent can call (name + kind), its memory mode and
chat/webhook reachability. Read-only convenience view; the heavy lifting
stays in /workflows + /chat.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import Workflow

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
async def list_agents(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    rows = (await db.execute(select(Workflow).order_by(Workflow.updated_at.desc()))).scalars().all()
    summarized = (_summarize(wf) for wf in rows)
    return [a for a in summarized if a]
