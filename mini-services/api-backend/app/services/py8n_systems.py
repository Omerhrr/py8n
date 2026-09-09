"""Py8n Systems (v61) - workflows, datasets, apps, dashboards, models and
reports bound into ONE operating unit.

The roadmap's endpoint of the platform arc: users stop creating
workflows and start creating systems. A system is a curated grouping
(membership is stored, like folders), but everything it REPORTS is
derived at read time - health, activity and delivery outcomes come from
the member objects themselves, so a system summary can never drift from
reality.

Attach validation resolves every reference against the live table with
owner scoping, so a system can never hold a foreign or nonexistent
object. The health rollup reuses the same primitives the Operations
Center uses (v50 dataset health, v53 pipeline rollups, v52 delivery
outcomes) scoped to the member ids.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models import (
    App,
    BusinessProcess,
    ChannelQueue,
    Dashboard,
    Dataset,
    ExecutionLog,
    ModelSystem,
    Py8nSystem,
    ReportDeliveryEvent,
    ScheduledReport,
    SystemComponent,
    TrainedModel,
    VoiceAgent,
    VoiceCampaign,
    VoiceMeeting,
    Workflow,
)
from .health import compute_health

# v81: the interaction layer joins the estate - a system covers
# Interactions (agents, waiting rooms, rooms), not just data plumbing.
COMPONENT_KINDS = ("workflow", "dataset", "app", "dashboard", "model", "report", "model_system",
                   "voice_agent", "queue", "meeting", "process", "campaign")
KIND_TABLES = {
    "workflow": Workflow,
    "dataset": Dataset,
    "app": App,
    "dashboard": Dashboard,
    "model": TrainedModel,
    "report": ScheduledReport,
    "model_system": ModelSystem,  # v63: the model-building operating unit
    "voice_agent": VoiceAgent,    # v81: the phone agent
    "queue": ChannelQueue,        # v81: the channel-side waiting room
    "meeting": VoiceMeeting,      # v81: the (persistent) room
    "process": BusinessProcess,   # v84: the business state machine
    "campaign": VoiceCampaign,    # v85 fix: the sales operator has shipped kind=campaign since v83
}
HEALTH_BUDGET = 10  # datasets fully health-scored per system-health call


async def resolve_component(db: AsyncSession, kind: str, ref_id: str, user_id: str | None):
    """Resolve a (kind, ref_id) against the live table + owner scope.

    Returns the row, or raises ValueError with a user-safe message.
    """
    if kind not in COMPONENT_KINDS:
        raise ValueError(f"unknown component kind {kind!r} (allowed: {', '.join(COMPONENT_KINDS)})")
    model = KIND_TABLES[kind]
    row = await db.get(model, ref_id)
    if row is None:
        raise ValueError(f"{kind} {ref_id} not found")
    owner = getattr(row, "owner_id", None)
    if user_id and owner not in (user_id, None):
        raise ValueError(f"{kind} {ref_id} not found")  # foreign rows look nonexistent
    return row


def _slug_counts(components: list[SystemComponent]) -> dict:
    counts = {k: 0 for k in COMPONENT_KINDS}
    for c in components:
        counts[c.kind] = counts.get(c.kind, 0) + 1
    return counts


_DL_NAME_RE = ("dead letter", "dead_letter", "dead-letter", "deadletter")


def architecture_layers(system: Py8nSystem, workflows: dict[str, Workflow]) -> dict:
    """v67: the system's data architecture DERIVED from bound graphs.

    Every bound workflow's dataset_write nodes classify into medallion
    layers by target name: 'staging' -> staging (bronze), 'dead letter' ->
    dead_letter (reject lane), everything else -> curated. Nothing is
    stored; a system with no writes simply has no layers.

    ``workflows`` maps workflow_id -> the loaded Workflow row (the caller
    resolves them for the detail payload anyway).
    """
    layers: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for c in system.components or []:
        if c.kind != "workflow":
            continue
        wf = workflows.get(c.ref_id)
        graph = (wf.graph if wf is not None else None) or {}
        for node in graph.get("nodes", []):
            if node.get("type") != "dataset_write":
                continue
            target = str((node.get("parameters") or {}).get("dataset") or "")
            if not target:
                continue
            low = target.lower()
            if any(marker in low for marker in _DL_NAME_RE):
                layer = "dead_letter"
            elif "staging" in low:
                layer = "staging"
            else:
                layer = "curated"
            key = (layer, target)
            if key not in seen:
                seen.add(key)
                layers.append({
                    "layer": layer,
                    "dataset": target,
                    "mode": (node.get("parameters") or {}).get("mode") or "append",
                    "workflow_id": c.ref_id,
                    "workflow": (wf.name if wf is not None else None) or c.ref_id[:8],
                })
            # v67: a write node carrying a dead_letter_dataset param routes its
            # rejects into a quarantine dataset - that param IS the reject lane
            dl_target = str((node.get("parameters") or {}).get("dead_letter_dataset") or "")
            if dl_target and (layer, dl_target) not in seen:
                seen.add((layer, dl_target))
                layers.append({
                    "layer": "dead_letter",
                    "dataset": dl_target,
                    "mode": "quarantine",
                    "workflow_id": c.ref_id,
                    "workflow": (wf.name if wf is not None else None) or c.ref_id[:8],
                })
    order = {"staging": 0, "curated": 1, "dead_letter": 2}
    layers.sort(key=lambda l: (order.get(l["layer"], 3), l["dataset"]))
    return {
        "layers": layers,
        "staging": any(l["layer"] == "staging" for l in layers),
        "dead_letter": any(l["layer"] == "dead_letter" for l in layers),
    }


async def system_health(db: AsyncSession, system: Py8nSystem) -> dict:
    """Derived health for one system - nothing stored.

    Workflows: runs/failures over 7d + failure rate (member-scoped).
    Datasets: v50 health tiers (budget-capped profiling).
    Reports: delivery outcomes over 7d.
    Verdict: healthy / degraded / unhealthy, mirroring the ops center.
    """
    comps = list(system.components or [])
    by_kind = _slug_counts(comps)

    now = datetime.now(timezone.utc)
    d7 = now - timedelta(days=7)

    wf_ids = [c.ref_id for c in comps if c.kind == "workflow"]
    ds_ids = [c.ref_id for c in comps if c.kind == "dataset"]
    report_ids = [c.ref_id for c in comps if c.kind == "report"]
    model_system_ids = [c.ref_id for c in comps if c.kind == "model_system"]  # v63

    runs_7d = failures_7d = 0
    last_error = None
    failing: list[dict] = {}
    if wf_ids:
        rows = (
            (
                await db.execute(
                    select(ExecutionLog)
                    .where(ExecutionLog.workflow_id.in_(wf_ids), ExecutionLog.started_at >= d7)
                    .order_by(ExecutionLog.started_at.desc())
                    .limit(500)
                )
            )
            .scalars()
            .all()
        )
        runs_7d = len(rows)
        for ex in rows:
            if ex.status == "error":
                failures_7d += 1
                entry = failing.setdefault(ex.workflow_id, {"failures": 0, "last_error": None})
                entry["failures"] += 1
                if entry["last_error"] is None:
                    entry["last_error"] = (ex.error or "")[:200] or None
                if last_error is None:
                    last_error = (ex.error or "")[:200] or None
        wf_names = dict(
            (await db.execute(select(Workflow.id, Workflow.name).where(Workflow.id.in_(wf_ids)))).all()
        )
        failing_workflows = [
            {"workflow_id": wid, "name": wf_names.get(wid, wid[:8]), **entry}
            for wid, entry in failing.items()
        ]
        failing_workflows.sort(key=lambda x: x["failures"], reverse=True)
    else:
        failing_workflows = []

    datasets = {"total": len(ds_ids), "healthy": 0, "degraded": 0, "unhealthy": 0, "unscored": 0, "worst": None}
    if ds_ids:
        rows = (await db.execute(select(Dataset).where(Dataset.id.in_(ds_ids)))).scalars().all()
        for i, ds in enumerate(rows):
            if i >= HEALTH_BUDGET:
                datasets["unscored"] += 1
                continue
            try:
                h = await compute_health(db, ds)
            except Exception:
                datasets["unscored"] += 1
                continue
            datasets[h["status"]] = datasets.get(h["status"], 0) + 1
            if datasets["worst"] is None or h["score"] < datasets["worst"]["score"]:
                datasets["worst"] = {"dataset_id": ds.id, "name": ds.name, "score": h["score"], "status": h["status"]}

    deliveries = {"ok_7d": 0, "error_7d": 0}
    if report_ids:
        ev_rows = (
            await db.execute(
                select(ReportDeliveryEvent)
                .where(ReportDeliveryEvent.report_id.in_(report_ids), ReportDeliveryEvent.created_at >= d7)
            )
        ).scalars().all()
        for ev in ev_rows:
            if ev.status in ("ok", "error"):
                deliveries[f"{ev.status}_7d"] += 1

    failure_rate = round(failures_7d / runs_7d * 100, 1) if runs_7d else 0.0
    verdict = "healthy"
    if failures_7d or datasets["unhealthy"] or deliveries["error_7d"] or datasets["degraded"]:
        verdict = "degraded"
    if (runs_7d >= 5 and failure_rate >= 50) or (datasets["unhealthy"] and datasets["unhealthy"] >= datasets["healthy"] and datasets["healthy"] == 0):
        verdict = "unhealthy"

    return {
        "verdict": verdict,
        "workflows": {"bound": len(wf_ids), "runs_7d": runs_7d, "failures_7d": failures_7d,
                      "failure_rate_7d": failure_rate, "failing_workflows": failing_workflows[:5]},
        "datasets": datasets,
        "reports": {"bound": len(report_ids), **deliveries},
        "model_systems": {"bound": len(model_system_ids)},
        "generated_at": now.isoformat(),
    }


def system_summary(db_rows: Py8nSystem) -> dict:
    comps = list(db_rows.components or [])
    return {
        "id": db_rows.id,
        "name": db_rows.name,
        "description": db_rows.description,
        "icon": db_rows.icon,
        "color": db_rows.color,
        "components": _slug_counts(comps),
        "total_components": len(comps),
        # v81: the runtime identity rides every summary and card
        "lifecycle": db_rows.lifecycle or "running",
        "source_solution_slug": db_rows.source_solution_slug,
        "upgraded_at": db_rows.upgraded_at.isoformat() if db_rows.upgraded_at else None,
        "created_at": db_rows.created_at.isoformat() if db_rows.created_at else None,
    }


# ---------------------------------------------------------------------------
# v102: the ESTATE health overview - "is my business actually healthy?"
# ---------------------------------------------------------------------------

async def health_overview(db: AsyncSession, user) -> dict:
    """The estate-level answer, one row per visible system:

        SYSTEM HEALTH
        Sales       ● running    98.7% successful   2 overdue   1 failed workflow
        Finance     ● running    99.4% successful   4 overdue   0 failed
        Operations  ● attention  3 escalations

    Composed ONLY from what the platform already keeps (derived, never
    stored): workflow executions (7d) for the success rate and the failed
    workflows, open instances past their SLA on the bound processes for
    overdue, the door's unacknowledged episodes for escalations. The
    status dot is the honest roll-up:

    * ``hold``      - the lifecycle gate is closed (paused / stopped);
    * ``attention`` - something needs a human (overdue, escalations or
      a failed workflow in the window);
    * ``running``   - green across the counters.

    A system with zero runs shows ``null`` success rate - the frontend
    renders a dash, never a lying 0%.

    v104: the dot also listens to the scheduled liveness probes - a LIVE
    deployment whose latest probe got no answer moves the dot to
    ``attention`` (the system may be green inside, but its people cannot
    reach it; that IS a business problem). The deployment object carries
    the boolean (``unreachable``) so the UI can say WHY.
    """
    from .business_processes import _terminal_states
    from .system_governance import member_role

    from ..models import BusinessProcess, BusinessProcessInstance, SystemDeployment

    rows = (
        (
            await db.execute(
                select(Py8nSystem)
                .options(selectinload(Py8nSystem.components))
                .order_by(Py8nSystem.updated_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .unique()
        .all()
    )

    visible: list[tuple[Py8nSystem, str]] = []
    for s in rows:
        role = await member_role(db, s, user)
        if role is not None:
            visible.append((s, role))

    now = datetime.now(timezone.utc)
    d7 = now - timedelta(days=7)

    # one pass over every bound workflow's executions (batched - the
    # overview is a single page load, not N estate queries)
    all_wf_ids: list[str] = []
    per_system: dict[str, dict] = {}
    for s, role in visible:
        comps = list(s.components or [])
        wf_ids = [c.ref_id for c in comps if c.kind == "workflow"]
        proc_ids = [c.ref_id for c in comps if c.kind == "process"]
        per_system[s.id] = {
            "system": s, "role": role, "wf_ids": wf_ids, "proc_ids": proc_ids,
            "runs": 0, "failures": 0, "failed_wfs": set(),
        }
        all_wf_ids.extend(wf_ids)

    if all_wf_ids:
        exec_rows = (
            await db.execute(
                select(ExecutionLog.workflow_id, ExecutionLog.status, ExecutionLog.error)
                .where(ExecutionLog.workflow_id.in_(set(all_wf_ids)),
                       ExecutionLog.started_at >= d7)
            )
        ).all()
        for wf_id, status, _error in exec_rows:
            for sid, info in per_system.items():
                if wf_id in info["wf_ids"]:
                    info["runs"] += 1
                    if status == "error":
                        info["failures"] += 1
                        info["failed_wfs"].add(wf_id)
                    break

    # one pass over the open instances of every bound process
    all_proc_ids = [p for info in per_system.values() for p in info["proc_ids"]]
    proc_defs: dict[str, dict] = {}
    if all_proc_ids:
        proc_rows = (
            await db.execute(select(BusinessProcess).where(BusinessProcess.id.in_(set(all_proc_ids))))
        ).scalars().all()
        proc_defs = {p.id: (p.definition or {}) for p in proc_rows}
        inst_rows = (
            await db.execute(
                select(BusinessProcessInstance)
                .where(BusinessProcessInstance.process_id.in_(set(all_proc_ids)),
                       BusinessProcessInstance.ended_at.is_(None),
                       BusinessProcessInstance.due_at.is_not(None))
            )
        ).scalars().all()
        for inst in inst_rows:
            for sid, info in per_system.items():
                if inst.process_id in info["proc_ids"]:
                    due = inst.due_at
                    if due is None:
                        break
                    if due.tzinfo is None:
                        due = due.replace(tzinfo=timezone.utc)
                    if (now - due).total_seconds() <= 0:
                        break  # the SLA still holds
                    definition = proc_defs.get(inst.process_id) or {}
                    if inst.state in _terminal_states(definition):
                        break  # a closed entity is not asking for attention
                    info["overdue"] = info.get("overdue", 0) + 1
                    book = (inst.context or {}).get("escalations")
                    if isinstance(book, dict) and int(book.get("count") or 0) > 0 \
                            and not isinstance(book.get("acked"), dict):
                        info["escalations"] = info.get("escalations", 0) + 1
                    break

    # deployment identity rides the row (the estate wears its environments)
    dep_rows = (
        (await db.execute(select(SystemDeployment))).scalars().all()
        if per_system else []
    )
    dep_by_system = {d.system_id: d for d in dep_rows}

    # v107: the pending-update chip rides the row - which systems hold an
    # applied-but-unruled upgrade. ONE batched walk over the trail (the
    # same predicate the Updates panel applies), never N queries.
    from . import system_runtime
    pending_by_system = await system_runtime.pending_updates_batch(
        db, list(per_system.keys()))

    out: list[dict] = []
    for s, role in visible:
        info = per_system[s.id]
        lifecycle = s.lifecycle or "running"
        overdue = info.get("overdue", 0)
        escalations = info.get("escalations", 0)
        failures = info["failures"]
        dep = dep_by_system.get(s.id)
        # v104: a live domain that stopped answering is attention - the
        # newest probe's evidence moves the dot, nothing is stored
        unreachable = bool(
            dep and dep.status == "live"
            and dep.last_ping_at is not None and not dep.last_ping_ok)
        if lifecycle in ("paused", "stopped"):
            status = "hold"
        elif unreachable or overdue or escalations or failures:
            status = "attention"
        else:
            status = "running"
        success_rate = (
            round((info["runs"] - failures) / info["runs"] * 100, 1)
            if info["runs"] else None
        )
        # v107: the pending-update chip - an applied-but-unruled upgrade
        # is exactly what the Updates panel's PENDING banner names; the
        # estate row says so at a glance (added_total counts the bindings
        # the upgrade put on the table, waiting for Accept / Roll back).
        pending = pending_by_system.get(s.id)
        pending_chip = None
        if pending is not None:
            added = pending.get("added_refs") or {}
            pending_chip = {
                "operation_id": pending.get("operation_id"),
                "created_at": pending.get("created_at"),
                "added_total": sum(len(v or []) for v in added.values())
                if isinstance(added, dict) else 0,
            }
        out.append({
            **system_summary(s),
            "my_role": role,
            "status": status,
            "success_rate_7d": success_rate,
            "runs_7d": info["runs"],
            "failed_workflows_7d": len(info["failed_wfs"]),
            "overdue": overdue,
            "escalations": escalations,
            "pending_update": pending_chip,
            "deployment": ({
                "domain": dep.domain or "",
                "url": f"https://{dep.domain}" if dep.domain else "",
                "environment": dep.environment,
                "status": dep.status,
                "unreachable": unreachable,
                # v103: what the domain answered the last time somebody
                # asked (compact - the full evidence lives on the record)
                "last_ping": (
                    {"at": dep.last_ping_at.isoformat(), "ok": bool(dep.last_ping_ok),
                     "ms": dep.last_ping_ms}
                    if dep.last_ping_at is not None else None),
            } if dep else None),
        })

    counts = {
        "running": sum(1 for r in out if r["status"] == "running"),
        "attention": sum(1 for r in out if r["status"] == "attention"),
        "hold": sum(1 for r in out if r["status"] == "hold"),
    }
    return {
        "systems": out,
        "counts": counts,
        "total": len(out),
        "generated_at": now.isoformat(),
    }
