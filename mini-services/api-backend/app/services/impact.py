"""Impact & lineage intelligence (v55) - "what breaks if this changes?".

The catalog already answers WHO produces and consumes a dataset. The
impact engine answers WHAT IS DOWNSTREAM: every active workflow whose
nodes reference the dataset, every dashboard that charts it, every app
bound to it, every model trained on it - and, one hop further, the
datasets those consumer workflows WRITE (the customer -> customer_360 ->
churn-model chain from the platform thesis).

Everything is DERIVED from live graphs and registries - nothing stored,
so impact can never go stale. Consumers are matched exactly the way the
engine itself resolves datasets at runtime (id, name, case-insensitive
name, view-name form), so a workflow that reads "Customers" shows up as
impacted by changes to the dataset called "Customers".

Risk ranking: a trained ML model silently degrades before anyone
notices, so models rank above apps, dashboards and workflows;
sensitivity tiers (critical > high > ...) bump the headline severity.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import App, Dashboard, Dataset, TrainedModel, Workflow

_CONSUMER_NODE_TYPES = {"dataset_read", "dataset_write", "sql_query", "dataset_export"}
_WRITER_NODE_TYPES = {"dataset_write"}

RISK_ORDER = {"model": 0, "app": 1, "dashboard": 2, "workflow": 3}
SENSITIVITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, None: 4}


def _view_names(ds: Dataset) -> set[str]:
    """Every string a workflow might use to reference this dataset."""
    view = "".join(ch if ch.isalnum() else "_" for ch in ds.name.lower())
    return {ds.id, ds.name, ds.name.lower(), view}


def _graph_mentions(graph: dict | None, names: set[str], node_types: set[str] | None = None) -> list[dict]:
    """Nodes in a graph whose params/SQL reference any of the names."""
    hits: list[dict] = []
    for node in (graph or {}).get("nodes", []):
        if node_types is not None and node.get("type") not in node_types:
            continue
        params = node.get("parameters") or {}
        blob = " ".join(str(v) for v in params.values()).lower()
        sql = str((node.get("parameters") or {}).get("sql") or "").lower()
        haystack = f"{blob} {sql}"
        if any(str(n).lower() in haystack for n in names):
            hits.append({"node_id": node.get("id"), "node_name": node.get("name"), "type": node.get("type")})
    return hits


async def compute_impact(db: AsyncSession, ds: Dataset) -> dict:
    """Full impact report for one dataset - derived, never stored.

    Shape::

        {
          "dataset": {...},
          "workflows":   [{id, name, ref, nodes, active}],
          "dashboards":  [{id, name, slug, ref, components}],
          "apps":        [{id, name, slug, ref, status}],
          "models":      [{id, name, version, algorithm, task, active, ref}],
          "downstream_datasets": [{id, name, ref}],   # written by consumers
          "totals": {"workflows": n, "dashboards": n, "apps": n, "models": n,
                     "downstream_datasets": n, "affected": n},
          "highest_risk": {...} | None,
          "severity": "critical|high|medium|low",
        }
    """
    names = _view_names(ds)

    # --- consumer workflows (active graphs, engine-resolution matching) ----
    workflows = (
        (await db.execute(select(Workflow).where(Workflow.is_active.is_(True)))).scalars().all()
    )
    wf_hits: list[dict] = []
    downstream_names: dict[str, set[str]] = {}
    for wf in workflows:
        nodes = _graph_mentions(wf.graph, names)
        if not nodes:
            continue
        wf_hits.append({
            "id": wf.id,
            "name": wf.name,
            "ref": f"/workflows/{wf.id}",
            "nodes": [n.get("node_name") or n.get("node_id") for n in nodes][:8],
            "node_count": len(nodes),
            "active": bool(wf.is_active),
        })
        # one hop further: what does this consumer WRITE? (any writer node in
        # a consumer workflow feeds a downstream dataset - except writes back
        # to the dataset itself)
        for node in (wf.graph or {}).get("nodes", []):
            if node.get("type") not in _WRITER_NODE_TYPES:
                continue
            params = node.get("parameters") or {}
            target = str(params.get("dataset") or "").strip()
            if target and target.lower() not in {str(x).lower() for x in names}:
                downstream_names.setdefault(target, set()).add(wf.name)

    # resolve downstream names/ids to real datasets
    downstream: list[dict] = []
    if downstream_names:
        wanted = {n.lower() for n in downstream_names}
        candidates = (
            (await db.execute(select(Dataset))).scalars().all()
        )
        for cand in candidates:
            if cand.id == ds.id:
                continue
            cand_names = {cand.name.lower(), "".join(ch if ch.isalnum() else "_" for ch in cand.name.lower())}
            if cand_names & wanted:
                producers = sorted(downstream_names.get(cand.name, set()) | downstream_names.get(cand.name.lower(), set()))
                downstream.append({
                    "id": cand.id,
                    "name": cand.name,
                    "ref": f"/datasets/{cand.id}",
                    "via": producers[:4],
                })

    # --- dashboards charting this dataset ----------------------------------
    dashboards = (await db.execute(select(Dashboard))).scalars().all()
    dash_hits: list[dict] = []
    for d in dashboards:
        comps = [
            c for c in (d.config or {}).get("components", [])
            if isinstance(c, dict) and c.get("dataset_id") == ds.id
        ]
        if comps:
            dash_hits.append({
                "id": d.id,
                "name": d.name,
                "slug": d.slug,
                "ref": f"/dashboards/{d.id}",
                "components": len(comps),
                "status": d.status,
            })

    # --- apps bound to this dataset ----------------------------------------
    apps = (
        (await db.execute(select(App).where(App.dataset_id == ds.id))).scalars().all()
    )
    app_hits = [
        {
            "id": a.id,
            "name": a.name,
            "slug": a.slug,
            "ref": f"/apps/{a.id}",
            "status": a.status,
        }
        for a in apps
    ]

    # --- models trained on this dataset (matched by name, as model_train does)
    models = (await db.execute(select(TrainedModel))).scalars().all()
    model_hits = [
        {
            "id": m.id,
            "name": m.name,
            "version": m.version,
            "algorithm": m.algorithm,
            "task": m.task,
            "active": bool(m.active),
            "ref": "/models",
        }
        for m in models
        if m.dataset_name and str(m.dataset_name).strip().lower() in {str(n).lower() for n in names if n != ds.id}
    ]

    affected = len(wf_hits) + len(dash_hits) + len(app_hits) + len(model_hits)

    def _risk(entry: dict, kind: str) -> tuple[int, int]:
        return (RISK_ORDER[kind], -SENSITIVITY_ORDER.get(ds.sensitivity, 4))

    candidates: list[tuple[tuple[int, int], str, dict]] = []
    candidates += [(_risk(m, "model"), "model", m) for m in model_hits]
    candidates += [(_risk(a, "app"), "app", a) for a in app_hits]
    candidates += [(_risk(d, "dashboard"), "dashboard", d) for d in dash_hits]
    candidates += [(_risk(w, "workflow"), "workflow", w) for w in wf_hits]
    candidates.sort(key=lambda x: x[0])
    highest_risk = None
    if candidates:
        _, kind, entry = candidates[0]
        highest_risk = {"kind": kind, **entry}

    severity = "low"
    if affected:
        sensitivity = (ds.sensitivity or "low").lower()
        if model_hits:
            severity = "critical" if sensitivity in ("critical", "high") else "high"
        elif app_hits or dash_hits:
            severity = "high" if sensitivity in ("critical", "high") else "medium"
        else:
            severity = "medium" if sensitivity in ("critical", "high") else "low"

    return {
        "dataset": {
            "id": ds.id,
            "name": ds.name,
            "ref": f"/datasets/{ds.id}",
            "owner_id": ds.owner_id,
            "steward": ds.steward,
            "domain": ds.domain,
            "sensitivity": ds.sensitivity,
            "classification": ds.classification,
        },
        "workflows": wf_hits,
        "dashboards": dash_hits,
        "apps": app_hits,
        "models": model_hits,
        "downstream_datasets": downstream,
        "totals": {
            "workflows": len(wf_hits),
            "dashboards": len(dash_hits),
            "apps": len(app_hits),
            "models": len(model_hits),
            "downstream_datasets": len(downstream),
            "affected": affected,
        },
        "highest_risk": highest_risk,
        "severity": severity,
    }
