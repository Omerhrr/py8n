"""Model Systems (v63) - the AI model-building operating unit.

Where a Py8n System runs a part of the BUSINESS, a Model System builds
and operates a MODEL: datasets in, training out, evaluation, registry,
deployment, monitoring, retraining. The membership is curated (stored,
like every grouping in py8n); EVERYTHING the model system reports is
derived at read time from the member objects:

* training  - registry rows grouped classical vs neural vs fine-tuned
* modalities - the declared focus + evidence from bound workflows
  (text_features/image_features/audio_features/document_extract nodes)
* composition - workflows that CHAIN models (2+ model_predict nodes)
* deployment - workflows that serve a model (1+ model_predict node)
* evaluation - latest metrics per active model
* monitoring - reference-stats coverage (drift capability)
* retraining - training workflows + their schedule triggers

A model system binds into a Py8n System as the ``model_system`` component
kind - the Company AI System pattern from the roadmap.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    Dataset,
    ExecutionLog,
    ModelSystem,
    ModelSystemComponent,
    ScheduledReport,
    TrainedModel,
    Workflow,
)
from .health import compute_health

COMPONENT_KINDS = ("dataset", "model", "workflow", "report")
KIND_TABLES = {
    "dataset": Dataset,
    "model": TrainedModel,
    "workflow": Workflow,
    "report": ScheduledReport,
}
NEURAL_PREFIX = "mlp_"
LANGUAGE_PREFIX = "lm_"
HEALTH_BUDGET = 10

# the honest modality capability matrix - what this inline-mode build can
# actually extract today (v65: cv2 frame sampling closes the video gap)
CAPABILITIES = [
    {"modality": "text", "available": True,
     "extractor": "text_features (TF-IDF+SVD, fit/transform) · lm_train (from-scratch transformer LM, continued pretraining) · lm_generate"},
    {"modality": "image", "available": True, "extractor": "image_features (PIL: channel stats, histogram, edges)"},
    {"modality": "audio", "available": True, "extractor": "audio_features (WAV: RMS, ZCR, FFT bands)"},
    {"modality": "video", "available": True,
     "extractor": "video_features (v65, OpenCV: uniform frame sampling, per-frame brightness/contrast/edges, clip motion)"},
    {"modality": "document", "available": True, "extractor": "document_extract (PDF/DOCX/XLSX/CSV/JSON -> text) + text_features"},
    {"modality": "tabular", "available": True, "extractor": "model_train (9 sklearn algorithms) + neural_train (from-scratch MLP)"},
]

MODALITY_NODE_TYPES = {
    "text_features": "text",
    "image_features": "image",
    "audio_features": "audio",
    "video_features": "video",   # v65: cv2 frame sampling is real video evidence
    "document_extract": "document",
    "lm_train": "text",      # v64: language-model training is text evidence
    "lm_generate": "text",   # v64: LM serving is text evidence
}
TRAINING_NODE_TYPES = {"model_train", "neural_train", "lm_train"}
PREDICT_NODE_TYPE = "model_predict"


async def resolve_component(db: AsyncSession, kind: str, ref_id: str, user_id: str | None):
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


def model_system_summary(row: ModelSystem) -> dict:
    comps = list(row.components or [])
    counts = {k: 0 for k in COMPONENT_KINDS}
    for c in comps:
        counts[c.kind] = counts.get(c.kind, 0) + 1
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "icon": row.icon,
        "color": row.color,
        "modalities": row.modalities or [],
        "components": counts,
        "total_components": len(comps),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _family(algorithm: str) -> str:
    algo = algorithm or ""
    if algo.startswith(LANGUAGE_PREFIX):
        return "language"  # v64: lm_transformer and friends
    return "neural" if algo.startswith(NEURAL_PREFIX) else "classical"


def _metrics_top(metrics: dict) -> dict:
    keep = ("accuracy", "f1_weighted", "roc_auc", "r2", "mae", "rmse",
            "perplexity", "architecture", "params_count", "epochs_run",
            "condition_dim", "multimodal", "continued_pretrained_from")
    return {k: metrics[k] for k in keep if k in (metrics or {})}


async def model_system_health(db: AsyncSession, ms: ModelSystem) -> dict:
    """Derived verdict, mirroring the v61 rollup."""
    comps = list(ms.components or [])
    wf_ids = [c.ref_id for c in comps if c.kind == "workflow"]
    ds_ids = [c.ref_id for c in comps if c.kind == "dataset"]
    model_ids = [c.ref_id for c in comps if c.kind == "model"]
    report_ids = [c.ref_id for c in comps if c.kind == "report"]

    now = datetime.now(timezone.utc)
    d7 = now - timedelta(days=7)

    runs_7d = failures_7d = 0
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
        failures_7d = sum(1 for ex in rows if ex.status == "error")

    datasets = {"total": len(ds_ids), "healthy": 0, "degraded": 0, "unhealthy": 0, "unscored": 0}
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

    models = {"bound": len(model_ids), "active": 0, "with_reference_stats": 0}
    if model_ids:
        rows = (await db.execute(select(TrainedModel).where(TrainedModel.id.in_(model_ids)))).scalars().all()
        models["active"] = sum(1 for m in rows if m.active)
        models["with_reference_stats"] = sum(1 for m in rows if m.reference_stats)

    deliveries = {"ok_7d": 0, "error_7d": 0}
    if report_ids:
        from ..models import ReportDeliveryEvent

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
    if runs_7d >= 5 and failure_rate >= 50:
        verdict = "unhealthy"

    return {
        "verdict": verdict,
        "workflows": {"bound": len(wf_ids), "runs_7d": runs_7d, "failures_7d": failures_7d,
                      "failure_rate_7d": failure_rate},
        "datasets": datasets,
        "models": models,
        "reports": {"bound": len(report_ids), **deliveries},
        "generated_at": now.isoformat(),
    }


async def model_system_detail(db: AsyncSession, ms: ModelSystem) -> dict:
    """The nine derived sections of the model system."""
    comps = list(ms.components or [])
    comp_ids = {(c.kind, c.ref_id): c.id for c in comps}
    ds_ids = [c.ref_id for c in comps if c.kind == "dataset"]
    model_ids = [c.ref_id for c in comps if c.kind == "model"]
    wf_ids = [c.ref_id for c in comps if c.kind == "workflow"]
    report_ids = [c.ref_id for c in comps if c.kind == "report"]

    # ---- members -------------------------------------------------------
    datasets_out = []
    if ds_ids:
        rows = (await db.execute(select(Dataset).where(Dataset.id.in_(ds_ids)))).scalars().all()
        datasets_out = [{"id": d.id, "name": d.name, "rows": d.row_count,
                         "component_id": comp_ids.get(("dataset", d.id)),
                         "columns": [c.get("name") for c in (d.schema_json or [])][:12]}
                        for d in rows]

    model_rows = []
    if model_ids:
        model_rows = (
            await db.execute(select(TrainedModel).where(TrainedModel.id.in_(model_ids)).order_by(TrainedModel.name, TrainedModel.version.desc()))
        ).scalars().all()

    wf_rows = []
    if wf_ids:
        wf_rows = (await db.execute(select(Workflow).where(Workflow.id.in_(wf_ids)))).scalars().all()

    report_rows = []
    if report_ids:
        report_rows = (await db.execute(select(ScheduledReport).where(ScheduledReport.id.in_(report_ids)))).scalars().all()

    # ---- training ------------------------------------------------------
    classical = [m for m in model_rows if _family(m.algorithm) == "classical"]
    neural = [m for m in model_rows if _family(m.algorithm) == "neural"]
    fine_tuned = [m for m in model_rows if (m.metrics or {}).get("fine_tuned_from")]
    names = {m.name for m in model_rows}
    language = [m for m in model_rows if _family(m.algorithm) == "language"]
    training = {
        "classical_versions": len(classical),
        "neural_versions": len(neural),
        "language_versions": len(language),
        "fine_tuned_versions": len(fine_tuned),
        "continued_pretrained_versions": len([m for m in model_rows if (m.metrics or {}).get("continued_pretrained_from")]),
        "distinct_models": len(names),
        "total_versions": len(model_rows),
        "latest": [
            {
                "id": m.id, "name": m.name, "version": m.version,
                "algorithm": m.algorithm, "family": _family(m.algorithm),
                "task": m.task, "active": m.active,
                "metrics": _metrics_top(m.metrics or {}),
                "fine_tuned_from": (m.metrics or {}).get("fine_tuned_from"),
            }
            for m in model_rows[:12]
        ],
    }

    # ---- modalities: declared + derived evidence from bound workflows ----
    evidence: set[str] = set()
    for wf in wf_rows:
        for node in (wf.graph or {}).get("nodes", []):
            mod = MODALITY_NODE_TYPES.get(node.get("type") or "")
            if mod:
                evidence.add(mod)
            nt = node.get("type") or ""
            if nt in TRAINING_NODE_TYPES and nt != "lm_train":
                evidence.add("tabular")  # table trainers; lm_train already proves 'text'
    modalities = {
        "declared": ms.modalities or [],
        "evidence": sorted(evidence),
        "capabilities": [c for c in CAPABILITIES if c["modality"] in (ms.modalities or [])]
        if ms.modalities else CAPABILITIES,
    }

    # ---- composition / deployment / retraining --------------------------
    deployment: list[dict] = []
    composition: list[dict] = []
    retraining: list[dict] = []
    for wf in wf_rows:
        nodes = (wf.graph or {}).get("nodes", [])
        predict_nodes = [n for n in nodes if n.get("type") == PREDICT_NODE_TYPE]
        train_nodes = [n for n in nodes if n.get("type") in TRAINING_NODE_TYPES]
        cron = None
        for n in nodes:
            if n.get("type") == "schedule_trigger":
                cron = (n.get("parameters") or {}).get("cron")
        if predict_nodes:
            deployment.append({"id": wf.id, "name": wf.name, "models_scored": len(predict_nodes),
                               "component_id": comp_ids.get(("workflow", wf.id)),
                               "active": bool(wf.is_active)})
        if len(predict_nodes) >= 2:
            composition.append({"id": wf.id, "name": wf.name, "chain_length": len(predict_nodes),
                                "component_id": comp_ids.get(("workflow", wf.id))})
        if train_nodes:
            retraining.append({"id": wf.id, "name": wf.name,
                               "trainer": sorted({n.get("type") for n in train_nodes}),
                               "schedule": cron or "manual", "active": bool(wf.is_active),
                               "component_id": comp_ids.get(("workflow", wf.id))})

    # ---- evaluation: latest metrics per active model --------------------
    evaluation = [
        {"model": m.name, "version": m.version, "family": _family(m.algorithm),
         "task": m.task, "metrics": _metrics_top(m.metrics or {})}
        for m in model_rows if m.active
    ]

    # ---- v67: LIVE ENDPOINTS - deployments serving this system's models ---
    model_ids = {m.id for m in model_rows}
    endpoints: list[dict] = []
    if model_ids:
        from ..models import ModelDeployment

        dep_rows = (await db.execute(
            select(ModelDeployment).where(ModelDeployment.model_registry_id.in_(model_ids))  # type: ignore[attr-defined]
        )).scalars().all()
        wf_cache: dict[str, Workflow | None] = {}
        for d in dep_rows:
            if d.workflow_id not in wf_cache:
                wf_cache[d.workflow_id] = await db.get(Workflow, d.workflow_id) if d.workflow_id else None
            wf = wf_cache[d.workflow_id]
            endpoints.append({
                "id": d.id, "name": d.name, "environment": d.environment,
                "enabled": bool(d.enabled), "serving_mode": d.serving_mode,
                "model_id": d.model_registry_id,
                "status": "live" if (d.enabled and wf is not None and wf.is_active) else
                          ("disabled" if not d.enabled else "inactive"),
                "webhook_path": f"/api/v1/webhooks/{d.workflow_id}" if d.workflow_id else None,
            })

    # ---- monitoring: drift capability coverage --------------------------
    monitored = [m for m in model_rows if m.reference_stats]
    monitoring = {
        "versions": len(model_rows),
        "with_reference_stats": len(monitored),
        "coverage_pct": round(len(monitored) / len(model_rows) * 100, 1) if model_rows else 0.0,
        "drift_capable": bool(model_rows) and len(monitored) == len(model_rows),
    }

    reports_out = [{"id": r.id, "name": r.name, "cron": r.cron, "fmt": r.fmt,
                    "component_id": comp_ids.get(("report", r.id)),
                    "enabled": bool(r.enabled)} for r in report_rows]

    return {
        **model_system_summary(ms),
        "datasets": datasets_out,
        "training": training,
        "modalities": modalities,
        "composition": composition,
        "evaluation": evaluation,
        "registry": [{"id": m.id, "name": m.name, "version": m.version, "algorithm": m.algorithm,
                      "family": _family(m.algorithm), "active": m.active,
                      "component_id": comp_ids.get(("model", m.id))} for m in model_rows],
        "deployment": deployment,
        "endpoints": endpoints,
        "monitoring": monitoring,
        "retraining": retraining,
        "reports": reports_out,
        "lifecycle": await derive_lifecycle(db, ms),
        "health": await model_system_health(db, ms),
    }


# ------------------------------------------------------------------ lifecycle
# v65: the full LM lifecycle as ONE derived plan + ONE sequenced run.  The
# stages are read off the bound workflows' graphs (never stored): an lm_train
# node WITHOUT base_model is a PRETRAIN stage, WITH base_model a CONTINUE
# stage, an lm_generate node a GENERATE stage.  Running the plan dispatches
# each stage's workflow through the REAL engine, waits for it, and reports
# what the LM nodes produced (perplexity chain + generated text).

_LM_TRAIN_TYPE = "lm_train"
_LM_GENERATE_TYPE = "lm_generate"
_LIFECYCLE_STAGE_ORDER = ("pretrain", "continue", "generate")
_LIFECYCLE_DEFAULT_TIMEOUT_S = 240
_LIFECYCLE_MAX_TIMEOUT_S = 900


def _workflow_lm_stage(wf: Workflow) -> str | None:
    """Classify a bound workflow's LM role from its graph, derived only."""
    nodes = (wf.graph or {}).get("nodes", [])
    types = [n.get("type") or "" for n in nodes]
    if _LM_GENERATE_TYPE in types:
        return "generate"
    lm_trains = [n for n in nodes if n.get("type") == _LM_TRAIN_TYPE]
    if any((n.get("parameters") or {}).get("base_model") for n in lm_trains):
        return "continue"
    if lm_trains:
        return "pretrain"
    return None


async def _bound_workflows(db: AsyncSession, ms: ModelSystem) -> list[Workflow]:
    wf_ids = [c.ref_id for c in ms.components if c.kind == "workflow"]
    if not wf_ids:
        return []
    return list(
        (await db.execute(select(Workflow).where(Workflow.id.in_(wf_ids)))).scalars().all()
    )


async def derive_lifecycle(db: AsyncSession, ms: ModelSystem) -> dict:
    """The derived LM lifecycle plan: ordered stages over bound workflows.

    Everything is read from the graphs at call time - installing a solution
    or binding new workflows changes the plan without any stored state.
    """
    stages: list[dict] = []
    skipped: list[dict] = []
    wfs = await _bound_workflows(db, ms)
    for wf in sorted(wfs, key=lambda w: w.name or ""):
        stage = _workflow_lm_stage(wf)
        if stage is None:
            skipped.append({"workflow_id": wf.id, "workflow_name": wf.name,
                            "reason": "no lm_train / lm_generate node in the graph"})
            continue
        stages.append({"stage": stage, "workflow_id": wf.id, "workflow_name": wf.name,
                       "active": bool(wf.is_active)})
    stages.sort(key=lambda s: (_LIFECYCLE_STAGE_ORDER.index(s["stage"]), s["workflow_name"]))
    for pos, s in enumerate(stages, 1):
        s["position"] = pos
    return {
        "stages": stages,
        "skipped": skipped,
        "lm_workflows": len(stages),
        "sequence": " -> ".join(f"{s['stage']}:{s['workflow_name']}" for s in stages),
    }


async def _wait_execution(execution_id: str, timeout_s: float) -> ExecutionLog:
    """Poll the execution log until a terminal state (inline dispatch runs
    on the same loop as a background task, so polling converges)."""
    from ..db import AsyncSessionLocal

    deadline = time.monotonic() + timeout_s
    while True:
        async with AsyncSessionLocal() as session:
            row = await session.get(ExecutionLog, execution_id)
            if row is not None and row.status not in ("running", "queued"):
                return row
        if time.monotonic() > deadline:
            raise TimeoutError(f"execution {execution_id} did not finish within {int(timeout_s)}s")
        await asyncio.sleep(0.15)


def _stage_result(pos: int, stage: str, wf: Workflow, log: ExecutionLog) -> dict:
    """Compress one finished execution into a lifecycle stage report."""
    out: dict = {
        "position": pos, "stage": stage,
        "workflow_id": wf.id, "workflow_name": wf.name,
        "execution_id": log.id, "status": log.status,
        "duration_ms": log.duration_ms,
    }
    if log.status != "success":
        out["error"] = (log.error or "execution failed")[:400]
        return out
    lm_run = None
    for run in log.node_runs or []:
        if run.get("node_type") in (_LM_TRAIN_TYPE, _LM_GENERATE_TYPE):
            lm_run = run
            break
    if lm_run is None:
        out["error"] = "the workflow succeeded but no lm_train/lm_generate node ran"
        return out
    payload = lm_run.get("output") or {}
    if lm_run.get("node_type") == _LM_TRAIN_TYPE:
        metrics = payload.get("metrics") or {}
        out["perplexity"] = payload.get("perplexity") or metrics.get("perplexity")
        out["mode"] = payload.get("mode")
        out["vocabulary"] = payload.get("vocabulary")
        out["tokenizer"] = metrics.get("tokenizer")
        out["continued_from"] = metrics.get("continued_pretrained_from")
        registry = payload.get("registry") or {}
        out["registry_name"] = registry.get("name")
        out["registry_version"] = registry.get("version")
    else:
        out["generated_text"] = payload.get("text")
        out["tokens_generated"] = payload.get("tokens_generated")
        out["tokenizer"] = payload.get("tokenizer")
    return out


async def run_lifecycle(db: AsyncSession, ms: ModelSystem, timeout_s: int | None = None) -> dict:
    """Run the derived LM lifecycle IN SEQUENCE through the real engine.

    Each stage's workflow is dispatched (manual trigger) and awaited; the
    sequence STOPS at the first failure - later stages depend on earlier
    model versions, so continuing would only produce dishonest noise.
    """
    from ..services.dispatcher import dispatch_execution

    plan = await derive_lifecycle(db, ms)
    stages = plan["stages"]
    if not stages:
        return {"ran": False,
                "note": "no LM lifecycle stages found - bind workflows with lm_train / lm_generate nodes",
                "skipped": plan["skipped"], "stages": []}

    timeout = min(max(int(timeout_s or _LIFECYCLE_DEFAULT_TIMEOUT_S), 10), _LIFECYCLE_MAX_TIMEOUT_S)
    results: list[dict] = []
    stopped_early = False
    started = time.monotonic()
    for s in stages:
        wf = await db.get(Workflow, s["workflow_id"])
        if wf is None:
            results.append({"position": s["position"], "stage": s["stage"],
                            "workflow_id": s["workflow_id"], "workflow_name": s["workflow_name"],
                            "status": "error", "error": "workflow no longer exists"})
            stopped_early = True
            break
        try:
            execution_id = await dispatch_execution(wf.id, trigger_type="manual",
                                                    trigger_payload={"payload": {}})
            log = await _wait_execution(execution_id, timeout)
            res = _stage_result(s["position"], s["stage"], wf, log)
        except TimeoutError as exc:
            res = {"position": s["position"], "stage": s["stage"], "workflow_id": wf.id,
                   "workflow_name": wf.name, "status": "error", "error": str(exc)}
        except ValueError as exc:
            res = {"position": s["position"], "stage": s["stage"], "workflow_id": wf.id,
                   "workflow_name": wf.name, "status": "error", "error": str(exc)}
        results.append(res)
        if res["status"] != "success":
            stopped_early = True
            break

    if stopped_early:
        for s in stages[len(results):]:
            results.append({"position": s["position"], "stage": s["stage"],
                            "workflow_id": s["workflow_id"], "workflow_name": s["workflow_name"],
                            "status": "not_run", "error": "an earlier stage failed"})

    ok = [r for r in results if r["status"] == "success"]
    train_ppl = [r["perplexity"] for r in ok if r.get("perplexity") is not None]
    summary = {
        "stages_total": len(stages),
        "stages_succeeded": len(ok),
        "perplexity_chain": train_ppl,
        "generated_text": next((r.get("generated_text") for r in ok if r.get("generated_text")), None),
        "tokenizer": next((r.get("tokenizer") for r in ok if r.get("tokenizer")), None),
        "total_seconds": round(time.monotonic() - started, 2),
        "stopped_early": stopped_early,
    }
    return {"ran": True, "summary": summary, "stages": results, "skipped": plan["skipped"]}
