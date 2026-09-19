"""The harness toolchest (v109) - PY8N ITSELF as the model's tools.

Every tool is a real service call in this process - the same functions the
API doors call, owner-scoped the same way. Nothing here shells out, nothing
here invents a parallel API: the harness reads and moves the estate through
the exact code paths the product uses, so the two can never drift.

Three tools are SENSITIVE - they change the business (start / advance /
acknowledge). The loop never runs them on the model's word alone: the turn
pauses and a fail-closed approval slip waits for a human (see interaction
semantics in services/harness/service.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...models import BusinessProcess, BusinessProcessInstance
from .. import business_processes as bp
from .. import py8n_systems

# the read tools hand the model bounded answers (the loop also truncates)
TOOL_MAX_ROWS = 25


class HarnessToolError(Exception):
    """A tool-level refusal that reaches the MODEL as feedback, not the API
    as a 500 (the house pattern: the model can self-correct)."""


@dataclass
class ToolDef:
    """One tool the harness speaks. ``handler(db, owner_id, args) -> dict``."""

    name: str
    description: str
    args: dict                      # compact JSON-schema hint for the model
    handler: Callable[[AsyncSession, str | None, dict], Awaitable[dict]]
    sensitive: bool = False
    moves: str = ""                 # what a sensitive tool changes (for the slip)


@dataclass
class ToolResult:
    """What a tool run hands back to the loop: a structured value, the text
    the model receives (already truncated by the caller) and a status."""

    value: dict | None = None
    text: str = ""
    status: str = "ok"              # ok | error


# ---------------------------------------------------------------------------
# process resolution - the harness accepts ids OR case-insensitive names,
# resolved through the same owner discipline the doors use
# ---------------------------------------------------------------------------

async def _resolve_process(db: AsyncSession, want: str,
                           owner_id: str | None) -> BusinessProcess:
    want = str(want or "").strip()
    if not want:
        raise HarnessToolError("name a process (id or name)")
    row = (await db.execute(
        select(BusinessProcess).where(BusinessProcess.id == want))).scalars().first()
    if row is None:
        from sqlalchemy import func
        row = (await db.execute(
            select(BusinessProcess)
            .where(func.lower(BusinessProcess.name) == want.lower())
            .order_by(BusinessProcess.created_at.desc()))).scalars().first()
    if row is None or (owner_id is not None and row.owner_id not in (owner_id, None)):
        raise HarnessToolError(f"process {want!r} not found")
    return row


# ---------------------------------------------------------------------------
# read tools - the estate as the model sees it
# ---------------------------------------------------------------------------

async def _estate_overview(db: AsyncSession, owner_id: str | None, args: dict) -> dict:
    return await py8n_systems.health_overview(db, None)


async def _list_processes(db: AsyncSession, owner_id: str | None, args: dict) -> dict:
    rows = await bp.list_processes(db, owner_id)
    return {"processes": rows, "count": len(rows)}


async def _find_instances(db: AsyncSession, owner_id: str | None, args: dict) -> dict:
    try:
        return await bp.query_instances(
            db, owner_id=owner_id,
            process=args.get("process"), state=args.get("state"),
            ref=args.get("ref"), stuck_only=bool(args.get("stuck_only", False)),
            open_only=bool(args.get("open_only", True)),
            limit=min(int(args.get("limit") or TOOL_MAX_ROWS), TOOL_MAX_ROWS))
    except bp.ProcessError as exc:
        raise HarnessToolError(str(exc)) from exc


async def _attention_feed(db: AsyncSession, owner_id: str | None, args: dict) -> dict:
    return await bp.attention_feed(db, owner_id,
                                   limit=min(int(args.get("limit") or 50), 100))


async def _chain_map(db: AsyncSession, owner_id: str | None, args: dict) -> dict:
    return await bp.chain_map(db, owner_id,
                              history_limit=min(int(args.get("history_limit") or 5), 20))


async def _escalation_heatmap(db: AsyncSession, owner_id: str | None, args: dict) -> dict:
    return await bp.escalation_history_grid(
        db, owner_id, days=min(int(args.get("days") or 14), 60))


async def _query_data(db: AsyncSession, owner_id: str | None, args: dict) -> dict:
    """Read-only SQL over the datasets - the node's own guard, zero drift."""
    from ...engine.nodes.agent import AgentNode
    from .. import datasets as ds_svc

    try:
        sql = AgentNode._guard_readonly_sql(str(args.get("sql", "")))  # noqa: SLF001 - shared guard
    except Exception as exc:  # NodeExecutionError -> tool feedback
        raise HarnessToolError(str(exc)) from exc
    if not sql:
        raise HarnessToolError('pass {"sql": "SELECT ..."}')
    try:
        out = await ds_svc.run_sql(db, sql, owner_id=owner_id)
    except ValueError as exc:
        raise HarnessToolError(str(exc)) from exc
    rows = out["rows"][:TOOL_MAX_ROWS]
    return {"columns": out["columns"], "rows": rows,
            "row_count": out["row_count"], "returned_rows": len(rows)}


# ---------------------------------------------------------------------------
# sensitive tools - the moves that change the business
# ---------------------------------------------------------------------------

async def _start_instance(db: AsyncSession, owner_id: str | None, args: dict) -> dict:
    proc = await _resolve_process(db, str(args.get("process", "")), owner_id)
    try:
        return await bp.start_instance(
            db, proc.id, owner_id=owner_id,
            ref=str(args.get("ref", "")), title=str(args.get("title", "")),
            context=args.get("context") if isinstance(args.get("context"), dict) else None,
            state=args.get("state"), actor=str(args.get("by") or "harness"))
    except bp.ProcessError as exc:
        raise HarnessToolError(str(exc)) from exc


async def _advance_instance(db: AsyncSession, owner_id: str | None, args: dict) -> dict:
    instance_id = str(args.get("instance_id", "")).strip()
    if not instance_id:
        raise HarnessToolError('pass {"instance_id": "...", "transition": "..."} - '
                               "find instances first to learn the ids")
    transition = str(args.get("transition", "")).strip() or None
    to_state = str(args.get("to_state", "")).strip() or None
    if not transition and not to_state:
        raise HarnessToolError("name the move: transition (by name) or to_state")
    due = args.get("due_in_seconds")
    try:
        return await bp.advance_instance(
            db, instance_id, owner_id=owner_id, transition=transition,
            to_state=to_state, actor=str(args.get("by") or "harness"),
            note=str(args.get("note", "")),
            due_in_seconds=int(due) if due is not None else None)
    except bp.ProcessError as exc:
        raise HarnessToolError(str(exc)) from exc


async def _acknowledge_escalation(db: AsyncSession, owner_id: str | None, args: dict) -> dict:
    instance_id = str(args.get("instance_id", "")).strip()
    if not instance_id:
        raise HarnessToolError('pass {"instance_id": "..."} - find instances first '
                               "to learn the ids")
    row = (await db.execute(
        select(BusinessProcessInstance).where(
            BusinessProcessInstance.id == instance_id))).scalars().first()
    if row is None or (owner_id is not None and row.owner_id not in (owner_id, None)):
        raise HarnessToolError(f"instance {instance_id!r} not found")
    snooze = args.get("snooze_hours")
    resched = args.get("reschedule_in_minutes")
    try:
        return await bp.acknowledge_escalation(
            db, row.process_id, instance_id, owner_id=owner_id,
            by=str(args.get("by") or "harness-operator"),
            note=str(args.get("note", "")),
            snooze_hours=float(snooze) if snooze is not None else None,
            reschedule_in_minutes=float(resched) if resched is not None else None)
    except bp.ProcessError as exc:
        raise HarnessToolError(str(exc)) from exc


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------

def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties,
            "required": required or []}


def build_registry() -> dict[str, ToolDef]:
    tools: list[ToolDef] = [
        ToolDef(
            name="estate_overview",
            description="One health row per installed system: lifecycle dot, "
                        "7d workflow success rate, overdue instances, open "
                        "escalations, liveness. The 'how is the estate' answer.",
            args=_schema({}),
            handler=_estate_overview),
        ToolDef(
            name="list_processes",
            description="Every business process machine (id, name, states, "
                        "transitions, instance counts). Read this before "
                        "moving anything.",
            args=_schema({}),
            handler=_list_processes),
        ToolDef(
            name="find_instances",
            description="Search running entities across machines. Filter by "
                        "process (id or name), state, ref, or stuck_only; "
                        "returns ids, refs, states, due clocks and the "
                        "escalation book per row.",
            args=_schema({
                "process": {"type": "string", "description": "process id or name"},
                "state": {"type": "string"},
                "ref": {"type": "string"},
                "stuck_only": {"type": "boolean"},
                "open_only": {"type": "boolean", "description": "default true"},
                "limit": {"type": "integer", "description": "default 25, max 25"},
            }),
            handler=_find_instances),
        ToolDef(
            name="attention_feed",
            description="Every open instance past its SLA across ALL machines, "
                        "most-overdue first, with the escalation book and the "
                        "policy line. The 'what needs a human today' list.",
            args=_schema({"limit": {"type": "integer", "description": "default 50"}}),
            handler=_attention_feed),
        ToolDef(
            name="chain_map",
            description="The cross-machine journey chains with live counts "
                        "(open / open_now / stuck per node) and recent leg "
                        "traversals. How the departments hand work to each other.",
            args=_schema({"history_limit": {"type": "integer", "description": "1-20, default 5"}}),
            handler=_chain_map),
        ToolDef(
            name="escalation_heatmap",
            description="Per machine per day over the last N days: what the "
                        "escalation door did (knocks), what landed in digests, "
                        "what humans acknowledged.",
            args=_schema({"days": {"type": "integer", "description": "1-60, default 14"}}),
            handler=_escalation_heatmap),
        ToolDef(
            name="query_data",
            description="Run ONE read-only SQL (SELECT/WITH) over the "
                        "registered datasets - each dataset is a view named "
                        "after it. For numbers the other tools do not carry.",
            args=_schema({"sql": {"type": "string"}}),
            handler=_query_data),
        ToolDef(
            name="start_instance",
            description="SENSITIVE - open a new entity on a machine (a new "
                        "case, lead, invoice). Needs the process and an "
                        "external ref. Waits for human approval.",
            args=_schema({
                "process": {"type": "string", "description": "process id or name"},
                "ref": {"type": "string", "description": "the external key"},
                "title": {"type": "string"},
                "context": {"type": "object"},
                "state": {"type": "string", "description": "optional start state"},
            }, required=["process", "ref"]),
            handler=_start_instance, sensitive=True,
            moves="opens a new entity on the machine"),
        ToolDef(
            name="advance_instance",
            description="SENSITIVE - move an entity through its machine "
                        "(transition by name or direct to_state, optional "
                        "note + due_in_seconds). Waits for human approval.",
            args=_schema({
                "instance_id": {"type": "string"},
                "transition": {"type": "string", "description": "transition name"},
                "to_state": {"type": "string", "description": "or the target state"},
                "note": {"type": "string"},
                "due_in_seconds": {"type": "integer"},
            }, required=["instance_id"]),
            handler=_advance_instance, sensitive=True,
            moves="moves an entity to its next state"),
        ToolDef(
            name="acknowledge_escalation",
            description="SENSITIVE - a human takes an escalation: the door "
                        "goes quiet for the stint (or a snooze loan). Needs "
                        "instance_id; the door must have knocked first. "
                        "Waits for human approval.",
            args=_schema({
                "instance_id": {"type": "string"},
                "note": {"type": "string"},
                "snooze_hours": {"type": "number", "description": "optional loan"},
                "reschedule_in_minutes": {"type": "number",
                                          "description": "or an explicit next knock; never both"},
            }, required=["instance_id"]),
            handler=_acknowledge_escalation, sensitive=True,
            moves="silences the escalation door for one entity"),
    ]
    return {t.name: t for t in tools}


def tool_catalogue(registry: dict[str, ToolDef]) -> list[dict]:
    """The API-facing catalog: name, description, args, sensitivity."""
    return [
        {"name": t.name, "description": t.description, "args": t.args,
         "sensitive": t.sensitive, "moves": t.moves}
        for t in registry.values()
    ]


def protocol_catalogue(registry: dict[str, ToolDef]) -> dict[str, dict]:
    """The wire-protocol catalogue - the SAME shape AgentNode._protocol
    renders, with each tool's argument schema folded into the description
    so the model knows how to call it."""
    return {
        t.name: {"type": "object",
                 "description": f"{t.description} Arguments JSON schema: "
                                f"{json.dumps(t.args, ensure_ascii=False)}"}
        for t in registry.values()
    }
