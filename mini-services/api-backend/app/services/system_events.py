"""System events (v80) - the real-time event system, a first-class primitive.

Everything the real-time layers do generates one: calls start and end,
callers wait and get seated, participants join rooms, tracks publish,
recordings land, texts arrive, callbacks schedule. py8n already had a
pile of features that each kept their own record; this module is the
primitive that makes them COMPOSABLE:

* **emit** - one door, ``emit()``. An event is traffic (it happened),
  appended to ``system_events`` with source / type / actor / target /
  payload / correlation_id / session_id. The write rides the caller's
  transaction (like every timeline append in py8n); the LIVE fan-out and
  the workflow dispatch happen after the flush.
* **live tail** - an in-process, owner-scoped pub/sub hub
  (``subscribe``/``publish``). The WS endpoint streams it. Honest about
  scope: single-process, like the sandbox event bus; the redis fan-out
  for multi-process rides the same seam as the execution bus later.
* **workflow dispatch** - the payoff. Workflows subscribe with the
  Event Trigger node (``event_trigger``, a type pattern like
  ``queue.*`` or the exact ``meeting.ended``). When an event lands,
  every active workflow of the same owner whose pattern matches is
  dispatched fire-and-forget (``executor.dispatch_inline``) with the
  event as the trigger payload - the run is a real ExecutionLog with
  trigger_type=event, so failures land in the run's own history.

Event emission NEVER breaks the operation that emitted it? No - the
opposite, deliberately: emit is a plain INSERT in the caller's open
transaction, so a failing event store fails the operation loudly (the
py8n rule). What is fire-and-forget is only the DISPATCH of reacting
workflows - their failures are recorded in their own executions.
"""

from __future__ import annotations

import asyncio
import fnmatch
import re
from datetime import datetime, timezone

from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import SystemEvent, Workflow

# The system components that emit events. ``user`` is the manual door
# (POST /events - a workflow or an operator composing); ``system`` is
# reserved for the platform itself.
EVENT_SOURCES = ("voice", "queue", "sms", "meeting", "video", "recording",
                 "media", "campaign", "user", "system", "business")

# dotted, lowercase, at least two segments: call.waiting, queue.position_changed
_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")

MAX_PAYLOAD_BYTES = 16 * 1024   # an event carries facts, not blobs
MAX_LIST = 500


class SystemEventError(ValueError):
    """Honest 4xx-grade event failures."""


def _now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# The live hub - owner-scoped, in-process
# ---------------------------------------------------------------------------

_subscribers: dict[str, set[asyncio.Queue]] = {}


def subscribe(owner_key: str | None) -> asyncio.Queue:
    """Live-tail queue for one owner (``None``/``*`` = the anonymous tail
    that only sees events emitted with no owner)."""
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.setdefault(owner_key or "*", set()).add(q)
    return q


def unsubscribe(owner_key: str | None, q: asyncio.Queue) -> None:
    bucket = _subscribers.get(owner_key or "*")
    if bucket is not None:
        bucket.discard(q)
        if not bucket:
            _subscribers.pop(owner_key or "*", None)


def _publish_live(event: dict) -> None:
    """Fan out to the live tails (put_nowait - the hub's queues are
    unbounded, nobody waits on anybody). Called at COMMIT time so a tail
    never sees an event the store does not."""
    keys = [event.get("owner_id") or "*"]
    if event.get("owner_id") is None:
        keys.append("*")  # an unowned event is world-visible inside the process
    for key in dict.fromkeys(keys):
        for q in list(_subscribers.get(key, ())):
            q.put_nowait(event)


def live_subscriber_count() -> int:
    return sum(len(s) for s in _subscribers.values())


# ---------------------------------------------------------------------------
# emit - the one door
# ---------------------------------------------------------------------------


def _validate_type(event_type: str) -> str:
    t = (event_type or "").strip().lower()
    if not _TYPE_RE.match(t):
        raise SystemEventError(
            f"event type {event_type!r} must be dotted lowercase with at least two "
            f"segments (call.waiting, queue.position_changed, meeting.ended)")
    if len(t) > 80:
        raise SystemEventError("event type is limited to 80 characters")
    return t


def _validate_source(source: str) -> str:
    s = (source or "").strip().lower()
    if s not in EVENT_SOURCES:
        raise SystemEventError(
            f"source {source!r} is not an event source (known: {', '.join(EVENT_SOURCES)})")
    return s


def event_out(row: SystemEvent) -> dict:
    return {
        "id": row.id,
        "owner_id": row.owner_id,
        "source": row.source,
        "type": row.type,
        "actor": row.actor,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "correlation_id": row.correlation_id,
        "session_id": row.session_id,
        "payload": row.payload or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def emit(db: AsyncSession, owner_id: str | None, event_type: str, *,
               source: str, actor: str = "", target_type: str = "",
               target_id: str = "", payload: dict | None = None,
               correlation_id: str = "", session_id: str | None = None) -> dict:
    """Append one event and set the real-time machinery in motion.

    The INSERT rides the caller's transaction (flush, never commit - the
    API layer owns the commit, exactly like every timeline append). The
    LIVE fan-out and the WORKFLOW DISPATCH are deferred to the
    transaction's commit: a tail never sees an event the store does not,
    a rolled-back operation never dispatches a phantom reaction, and the
    reacting runs (which open their own sessions) never contend with the
    emitting request's write lock. The live fan-out and dispatch list are
    drained by :func:`drain_dispatch` (tests) and by the tasks themselves.
    """
    t = _validate_type(event_type)
    s = _validate_source(source)
    if payload is not None and not isinstance(payload, dict):
        raise SystemEventError("event payload must be a JSON object")
    row = SystemEvent(
        owner_id=owner_id, source=s, type=t,
        actor=(actor or "").strip()[:180],
        target_type=(target_type or "").strip()[:40],
        target_id=(target_id or "").strip()[:180],
        correlation_id=(correlation_id or "").strip()[:180],
        session_id=session_id,
        payload=payload or {},
        created_at=_now(),
    )
    db.add(row)
    await db.flush()
    out = event_out(row)
    _schedule_after_commit(db, out)
    return out


# ---------------------------------------------------------------------------
# workflow dispatch - the composable-systems payoff (deferred to commit)
# ---------------------------------------------------------------------------

# dispatch tasks created by after-commit hooks (drained by tests)
_DISPATCH_TASKS: set = set()


def _schedule_after_commit(db: AsyncSession, event: dict) -> None:
    """Queue the event on its session; the session's FIRST after-commit
    hook fans out everything queued so far. One hook per session, armed
    lazily on the first emit."""
    sync = db.sync_session
    pending = getattr(sync, "_py8n_pending_events", None)
    if pending is None:
        pending = []
        sync._py8n_pending_events = pending
        sa_event.listen(sync, "after_commit", _after_commit)
    pending.append(event)


def _after_commit(session) -> None:
    """The transaction committed: the queued events are real. Fan out to
    the live tails now (in-memory, instant) and schedule the workflow
    dispatches as tasks on the running loop."""
    pending = getattr(session, "_py8n_pending_events", None) or []
    session._py8n_pending_events = []
    if not pending:
        return
    loop = asyncio.get_running_loop()
    for ev in pending:
        _publish_live(ev)
        task = loop.create_task(_dispatch_event(ev))
        _DISPATCH_TASKS.add(task)
        task.add_done_callback(_DISPATCH_TASKS.discard)


async def _dispatch_event(event: dict) -> None:
    """The dispatch runs against its OWN session - the emitting request's
    transaction is long gone by then (no lock contention), and the event
    data rides as a plain dict (no lazy loads, no expired rows)."""
    from ..db import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as session:
            await dispatch_for_event(session, event)
    except Exception:  # pragma: no cover - a dispatch bug must never crash
        # the emitter; the event is stored, the live tails saw it, and the
        # failing runs would have their own logs - log and move on
        import logging

        logging.getLogger(__name__).exception(
            "event dispatch failed for %s", event.get("type"))


async def drain_dispatch() -> None:
    """Await every pending after-commit dispatch task (test determinism:
    call before draining the executor's own background tasks)."""
    tasks = [t for t in list(_DISPATCH_TASKS) if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _event_matches(params: dict, event: dict) -> bool:
    """One event_trigger node's params against one event."""
    pattern = str(params.get("event_type") or "").strip().lower()
    if not pattern:
        return False
    if not fnmatch.fnmatchcase(event["type"], pattern):
        return False
    want_source = str(params.get("source") or "").strip().lower()
    if want_source and want_source != event.get("source"):
        return False
    want_corr = str(params.get("correlation_id") or "").strip()
    if want_corr and want_corr != event.get("correlation_id"):
        return False
    return True


def _trigger_nodes_of(workflow: Workflow) -> list[dict]:
    """The workflow's enabled event_trigger nodes (id + merged params)."""
    out: list[dict] = []
    for n in (workflow.graph or {}).get("nodes", []):
        if not isinstance(n, dict) or n.get("type") != "event_trigger":
            continue
        if n.get("disabled") is True:
            continue
        out.append(n)
    return out


async def dispatch_for_event(db: AsyncSession, event: dict) -> list[dict]:
    """Dispatch every matching workflow of the event's owner.

    Fire-and-forget on purpose: the event returns immediately; the runs
    are real executions (trigger_type=event) whose failures land in their
    own logs. One dispatch per workflow even when several triggers match.

    v81: the system lifecycle gate rides here - a workflow bound to a
    paused/stopped system does not react (at least one RUNNING binding
    keeps a multi-system workflow alive; unbound workflows are ungated).
    """
    from . import executor
    from . import system_runtime

    owner_id = event.get("owner_id")
    q = select(Workflow).where(Workflow.is_active.is_(True))
    if owner_id is not None:
        q = q.where(Workflow.owner_id == owner_id)
    workflows = (await db.execute(q)).scalars().all()
    matching = [wf for wf in workflows
                if any(_event_matches(_params_of(n), event) for n in _trigger_nodes_of(wf))]
    if not matching:
        return []
    gate = await system_runtime.lifecycle_gate_map(db, [wf.id for wf in matching])
    dispatched: list[dict] = []
    for wf in matching:
        if gate.get(wf.id, False):
            continue  # the workflow's system is paused/stopped - it holds still
        exec_id = await executor.dispatch_inline(
            wf.id, trigger_type="event",
            trigger_payload={"event": event}, owner_id=wf.owner_id)
        dispatched.append({"workflow_id": wf.id, "workflow_name": wf.name,
                           "execution_id": exec_id})
    return dispatched


def _params_of(node: dict) -> dict:
    # graph nodes carry their parameters under "parameters" (the NodeSpec
    # shape); "params" is accepted for robustness
    p = node.get("parameters")
    if not isinstance(p, dict):
        p = node.get("params")
    return p if isinstance(p, dict) else {}


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


async def get_event(db: AsyncSession, owner_id: str | None, event_id: str) -> dict:
    row = await db.get(SystemEvent, event_id)
    if row is None or (row.owner_id is not None and row.owner_id != owner_id):
        raise SystemEventError(f"event {event_id!r} not found")
    return event_out(row)


async def list_events(db: AsyncSession, owner_id: str | None, *,
                      type_prefix: str | None = None,
                      source: str | None = None,
                      session_id: str | None = None,
                      correlation_id: str | None = None,
                      since: datetime | None = None,
                      until: datetime | None = None,
                      limit: int = 100) -> list[dict]:
    """Newest-first event query. ``type_prefix`` is a dotted-prefix OR an
    fnmatch pattern (``queue.*``) - the same patterns Event Triggers use."""
    limit = max(1, min(int(limit or 100), MAX_LIST))
    q = select(SystemEvent).order_by(SystemEvent.created_at.desc(), SystemEvent.id.desc())
    if owner_id is not None:
        q = q.where(SystemEvent.owner_id == owner_id)
    if source:
        q = q.where(SystemEvent.source == _validate_source(source))
    if session_id:
        q = q.where(SystemEvent.session_id == session_id)
    if correlation_id:
        q = q.where(SystemEvent.correlation_id == correlation_id)
    if since is not None:
        q = q.where(SystemEvent.created_at >= since)
    if until is not None:
        q = q.where(SystemEvent.created_at <= until)
    if type_prefix:
        pat = type_prefix.strip().lower()
        if any(c in pat for c in "*?["):
            # a pattern rides the python side AFTER every sql filter
            rows = (await db.execute(q.limit(MAX_LIST * 4))).scalars().all()
            return [event_out(r) for r in rows
                    if fnmatch.fnmatchcase(r.type, pat)][:limit]
        q = q.where(SystemEvent.type.like(f"{pat}%"))
    rows = (await db.execute(q.limit(limit))).scalars().all()
    return [event_out(r) for r in rows]


def stream_out_note() -> dict:
    """The honest scope note the stream endpoint returns."""
    return {
        "scope": "this process",
        "note": ("the live tail is owner-scoped and in-process; a multi-process "
                 "deployment rides the same redis seam as the execution bus"),
    }
