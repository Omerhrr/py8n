"""System Runtime (v81) - systems as RUNNING entities.

The roadmap's third step (after first-class video in v78 and the
real-time event system in v80): a Py8n System stops being a curated
grouping and becomes an operating entity with a LIFECYCLE -

    install -> start -> pause/resume/stop -> upgrade

* **The lifecycle gate.** A system that is ``paused`` or ``stopped``
  holds its workflows still on every REACTIVE path: event triggers stop
  dispatching, schedule ticks and dataset-trigger polls return early,
  webhook hits are refused with 409. The gate is an ADDITIONAL gate on
  top of each workflow's own ``is_active`` - the verbs never touch that
  flag, so a stop/start cycle restores exactly what the user had active
  (the per-workflow intent survives the operation; the explicit
  ``activate_workflows`` door on start can turn the inactive pack
  installs on, loudly and on the record).

  A workflow bound to SEVERAL systems reacts when AT LEAST ONE of them
  is running - system A stopping must not take down system B; a
  workflow bound to no system is ungated (the builder's own).

* **Operations.** Every accepted verb writes a SystemOperation row - the
  system's durable audit (who, what, when, with what facts). This is the
  deliberate exception to derived-never-stored, like campaign targets
  and serving-token hits: an operations log IS state about traffic.

* **Events.** Every accepted verb emits through the v80 door -
  ``system.started``, ``system.stopped``, ``system.paused``,
  ``system.resumed``, ``system.upgraded``, ``system.installed``,
  ``system.component_added``, ``system.component_removed`` - with the
  system id as correlation_id, so a workflow can subscribe to
  ``system.*`` and react to the platform's own operations.

* **Upgrade.** A system installed from a marketplace solution remembers
  its ``source_solution_slug``; upgrade re-applies that solution's pack
  through the same install machinery and reconciles: genuinely new
  workflows/datasets are bound, objects already bound stay bound, and
  same-name objects are reported and left untouched - py8n never
  rewrites a live workflow under a running system.

Manual runs (POST /workflows/{id}/run) stay open on a stopped system on
purpose: they are the builder's explicit debugging door, not the system
operating. The state endpoint reports the gate honestly either way.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    ChannelQueue,
    ChannelQueueEntry,
    ExecutionLog,
    Py8nSystem,
    Solution,
    SystemComponent,
    SystemEvent,
    SystemOperation,
    VoiceMeeting,
    VoiceSession,
    Workflow,
)
from . import system_events as events_svc

LIFECYCLE_STATES = ("running", "paused", "stopped")

# verb -> (allowed from-states, to-state). Invalid transitions fail loud
# with the current state in the message (409 at the API layer).
TRANSITIONS: dict[str, tuple[set[str], str]] = {
    "start": ({"stopped", "paused"}, "running"),
    "stop": ({"running", "paused"}, "stopped"),
    "pause": ({"running"}, "paused"),
    "resume": ({"paused"}, "running"),
}

VERB_EVENT_TYPES = {
    "start": "system.started",
    "stop": "system.stopped",
    "pause": "system.paused",
    "resume": "system.resumed",
}

MAX_OPERATIONS = 200


class SystemRuntimeError(ValueError):
    """Honest 4xx-grade runtime failures."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# ---------------------------------------------------------------------------
# the lifecycle gate - the one question every reactive path asks
# ---------------------------------------------------------------------------

async def lifecycle_gate_map(db: AsyncSession, workflow_ids: list[str]) -> dict[str, bool]:
    """workflow_id -> True when EVERY system binding holds it still.

    One query for the whole candidate set (the dispatch path asks this on
    every event). A workflow bound to nothing is never gated; bound to
    several systems, any running one keeps it alive.
    """
    ids = [w for w in set(workflow_ids) if w]
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(SystemComponent.ref_id, Py8nSystem.lifecycle)
            .join(Py8nSystem, Py8nSystem.id == SystemComponent.system_id)
            .where(SystemComponent.kind == "workflow", SystemComponent.ref_id.in_(ids))
        )
    ).all()
    lifecycles: dict[str, list[str]] = {}
    for ref_id, lifecycle in rows:
        lifecycles.setdefault(ref_id, []).append(lifecycle or "running")
    return {
        wf_id: bool(states) and all(s != "running" for s in states)
        for wf_id, states in lifecycles.items()
    }


async def workflow_gated(db: AsyncSession, workflow_id: str) -> bool:
    """The single-workflow form (schedule ticks, webhook hits)."""
    return bool((await lifecycle_gate_map(db, [workflow_id])).get(workflow_id, False))


# ---------------------------------------------------------------------------
# operations + events
# ---------------------------------------------------------------------------

async def record_operation(db: AsyncSession, system: Py8nSystem, verb: str,
                           actor: str, detail: dict | None = None) -> SystemOperation:
    row = SystemOperation(
        system_id=system.id, verb=verb,
        actor=(actor or "system")[:180] or "system",
        detail=detail or {},
        created_at=_now(),
    )
    db.add(row)
    await db.flush()
    return row


async def _emit_system_event(db: AsyncSession, system: Py8nSystem, event_type: str,
                             payload: dict) -> dict:
    """Every runtime verb rides the v80 door - one correlation thread per
    system, so ``system.*`` triggers and the live tail see the platform's
    own operations next to the business events."""
    return await events_svc.emit(
        db, system.owner_id, event_type,
        source="system", actor="system",
        target_type="system", target_id=system.id,
        payload={**payload, "system_id": system.id, "system_name": system.name},
        correlation_id=system.id,
    )


async def list_operations(db: AsyncSession, system: Py8nSystem, limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit or 50), MAX_OPERATIONS))
    rows = (
        await db.execute(
            select(SystemOperation)
            .where(SystemOperation.system_id == system.id)
            .order_by(SystemOperation.created_at.desc(), SystemOperation.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "id": r.id, "verb": r.verb, "actor": r.actor,
            "detail": r.detail or {},
            "created_at": _iso(r.created_at),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# the verbs
# ---------------------------------------------------------------------------

async def apply_lifecycle(db: AsyncSession, system: Py8nSystem, verb: str, *,
                          actor: str = "", activate_workflows: bool = False) -> dict:
    """Validate the transition, flip the gate, write the operation, emit
    the event. The workflows' own ``is_active`` is NEVER touched here -
    the gate is the system's, and start's explicit ``activate_workflows``
    door is the only loud way to turn pack-installed workflows on."""
    if verb not in TRANSITIONS:
        raise SystemRuntimeError(
            f"unknown lifecycle verb {verb!r} (allowed: {', '.join(TRANSITIONS)})")
    sources, target = TRANSITIONS[verb]
    current = system.lifecycle or "running"
    if current not in LIFECYCLE_STATES:
        # a corrupted row fails loud instead of pretending
        raise SystemRuntimeError(
            f"system lifecycle is corrupted ({current!r}); repair the row before operating")
    if current not in sources:
        raise SystemRuntimeError(
            f"cannot {verb} a system that is {current!r} "
            f"({verb} applies to: {', '.join(sorted(sources))})")

    system.lifecycle = target

    workflows_activated = 0
    if verb == "start" and activate_workflows:
        wf_ids = [c.ref_id for c in system.components or [] if c.kind == "workflow"]
        if wf_ids:
            rows = (await db.execute(select(Workflow).where(Workflow.id.in_(wf_ids)))).scalars().all()
            for wf in rows:
                if not wf.is_active:
                    wf.is_active = True
                    workflows_activated += 1

    detail = {"from": current, "to": target}
    if verb == "start":
        detail["activate_workflows"] = workflows_activated
        detail["note"] = (
            "workflows activated by the start door" if workflows_activated else
            "per-workflow is_active untouched (the gate is the system's, not the flag's)")
    op = await record_operation(db, system, verb, actor, detail)
    event = await _emit_system_event(db, system, VERB_EVENT_TYPES[verb], {
        "operation_id": op.id, **detail,
    })
    return {
        "verb": verb, "from": current, "lifecycle": target,
        "workflows_activated": workflows_activated if verb == "start" else None,
        "operation_id": op.id, "event": event,
    }


async def install_mark(db: AsyncSession, system: Py8nSystem, *, solution_slug: str,
                       actor: str = "", component_counts: dict | None = None) -> dict:
    """Stamp solution provenance + write the installed operation/event."""
    system.source_solution_slug = solution_slug
    detail = {"solution": solution_slug, "components": component_counts or {}}
    op = await record_operation(db, system, "installed", actor or "system", detail)
    event = await _emit_system_event(db, system, "system.installed", detail)
    return {"operation_id": op.id, "event": event}


# ---------------------------------------------------------------------------
# v102: the update lifecycle - preview -> apply -> see changes -> accept/rollback
# ---------------------------------------------------------------------------

def _origin_of(name: str, origins: list[str]) -> str | None:
    """The pack origin of a bound name - "faq 2" traces back to "faq"
    (dataset names are globally unique; the install that came second
    owns the numbered copy, but the ORIGIN is already provided)."""
    if name in origins:
        return name
    for o in origins:
        if o and name.startswith(o + " "):
            return o
    return None


async def pack_reconcile_plan(db: AsyncSession, system: Py8nSystem,
                              pack: dict) -> dict:
    """The shared pre-scan behind preview AND upgrade (zero drift): what
    the pack offers vs what the system already binds. Pure read - the
    preview endpoint answers from this and so does the upgrade itself,
    so "what will change" and "what changed" can never disagree."""
    pack_wf_names = [str(w.get("name") or "").strip() for w in pack.get("workflows", [])]
    pack_ds_names = [str(d.get("name") or "").strip() for d in pack.get("datasets", [])]

    # resolve the names of what the system already binds
    from ..models import Dataset as _DS
    from ..models import Workflow as _WF
    bound_wf_names: set[str] = set()
    bound_ds_names: set[str] = set()
    for kind, model, acc in (("workflow", _WF, bound_wf_names), ("dataset", _DS, bound_ds_names)):
        ref_ids = [c.ref_id for c in system.components or [] if c.kind == kind]
        if ref_ids:
            rows = (await db.execute(select(model).where(model.id.in_(ref_ids)))).scalars().all()
            acc.update(r.name for r in rows if r is not None)

    wf_missing = [n for n in pack_wf_names if n and n not in bound_wf_names]
    # dataset names are globally unique - normalize every bound name back
    # to its pack origin before the missing check (same rule as ever)
    bound_ds_origins = {_origin_of(n, pack_ds_names) or n for n in bound_ds_names}
    ds_missing = [n for n in pack_ds_names if n and n not in bound_ds_origins]
    return {
        "pack_workflows": pack_wf_names,
        "pack_datasets": pack_ds_names,
        "wf_missing": wf_missing,
        "ds_missing": ds_missing,
    }


async def last_update_operation(db: AsyncSession, system: Py8nSystem) -> SystemOperation | None:
    """The most recent op of the upgrade trail (upgraded / accepted /
    rolled back) - the update lifecycle reads the trail, it invents
    nothing."""
    return (
        await db.execute(
            select(SystemOperation)
            .where(SystemOperation.system_id == system.id,
                   SystemOperation.verb.in_(("upgraded", "upgrade_accepted",
                                             "upgrade_rolled_back")))
            .order_by(SystemOperation.created_at.desc(), SystemOperation.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def pending_update(db: AsyncSession, system: Py8nSystem) -> dict | None:
    """The changes an UPGRADE put on the table that no human has ruled on
    yet: the latest trail op is an ``upgraded`` that actually ADDED
    bindings. Everything else (idempotent upgrade, accepted, rolled
    back) is settled history."""
    op = await last_update_operation(db, system)
    if op is None or op.verb != "upgraded":
        return None
    added_refs = (op.detail or {}).get("added_refs") or {}
    if not any(added_refs.values()):
        return None
    return {
        "operation_id": op.id,
        "added_refs": added_refs,
        "created_at": _iso(op.created_at),
    }


async def preview_update(db: AsyncSession, system: Py8nSystem) -> dict:
    """"What's going to change if I upgrade?" - answered WITHOUT touching
    anything. The same reconcile plan the upgrade runs, read-only; the
    pending changeset (an applied-but-unruled upgrade) rides along so
    the surface can offer Accept / Rollback in the same breath."""
    slug = system.source_solution_slug
    pending = await pending_update(db, system)
    if not slug:
        return {
            "updatable": False, "path": "none", "pending": pending,
            "note": ("this system was not installed from a solution or operator - "
                     "it was built by hand, so there is no pack to update from"),
        }
    if slug.startswith("operator:"):
        operator = slug.split(":", 1)[1]
        return {
            "updatable": False, "path": "operator_reinstall", "operator": operator,
            "pending": pending,
            "note": (f"this system was installed from the OPERATOR {operator!r} - "
                     "operators upgrade by reinstalling from the operators shelf "
                     "(a re-install builds a fresh system; components are never "
                     "swapped under a running one)"),
        }
    sol = (await db.execute(select(Solution).where(Solution.slug == slug))).scalar_one_or_none()
    if sol is None:
        return {
            "updatable": False, "path": "solution", "solution": slug,
            "pending": pending,
            "note": f"solution {slug!r} no longer exists - there is nothing to update from",
        }
    plan = await pack_reconcile_plan(db, system, sol.pack_json or {})
    wf_missing, ds_missing = plan["wf_missing"], plan["ds_missing"]
    pack_wf, pack_ds = plan["pack_workflows"], plan["pack_datasets"]
    adds = {"workflow": wf_missing, "dataset": ds_missing}
    already = {
        "workflow": max(0, len([n for n in pack_wf if n]) - len(wf_missing)),
        "dataset": max(0, len([n for n in pack_ds if n]) - len(ds_missing)),
    }
    idempotent = not wf_missing and not ds_missing
    return {
        "updatable": True, "path": "solution", "solution": slug,
        "adds": adds, "already_bound": already, "idempotent": idempotent,
        "pending": pending,
        "note": ("everything the solution's pack offers is already bound - "
                 "re-applying would change nothing" if idempotent else
                 "re-applying the pack binds the new objects; same-name "
                 "objects are never rewritten under a running system"),
    }


async def accept_update(db: AsyncSession, system: Py8nSystem, *,
                        actor: str = "") -> dict:
    """Rule on a pending upgrade: the changes are accepted and become
    settled history (the audit trail says WHO accepted)."""
    pending = await pending_update(db, system)
    if pending is None:
        raise SystemRuntimeError(
            "nothing to accept - there is no applied-but-unruled upgrade here")
    detail = {"upgrade_operation_id": pending["operation_id"],
              "accepted": pending["added_refs"]}
    op = await record_operation(db, system, "upgrade_accepted", actor or "system", detail)
    event = await _emit_system_event(db, system, "system.update_accepted", {
        "operation_id": op.id, "upgrade_operation_id": pending["operation_id"],
    })
    return {"accepted": True, "upgrade_operation_id": pending["operation_id"],
            "operation_id": op.id, "event": event}


async def rollback_update(db: AsyncSession, system: Py8nSystem, *,
                          actor: str = "") -> dict:
    """Undo a pending upgrade: unbind exactly what it bound. The imported
    objects themselves stay in the estate, unbound (they may hold data;
    deleting them is the owner's separate, explicit call) - the system
    returns to the shape it had before the upgrade."""
    pending = await pending_update(db, system)
    if pending is None:
        raise SystemRuntimeError(
            "nothing to roll back - there is no applied-but-unruled upgrade here")
    unbound: list[dict] = []
    existing = {(c.kind, c.ref_id): c for c in system.components or []}
    for kind, refs in (pending["added_refs"] or {}).items():
        for item in refs or []:
            ref_id = item.get("id") if isinstance(item, dict) else item
            comp = existing.get((kind, ref_id))
            if comp is not None:
                await db.delete(comp)
                unbound.append({"kind": kind, "ref_id": ref_id,
                                "name": item.get("name") if isinstance(item, dict) else None})
    await db.flush()
    detail = {"upgrade_operation_id": pending["operation_id"], "unbound": unbound,
              "note": ("the imported objects stay in the estate, unbound - "
                       "retire them separately if nobody wants them")}
    op = await record_operation(db, system, "upgrade_rolled_back", actor or "system", detail)
    event = await _emit_system_event(db, system, "system.update_rolled_back", {
        "operation_id": op.id, "upgrade_operation_id": pending["operation_id"],
        "unbound": len(unbound),
    })
    return {"rolled_back": True, "unbound": unbound,
            "upgrade_operation_id": pending["operation_id"],
            "operation_id": op.id, "event": event}


async def upgrade_from_solution(db: AsyncSession, system: Py8nSystem, *,
                                actor: str = "") -> dict:
    """Re-apply the source solution's pack and reconcile the components.

    A pre-scan decides the honest path: when every workflow name and
    dataset name the pack offers is ALREADY bound, the import is skipped
    entirely (an idempotent upgrade leaves zero orphan rows). When
    something is genuinely new, the pack imports and only the new-name
    objects bind - same-name objects arrive as spare rows and are
    reported, never silently swapped under a running system.
    """
    slug = system.source_solution_slug
    if not slug:
        raise SystemRuntimeError(
            "this system was not installed from a solution - upgrade needs "
            "source_solution_slug (install with as_system=true)")
    if slug.startswith("operator:"):
        raise SystemRuntimeError(
            f"this system was installed from the OPERATOR {slug.split(':', 1)[1]!r} - "
            "operators upgrade by reinstalling from the operators shelf (a re-install "
            "builds a fresh system; components are never swapped under a running one)")
    sol = (await db.execute(select(Solution).where(Solution.slug == slug))).scalar_one_or_none()
    if sol is None:
        raise SystemRuntimeError(f"solution {slug!r} no longer exists - cannot upgrade")

    pack = sol.pack_json or {}
    # the SHARED pre-scan (the preview endpoint answers from the very
    # same plan - "what will change" and "what changed" never disagree)
    plan = await pack_reconcile_plan(db, system, pack)
    pack_wf_names = plan["pack_workflows"]
    pack_ds_names = plan["pack_datasets"]
    wf_missing = plan["wf_missing"]
    ds_missing = plan["ds_missing"]
    existing = {(c.kind, c.ref_id) for c in system.components or []}

    system.upgraded_at = _now()
    sol.installs = int(sol.installs or 0) + 1

    if not wf_missing and not ds_missing:
        # idempotent upgrade: everything the pack offers is already here -
        # importing would only litter the estate with orphan copies
        detail = {
            "solution": slug,
            "added": {"workflow": 0, "dataset": 0},
            "already_bound": {"workflow": len([n for n in pack_wf_names if n]),
                              "dataset": len([n for n in pack_ds_names if n])},
            "imports_skipped": True,
            "same_name_left_untouched": [],
            "note": ("everything the solution's pack offers is already bound - "
                     "nothing imported, nothing rewritten"),
        }
        op = await record_operation(db, system, "upgraded", actor, detail)
        event = await _emit_system_event(db, system, "system.upgraded", {
            "operation_id": op.id, "added": {"workflow": 0, "dataset": 0},
            "imports_skipped": True,
        })
        return {"solution": slug, **detail, "operation_id": op.id, "event": event}

    # the same install machinery the marketplace door uses (final names,
    # validated graphs) - imported objects are NEW rows by design
    from ..api.packs import PackDocument, _import_pack_doc
    from .solutions import finalize_pack_dataset_names, finalize_pack_model_names

    owner = system.owner_id
    pack_dict = await finalize_pack_dataset_names(db, pack)
    pack_dict = await finalize_pack_model_names(db, pack_dict, owner)
    pack_doc = PackDocument.model_validate(pack_dict)
    result = await _import_pack_doc(pack_doc, owner, db)

    added: dict[str, list] = {"workflow": [], "dataset": []}
    same_name_left: list[dict] = []

    for item in result.get("datasets", []):
        ref_id, name = item["id"], str(item.get("name") or "")
        if ("dataset", ref_id) in existing:
            continue
        origin = _origin_of(name, pack_ds_names)
        if origin is not None and origin in ds_missing:
            db.add(SystemComponent(system_id=system.id, kind="dataset", ref_id=ref_id))
            added["dataset"].append({"id": ref_id, "name": name})
        else:
            same_name_left.append({"kind": "dataset", "name": name, "ref_id": ref_id})
    for item in result.get("workflows", []):
        ref_id, name = item["id"], str(item.get("name") or "")
        if ("workflow", ref_id) in existing:
            continue
        if name in wf_missing:
            db.add(SystemComponent(system_id=system.id, kind="workflow", ref_id=ref_id))
            added["workflow"].append({"id": ref_id, "name": name})
        else:
            same_name_left.append({"kind": "workflow", "name": name, "ref_id": ref_id})
    await db.flush()

    detail = {
        "solution": slug,
        "added": {k: len(v) for k, v in added.items()},
        "added_refs": {k: v for k, v in added.items() if v},
        "same_name_left_untouched": same_name_left,
        "note": ("py8n does not rewrite a live system - same-name objects are "
                 "imported as spare rows and left unbound; rebind or retire manually"),
    }
    op = await record_operation(db, system, "upgraded", actor, detail)
    event = await _emit_system_event(db, system, "system.upgraded", {
        "operation_id": op.id,
        "added": {k: len(v) for k, v in added.items()},
        "same_name_left": len(same_name_left),
    })
    return {"solution": slug, **detail, "operation_id": op.id, "event": event}


# ---------------------------------------------------------------------------
# runtime state + metrics - derived, nothing stored
# ---------------------------------------------------------------------------

async def system_state(db: AsyncSession, system: Py8nSystem) -> dict:
    """The runtime snapshot: the gate, the workflows it holds, the live
    interactions the bound components are carrying right now."""
    comps = list(system.components or [])
    wf_ids = [c.ref_id for c in comps if c.kind == "workflow"]
    agent_ids = [c.ref_id for c in comps if c.kind == "voice_agent"]
    queue_ids = [c.ref_id for c in comps if c.kind == "queue"]
    meeting_ids = [c.ref_id for c in comps if c.kind == "meeting"]

    workflows = {"bound": len(wf_ids), "active": 0, "gate_blocked": 0}
    if wf_ids:
        rows = (await db.execute(
            select(Workflow.id, Workflow.is_active).where(Workflow.id.in_(wf_ids))
        )).all()
        workflows["active"] = sum(1 for _, a in rows if a)
        gate = await lifecycle_gate_map(db, wf_ids)
        workflows["gate_blocked"] = sum(1 for wf_id in wf_ids if gate.get(wf_id, False))

    last_exec_at = None
    if wf_ids:
        row = (await db.execute(
            select(ExecutionLog.started_at)
            .where(ExecutionLog.workflow_id.in_(wf_ids))
            .order_by(ExecutionLog.started_at.desc())
            .limit(1)
        )).first()
        last_exec_at = row[0] if row else None

    live = {"active_calls": 0, "waiting_in_queues": 0, "live_meetings": 0}
    if agent_ids:
        # the agent binding rides the session's context JSON (v71: sessions
        # COPY the agent config at creation), so the lookup is a json_extract
        from sqlalchemy import func

        live["active_calls"] = len((
            await db.execute(
                select(VoiceSession.id).where(
                    func.json_extract(
                        VoiceSession.context, "$.voice_agent.voice_agent_id"
                    ).in_(agent_ids),
                    VoiceSession.state.in_(("active", "in_progress", "on_hold")),
                )
            )).scalars().all())
    if queue_ids:
        live["waiting_in_queues"] = len((
            await db.execute(
                select(ChannelQueueEntry.id).where(
                    ChannelQueueEntry.queue_id.in_(queue_ids),
                    ChannelQueueEntry.status == "waiting",
                )
            )).scalars().all())
    if meeting_ids:
        live["live_meetings"] = len((
            await db.execute(
                select(VoiceMeeting.id).where(
                    VoiceMeeting.id.in_(meeting_ids),
                    VoiceMeeting.state == "active",
                )
            )).scalars().all())

    ops = (await db.execute(
        select(SystemOperation).where(SystemOperation.system_id == system.id)
        .order_by(SystemOperation.created_at.desc()).limit(1)
    )).scalar_one_or_none()

    lifecycle = system.lifecycle or "running"
    note = {
        "running": "the gate is open - reactive paths (events, schedules, webhooks) flow",
        "paused": "the gate holds the system's workflows on every reactive path; "
                  "resume reopens, is_active flags are untouched",
        "stopped": "the gate holds the system's workflows on every reactive path; "
                   "manual runs stay open as the builder's debugging door",
    }[lifecycle]

    return {
        "lifecycle": lifecycle,
        "gate": {"holds_workflows": workflows["gate_blocked"], "note": note},
        "workflows": workflows,
        "live": live,
        "components": len(comps),
        "last_execution_at": _iso(last_exec_at),
        "last_operation_at": _iso(ops.created_at) if ops else None,
        "last_operation_verb": ops.verb if ops else None,
        "source_solution_slug": system.source_solution_slug,
        "generated_at": _now().isoformat(),
    }


async def system_metrics(db: AsyncSession, system: Py8nSystem, hours: int = 24) -> dict:
    """Derived counters over a window: executions by status on the bound
    workflows, the system's own lifecycle events, the operations count."""
    hours = max(1, min(int(hours or 24), 24 * 30))
    since = _now() - timedelta(hours=hours)
    comps = list(system.components or [])
    wf_ids = [c.ref_id for c in comps if c.kind == "workflow"]

    executions = {"total": 0, "success": 0, "error": 0, "running": 0, "other": 0}
    if wf_ids:
        rows = (await db.execute(
            select(ExecutionLog.status)
            .where(ExecutionLog.workflow_id.in_(wf_ids), ExecutionLog.started_at >= since)
        )).scalars().all()
        executions["total"] = len(rows)
        for s in rows:
            if s in ("success", "error", "running"):
                executions[s] += 1
            else:
                executions["other"] += 1

    lifecycle_events = (await db.execute(
        select(SystemEvent.type, SystemEvent.id)
        .where(SystemEvent.correlation_id == system.id, SystemEvent.created_at >= since)
    )).all()
    events_by_type: dict[str, int] = {}
    for t, _ in lifecycle_events:
        events_by_type[t] = events_by_type.get(t, 0) + 1

    ops_count = len((await db.execute(
        select(SystemOperation.id).where(
            SystemOperation.system_id == system.id,
            SystemOperation.created_at >= since,
        )
    )).scalars().all())

    failure_rate = round(executions["error"] / executions["total"] * 100, 1) if executions["total"] else 0.0
    return {
        "window_hours": hours,
        "since": since.isoformat(),
        "executions": {**executions, "failure_rate": failure_rate},
        "events": {"total": len(lifecycle_events), "by_type": events_by_type},
        "operations": ops_count,
        "generated_at": _now().isoformat(),
    }


async def system_scoped_events(db: AsyncSession, system: Py8nSystem, limit: int = 100) -> list[dict]:
    """The system's event view: lifecycle events on the system's
    correlation thread + component events whose target IS a bound object.
    Matched honestly (correlation or target binding) - never guessed."""
    limit = max(1, min(int(limit or 100), events_svc.MAX_LIST))
    bound_refs = {c.ref_id for c in system.components or []}
    rows = (
        await db.execute(
            select(SystemEvent)
            .where(SystemEvent.owner_id == system.owner_id)
            .order_by(SystemEvent.created_at.desc(), SystemEvent.id.desc())
            .limit(events_svc.MAX_LIST)
        )
    ).scalars().all()
    out = []
    for r in rows:
        if r.correlation_id == system.id or r.target_id in bound_refs:
            out.append(events_svc.event_out(r))
        if len(out) >= limit:
            break
    return out
