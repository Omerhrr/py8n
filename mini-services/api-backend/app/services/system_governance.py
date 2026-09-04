"""System governance (v62) - role-specific templates + system-level roles.

Three governance surfaces land here:

* ROLES - ``system_members`` holds invited members (editor | viewer); the
  creator stays the single owner via ``py8n_systems.owner_id`` so pre-v62
  systems keep working with zero migration. ``member_role`` answers "what
  can this user do to this system" and ``require_role`` enforces it with
  the platform's fail-closed semantics: a system you are not part of
  looks nonexistent (404), an action above your role is 403.

* TEMPLATES - role-specific system starter kits. A data engineer, an ML
  engineer, an ops lead and a support lead do not want the same system:
  each template ships the pack (workflows + datasets, imported through the
  exact marketplace machinery so every graph passes the same validation
  gate user workflows go through) plus the dashboards/reports that role
  expects, and instantiates into a real Py8n System with everything bound.

* (dependency views live in system_dependencies.py - derived, never stored)
"""

from __future__ import annotations

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Dataset, Py8nSystem, SystemMember, User

ROLES = ("owner", "editor", "viewer")
ROLE_ORDER = {"viewer": 0, "editor": 1, "owner": 2}
INVITABLE_ROLES = ("editor", "viewer")


class RoleDenied(Exception):
    """Raised by require_role; the API layer maps it to 404/403."""

    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


async def member_role(db: AsyncSession, system: Py8nSystem, user) -> str | None:
    """The caller's role on this system, or None when they have none.

    * auth off (user None) or unauthenticated dev mode -> owner
      (matches the platform-wide convention: PY8N_REQUIRE_AUTH=false is a
      single-operator install)
    * creator (owner_id match) or unclaimed system (owner_id NULL) -> owner
      (bootstrap story: the first user claims unclaimed rows)
    * otherwise: the invited member's role, or None
    """
    if user is None:
        return "owner"
    uid = getattr(user, "id", None)
    if not uid:
        return "owner"
    if system.owner_id in (uid, None):
        return "owner"
    row = (
        await db.execute(
            select(SystemMember).where(SystemMember.system_id == system.id, SystemMember.user_id == uid)
        )
    ).scalar_one_or_none()
    return row.role if row else None


async def require_role(db: AsyncSession, system: Py8nSystem, user, min_role: str) -> str:
    """Enforce ``min_role``; foreign systems look nonexistent (404)."""
    if min_role not in ROLE_ORDER:
        raise RoleDenied(400, f"unknown role {min_role!r}")
    role = await member_role(db, system, user)
    if role is None:
        raise RoleDenied(404, "System not found")
    if ROLE_ORDER[role] < ROLE_ORDER[min_role]:
        raise RoleDenied(403, f"this action requires the {min_role} role (you are {role})")
    return role


async def visible_system_ids(db: AsyncSession, user_id: str | None) -> list[str] | None:
    """System ids the user may READ, or None = no auth (everything)."""
    if user_id is None:
        return None
    member_rows = (
        await db.execute(select(SystemMember.system_id).where(SystemMember.user_id == user_id))
    ).scalars().all()
    return list(member_rows)


async def member_list(db: AsyncSession, system: Py8nSystem) -> list[dict]:
    """Members with the owner first (synthesized from owner_id)."""
    rows = (
        await db.execute(
            select(SystemMember, User).join(User, User.id == SystemMember.user_id, isouter=True)
            .where(SystemMember.system_id == system.id)
            .order_by(SystemMember.added_at)
        )
    ).all()
    out: list[dict] = []
    owner_row = await db.get(User, system.owner_id) if system.owner_id else None
    if system.owner_id:
        out.append({
            "user_id": system.owner_id,
            "email": owner_row.email if owner_row else None,
            "name": owner_row.name if owner_row else None,
            "role": "owner",
            "added_at": system.created_at.isoformat() if system.created_at else None,
            "is_owner": True,
        })
    for member, user in rows:
        if member.user_id == system.owner_id:
            continue  # owner row synthesized above
        out.append({
            "user_id": member.user_id,
            "email": user.email if user else None,
            "name": user.name if user else None,
            "role": member.role,
            "added_at": member.added_at.isoformat() if member.added_at else None,
            "is_owner": False,
        })
    return out


async def invite_member(db: AsyncSession, system: Py8nSystem, email: str, role: str) -> SystemMember:
    """Add an editor/viewer by email. Owner is unique - never invited."""
    if role not in INVITABLE_ROLES:
        raise ValueError(f"role must be one of {', '.join(INVITABLE_ROLES)} - ownership is not shared")
    user = (
        await db.execute(select(User).where(User.email == email.strip().lower()))
    ).scalar_one_or_none()
    if user is None:
        raise LookupError(f"no user with email {email!r}")
    if user.id == system.owner_id:
        raise ValueError("that user is the system owner")
    dup = (
        await db.execute(
            select(SystemMember).where(SystemMember.system_id == system.id, SystemMember.user_id == user.id)
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise ValueError(f"{email} is already a {dup.role} on this system")
    member = SystemMember(system_id=system.id, user_id=user.id, role=role)
    db.add(member)
    await db.flush()
    return member


async def set_member_role(db: AsyncSession, system: Py8nSystem, user_id: str, role: str) -> SystemMember:
    if role not in INVITABLE_ROLES:
        raise ValueError(f"role must be one of {', '.join(INVITABLE_ROLES)} - ownership is not shared")
    if user_id == system.owner_id:
        raise ValueError("the system owner's role is fixed - ownership is not shared")
    member = (
        await db.execute(
            select(SystemMember).where(SystemMember.system_id == system.id, SystemMember.user_id == user_id)
        )
    ).scalar_one_or_none()
    if member is None:
        raise LookupError("that user is not a member of this system")
    member.role = role
    db.add(member)
    await db.flush()
    return member


async def remove_member(db: AsyncSession, system: Py8nSystem, user_id: str) -> None:
    if user_id == system.owner_id:
        raise ValueError("the system owner cannot be removed - dissolve the system instead")
    member = (
        await db.execute(
            select(SystemMember).where(SystemMember.system_id == system.id, SystemMember.user_id == user_id)
        )
    ).scalar_one_or_none()
    if member is None:
        raise LookupError("that user is not a member of this system")
    await db.delete(member)
    await db.flush()


# ----------------------------------------------------------------------
# Role-specific system templates (v62)
# ----------------------------------------------------------------------
# Each template: a curated pack (workflows + datasets, imported through the
# SAME machinery as marketplace installs - validation gate, dataset name
# collision handling) plus the dashboard/report the role expects, and the
# system shell everything lands in. Deterministic and in-code: the catalog
# cannot drift from what instantiate actually creates.

def _node(nid: str, ntype: str, name: str, params: dict | None = None, x: int = 0, y: int = 0) -> dict:
    return {"id": nid, "type": ntype, "name": name, "position": {"x": x, "y": y}, "parameters": params or {}}


def _edge(src: str, dst: str) -> dict:
    return {"id": f"{src}->{dst}", "source": src, "target": dst, "sourceHandle": "main", "targetHandle": "main"}


def _pack(workflows: list[dict], datasets: list[dict]) -> dict:
    return {
        "format": "py8n-pack",
        "pack_version": 1,
        "workflows": workflows,
        "datasets": datasets,
    }


SAMPLE_EVENTS = [
    {"event_id": "e-1001", "user_id": "u-01", "action": "signup", "channel": "web", "amount": 0},
    {"event_id": "e-1002", "user_id": "u-02", "action": "purchase", "channel": "web", "amount": 120.5},
    {"event_id": "e-1003", "user_id": "u-03", "action": "signup", "channel": "mobile", "amount": 0},
    {"event_id": "e-1001", "user_id": "u-01", "action": "signup", "channel": "web", "amount": 0},  # dup
    {"event_id": "e-1004", "user_id": "u-02", "action": "purchase", "channel": None, "amount": 89.9},
    {"event_id": "e-1005", "user_id": "u-04", "action": "refund", "channel": "mobile", "amount": 42.0},
]

SAMPLE_TICKETS = [
    {"ticket_id": "t-2001", "subject": "Cannot log in", "priority": "high", "status": "open", "queue": "auth"},
    {"ticket_id": "t-2002", "subject": "Refund not received", "priority": "high", "status": "open", "queue": "billing"},
    {"ticket_id": "t-2003", "subject": "How to export data?", "priority": "low", "status": "open", "queue": "howto"},
    {"ticket_id": "t-2004", "subject": "Page loads slowly", "priority": "medium", "status": "open", "queue": "performance"},
    {"ticket_id": "t-2005", "subject": "Cannot log in again", "priority": "high", "status": "open", "queue": "auth"},
]

CHURN_ROWS = [
    {"customer_id": f"c-{i:03d}", "tenure": 40 - i, "monthly_spend": 20 + (i * 7) % 60,
     "support_tickets": i % 4, "churned": "yes" if (i * 13) % 7 < 3 else "no"}
    for i in range(1, 25)
]

OPS_METRICS = [
    {"service": "api", "ts": f"2026-09-0{d}T0{h}:00:00Z", "latency_ms": 120 + d * h, "errors": h % 3}
    for d in range(1, 6)
    for h in range(1, 5)
]

SYSTEM_TEMPLATES: list[dict] = [
    {
        "slug": "ingestion-quality",
        "name": "Ingestion & Quality",
        "role": "data_engineer",
        "tagline": "Raw intake lands dirty; a quality-gated pipeline lands it clean.",
        "icon": "database",
        "color": "#22d3ee",
        "outcomes": [
            "raw_events dataset with sample rows",
            "cleaning pipeline: dedupe -> quality gate -> upsert write",
            "clean_events dataset (pipeline writes here)",
            "weekly CSV export of the clean data",
        ],
        "pack": _pack(
            workflows=[{
                "name": "clean_raw_events",
                "description": "Dedupes raw intake, quality-gates it and upserts into clean_events.",
                "graph": {
                    "nodes": [
                        _node("trig", "manual_trigger", "Run cleaning"),
                        _node("read", "dataset_read", "raw_events", {"dataset": "raw_events", "limit": 500}),
                        _node("dedupe", "remove_duplicates", "Drop duplicate events",
                              {"key_columns": "event_id", "keep": "first"}),
                        _node("quality", "data_quality", "Quality gate",
                              {"checks": "nulls:channel,unique:event_id", "on_fail": "warn"}),
                        _node("write", "dataset_write", "clean_events",
                              {"dataset": "clean_events", "mode": "upsert", "key_columns": "event_id"}),
                    ],
                    "edges": [_edge("trig", "read"), _edge("read", "dedupe"),
                              _edge("dedupe", "quality"), _edge("quality", "write")],
                },
            }],
            datasets=[
                {"name": "raw_events", "description": "Raw event intake (sample rows include duplicates + nulls).",
                 "rows": SAMPLE_EVENTS},
                {"name": "clean_events", "description": "Quality-gated clean events - the pipeline writes here.",
                 "rows": []},
            ],
        ),
        "report": {"name": "clean_events weekly export", "cron": "0 6 * * 1", "fmt": "csv"},
        "dashboard": False,
    },
    {
        "slug": "mlops-foundation",
        "name": "ML Ops Foundation",
        "role": "ml_engineer",
        "tagline": "Train a churn scorer, register it, PSI-gate every future batch.",
        "icon": "network",
        "color": "#818cf8",
        "outcomes": [
            "churn_train dataset with sample training rows",
            "training pipeline: model_train (registry) -> drift_check (PSI gate)",
            "churn_model registered as versioned model",
            "monthly model report",
        ],
        "pack": _pack(
            workflows=[{
                "name": "train_churn_model",
                "description": "Trains + registers churn_model, then PSI-gates the training batch as a smoke check.",
                "graph": {
                    "nodes": [
                        _node("trig", "manual_trigger", "Retrain"),
                        _node("read", "dataset_read", "churn_train",
                              {"dataset": "churn_train", "limit": 1000}),
                        _node("train", "model_train", "Train churn_scorer",
                              {"model": "random_forest_classifier", "target": "churned",
                               "features": "tenure,monthly_spend,support_tickets",
                               "cross_validation": 3, "model_name": "churn_model", "register": True}),
                        _node("drift", "drift_check", "PSI smoke gate",
                              {"model": "churn_model", "on_drift": "warn"}),
                    ],
                    "edges": [_edge("trig", "read"), _edge("read", "train"), _edge("train", "drift")],
                },
            }],
            datasets=[
                {"name": "churn_train", "description": "Churn training rows (sample).",
                 "rows": CHURN_ROWS},
            ],
        ),
        "report": {"name": "churn_model monthly report", "cron": "0 7 1 * *", "fmt": "json"},
        "dashboard": False,
    },
    {
        "slug": "operations-command",
        "name": "Operations Command",
        "role": "ops_lead",
        "tagline": "Service metrics land on a schedule; a dashboard answers 'are we OK?'.",
        "icon": "activity",
        "color": "#f97316",
        "outcomes": [
            "ops_metrics dataset with sample service metrics",
            "scheduled 5-minute collection pipeline with stats rollup",
            "ops dashboard (stat + chart + table)",
            "daily ops report",
        ],
        "pack": _pack(
            workflows=[{
                "name": "collect_ops_metrics",
                "description": "Scheduled rollup: reads ops_metrics, aggregates per service, writes ops_summary.",
                "graph": {
                    "nodes": [
                        _node("trig", "schedule_trigger", "Every 5 minutes",
                              {"cron": "*/5 * * * *"}),
                        _node("read", "dataset_read", "ops_metrics",
                              {"dataset": "ops_metrics", "limit": 1000}),
                        _node("agg", "summarize", "Per-service rollup",
                              {"group_by": "service", "aggregations": "latency_ms:mean,errors:sum"}),
                        _node("write", "dataset_write", "ops_summary",
                              {"dataset": "ops_summary", "mode": "replace"}),
                    ],
                    "edges": [_edge("trig", "read"), _edge("read", "agg"), _edge("agg", "write")],
                },
            }],
            datasets=[
                {"name": "ops_metrics", "description": "Raw service metrics (sample rows).",
                 "rows": OPS_METRICS},
                {"name": "ops_summary", "description": "Per-service rollup - the pipeline writes here.",
                 "rows": []},
            ],
        ),
        "report": {"name": "ops daily report", "cron": "0 8 * * *", "fmt": "csv"},
        "dashboard": True,
    },
    {
        "slug": "support-desk",
        "name": "Support Desk",
        "role": "support_lead",
        "tagline": "Tickets in, priorities triaged, a queue summary your team can read.",
        "icon": "life-buoy",
        "color": "#34d399",
        "outcomes": [
            "tickets dataset with sample queue rows",
            "triage pipeline: high-priority filter -> escalations dataset",
            "queue summary rollup into ticket_summary",
            "daily desk report",
        ],
        "pack": _pack(
            workflows=[{
                "name": "triage_tickets",
                "description": "Filters high-priority tickets into escalations and rolls the queue summary.",
                "graph": {
                    "nodes": [
                        _node("trig", "manual_trigger", "Run triage"),
                        _node("read", "dataset_read", "tickets", {"dataset": "tickets", "limit": 500}),
                        _node("high", "filter", "High priority only",
                              {"condition": "{{priority}} == 'high'"}),
                        _node("esc", "dataset_write", "escalations",
                              {"dataset": "escalations", "mode": "replace"}),
                        _node("sum", "summarize", "Queue summary",
                              {"group_by": "queue", "aggregations": "ticket_id:count"}),
                        _node("wr", "dataset_write", "ticket_summary",
                              {"dataset": "ticket_summary", "mode": "replace"}),
                    ],
                    "edges": [_edge("trig", "read"), _edge("read", "high"), _edge("high", "esc"),
                              _edge("read", "sum"), _edge("sum", "wr")],
                },
            }],
            datasets=[
                {"name": "tickets", "description": "Support tickets (sample rows).", "rows": SAMPLE_TICKETS},
                {"name": "escalations", "description": "High-priority tickets land here.", "rows": []},
                {"name": "ticket_summary", "description": "Per-queue summary - the pipeline writes here.", "rows": []},
            ],
        ),
        "report": {"name": "support desk daily report", "cron": "0 9 * * *", "fmt": "csv"},
        "dashboard": True,
    },
]

TEMPLATE_ROLES = ("data_engineer", "ml_engineer", "ops_lead", "support_lead")


def template_summaries(role: str = "") -> list[dict]:
    """The catalog as the UI shows it - no pack internals."""
    needle = (role or "").strip().lower()
    out = []
    for t in SYSTEM_TEMPLATES:
        if needle and t["role"] != needle:
            continue
        out.append({
            "slug": t["slug"],
            "name": t["name"],
            "role": t["role"],
            "tagline": t["tagline"],
            "icon": t["icon"],
            "color": t["color"],
            "outcomes": t["outcomes"],
            "workflows": [w["name"] for w in t["pack"]["workflows"]],
            "datasets": [d["name"] for d in t["pack"]["datasets"]],
        })
    return out


def get_template(slug: str) -> dict:
    for t in SYSTEM_TEMPLATES:
        if t["slug"] == slug:
            return t
    raise LookupError(f"no template {slug!r}")


async def instantiate_template(db: AsyncSession, template: dict, owner_id: str | None,
                               imported: dict) -> dict:
    """Bind the pack-import result into a fresh system + template extras.

    The API layer runs the pack import first (it commits internally and
    returns created workflows/datasets); this function creates the system
    shell, binds every created object, then adds the dashboard/report the
    role expects. ``imported`` is the ``_import_pack_doc`` result with
    ``workflows`` / ``datasets`` lists of {id, name}.
    """
    from ..models import Dashboard, ScheduledReport, SystemComponent
    from . import dashboards as dash_svc

    system = Py8nSystem(
        name=template["name"],
        description=f"{template['tagline']} (from the {template['role']} template)",
        icon=template["icon"],
        color=template["color"],
    )
    system.owner_id = owner_id
    db.add(system)
    await db.flush()

    created: dict = {
        "workflows": list(imported.get("workflows") or []),
        "datasets": list(imported.get("datasets") or []),
        "dashboard": None,
        "report": None,
    }
    for wf in created["workflows"]:
        db.add(SystemComponent(system_id=system.id, kind="workflow", ref_id=wf["id"]))
    for ds in created["datasets"]:
        db.add(SystemComponent(system_id=system.id, kind="dataset", ref_id=ds["id"]))

    if template.get("dashboard") and created["datasets"]:
        first = created["datasets"][0]
        ds_row = await db.get(Dataset, first["id"])
        config = dash_svc.generate_config([(ds_row, pd.DataFrame())])
        board = Dashboard(
            name=f"{template['name']} dashboard",
            slug=await dash_svc.unique_slug(db, f"{template['name']} dashboard"),
            description=f"Auto-created by the {template['role']} template",
            config=config,
            status="draft",
        )
        board.owner_id = owner_id
        db.add(board)
        await db.flush()
        created["dashboard"] = {"id": board.id, "name": board.name}
        db.add(SystemComponent(system_id=system.id, kind="dashboard", ref_id=board.id))

    if template.get("report") and created["datasets"]:
        spec = template["report"]
        rep = ScheduledReport(
            name=spec["name"], source_type="dataset",
            source_id=created["datasets"][0]["id"],
            fmt=spec.get("fmt") or "csv", cron=spec.get("cron") or "0 6 * * 1", enabled=True,
        )
        rep.owner_id = owner_id
        db.add(rep)
        await db.flush()
        created["report"] = {"id": rep.id, "name": rep.name, "cron": rep.cron}
        db.add(SystemComponent(system_id=system.id, kind="report", ref_id=rep.id))

    return {"system_id": system.id, "created": created}
