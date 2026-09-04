"""Model deployments (v67) - the DEPLOY verb made first-class.

Training a model lands it in the registry; DEPLOYING it turns that row
into a live serving endpoint. A deployment is a small stored handle (the
pairing of registry row + serving workflow + environment) while
everything it REPORTS is derived:

* the serving workflow is GENERATED from the model's family - a webhook
  trigger wired to ``lm_generate`` for language models (prompt comes in
  the POST body) or ``split_out(body.rows) -> model_predict`` for the
  sklearn/neural surface (rows come in the POST body);
* the workflow is a normal py8n workflow, created ACTIVE (it needs no
  external credentials - the trigger is the HTTP call itself), so you can
  watch its executions, edit its graph, or hang monitoring nodes off it;
* serving statistics (invocations, failures, last call) are derived from
  the execution log at read time and never stored;
* deployments PIN the registry version they were created against: train
  v2 and the endpoint still serves v1 until you deploy again - a
  redeploy is just a new deployment (the old one stays for rollback).

Environment is a label (dev | staging | prod), not infrastructure: the
sandbox has one runtime, and pretending otherwise would be a lie.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..engine.runner import validate_graph_document
from ..models import ExecutionLog, ModelDeployment, TrainedModel, Workflow
from . import models as model_svc

ENVIRONMENTS = ("dev", "staging", "prod")
LM_ALGORITHMS = {"lm_transformer"}


def _node(nid: str, ntype: str, params: dict, name: str) -> dict:
    return {"id": nid, "type": ntype, "name": name, "position": {"x": 0, "y": 0}, "parameters": params}


def _edge(eid: str, source: str, target: str) -> dict:
    return {"id": eid, "source": source, "target": target, "sourceHandle": "main", "targetHandle": "main"}


def _serving_graph(model: TrainedModel, serving_mode: str, generate_params: dict) -> dict:
    """Build the serving graph for a registry row.

    Both shapes answer the webhook SYNCHRONOUSLY (response_mode=last_node:
    the caller gets the final node's output as the HTTP response body).
    """
    nodes = [_node("trigger", "webhook_trigger", {"response_mode": "last_node"}, "Serving endpoint")]
    if serving_mode == "generate":
        params = {"model": model.id, "prompt": "{{ nodes.trigger.output.body.prompt }}"}
        params.update(generate_params)
        nodes.append(_node("serve", "lm_generate", params, f"Serve {model.name}"))
    else:
        nodes.append(_node("rows", "split_out", {"field": "body.rows", "include_meta": False}, "Rows from body"))
        nodes.append(_node("serve", "model_predict", {"model": model.id}, f"Serve {model.name}"))
    edges = [{"id": f"e{i}", "source": a, "target": b, "sourceHandle": "main", "targetHandle": "main"}
             for i, (a, b) in enumerate(zip([n["id"] for n in nodes], [n["id"] for n in nodes][1:]))]
    return {"nodes": nodes, "edges": edges}


def _body_example(serving_mode: str, model: TrainedModel) -> dict:
    if serving_mode == "generate":
        return {"prompt": "your prompt text"}
    features = list(model.features or [])
    row = {f: 0 for f in features[:6]} if features else {"feature": 0}
    return {"rows": [row]}


def deployment_out(row: ModelDeployment, wf: Workflow | None, model: TrainedModel | None,
                   stats: dict | None = None) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "serving_mode": row.serving_mode,
        "environment": row.environment,
        "enabled": bool(row.enabled),
        "notes": row.notes,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "model": ({"id": model.id, "name": model.name, "version": model.version,
                   "algorithm": model.algorithm, "task": model.task,
                   "features": model.features or []} if model is not None else
                  {"id": row.model_registry_id, "name": None, "version": None,
                   "algorithm": None, "task": None, "features": []}),
        "workflow": ({"id": wf.id, "name": wf.name, "is_active": bool(wf.is_active),
                      "webhook_path": f"/api/v1/webhooks/{wf.id}"} if wf is not None else None),
        "status": _status(row, wf),
        "stats": stats or {"runs_7d": 0, "failures_7d": 0, "last_call_at": None, "last_call_status": None},
    }


def _status(row: ModelDeployment, wf: Workflow | None) -> str:
    if not row.enabled:
        return "disabled"
    if wf is None:
        return "orphaned"  # the serving workflow was deleted under us
    return "live" if wf.is_active else "inactive"


async def _serving_stats(db: AsyncSession, workflow_id: str | None) -> dict:
    """Derived serving statistics - 7d invocations straight off the log."""
    if workflow_id is None:
        return {"runs_7d": 0, "failures_7d": 0, "last_call_at": None, "last_call_status": None}
    since = datetime.now(timezone.utc) - timedelta(days=7)
    q = (select(ExecutionLog)
         .where(ExecutionLog.workflow_id == workflow_id, ExecutionLog.started_at >= since)
         .order_by(ExecutionLog.started_at.desc()))
    runs = (await db.execute(q)).scalars().all()
    failures = sum(1 for r in runs if r.status == "error")
    last = runs[0] if runs else None
    return {
        "runs_7d": len(runs),
        "failures_7d": failures,
        "last_call_at": last.started_at.isoformat() if last else None,
        "last_call_status": last.status if last else None,
    }


async def _load_refs(db: AsyncSession, row: ModelDeployment) -> tuple[Workflow | None, TrainedModel | None]:
    wf = await db.get(Workflow, row.workflow_id) if row.workflow_id else None
    model = await db.get(TrainedModel, row.model_registry_id)
    return wf, model


async def list_deployments(db: AsyncSession, owner_id: str | None) -> list[dict]:
    q = select(ModelDeployment).order_by(ModelDeployment.created_at.desc())
    rows = (await db.execute(q)).scalars().all()
    if owner_id is not None:
        rows = [r for r in rows if r.owner_id is None or r.owner_id == owner_id]
    out = []
    for row in rows:
        wf, model = await _load_refs(db, row)
        out.append(deployment_out(row, wf, model, await _serving_stats(db, row.workflow_id)))
    return out


async def get_deployment(db: AsyncSession, deployment_id: str, owner_id: str | None) -> dict | None:
    row = await db.get(ModelDeployment, deployment_id)
    if row is None:
        return None
    if owner_id is not None and row.owner_id is not None and row.owner_id != owner_id:
        return None  # foreign deployments look nonexistent
    wf, model = await _load_refs(db, row)
    return deployment_out(row, wf, model, await _serving_stats(db, row.workflow_id))


async def create_deployment(db: AsyncSession, *, owner_id: str | None, name: str,
                            model_ref: str, environment: str = "dev",
                            notes: str = "", generate_params: dict | None = None) -> dict:
    """Resolve the registry row, generate the serving workflow, go live."""
    environment = (environment or "dev").strip().lower()
    if environment not in ENVIRONMENTS:
        raise ValueError(f"unknown environment {environment!r} (allowed: {', '.join(ENVIRONMENTS)})")
    if not name or not name.strip():
        raise ValueError("a deployment name is required")
    model = await model_svc.resolve_model(db, model_ref.strip(), owner_id=owner_id)
    if model is None:
        raise ValueError(f"model {model_ref!r} not found in the registry (train one with "
                         "model_train / neural_train / lm_train first)")
    serving_mode = "generate" if model.algorithm in LM_ALGORITHMS else "predict"

    graph = _serving_graph(model, serving_mode, generate_params or {})
    try:
        graph = validate_graph_document(graph).model_dump()
    except Exception as exc:  # noqa: BLE001 - a generated graph failing the gate is a bug, but fail honest
        raise ValueError(f"generated serving graph failed validation: {exc}") from exc

    wf = Workflow(
        name=f"{name.strip()} serving",
        description=f"Serving endpoint for {model.name} v{model.version} "
                    f"({model.algorithm}) - generated by the v67 deployment surface",
        graph=graph,
        is_active=True,  # a deployment goes live: webhook + registry need no credentials
        tags=["deployment"],
    )
    wf.owner_id = owner_id
    db.add(wf)
    await db.flush()
    await db.refresh(wf)

    row = ModelDeployment(
        name=name.strip()[:140],
        model_registry_id=model.id,
        serving_mode=serving_mode,
        environment=environment,
        workflow_id=wf.id,
        enabled=True,
        notes=(notes or "")[:500],
    )
    row.owner_id = owner_id
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return deployment_out(row, wf, model, await _serving_stats(db, row.workflow_id))


async def toggle_deployment(db: AsyncSession, row: ModelDeployment) -> dict:
    """Enable/disable - the serving workflow follows the deployment's state."""
    row.enabled = not bool(row.enabled)
    wf = await db.get(Workflow, row.workflow_id) if row.workflow_id else None
    if wf is not None:
        wf.is_active = row.enabled
        db.add(wf)
    db.add(row)
    await db.flush()
    await db.refresh(row)
    wf, model = await _load_refs(db, row)
    return deployment_out(row, wf, model, await _serving_stats(db, row.workflow_id))


async def delete_deployment(db: AsyncSession, row: ModelDeployment) -> dict:
    """Retire the deployment. The serving workflow SURVIVES ( deactivated)
    so its execution history stays browsable - members outlive containers."""
    wf = await db.get(Workflow, row.workflow_id) if row.workflow_id else None
    if wf is not None:
        wf.is_active = False
        db.add(wf)
    payload = {"id": row.id, "name": row.name,
               "workflow_id": row.workflow_id,
               "workflow_deactivated": wf is not None}
    await db.delete(row)
    await db.flush()
    return payload
