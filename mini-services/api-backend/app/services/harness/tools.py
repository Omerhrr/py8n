"""The harness toolchest (v109 reads+moves, v110 builds) - PY8N ITSELF as
the model's tools.

Every tool is a real service call in this process - the same functions the
API doors call, owner-scoped the same way. Nothing here shells out, nothing
here invents a parallel API: the harness reads, moves and BUILDS the estate
through the exact code paths the product uses, so the two can never drift.

Five tools are SENSITIVE - they change the business (start / advance /
acknowledge / build_machine / install_operator). The loop never runs them
on the model's word alone: the turn pauses and a fail-closed approval slip
waits for a human (see interaction semantics in services/harness/service.py).
Sensitive tools may also carry a ``preflight`` - a cheap check that turns a
malformed call into immediate tool feedback, so a human is never asked to
decide a call that would not even run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...models import BusinessProcess, BusinessProcessInstance, Dataset
from .. import business_processes as bp
from .. import erp_books
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
    # pre-gate check for SENSITIVE tools: return a refusal string to bounce
    # a malformed call as tool feedback WITHOUT pausing a human (the gate is
    # for real decisions, not for syntax errors); None = may proceed
    preflight: Callable[[dict], str | None] | None = None


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
# the books (v119) - the ERP ledger as the model sees it. Every tool rides
# the SAME erp_books service the /erp doors serve, so the agent's numbers
# and the console's numbers can never disagree.
# ---------------------------------------------------------------------------

async def _resolve_book(db: AsyncSession, want: str, owner_id: str | None):
    """The book's dataset by id or case-insensitive name, owner-scoped -
    the same resolution the doors use, refusals as tool feedback."""
    from types import SimpleNamespace
    try:
        return await erp_books.book_dataset(
            db, want, SimpleNamespace(id=owner_id) if owner_id else None)
    except erp_books.BookNotFound as exc:
        raise HarnessToolError(str(exc)) from exc


async def _erp_books(db: AsyncSession, owner_id: str | None, args: dict) -> dict:
    rows = (await db.execute(
        select(Dataset).order_by(Dataset.created_at.desc()).limit(50))).scalars().all()
    out = []
    for d in rows:
        if owner_id is not None and d.owner_id not in (owner_id, None):
            continue
        schema = d.schema_json if isinstance(d.schema_json, list) else []
        cols = {str(c.get("name") or "") for c in schema}
        out.append({"id": d.id, "name": d.name, "row_count": d.row_count,
                    "looks_like_book": "account" in cols})
    out.sort(key=lambda x: (not x["looks_like_book"], -(x["row_count"] or 0)))
    return {"datasets": out[:TOOL_MAX_ROWS], "count": len(out)}


async def _erp_statements(db: AsyncSession, owner_id: str | None, args: dict) -> dict:
    ds = await _resolve_book(db, str(args.get("dataset", "")), owner_id)
    tb = erp_books.trial_balance_payload(ds)
    st = erp_books.statements_payload(ds)
    return {
        "dataset": st["dataset"],
        "trial_balance": {"balanced": tb["totals"]["balanced"],
                          "summary": tb["summary"],
                          "income": tb["income"]},
        "income_statement": st["income"],
        "balance_sheet": st["balance"],
    }


async def _erp_gl(db: AsyncSession, owner_id: str | None, args: dict) -> dict:
    ds = await _resolve_book(db, str(args.get("dataset", "")), owner_id)
    account = str(args.get("account") or "").strip().lower()
    ref = str(args.get("ref") or "").strip().lower()
    out = []
    for r in reversed(erp_books.raw_rows(ds)):  # the newest lines first
        if account and str(r.get("account") or "").strip().lower() != account:
            continue
        if ref and str(r.get("ref") or "").strip().lower() != ref:
            continue
        out.append(r)
        if len(out) >= TOOL_MAX_ROWS:
            break
    return {"dataset": {"id": ds.id, "name": ds.name}, "lines": out,
            "returned_lines": len(out),
            "filter": {"account": account or None, "ref": ref or None}}


async def _erp_aging(db: AsyncSession, owner_id: str | None, args: dict) -> dict:
    ds = await _resolve_book(db, str(args.get("dataset", "")), owner_id)
    aging = erp_books.aging_payload(ds)
    for side in ("receivables", "payables"):
        aging[side]["lines"] = aging[side]["lines"][:TOOL_MAX_ROWS]
    return aging


async def _erp_cash_flow(db: AsyncSession, owner_id: str | None, args: dict) -> dict:
    ds = await _resolve_book(db, str(args.get("dataset", "")), owner_id)
    return erp_books.cash_flow_payload(ds)


async def _erp_health(db: AsyncSession, owner_id: str | None, args: dict) -> dict:
    """THE BOOKS PATROL CHECK - the findings a patrol's round mails home:
    imbalance, unclassified money, overdrawn cash, receivables past 90."""
    want = str(args.get("dataset") or "").strip() or "GL entries"
    try:
        ds = await _resolve_book(db, want, owner_id)
    except HarnessToolError:
        return {"dataset": want, "healthy": False, "findings": [
            {"severity": "info",
             "finding": f"no dataset named {want!r} is visible - pass "
                        "dataset=<name or id>, or install the ERP core"}]}

    findings: list[dict] = []
    tb = erp_books.trial_balance_payload(ds)
    if not tb["totals"]["balanced"]:
        findings.append({
            "severity": "critical",
            "finding": f"the books DO NOT balance (debits "
                       f"{tb['totals']['debits']:.2f} vs credits "
                       f"{tb['totals']['credits']:.2f})"})
    for r in tb["rows"]:
        if r["kind"] == "other" and abs(r["balance"]) >= 0.005:
            findings.append({
                "severity": "warning",
                "finding": f"account {r['account']!r} is unclassified - "
                           f"its {r['balance']:.2f} sits unplaced"})
    for c in tb["rows"]:
        name = c["account"].lower()
        if erp_books.kind_of(name) == "asset" and ("cash" in name or "bank" in name) \
                and c["balance"] < 0:
            findings.append({
                "severity": "critical",
                "finding": f"{c['account']} is OVERDRAWN at {c['balance']:.2f}"})
    aging = erp_books.aging_payload(ds)
    for b in aging["receivables"]["buckets"]:
        if b["bucket"] == "d90_plus" and b["total"] > 0:
            findings.append({
                "severity": "warning",
                "finding": f"receivables past 90 days: {b['total']:.2f} - "
                           "the collection desk should call"})
    return {"dataset": {"id": ds.id, "name": ds.name},
            "healthy": not findings,
            "closed": await erp_books.is_closed(db, ds),
            "balanced": tb["totals"]["balanced"],
            "net_income": tb["income"]["net"],
            "findings": findings}


async def _erp_close(db: AsyncSession, owner_id: str | None, args: dict) -> dict:
    ds = await _resolve_book(db, str(args.get("dataset", "")), owner_id)
    try:
        return await erp_books.close_book(db, ds,
                                          period=str(args.get("period") or ""))
    except erp_books.BookError as exc:
        raise HarnessToolError(exc.detail) from exc


def _preflight_erp_close(args: dict) -> str | None:
    if not str(args.get("dataset") or "").strip():
        return 'pass {"dataset": "<the book\'s name or id>"} - which book closes?'
    return None


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
# builder tools (v110) - the harness as the COMPOSER AND BUILDER
#
# The estate is no longer only OPERATED by the harness - it gets BUILT by
# it. Blueprints are free (draft_machine validates a spec and builds
# nothing); the builds themselves ride the SAME fail-closed gate as the
# moves: the slip carries the whole spec, so the human reviews exactly what
# will exist before it does. Every build call goes through the composer's
# or the operators' own service - zero parallel build paths.
# ---------------------------------------------------------------------------

async def _build_menu(db: AsyncSession, owner_id: str | None, args: dict) -> dict:
    """What py8n can build: archetypes, component kinds, the operator shelf."""
    from ...services import ai_composer
    from ...services.operators import operator_catalog
    return {
        "archetypes": ai_composer.archetypes_out(),
        "kinds": {k: v["builds"] for k, v in ai_composer.COMPOSER_KINDS.items()},
        "operators": operator_catalog()["operators"],
        "note": "draft_machine composes a blueprint from a description (or "
                "validates one you hand-write); build_machine and "
                "install_operator wait for a human decision",
    }


async def _draft_machine(db: AsyncSession, owner_id: str | None, args: dict) -> dict:
    """Blueprint preview: a description becomes a validated spec through the
    archetypes, or a hand-composed spec is validated as-is. Builds NOTHING."""
    from ...services import ai_composer
    spec = args.get("spec")
    try:
        if isinstance(spec, dict) and spec:
            validated = ai_composer.validate_spec(spec)
            source = "hand-composed"
        else:
            description = str(args.get("description", "")).strip()
            if not description:
                raise HarnessToolError(
                    'pass {"description": "..."} (py8n drafts from the '
                    'archetypes) or {"spec": {...}} (you compose, py8n '
                    "validates) - then build_machine when it looks right")
            validated = ai_composer.validate_spec(
                ai_composer.synthesize_spec(description))
            source = "archetype"
    except ai_composer.AIComposerError as exc:
        raise HarnessToolError(f"the draft does not validate: {exc}") from exc
    return {
        "source": source, "mode": validated.get("mode"),
        "archetype": validated.get("archetype"),
        "name": validated.get("name"),
        "blueprint": [{"kind": c.get("kind"), "name": c.get("name")}
                      for c in validated.get("components", [])],
        "component_count": len(validated.get("components", [])),
        "spec": validated,
        "note": "blueprint only - nothing was built; call build_machine with "
                "this exact spec when it looks right (the human sees the "
                "whole spec on the approval slip)",
    }


def _preflight_build_machine(args: dict) -> str | None:
    """Bounce a spec-less or invalid build BEFORE a human is bothered."""
    from ...services import ai_composer
    spec = args.get("spec")
    if not isinstance(spec, dict) or not spec:
        return ('pass {"spec": {...}} - draft_machine returns a validated '
                "blueprint; the human reviews the whole spec on the slip")
    try:
        ai_composer.validate_spec(spec)
    except ai_composer.AIComposerError as exc:
        return f"the spec does not validate: {exc}"
    return None


async def _build_machine(db: AsyncSession, owner_id: str | None, args: dict) -> dict:
    """SENSITIVE - the composer's own build path (datasets, workflows,
    agents, rooms, queues, the running system). The caller commits; the
    loop's post-tool commit lands the machine."""
    from ...services import ai_composer
    try:
        return await ai_composer.build_system(db, args.get("spec") or {},
                                              owner_id=owner_id)
    except ai_composer.AIComposerError as exc:
        raise HarnessToolError(f"the build refused: {exc}") from exc


def _preflight_install_operator(args: dict) -> str | None:
    from ...services.operators import OPERATORS_BY_SLUG
    slug = str(args.get("slug", "")).strip()
    if not slug:
        return 'pass {"slug": "..."} - build_menu lists the shelf'
    if slug not in OPERATORS_BY_SLUG:
        return (f"unknown operator {slug!r} - the shelf: "
                f"{', '.join(sorted(OPERATORS_BY_SLUG))}")
    return None


async def _install_operator(db: AsyncSession, owner_id: str | None, args: dict) -> dict:
    """SENSITIVE - a shelf operator lands whole: datasets, processes,
    workflows, agent, dashboard, the system - pre-wired by the operator's
    own install path."""
    from ...services.operators import OperatorError, install_operator
    try:
        return await install_operator(
            db, str(args.get("slug", "")).strip(), owner_id=owner_id,
            brain=str(args.get("brain") or "scaffold"),
            note=str(args.get("note") or "installed by the harness"))
    except OperatorError as exc:
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
            name="erp_books",
            description="List the estate's datasets (id, name, row count), "
                        "flagging the ones that look like GL books (an "
                        "'account' column). The erp_* tools take dataset = "
                        "name or id; the shelf's standard book is 'GL "
                        "entries'.",
            args=_schema({}),
            handler=_erp_books),
        ToolDef(
            name="erp_statements",
            description="Read one book: the trial balance (balanced flag + "
                        "per-kind buckets), the income statement and the "
                        "balance sheet with the accounting-equation check. "
                        "The 'how are the numbers' answer.",
            args=_schema({"dataset": {"type": "string",
                                      "description": "book name or id"}},
                         required=["dataset"]),
            handler=_erp_statements),
        ToolDef(
            name="erp_gl",
            description="Read one book's journal lines, newest first - "
                        "optionally filtered to one account or one ref. "
                        "The drill behind every statement line.",
            args=_schema({"dataset": {"type": "string"},
                          "account": {"type": "string"},
                          "ref": {"type": "string"},
                          "limit": {"type": "integer",
                                    "description": "default 25, max 25"}},
                         required=["dataset"]),
            handler=_erp_gl),
        ToolDef(
            name="erp_aging",
            description="Read one book's AR/AP aging: open receivable and "
                        "payable refs bucketed current / 31-60 / 61-90 / "
                        "90+ / undated. Who owes the company, who it owes.",
            args=_schema({"dataset": {"type": "string"}},
                         required=["dataset"]),
            handler=_erp_aging),
        ToolDef(
            name="erp_cash_flow",
            description="Read one book's direct-method cash flow: inflows, "
                        "outflows and net per section (operating / investing "
                        "/ financing / other) - the sections' net IS the "
                        "cash movement.",
            args=_schema({"dataset": {"type": "string"}},
                         required=["dataset"]),
            handler=_erp_cash_flow),
        ToolDef(
            name="erp_health",
            description="THE BOOKS PATROL CHECK: the findings worth mailing "
                        "home - imbalance, unclassified money, overdrawn "
                        "cash, receivables past 90 days. healthy=true means "
                        "nothing to raise.",
            args=_schema({"dataset": {"type": "string",
                                      "description": "default 'GL entries'"}}),
            handler=_erp_health),
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
            name="erp_close",
            description="SENSITIVE - close the period on one book: real "
                        "closing entries sweep revenue and expense through "
                        "Income summary into Retained earnings and the book "
                        "LOCKS (a second close refuses). Waits for human "
                        "approval.",
            args=_schema({"dataset": {"type": "string",
                                      "description": "book name or id"},
                          "period": {"type": "string",
                                     "description": "optional label, e.g. 2026-08"}},
                         required=["dataset"]),
            handler=_erp_close, sensitive=True,
            preflight=_preflight_erp_close,
            moves="posts the period close and LOCKS the book"),
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
        ToolDef(
            name="build_menu",
            description="What py8n can BUILD: the composer's archetypes and "
                        "component kinds, plus the operator shelf (slug, "
                        "tagline, topology, chains). Read this before "
                        "drafting or installing.",
            args=_schema({}),
            handler=_build_menu),
        ToolDef(
            name="draft_machine",
            description="Compose a machine BLUEPRINT: a description becomes "
                        "a validated spec via the archetypes, or hand in "
                        "your own spec for validation. Builds nothing - "
                        "draft, look, adjust, then build_machine with the "
                        "exact spec.",
            args=_schema({
                "description": {"type": "string",
                                "description": "what the machine should do"},
                "spec": {"type": "object",
                         "description": "or a hand-composed spec to validate"},
            }),
            handler=_draft_machine),
        ToolDef(
            name="build_machine",
            description="SENSITIVE - compose a validated spec into REAL "
                        "primitives: datasets, workflows (installed "
                        "inactive), voice agents, rooms, queues, and the "
                        "running system binding them. The approval slip "
                        "carries the whole spec - the human sees exactly "
                        "what will exist. Waits for human approval.",
            args=_schema({"spec": {"type": "object",
                                   "description": "a validated blueprint "
                                                  "(draft_machine returns one)"}},
                         required=["spec"]),
            handler=_build_machine, sensitive=True,
            preflight=_preflight_build_machine,
            moves="builds new datasets, workflows and a running system"),
        ToolDef(
            name="install_operator",
            description="SENSITIVE - install one of the shelf's business "
                        "operators: its datasets, processes, workflows, "
                        "agent, dashboard and system land together, "
                        "pre-wired. Waits for human approval.",
            args=_schema({
                "slug": {"type": "string", "description": "from build_menu"},
                "brain": {"type": "string",
                          "description": "scaffold (default) or ai_agent"},
                "note": {"type": "string"},
            }, required=["slug"]),
            handler=_install_operator, sensitive=True,
            preflight=_preflight_install_operator,
            moves="installs a full business operator onto the estate"),
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
