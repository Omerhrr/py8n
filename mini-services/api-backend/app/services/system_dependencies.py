"""Cross-system dependency views (v62) - derived, never stored.

Two systems depend on each other when their MEMBERS touch the same
objects. Nothing about that is persisted: every read recomputes the
graph from the live bindings and the live workflow graphs, so the view
can never drift from reality.

Edge types:

* ``shared_object`` - the same workflow/dataset/app/dashboard/model/
  report is bound to two systems (the undirected "we both hold this").
* ``data_flow``   - system A's workflow READS or WRITES a dataset bound
  to system B (node-type aware: dataset_write vs dataset_read/sql/export).
* ``model_flow``  - system A's workflow scores with a model bound to
  system B (model_predict -> registry row -> B's member).

Visibility first: a system you cannot read does not exist for this view,
and evidence pointing into foreign objects is dropped rather than leaked.
Model Systems (v63) expand one level: a system binding a model system
shares that member's refs with evidence ``via model system X``.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models import (
    Dataset,
    ModelSystem,
    Py8nSystem,
    TrainedModel,
    Workflow,
)
from .impact import _graph_mentions
from .py8n_systems import system_health

# node types that reference datasets, split by direction
_DATASET_WRITERS = {"dataset_write"}
_DATASET_READERS = {"dataset_read", "sql_query", "dataset_export"}
HEALTH_BUDGET = 20  # max systems fully health-scored per graph read
EVIDENCE_CAP = 3


async def dependency_graph(db: AsyncSession, user, system_id: str | None = None) -> dict:
    """Build the systems dependency graph for everything ``user`` can read."""
    from .system_governance import visible_system_ids

    q = (
        select(Py8nSystem)
        .options(selectinload(Py8nSystem.components))
        .order_by(Py8nSystem.created_at)
        .limit(200)
    )
    rows = (await db.execute(q)).scalars().unique().all()

    uid = getattr(user, "id", None)
    allowed = await visible_system_ids(db, uid)
    systems = [s for s in rows if allowed is None or s.owner_id in (uid, None) or s.id in set(allowed)]
    if system_id:
        systems = [s for s in systems if s.id == system_id]
    by_id = {s.id: s for s in systems}

    # ---- index: kind -> ref_id -> [(system_id, via_model_system_name)] ----
    index: dict[str, dict[str, list[tuple[str, str | None]]]] = {}
    for s in systems:
        for c in s.components or []:
            index.setdefault(c.kind, {}).setdefault(c.ref_id, []).append((s.id, None))

    # ---- v63: expand bound model systems one level ----
    ms_ids = [c.ref_id for s in systems for c in s.components or [] if c.kind == "model_system"]
    ms_names: dict[str, str] = {}
    if ms_ids:
        for ms in (await db.execute(select(ModelSystem).options(selectinload(ModelSystem.components)).where(ModelSystem.id.in_(ms_ids)))).scalars().all():
            ms_names[ms.id] = ms.name
            for s in systems:
                holder = next((c.ref_id for c in s.components or [] if c.kind == "model_system" and c.ref_id == ms.id), None)
                if holder is None:
                    continue
                for mc in ms.components or []:
                    index.setdefault(mc.kind, {}).setdefault(mc.ref_id, []).append((s.id, ms.name))

    # ---- shared_object edges (undirected: normalized pair key) ----
    edges: dict[tuple[str, str, str], list[dict]] = {}

    def _add_edge(a: str, b: str, etype: str, evidence: dict) -> None:
        if a == b:
            return
        if etype in ("data_flow", "model_flow"):
            key = (a, b, etype)
        else:
            key = (min(a, b), max(a, b), etype)
        edges.setdefault(key, []).append(evidence)

    names_cache: dict[str, str] = {}

    async def _object_name(kind: str, ref_id: str) -> str:
        if ref_id in names_cache:
            return names_cache[ref_id]
        from .py8n_systems import KIND_TABLES

        model = KIND_TABLES.get(kind)
        name = ref_id[:8]
        if model is not None:
            row = await db.get(model, ref_id)
            if row is not None and getattr(row, "name", None):
                name = row.name
        names_cache[ref_id] = name
        return name

    for kind, refs in index.items():
        for ref_id, holders in refs.items():
            holder_ids = sorted({h[0] for h in holders})
            if len(holder_ids) < 2:
                continue
            name = await _object_name(kind, ref_id)
            via = next((h[1] for h in holders if h[1]), None)
            for i in range(len(holder_ids)):
                for j in range(i + 1, len(holder_ids)):
                    _add_edge(holder_ids[i], holder_ids[j], "shared_object", {
                        "kind": kind, "name": name,
                        **({"via": via} if via else {}),
                    })

    # ---- data_flow + model_flow edges over workflows bound to systems ----
    wf_refs = index.get("workflow", {})
    ds_index = index.get("dataset", {})
    model_index = index.get("model", {})
    for wf_id, holders in wf_refs.items():
        wf = await db.get(Workflow, wf_id)
        if wf is None or not wf.graph:
            continue
        wf_name = wf.name

        # dataset flow: for each dataset bound to a visible system, does
        # THIS workflow's graph mention its name in write-type or
        # read-type nodes?
        for ds_id, ds_holders in ds_index.items():
            ds_row = await db.get(Dataset, ds_id)
            if ds_row is None:
                continue
            direction = None
            if _graph_mentions(wf.graph, {ds_row.name}, node_types=_DATASET_WRITERS):
                direction = "write"
            elif _graph_mentions(wf.graph, {ds_row.name}, node_types=_DATASET_READERS):
                direction = "read"
            if direction is None:
                continue
            for target_sys, via in ds_holders:
                for src_sys, _ in holders:
                    if src_sys == target_sys:
                        continue
                    _add_edge(src_sys, target_sys, "data_flow", {
                        "workflow": wf_name, "dataset": ds_row.name,
                        "direction": direction,
                        **({"via": via} if via else {}),
                    })

        # model flow: model_predict params.model -> registry row -> holders
        for node in (wf.graph or {}).get("nodes", []):
            if node.get("type") != "model_predict":
                continue
            ref = str((node.get("parameters") or {}).get("model") or "").strip()
            if not ref:
                continue
            row = (
                await db.execute(
                    select(TrainedModel).where(
                        (TrainedModel.id == ref)
                        | ((TrainedModel.name == ref) & (TrainedModel.active.is_(True)))
                    ).order_by(TrainedModel.version.desc()).limit(1)
                )
            ).scalars().first()
            if row is None:
                continue
            for target_sys, via in model_index.get(row.id, []):
                for src_sys, _ in holders:
                    if src_sys == target_sys:
                        continue
                    _add_edge(src_sys, target_sys, "model_flow", {
                        "workflow": wf_name, "model": row.name,
                        **({"via": via} if via else {}),
                    })

    # ---- nodes with derived verdicts (budget-capped) ----
    nodes = []
    for i, s in enumerate(systems):
        comps = list(s.components or [])
        verdict = "unscored"
        if i < HEALTH_BUDGET:
            try:
                verdict = (await system_health(db, s))["verdict"]
            except Exception:
                verdict = "unscored"
        nodes.append({
            "id": s.id,
            "name": s.name,
            "icon": s.icon,
            "color": s.color,
            "total_components": len(comps),
            "verdict": verdict,
            "owner": s.owner_id,
        })

    edge_list = [
        {
            "from": k[0],
            "to": k[1],
            "type": k[2],
            "weight": len(evs),
            "evidence": evs[:EVIDENCE_CAP],
        }
        for k, evs in sorted(edges.items())
        if k[0] in by_id and k[1] in by_id
    ]

    return {
        "nodes": nodes,
        "edges": edge_list,
        "summary": {
            "systems": len(nodes),
            "edges": len(edge_list),
            "by_type": {
                t: sum(1 for e in edge_list if e["type"] == t)
                for t in ("shared_object", "data_flow", "model_flow")
            },
        },
        "generated_at": _now_iso(),
    }


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
