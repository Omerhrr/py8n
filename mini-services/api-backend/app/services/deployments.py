"""Model deployments (v67, deepened in v68) - the DEPLOY verb made first-class.

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
* deployments PIN the registry version they were created against.

v68 adds the three operations a live endpoint needs to survive contact
with production traffic:

* **Serving tokens** - mint-once/revoke credentials scoped to ONE
  deployment. A deployment with >=1 active token demands
  ``Authorization: Bearer py8nd_...`` (or ``X-Deployment-Token``) on every
  call; zero active tokens keeps the v67 open-endpoint behavior. Hashes
  only at rest, timing-safe comparisons, per-token last_used tracking.
* **Redeploy / rollback** - an append-only revision ledger records every
  registry version the endpoint has pointed at; redeploying patches the
  serving workflow's model parameter IN PLACE (same URL, new weights) and
  rollback re-activates an older ledger entry. Same URL across versions
  is the whole point: clients never chase a moving endpoint.
* **SSE streaming** - ``POST /deployments/{id}/stream`` samples the LM
  token by token and emits Server-Sent Events (meta -> token* -> done),
  so callers render the first token while the rest are still sampling.
  The shared sampler drives both the numpy and torch cores.

Environment is a label (dev | staging | prod), not infrastructure: the
sandbox has one runtime, and pretending otherwise would be a lie.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..engine.runner import validate_graph_document
from ..models import (DeploymentRevision, DeploymentToken,
                      DeploymentTokenPolicy, ExecutionLog, ModelDeployment,
                      TrainedModel, Workflow)
from . import serving_limits
from . import models as model_svc

ENVIRONMENTS = ("dev", "staging", "prod")
LM_ALGORITHMS = {"lm_transformer"}

TOKEN_PREFIX = "py8nd_"
_PREFIX_DISPLAY_LEN = 13  # e.g. py8nd_ab12cd34


# ---------------------------------------------------------------------------
# Serving tokens (v68)
# ---------------------------------------------------------------------------

def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def token_out(row: DeploymentToken, policy: DeploymentTokenPolicy | None = None) -> dict:
    out = {
        "id": row.id,
        "name": row.name,
        "prefix": row.prefix,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "revoked": row.revoked_at is not None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        # v69: the token's traffic policy + live usage (None = unlimited);
        # v70: usage reads the SHARED hit table - every worker's truth
        "limits": {"rate_per_min": policy.rate_per_min if policy else None,
                   "daily_quota": policy.daily_quota if policy else None},
        "usage": await serving_limits.usage_snapshot(row.id, policy),
    }
    return out


async def list_tokens(db: AsyncSession, deployment_id: str) -> list[dict]:
    q = (select(DeploymentToken)
         .where(DeploymentToken.deployment_id == deployment_id)
         .order_by(DeploymentToken.created_at.desc()))
    rows = (await db.execute(q)).scalars().all()
    ids = [r.id for r in rows]
    policies: dict[str, DeploymentTokenPolicy] = {}
    if ids:
        for p in (await db.execute(
                select(DeploymentTokenPolicy)
                .where(DeploymentTokenPolicy.token_id.in_(ids)))).scalars().all():
            policies[p.token_id] = p
    return [await token_out(r, policies.get(r.id)) for r in rows]


async def active_token_count(db: AsyncSession, deployment_id: str) -> int:
    q = (select(DeploymentToken)
         .where(DeploymentToken.deployment_id == deployment_id,
                DeploymentToken.revoked_at.is_(None)))
    return len((await db.execute(q)).scalars().all())


async def mint_token(db: AsyncSession, *, owner_id: str | None, deployment_id: str,
                     name: str, rate_per_min: int | None = None,
                     daily_quota: int | None = None) -> dict:
    """Create a serving token. The raw value is returned EXACTLY once.

    v69: optional rate-shaping/quotas ride along - a policy row is created
    when either limit is set; without one the token stays unlimited.
    """
    if not name or not name.strip():
        raise ValueError("a token name is required")
    for label, value in (("rate_per_min", rate_per_min), ("daily_quota", daily_quota)):
        if value is not None and value < 1:
            raise ValueError(f"{label} must be >= 1 (or null for unlimited)")
    raw = TOKEN_PREFIX + secrets.token_urlsafe(24)
    row = DeploymentToken(
        deployment_id=deployment_id,
        name=name.strip()[:120],
        prefix=raw[:_PREFIX_DISPLAY_LEN],
        key_hash=_hash_token(raw),
    )
    row.owner_id = owner_id
    db.add(row)
    await db.flush()
    await db.refresh(row)
    policy = None
    if rate_per_min is not None or daily_quota is not None:
        policy = DeploymentTokenPolicy(token_id=row.id, rate_per_min=rate_per_min,
                                       daily_quota=daily_quota)
        db.add(policy)
        await db.flush()
    out = await token_out(row, policy)
    out["token"] = raw  # shown once
    return out


async def revoke_token(db: AsyncSession, deployment_id: str, token_id: str) -> dict | None:
    row = await db.get(DeploymentToken, token_id)
    if row is None or row.deployment_id != deployment_id:
        return None
    if row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        db.add(row)
        await db.flush()
    return await token_out(row)


async def check_deployment_token(db: AsyncSession, dep: ModelDeployment, request
                                 ) -> DeploymentToken | None:
    """Enforce serving-token auth for ONE deployment row (v68, v69 return).

    Used by the webhook catcher (after it resolves the workflow's
    deployment) and by the SSE stream endpoint directly. Zero active
    tokens = open endpoint (returns None); otherwise the request must
    carry the token via ``Authorization: Bearer`` or ``X-Deployment-Token``
    - timing-safe compares against stored sha256 hashes, success stamps
    last_used_at and RETURNS the matched token row so the caller can
    apply the v69 rate-shaping/quotas.

    The last_used stamp is written on its OWN short-lived session and
    committed immediately: the webhook request session must stay
    read-only here, because response_mode=last_node runs the whole flow
    INSIDE the request on separate sessions (SQLite = one writer).
    """
    tokens = (await db.execute(
        select(DeploymentToken)
        .where(DeploymentToken.deployment_id == dep.id,
               DeploymentToken.revoked_at.is_(None)))).scalars().all()
    if not tokens:
        return None  # auth off - no credentials ever minted (or all revoked)

    provided = ""
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        provided = auth_header[7:].strip()
    if not provided:
        provided = request.headers.get("x-deployment-token", "").strip()
    if not provided:
        raise PermissionError(
            "this deployment requires a serving token - pass it as "
            "'Authorization: Bearer <token>' or 'X-Deployment-Token: <token>'")

    provided_hash = _hash_token(provided)
    for tok in tokens:
        if hmac_compare(provided_hash, tok.key_hash):
            await _stamp_token_used(tok.id)
            return tok
    raise PermissionError("invalid serving token for this deployment")


async def _stamp_token_used(token_id: str) -> None:
    """last_used_at on a dedicated session - never holds the request's lock."""
    from ..db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        row = await session.get(DeploymentToken, token_id)
        if row is not None:
            row.last_used_at = datetime.now(timezone.utc)
            await session.commit()


async def check_serving_auth(db: AsyncSession, workflow_id: str, request
                             ) -> DeploymentToken | None:
    """Enforce deployment-token auth on a serving workflow's HTTP call.

    Called from the webhook catcher BEFORE the flow runs. No deployment
    for this workflow, or no active tokens -> open endpoint (v67
    behavior, returns None). Returns the matched token (v69) so the
    catcher can enforce its rate-shaping/quotas right after.
    """
    q = (select(ModelDeployment)
         .where(ModelDeployment.workflow_id == workflow_id)
         .order_by(ModelDeployment.created_at.desc()))
    dep = (await db.execute(q)).scalars().first()
    if dep is None:
        return None
    return await check_deployment_token(db, dep, request)


async def enforce_serving_limits(token: DeploymentToken | None) -> dict[str, str]:
    """Apply a token's rate-shaping/quotas (v69) and admit the request.

    Zero policy = unlimited (no headers). Raises LimitExceeded -> the
    API layers map that to 429 with Retry-After + X-RateLimit headers.
    Callers must pass the token they got from check_serving_auth /
    check_deployment_token - the limits belong to THAT credential.
    """
    if token is None:
        return {}
    from ..db import AsyncSessionLocal

    # the request session must stay read-only (single-writer SQLite), so
    # the policy read rides its own short-lived session like last_used
    async with AsyncSessionLocal() as session:
        policy = await serving_limits.policy_for_token(session, token.id)
        await session.commit()
    # v70: admit records the hit in the SHARED deployment_token_hits table
    # (its own short-lived session) - the limit one balancer-wide truth
    return await serving_limits.admit(token.id, policy)


async def set_token_limits(db: AsyncSession, deployment_id: str, token_id: str,
                           rate_per_min: int | None, daily_quota: int | None) -> dict | None:
    """Upsert a token's traffic policy (v69). None = unlimited for that axis."""
    tok = await db.get(DeploymentToken, token_id)
    if tok is None or tok.deployment_id != deployment_id:
        return None
    for label, value in (("rate_per_min", rate_per_min), ("daily_quota", daily_quota)):
        if value is not None and value < 1:
            raise ValueError(f"{label} must be >= 1 (or null for unlimited)")
    policy = await serving_limits.policy_for_token(db, token_id)
    if policy is None:
        policy = DeploymentTokenPolicy(token_id=token_id, rate_per_min=rate_per_min,
                                       daily_quota=daily_quota)
    else:
        policy.rate_per_min = rate_per_min
        policy.daily_quota = daily_quota
    db.add(policy)
    await db.flush()
    return await token_out(tok, policy)


async def get_token_usage(db: AsyncSession, deployment_id: str, token_id: str) -> dict | None:
    tok = await db.get(DeploymentToken, token_id)
    if tok is None or tok.deployment_id != deployment_id:
        return None
    policy = await serving_limits.policy_for_token(db, token_id)
    return {"token": await token_out(tok, policy),
            "usage": await serving_limits.usage_snapshot(token_id, policy)}


def hmac_compare(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a, b)


# ---------------------------------------------------------------------------
# Revisions: redeploy / rollback (v68)
# ---------------------------------------------------------------------------

def _revision_out(row: DeploymentRevision) -> dict:
    return {
        "id": row.id,
        "revision": row.revision,
        "model_registry_id": row.model_registry_id,
        "model_name": row.model_name,
        "model_version": row.model_version,
        "algorithm": row.algorithm,
        "action": row.action,
        "note": row.note,
        "active": bool(row.active),
        "deployed_at": row.deployed_at.isoformat() if row.deployed_at else None,
    }


async def list_revisions(db: AsyncSession, deployment_id: str) -> list[dict]:
    q = (select(DeploymentRevision)
         .where(DeploymentRevision.deployment_id == deployment_id)
         .order_by(DeploymentRevision.revision.desc()))
    return [_revision_out(r) for r in (await db.execute(q)).scalars().all()]


async def _patch_serving_workflow(db: AsyncSession, wf: Workflow, new_model: TrainedModel) -> None:
    """Point the generated serving workflow at a new registry row, in place."""
    graph = dict(wf.graph or {})
    nodes = [dict(n) for n in graph.get("nodes", [])]
    serve_types = {"lm_generate"} if new_model.algorithm in LM_ALGORITHMS else {"model_predict"}
    patched = 0
    for n in nodes:
        if n.get("type") in serve_types:
            params = dict(n.get("parameters") or {})
            params["model"] = new_model.id
            n["parameters"] = params
            patched += 1
        # a generate-mode workflow keeps its split_out rows node only in predict mode and vice versa:
        # redeploy keeps the SAME serving shape, so only matching-family models are allowed (checked upstream).
    if not patched:
        raise ValueError("serving workflow has no serving node to repoint - the graph was edited outside the deployment surface")
    graph["nodes"] = nodes
    wf.graph = graph
    db.add(wf)
    await db.flush()


async def _activate_revision(db: AsyncSession, *, dep: ModelDeployment, new_model: TrainedModel,
                             wf: Workflow | None, action: str, note: str) -> dict:
    """Deactivate old revisions, patch the serving graph, write the new one."""
    if wf is not None:
        await _patch_serving_workflow(db, wf, new_model)
    dep.model_registry_id = new_model.id
    db.add(dep)

    old = (await db.execute(
        select(DeploymentRevision)
        .where(DeploymentRevision.deployment_id == dep.id,
               DeploymentRevision.active.is_(True)))).scalars().all()
    for r in old:
        r.active = False
        db.add(r)
    next_rev = 1 + max((r.revision for r in old), default=0)
    row = DeploymentRevision(
        deployment_id=dep.id,
        revision=next_rev,
        model_registry_id=new_model.id,
        model_name=new_model.name,
        model_version=new_model.version,
        algorithm=new_model.algorithm,
        action=action,
        note=(note or "")[:500],
        active=True,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return _revision_out(row)


async def list_deployment_versions(db: AsyncSession, dep: ModelDeployment,
                                   owner_id: str | None) -> dict:
    """The revision ledger + the other registry versions available as targets."""
    revisions = await list_revisions(db, dep.id)
    current_name = None
    if dep.model_registry_id:
        cur = await db.get(TrainedModel, dep.model_registry_id)
        current_name = cur.name if cur else None
    available: list[dict] = []
    if current_name:
        q = (select(TrainedModel)
             .where(TrainedModel.name == current_name)
             .order_by(TrainedModel.version.desc()))
        for m in (await db.execute(q)).scalars().all():
            if owner_id is not None and m.owner_id is not None and m.owner_id != owner_id:
                continue
            available.append({
                "id": m.id, "version": m.version, "active_version": bool(m.active),
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "current": m.id == dep.model_registry_id,
                "metrics": {k: m.metrics.get(k) for k in ("perplexity", "accuracy", "eval_loss")
                            if k in (m.metrics or {})},
            })
    return {"revisions": revisions, "available": available,
            "model_name": current_name,
            "serving_mode": dep.serving_mode}


async def redeploy_deployment(db: AsyncSession, dep: ModelDeployment, *,
                              owner_id: str | None, model_ref: str, note: str = "") -> dict:
    """Point this deployment at another registry row (same serving shape).

    Same URL, new weights: the serving workflow's model parameter is
    patched in place so callers never chase a moving endpoint. A
    generate-mode deployment only accepts language models and a
    predict-mode one only tabular models - switching families would
    change the request contract, and that is a NEW deployment, not a
    redeploy.
    """
    new_model = await model_svc.resolve_model(db, (model_ref or "").strip(), owner_id=owner_id)
    if new_model is None:
        raise ValueError(f"model {model_ref!r} not found in the registry")
    cur = await db.get(TrainedModel, dep.model_registry_id)
    cur_family_lm = bool(cur and cur.algorithm in LM_ALGORITHMS)
    new_family_lm = new_model.algorithm in LM_ALGORITHMS
    if new_family_lm != cur_family_lm:
        raise ValueError(
            f"cannot redeploy a {dep.serving_mode}-mode deployment to "
            f"{new_model.algorithm!r} - the request contract would change; "
            "create a separate deployment for the other family")
    if new_model.id == dep.model_registry_id:
        raise ValueError("this deployment already serves that model version")
    if cur is not None and new_model.name != cur.name:
        note = (note or "").strip() or f"switched from {cur.name} v{cur.version}"
    wf = await db.get(Workflow, dep.workflow_id) if dep.workflow_id else None
    revision = await _activate_revision(db, dep=dep, new_model=new_model, wf=wf,
                                        action="redeploy", note=note)
    wf2, model = await _load_refs(db, dep)
    return {"deployment": deployment_out(dep, wf2, model, await _serving_stats(db, dep.workflow_id)),
            "revision": revision}


async def rollback_deployment(db: AsyncSession, dep: ModelDeployment, *,
                              owner_id: str | None, revision: int | None = None,
                              version: int | None = None, note: str = "") -> dict:
    """Re-activate an older revision (by ledger number) or registry version."""
    if revision is None and version is None:
        raise ValueError("pass the revision number or the registry version to roll back to")
    target_model: TrainedModel | None = None
    target_rev: DeploymentRevision | None = None
    if revision is not None:
        q = (select(DeploymentRevision)
             .where(DeploymentRevision.deployment_id == dep.id,
                    DeploymentRevision.revision == revision))
        target_rev = (await db.execute(q)).scalars().first()
        if target_rev is None:
            raise ValueError(f"revision {revision} does not exist on this deployment")
        target_model = await db.get(TrainedModel, target_rev.model_registry_id)
        if target_model is None:
            raise ValueError(
                f"revision {revision} pointed at registry row {target_rev.model_registry_id} "
                "which no longer exists - it cannot be rolled back to")
    else:
        cur = await db.get(TrainedModel, dep.model_registry_id)
        if cur is None:
            raise ValueError("the currently served model row is missing - roll back by revision instead")
        q = (select(TrainedModel)
             .where(TrainedModel.name == cur.name, TrainedModel.version == version))
        target_model = (await db.execute(q)).scalars().first()
        if target_model is None or (owner_id is not None and target_model.owner_id is not None
                                    and target_model.owner_id != owner_id):
            raise ValueError(f"no registry row for version {version} of {cur.name!r}")
    if target_model.id == dep.model_registry_id:
        raise ValueError("this deployment already serves that model version")

    wf = await db.get(Workflow, dep.workflow_id) if dep.workflow_id else None
    rev_note = (note or "").strip() or (
        f"rollback to {target_model.name} v{target_model.version}"
        + (f" (revision {target_rev.revision})" if target_rev else ""))
    result = await _activate_revision(db, dep=dep, new_model=target_model, wf=wf,
                                      action="rollback", note=rev_note)
    wf2, model = await _load_refs(db, dep)
    return {"deployment": deployment_out(dep, wf2, model, await _serving_stats(db, dep.workflow_id)),
            "revision": result}


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
                   stats: dict | None = None, active_tokens: int = 0) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "serving_mode": row.serving_mode,
        "environment": row.environment,
        "enabled": bool(row.enabled),
        "notes": row.notes,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        # v68 serving auth: active_tokens > 0 means the endpoint demands a token
        "active_tokens": active_tokens,
        "auth_required": active_tokens > 0,
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
        out.append(deployment_out(row, wf, model, await _serving_stats(db, row.workflow_id),
                                  active_tokens=await active_token_count(db, row.id)))
    return out


async def get_deployment(db: AsyncSession, deployment_id: str, owner_id: str | None) -> dict | None:
    row = await db.get(ModelDeployment, deployment_id)
    if row is None:
        return None
    if owner_id is not None and row.owner_id is not None and row.owner_id != owner_id:
        return None  # foreign deployments look nonexistent
    wf, model = await _load_refs(db, row)
    return deployment_out(row, wf, model, await _serving_stats(db, row.workflow_id),
                          active_tokens=await active_token_count(db, row.id))


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
    # v68: the initial deploy IS revision 1 - the ledger starts complete.
    db.add(DeploymentRevision(
        deployment_id=row.id,
        revision=1,
        model_registry_id=model.id,
        model_name=model.name,
        model_version=model.version,
        algorithm=model.algorithm,
        action="deploy",
        note="initial deploy",
        active=True,
    ))
    await db.flush()
    return deployment_out(row, wf, model, await _serving_stats(db, row.workflow_id),
                          active_tokens=0)


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
    return deployment_out(row, wf, model, await _serving_stats(db, row.workflow_id),
                          active_tokens=await active_token_count(db, row.id))


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


# ---------------------------------------------------------------------------
# SSE streaming generation (v68)
# ---------------------------------------------------------------------------

def sse_event(event: str, data: dict) -> str:
    """One Server-Sent Event frame."""
    import json
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _resolve_lm_net(payload: dict, device: str = "cpu"):
    """Load the LM net exactly like lm_generate does (numpy or torch core)."""
    from ..engine.nodes.lm import _TinyLM

    from .devices import resolve_device
    dev = resolve_device(device)
    if dev["backend"] == "torch":
        from ..engine.torch_backend import _TorchLM
        return _TorchLM.from_state(payload["net"], device=dev["resolved"]), dev
    return _TinyLM.from_state(payload["net"]), dev


async def stream_generation(db: AsyncSession, dep: ModelDeployment, *, owner_id: str | None,
                            prompt: str, max_tokens: int = 32, temperature: float = 0.8,
                            top_k: int = 40, seed: int = 42):
    """Async generator yielding SSE frames for a token-by-token LM sampling.

    Reuses the registry artifact loader and the shared ``stream_generate``
    sampler (numpy + torch cores) - the SAME math as lm_generate, observed
    one token at a time. The caller (API layer) owns the HTTP plumbing.
    """
    import numpy as np

    from ..engine.nodes.lm import (_decode_with, _encode_with,
                                   _resolve_registry_artifact, stream_generate)

    model = await db.get(TrainedModel, dep.model_registry_id)
    model_name = model.name if model else None
    try:
        if dep.serving_mode != "generate":
            raise ValueError(
                f"streaming is a language-model surface - this deployment serves "
                f"{model_name or dep.model_registry_id!r} in {dep.serving_mode!r} mode "
                "(tabular scoring answers in one shot; there is nothing to stream)")
        info, payload = await _resolve_registry_artifact(dep.model_registry_id, owner_id)
        net, dev = _resolve_lm_net(payload)
        vocab = payload["vocab"]
        tokenizer_state = dict(payload.get("tokenizer") or {"type": "word"})
        cfg = payload["config"]
        model_ctx = int(cfg["n_ctx"])

        ids = _encode_with(str(prompt or ""), vocab, tokenizer_state) if str(prompt or "").strip() else []
        yield sse_event("meta", {
            "model": {"id": info["id"], "name": info["name"], "version": info["version"]},
            "tokenizer": tokenizer_state.get("type") or "word",
            "context_window": model_ctx,
            "max_tokens": max_tokens,
            "device": dev["resolved"],
            "device_backend": dev["backend"],
        })

        pieces: list[str] = []
        window_slid = False
        for i, tok in enumerate(stream_generate(net, ids, max_tokens,
                                                temperature=temperature, top_k=top_k,
                                                seed=seed)):
            piece = _decode_with([tok], vocab, tokenizer_state)
            pieces.append(piece)
            yield sse_event("token", {"index": i, "text": piece,
                                      "tokens_generated": i + 1})
        text = "".join(pieces)
        window_slid = (len(ids) + len(pieces)) > model_ctx
        yield sse_event("done", {
            "text": text,
            "tokens_generated": len(pieces),
            "context_window": model_ctx,
            "window_slid": window_slid,
            "model": {"id": info["id"], "name": info["name"], "version": info["version"]},
        })
    except Exception as exc:  # noqa: BLE001 - streamed errors must reach the caller as an event, not a broken pipe
        yield sse_event("error", {"error": str(exc)})
