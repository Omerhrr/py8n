"""AI Operations (v58) - the agent gets operational context.

The roadmap's v1.58 picture: instead of an AI agent only querying
datasets, give it the OPERATIONAL context py8n already derives - the
workflow, the failed execution, the node, the input, the error, the
previous successful run, dataset health and the recent graph changes -
so it can answer "why did our daily sales pipeline fail?" and, crucially,
**propose** a change that the USER then executes.

This module is deliberately split in two:

**investigate()** - a deterministic investigation walk (no LLM in the
loop): the roadmap's 7-step checklist, a rule-based cause classifier on
the error text (rate limit / timeout / auth / connection / contract /
code / validation), a recommendation, the affected surface (from the v55
impact engine) and a STRUCTURED proposed action the UI can offer to
apply (e.g. raise the workflow policy backoff). Deterministic means the
answer is testable and cannot hallucinate a cause that contradicts the
evidence.

**narrate()** - the optional LLM layer: the structured findings are sent
to the sandbox-bridge chat completion (the same free provider the
llm_chat node uses) to produce a natural-language incident report. It is
fail-soft by design: an unreachable bridge returns ``narration=None``
with a note - the findings stand on their own.

Nothing is stored; every investigation re-derives the truth.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Workflow, WorkflowVersion
from .ops import incident_chain

NARRATE_TIMEOUT = 30.0

# --- cause classification rules (first match wins) -------------------------
_CAUSE_RULES: list[dict] = [
    {"kind": "rate_limit", "label": "API rate limit", "patterns": ["429", "rate limit", "too many requests", "quota exceeded"]},
    {"kind": "timeout", "label": "Timeout", "patterns": ["timeout", "timed out", "deadline exceeded"]},
    {"kind": "auth", "label": "Authentication / authorization", "patterns": ["401", "403", "unauthorized", "forbidden", "invalid api key", "authentication", "permission denied", "signature"]},
    {"kind": "connection", "label": "Connection / network", "patterns": ["connection", "connect", "dns", "network", "unreachable", "refused", "reset by peer", "getaddrinfo"]},
    {"kind": "contract", "label": "Data contract violation", "patterns": ["data contract violated", "contract violation"]},
    {"kind": "code", "label": "Node code error", "patterns": ["zerodivision", "nameerror", "typeerror", "keyerror", "attributeerror", "indexerror", "valueerror", "runtimeerror", "nodeexecutionerror"]},
]

_RECOMMENDATIONS: dict[str, str] = {
    "rate_limit": "The upstream API is throttling calls. Increase the retry backoff and retry count on this workflow so transient 429s are absorbed instead of failing the run.",
    "timeout": "The node exceeded its time budget. Raise the timeout (node timeout_ms or workflow timeout_seconds policy) and consider splitting the work into smaller batches.",
    "auth": "The credential was rejected. Rotate or fix the credential in the vault and re-test the node; the data itself looks fine.",
    "connection": "The upstream endpoint was unreachable. Add retries with exponential backoff so transient network blips are absorbed, and verify the endpoint/URL.",
    "contract": "The incoming data broke the dataset's contract. Either fix the upstream data or relax the contract (warn mode) - check the contract diff before deciding.",
    "code": "The node's own logic failed. Fix the node code (the input it received is captured in the drilldown) and re-run.",
    "validation": "The payload failed validation. Inspect the node input - the upstream producer changed shape.",
    "unknown": "No known failure signature matched. Walk the chain manually - the node input and the previous successful run are the best starting points.",
}


def classify_cause(error_text: str) -> dict:
    """Rule-based cause classification over the error text (evidence kept)."""
    text = (error_text or "").lower()
    for rule in _CAUSE_RULES:
        for p in rule["patterns"]:
            if p in text:
                idx = text.find(p)
                return {
                    "kind": rule["kind"],
                    "label": rule["label"],
                    "evidence": (error_text[max(0, idx - 30): idx + 60] or p),
                    "confidence": "high",
                }
    return {"kind": "unknown", "label": "Unclassified", "evidence": (error_text or "")[:90], "confidence": "low"}


def _proposed_action(cause_kind: str, chain: dict, wf_policy: dict | None) -> dict | None:
    """A structured, user-executable change proposal (AI proposes; user applies)."""
    if cause_kind in ("rate_limit", "connection"):
        current = dict(wf_policy or {})
        backoff = int(current.get("backoff_ms") or 500)
        retries = int(current.get("retries") or 0)
        return {
            "kind": "policy_patch",
            "workflow_id": chain["workflow"]["id"],
            "patch": {
                "retries": min(retries + 2, 5),
                "backoff_ms": min(max(backoff * 4, 2000), 60_000),
                "backoff_multiplier": float(current.get("backoff_multiplier") or 2.0),
            },
            "rationale": "raise retries to absorb throttling and multiply the backoff so repeated 429/connection errors wait longer between attempts",
        }
    if cause_kind == "timeout":
        current = dict(wf_policy or {})
        timeout = int(current.get("timeout_seconds") or 30)
        return {
            "kind": "policy_patch",
            "workflow_id": chain["workflow"]["id"],
            "patch": {"timeout_seconds": min(timeout * 4, 3600)},
            "rationale": "give every node a larger time budget - the previous successful run completed, so this looks like growth, not a broken step",
        }
    return None


async def _recent_change_check(db: AsyncSession, chain: dict) -> dict:
    """Did the graph change since the previous successful run? (roadmap check 7)"""
    prev = chain.get("comparison_with_previous_success")
    if not prev or not prev.get("previous_started_at"):
        return {"checked": True, "changed": None, "detail": "no previous successful run to compare against"}
    prev_started = prev["previous_started_at"]
    try:
        since = datetime.fromisoformat(prev_started)
    except ValueError:
        return {"checked": True, "changed": None, "detail": "could not parse the previous run timestamp"}

    wf = await db.get(Workflow, chain["workflow"]["id"])
    if wf is None:
        return {"checked": False, "changed": None, "detail": "workflow gone"}
    snaps = (
        (
            await db.execute(
                select(WorkflowVersion)
                .where(WorkflowVersion.workflow_id == wf.id)
                .order_by(WorkflowVersion.version.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    since_aware = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    # snapshots saved AFTER the last success started (a save mid-incident counts:
    # it may BE the change that broke it)
    changed_after = [s for s in snaps if s.created_at and s.created_at.replace(tzinfo=timezone.utc) >= since_aware]
    if not changed_after:
        return {"checked": True, "changed": False, "detail": "no graph changes since the last success"}

    # diff the newest post-success snapshot against the one just before it
    newer = changed_after[-1]
    older_candidates = [s for s in snaps if s.version < newer.version]
    detail = f"graph changed since the last success (version {newer.version} saved)"
    summary = None
    if older_candidates:
        from .workflow_intel import diff_workflow_graphs

        older = older_candidates[0]
        d = diff_workflow_graphs(older.graph, newer.graph)
        if not d["identical"]:
            summary = d["summary"]
            detail = f"graph changed since the last success (v{older.version} -> v{newer.version}): {summary}"
    return {"checked": True, "changed": True, "detail": detail, "summary": summary,
            "from_version": older_candidates[0].version if older_candidates else None,
            "to_version": newer.version}


async def investigate(db: AsyncSession, user_id: str | None, execution_id: str, narrate: bool = False) -> dict:
    """The full AI-operations investigation for one execution (deterministic)."""
    chain = await incident_chain(db, user_id, execution_id)
    if not chain:
        return {}

    error_text = chain["execution"].get("error") or ""
    node_error = (chain.get("failed_node") or {}).get("error") or error_text
    cause = classify_cause(node_error or error_text)

    comp = chain.get("comparison_with_previous_success")
    # supporting evidence: a node that worked before and fails now, with an
    # unchanged graph, points at the world; a brand-new node points at the edit
    hints = []
    if comp and comp.get("node"):
        if not comp["node"].get("present_in_previous"):
            hints.append("the failing node is NEW since the last successful run")
        elif comp["node"].get("previous_status") == "success":
            hints.append("the same node succeeded on the previous run")
    change = await _recent_change_check(db, chain)
    if change.get("changed"):
        hints.append(f"the graph changed recently: {change.get('summary') or 'see version history'}")

    proposal = _proposed_action(cause["kind"], chain, None)
    if proposal:
        # read the CURRENT policy so the patch builds on it
        wf = await db.get(Workflow, chain["workflow"]["id"])
        proposal = _proposed_action(cause["kind"], chain, (wf.policy_json if wf else None) or {})

    affected = {
        "severity": chain.get("severity"),
        "datasets": [d["name"] for d in chain.get("related_datasets", [])],
        "impacts": [
            {"dataset": i.get("dataset", {}).get("name"), "totals": i.get("totals"),
             "highest_risk": i.get("highest_risk"), "severity": i.get("severity")}
            for i in chain.get("impact", [])
        ],
    }

    checklist = [
        {"step": "workflow_identified", "ok": True,
         "detail": chain["workflow"]["name"], "ref": chain["workflow"]["ref"]},
        {"step": "failed_execution_identified", "ok": True,
         "detail": f"{chain['execution']['status']} · {chain['execution']['trigger_type']} · {chain['execution']['started_at']}",
         "ref": chain["execution"]["ref"]},
        {"step": "failed_node_identified",
         "ok": chain.get("failed_node") is not None,
         "detail": f"{(chain.get('failed_node') or {}).get('node_name') or 'no node-level failure recorded'}"
                   f" ({(chain.get('failed_node') or {}).get('node_type') or '-'})",
         "ref": None},
        {"step": "error_inspected", "ok": bool(node_error),
         "detail": (node_error or "no error text")[:200], "ref": None},
        {"step": "previous_run_compared", "ok": comp is not None,
         "detail": (f"previous success {comp['previous_execution_id'][:8]}: "
                    f"{comp['previous_duration_ms']}ms vs {comp['failed_duration_ms']}ms")
                   if comp else "no previous successful run on record",
         "ref": f"/executions/{comp['previous_execution_id']}" if comp else None},
        {"step": "dataset_health_checked",
         "ok": bool(chain.get("related_datasets")),
         "detail": ", ".join(f"{d['name']}={d['health']['score'] if d.get('health') else 'unscored'}"
                             for d in chain.get("related_datasets", [])[:4]) or "no datasets touched by this workflow",
         "ref": None},
        {"step": "recent_changes_checked", "ok": change.get("checked", False),
         "detail": change.get("detail") or "checked", "ref": None},
    ]

    narration = None
    narration_note = None
    if narrate:
        narration, narration_note = await _narrate(checklist, cause, chain, affected)

    return {
        "execution_id": chain["execution"]["id"],
        "workflow_id": chain["workflow"]["id"],
        "severity": chain.get("severity"),
        "checklist": checklist,
        "cause": cause,
        "hints": hints,
        "recommendation": _RECOMMENDATIONS.get(cause["kind"], _RECOMMENDATIONS["unknown"]),
        "affected": affected,
        "proposed_action": proposal,
        "narration": narration,
        "narration_note": narration_note,
        "disclaimer": "AI proposes - py8n/user executes. The proposed change is applied only when you apply it.",
    }


async def _narrate(checklist: list[dict], cause: dict, chain: dict, affected: dict) -> tuple[str | None, str | None]:
    """Optional LLM narration through the free sandbox bridge (fail-soft)."""
    facts = {
        "workflow": chain["workflow"]["name"],
        "execution": {"status": chain["execution"]["status"], "trigger": chain["execution"]["trigger_type"],
                      "duration_ms": chain["execution"]["duration_ms"]},
        "failed_node": (chain.get("failed_node") or {}).get("node_name"),
        "error": (chain.get("failed_node") or {}).get("error") or chain["execution"].get("error"),
        "cause": cause,
        "affected": {k: affected[k] for k in ("severity", "datasets") if k in affected},
    }
    system = (
        "You are py8n's operations copilot. You get structured facts about a failed "
        "automation run (already investigated deterministically). Write a SHORT incident "
        "report for a busy operator: what failed, the cause, the recommendation, what is "
        "affected. Max 120 words, plain text, no preamble."
    )
    user = "Facts (JSON):\n" + _safe_json(facts) + "\n\nChecklist results:\n" + "\n".join(
        f"- {c['step']}: {'ok' if c['ok'] else 'missing'} - {c['detail']}" for c in checklist
    )
    url = f"{settings.llm_bridge_url.rstrip('/')}/v1/chat/completions"
    payload = {
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.2,
        "max_tokens": 400,
    }
    try:
        async with httpx.AsyncClient(timeout=NARRATE_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code >= 400:
            return None, f"LLM bridge returned HTTP {resp.status_code} - findings above are the deterministic investigation"
        data = resp.json()
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
        return (content or None), (None if content else "LLM bridge returned an empty completion")
    except Exception as exc:  # bridge down = findings still stand
        return None, f"LLM bridge unreachable ({type(exc).__name__}) - findings above are the deterministic investigation"


def _safe_json(value) -> str:
    import json

    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)
