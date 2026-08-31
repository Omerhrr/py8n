"""Workflow versioning helpers (v13) - snapshot + retention.

The ``WorkflowVersion`` model lives in ``models.py`` next to the other ORM
models; this module owns the write path: monotonic per-workflow version
numbers, snapshotting a workflow's current content, and pruning beyond the
retention cap.

Snapshots capture *content* (name, description, graph). Organizational
metadata (tags, is_active, error binding) is not versioned.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Workflow, WorkflowVersion

MAX_VERSIONS = 20


async def next_version(db: AsyncSession, workflow_id: str) -> int:
    last = (
        await db.execute(
            select(WorkflowVersion.version)
            .where(WorkflowVersion.workflow_id == workflow_id)
            .order_by(WorkflowVersion.version.desc())
            .limit(1)
        )
    ).scalar()
    return (last or 0) + 1


async def snapshot_workflow_version(db: AsyncSession, wf: Workflow) -> WorkflowVersion:
    """Persist a new version for ``wf``'s CURRENT state and prune old ones.

    Caller is responsible for committing (the API endpoints commit anyway to
    dodge the teardown-commit race).
    """
    snap = WorkflowVersion(
        workflow_id=wf.id,
        version=await next_version(db, wf.id),
        name=wf.name,
        description=wf.description or "",
        graph=wf.graph or {"nodes": [], "edges": []},
        tags=list(wf.tags or []),
        node_count=len((wf.graph or {}).get("nodes", [])),
    )
    db.add(snap)
    await db.flush()

    # Prune beyond the retention cap (keep the newest MAX_VERSIONS).
    stale = (
        await db.execute(
            select(WorkflowVersion.id)
            .where(WorkflowVersion.workflow_id == wf.id)
            .order_by(WorkflowVersion.version.desc())
            .offset(MAX_VERSIONS)
        )
    ).scalars().all()
    if stale:
        await db.execute(delete(WorkflowVersion).where(WorkflowVersion.id.in_(stale)))
    return snap
