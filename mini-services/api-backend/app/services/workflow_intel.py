"""Workflow intelligence (v56) - workflows understood as systems.

Two derived capabilities, zero new storage:

**Workflow health** - the ExecutionLog table already owns the run truth;
this module folds a workflow's finished runs (default: last 30 days) into
the report the roadmap asks for::

    Runs              12,842
    Success rate       99.1%
    Avg duration        8.4s
    P95                 18.2s
    Failures / Retries / Fallbacks
    Most failing node / Most expensive node

Node-level numbers come from each log's ``node_runs`` records (the runner
persists per-node status, duration, ``attempts`` and ``fallback_used``).

**Workflow diff** - the WorkflowVersion table (v13) already snapshots the
full graph on every content save; this module compares two snapshots at
the param level (added/removed/renamed nodes, per-parameter change lines
like ``Retry policy: 2 -> 4``, edge changes) and estimates the potential
execution-time impact from the workflow's own run history (avg duration
of the changed nodes across recent runs vs the avg run duration). With no
run history the estimate says so honestly instead of inventing a number.

Nothing is stored, so intelligence can never drift from what happened.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ExecutionLog, Workflow, WorkflowVersion

DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 365
MAX_LOGS = 500          # per-query cap before folding
DIFF_SAMPLE_RUNS = 20   # recent runs consulted for the impact estimate

# Node param keys worth surfacing with a friendly label in change lines.
_PARAM_LABELS = {
    "url": "URL",
    "endpoint": "endpoint",
    "method": "method",
    "path": "path",
    "table": "table",
    "sql": "query",
    "query": "query",
    "mode": "mode",
    "watermark_column": "watermark column",
    "key_columns": "key columns",
    "lookback": "lookback",
    "model": "model",
    "provider": "provider",
    "system_prompt": "system prompt",
    "to_address": "recipient",
    "subject": "subject",
}

# Per-node resilience settings (schema.NodeSettings) are part of the graph
# snapshot, so the diff walks them too - "Retry policy: 2 -> 4" comes from here.
_SETTING_LABELS = {
    "retry_on_fail": "retry on fail",
    "max_retries": "Retry policy",
    "retry_wait_ms": "retry wait",
    "retry_backoff_multiplier": "backoff multiplier",
    "timeout_ms": "timeout",
    "continue_on_fail": "continue on fail",
    "fallback_enabled": "fallback enabled",
    "disabled": "disabled",
}


def _p95(values: list[int]) -> int | None:
    """Nearest-rank p95 over a non-empty duration list."""
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, round(0.95 * (len(ordered) - 1))))
    return int(ordered[idx])


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# --------------------------------------------------------------------- health


async def workflow_health(db: AsyncSession, wf: Workflow, window_days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """Fold a workflow's finished runs into one derived health report."""
    window_days = max(1, min(int(window_days or DEFAULT_WINDOW_DAYS), MAX_WINDOW_DAYS))
    since = datetime.now(timezone.utc) - timedelta(days=window_days)

    rows = (
        (
            await db.execute(
                select(ExecutionLog)
                .where(
                    ExecutionLog.workflow_id == wf.id,
                    ExecutionLog.started_at >= since,
                    ExecutionLog.status != "running",
                )
                .order_by(ExecutionLog.started_at.desc())
                .limit(MAX_LOGS)
            )
        )
        .scalars()
        .all()
    )

    runs = len(rows)
    statuses = {"success": 0, "error": 0, "cancelled": 0}
    durations: list[int] = []
    total_retries = 0
    total_fallbacks = 0

    # per-node aggregation keyed by node_id
    node_runs_n: dict[str, int] = {}
    node_errors: dict[str, int] = {}
    node_ms: dict[str, int] = {}
    node_type: dict[str, str] = {}
    node_name: dict[str, str] = {}
    last_error: str | None = None
    last_failed_at: str | None = None

    for ex in rows:  # newest first
        status = ex.status or "success"
        statuses[status] = statuses.get(status, 0) + 1
        if ex.duration_ms is not None:
            durations.append(int(ex.duration_ms))
        if status == "error":
            if last_error is None:
                last_error = (ex.error or "unknown error")[:300]
                last_failed_at = _aware(ex.started_at).isoformat() if ex.started_at else None
        for rec in ex.node_runs or []:
            nid = str(rec.get("node_id") or "?")
            node_type.setdefault(nid, str(rec.get("node_type") or "?"))
            node_name.setdefault(nid, str(rec.get("node_name") or nid))
            node_runs_n[nid] = node_runs_n.get(nid, 0) + 1
            attempts = int(rec.get("attempts") or 1)
            total_retries += max(0, attempts - 1)
            if rec.get("fallback_used"):
                total_fallbacks += 1
            if rec.get("status") == "error":
                node_errors[nid] = node_errors.get(nid, 0) + 1
            ms = rec.get("duration_ms")
            if isinstance(ms, (int, float)):
                node_ms[nid] = node_ms.get(nid, 0) + int(ms)

    succeeded = statuses.get("success", 0)
    failed = statuses.get("error", 0)
    success_rate = round(succeeded / runs * 100, 1) if runs else None
    avg_ms = round(sum(durations) / len(durations), 1) if durations else None

    most_failing = None
    if node_errors:
        nid = max(node_errors, key=lambda k: node_errors[k])
        most_failing = {
            "node_id": nid,
            "type": node_type.get(nid, "?"),
            "name": node_name.get(nid, nid),
            "errors": node_errors[nid],
        }
    most_expensive = None
    if node_ms:
        nid = max(node_ms, key=lambda k: node_ms[k])
        runs_n = max(1, node_runs_n.get(nid, 1))
        total_run_ms = sum(node_ms.values()) or 1
        most_expensive = {
            "node_id": nid,
            "type": node_type.get(nid, "?"),
            "name": node_name.get(nid, nid),
            "total_ms": node_ms[nid],
            "avg_ms": round(node_ms[nid] / runs_n, 1),
            "share_pct": round(node_ms[nid] / total_run_ms * 100, 1),
        }

    if not runs:
        verdict = "unscored"
    elif success_rate >= 95.0:
        verdict = "healthy"
    elif success_rate >= 80.0:
        verdict = "degraded"
    else:
        verdict = "unhealthy"

    return {
        "workflow_id": wf.id,
        "window_days": window_days,
        "verdict": verdict,
        "runs": runs,
        "succeeded": succeeded,
        "failed": failed,
        "cancelled": statuses.get("cancelled", 0),
        "success_rate": success_rate,
        "avg_duration_ms": avg_ms,
        "p95_duration_ms": _p95(durations),
        "retries": total_retries,
        "fallbacks": total_fallbacks,
        "last_error": last_error,
        "last_failed_at": last_failed_at,
        "most_failing_node": most_failing,
        "most_expensive_node": most_expensive,
        "nodes_seen": len(node_runs_n),
    }


# ---------------------------------------------------------------- diff


def _norm_params(params: dict) -> dict:
    return dict(params or {})


def _setting_changes(old: dict, new: dict) -> list[dict]:
    """Diff the resilience settings of two node versions.

    Both sides are normalized through NodeSettings first: older snapshots may
    predate a settings key (missing = "use the default"), so a missing key
    must compare equal to the default, not count as a change.
    """
    from ..engine.schema import NodeSettings

    try:
        old_n = NodeSettings.model_validate(old or {}).model_dump()
    except Exception:
        old_n = dict(old or {})
    try:
        new_n = NodeSettings.model_validate(new or {}).model_dump()
    except Exception:
        new_n = dict(new or {})
    walk = {k for k in _SETTING_LABELS if k in old_n or k in new_n}
    changes: list[dict] = []
    for key in sorted(walk):
        ov, nv = old_n.get(key), new_n.get(key)
        if ov == nv:
            continue
        changes.append({"param": key, "label": _SETTING_LABELS[key], "old": ov, "new": nv})
    return changes


def _param_changes(old: dict, new: dict) -> list[dict]:
    """Per-parameter change lines between two param dicts."""
    changes: list[dict] = []
    for key in sorted(set(old) | set(new)):
        ov, nv = old.get(key), new.get(key)
        if ov == nv:
            continue
        label = _PARAM_LABELS.get(key, key.replace("_", " "))
        changes.append({
            "param": key,
            "label": label,
            "old": ov if isinstance(ov, (str, int, float, bool)) or ov is None else _json_tag(ov),
            "new": nv if isinstance(nv, (str, int, float, bool)) or nv is None else _json_tag(nv),
        })
    return changes


def _json_tag(value) -> str:
    return "json: " + _short_json(value)


def _short_json(value) -> str:
    try:
        text = json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _change_summary(node_type: str, changes: list[dict]) -> str | None:
    """One human line for the most interesting change of a node (roadmap style)."""
    if not changes:
        return None
    keys = {c["param"] for c in changes}
    if "max_retries" in keys or "retry_wait_ms" in keys or "timeout_ms" in keys or "timeout_seconds" in keys:
        for c in changes:
            if c["param"] in ("max_retries", "retries"):
                return f"Retry policy: {_fmt_val(c['old'])} -> {_fmt_val(c['new'])}"
        for c in changes:
            if c["param"] in ("timeout_ms", "timeout_seconds"):
                return f"Timeout: {_fmt_val(c['old'])} -> {_fmt_val(c['new'])}"
        for c in changes:
            if c["param"] == "retry_wait_ms":
                return f"Backoff: {_fmt_val(c['old'])}ms -> {_fmt_val(c['new'])}ms"
    if node_type == "http_request" and ("url" in keys or "endpoint" in keys):
        return "Changed HTTP endpoint"
    if node_type == "llm_chat" and ("system_prompt" in keys or "model" in keys):
        return "Changed LLM prompt/model"
    return None


def _fmt_val(v) -> str:
    if v is None:
        return "-"
    text = str(v)
    return text if len(text) <= 40 else text[:37] + "..."


def diff_workflow_graphs(old_graph: dict, new_graph: dict) -> dict:
    """Compare two workflow graphs at the node/param/edge level.

    Nodes are matched by id (stable across saves in the editor); renames
    surface as a ``renamed`` change rather than remove+add.
    """
    old_nodes = {str(n.get("id")): n for n in (old_graph or {}).get("nodes", []) if n.get("id")}
    new_nodes = {str(n.get("id")): n for n in (new_graph or {}).get("nodes", []) if n.get("id")}

    added, removed, changed, renamed = [], [], [], []
    for nid, n in new_nodes.items():
        if nid not in old_nodes:
            added.append({"node_id": nid, "type": n.get("type"), "name": n.get("name")})
            continue
        o = old_nodes[nid]
        if (o.get("name") or "") != (n.get("name") or ""):
            renamed.append({
                "node_id": nid,
                "type": n.get("type"),
                "old": o.get("name"),
                "new": n.get("name"),
            })
        pc = _param_changes(_norm_params(o.get("parameters")), _norm_params(n.get("parameters")))
        pc = _setting_changes(o.get("settings") or {}, n.get("settings") or {}) + pc
        if pc:
            changed.append({
                "node_id": nid,
                "type": n.get("type"),
                "name": n.get("name"),
                "changes": pc,
                "summary": _change_summary(str(n.get("type") or ""), pc),
            })
    for nid, n in old_nodes.items():
        if nid not in new_nodes:
            removed.append({"node_id": nid, "type": n.get("type"), "name": n.get("name")})

    def _edge_key(e: dict) -> tuple | None:
        if not e.get("source") or not e.get("target"):
            return None
        return (str(e.get("source")), str(e.get("sourceHandle") or ""), str(e.get("target")))

    old_edges = {k for k in (_edge_key(e) for e in (old_graph or {}).get("edges", [])) if k}
    new_edges = {k for k in (_edge_key(e) for e in (new_graph or {}).get("edges", [])) if k}

    def _edge_label(key: tuple, nodes: dict) -> str:
        src, handle, tgt = key
        s = nodes.get(src, {}).get("name") or src
        t = nodes.get(tgt, {}).get("name") or tgt
        return f"{s} -> {t}" + (f" [{handle}]" if handle and handle != "main" else "")

    name_index = {**{k: v for k, v in old_nodes.items()}, **new_nodes}
    edges_added = [_edge_label(k, name_index) for k in sorted(new_edges - old_edges)]
    edges_removed = [_edge_label(k, name_index) for k in sorted(old_edges - new_edges)]

    parts = []
    if added:
        parts.append(f"{len(added)} added")
    if removed:
        parts.append(f"{len(removed)} removed")
    if changed:
        parts.append(f"{len(changed)} changed")
    if renamed:
        parts.append(f"{len(renamed)} renamed")
    if edges_added:
        parts.append(f"{len(edges_added)} edges added")
    if edges_removed:
        parts.append(f"{len(edges_removed)} edges removed")
    summary = "; ".join(parts) if parts else "graphs identical"

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "renamed": renamed,
        "edges_added": edges_added,
        "edges_removed": edges_removed,
        "summary": summary,
        "identical": not parts,
    }


async def estimate_impact(db: AsyncSession, wf: Workflow, diff: dict) -> dict:
    """Potential execution-time impact of a graph diff, from run history.

    Compares the historical avg duration of the changed/added nodes against
    the workflow's avg total run duration across the last DIFF_SAMPLE_RUNS
    finished runs. No history -> honest 'estimate unavailable'.
    """
    touched = [c["node_id"] for c in diff.get("changed", [])]
    touched += [n["node_id"] for n in diff.get("added", [])]
    if not touched:
        return {"estimate": None, "detail": "no node changes - execution profile unaffected",
                "runs_analyzed": 0}

    rows = (
        (
            await db.execute(
                select(ExecutionLog)
                .where(
                    ExecutionLog.workflow_id == wf.id,
                    ExecutionLog.status != "running",
                )
                .order_by(ExecutionLog.started_at.desc())
                .limit(DIFF_SAMPLE_RUNS)
            )
        )
        .scalars()
        .all()
    )
    total_durations = [int(e.duration_ms) for e in rows if e.duration_ms is not None]
    if not total_durations:
        return {"estimate": None, "detail": "estimate unavailable - no run history for this workflow",
                "runs_analyzed": 0}

    touched_set = set(touched)
    node_ms: dict[str, list[int]] = {}
    for ex in rows:
        for rec in ex.node_runs or []:
            nid = str(rec.get("node_id") or "")
            if nid in touched_set and isinstance(rec.get("duration_ms"), (int, float)):
                node_ms.setdefault(nid, []).append(int(rec["duration_ms"]))

    changed_total = sum(sum(v) for v in node_ms.values())
    changed_samples = sum(len(v) for v in node_ms.values())
    run_avg = sum(total_durations) / len(total_durations)

    # Historical avg duration per RUN of the touched nodes, relative to the
    # avg total run duration. Total per node / runs-with-that-node, summed,
    # then divided by run avg -> % of a typical run spent in changed code.
    per_run_changed = 0.0
    for nid, vals in node_ms.items():
        per_run_changed += sum(vals) / len(vals)
    pct = round(per_run_changed / run_avg * 100, 1) if run_avg else None

    detail = (
        f"changed nodes historically take ~{round(per_run_changed, 1)}ms of a "
        f"~{round(run_avg, 1)}ms run ({pct}% across {len(total_durations)} analyzed runs)"
    )
    return {
        "estimate": pct,
        "detail": detail,
        "runs_analyzed": len(total_durations),
        "node_samples": changed_samples,
        "unmeasured": len(touched_set) - len(node_ms),
    }


async def workflow_version_diff(
    db: AsyncSession, wf: Workflow, from_version: int | None, to_version: int | None
) -> dict:
    """Diff two WorkflowVersion snapshots (defaults: the two most recent)."""
    snaps = (
        (
            await db.execute(
                select(WorkflowVersion)
                .where(WorkflowVersion.workflow_id == wf.id)
                .order_by(WorkflowVersion.version.desc())
            )
        )
        .scalars()
        .all()
    )
    by_version = {s.version: s for s in snaps}
    if from_version is None or to_version is None:
        if len(snaps) < 2:
            raise ValueError("need at least two versions to diff")
        to_version = to_version or snaps[0].version
        from_version = from_version or snaps[1].version
    for v in (from_version, to_version):
        if v not in by_version:
            raise KeyError(v)

    older = by_version[from_version]
    newer = by_version[to_version]
    diff = diff_workflow_graphs(older.graph, newer.graph)
    impact = await estimate_impact(db, wf, diff)
    return {
        "workflow_id": wf.id,
        "from": {"version": older.version, "created_at": _aware(older.created_at).isoformat() if older.created_at else None,
                 "name": older.name, "node_count": older.node_count},
        "to": {"version": newer.version, "created_at": _aware(newer.created_at).isoformat() if newer.created_at else None,
               "name": newer.name, "node_count": newer.node_count},
        **diff,
        "potential_impact": impact,
    }
