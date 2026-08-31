"""Artifacts API (v28) - chart PNGs / model pickles produced by runs.

Endpoints
---------
GET    /artifacts                  list metadata (newest first)
GET    /artifacts/{id}             one metadata row
GET    /artifacts/{id}/content     raw bytes (image/png etc.) - used by the
                                   executions drawer to render charts inline
DELETE /artifacts/{id}             drop metadata + file
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user, own_or_404
from ..db import get_db
from ..models import Artifact, Workflow
from ..services import artifacts as art_svc

router = APIRouter(prefix="/artifacts", tags=["artifacts"])


def _out(row: Artifact) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "filename": row.filename,
        "content_type": row.content_type,
        "size_bytes": row.size_bytes,
        "meta": row.meta or {},
        "workflow_id": row.workflow_id,
        "execution_id": row.execution_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "url": f"/api/v1/artifacts/{row.id}/content",
    }


async def _get_or_404(db: AsyncSession, artifact_id: str, user=None) -> Artifact:
    row = await db.get(Artifact, artifact_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if user is not None and row.workflow_id:
        # v37: an artifact of another user's workflow looks nonexistent
        wf = await db.get(Workflow, row.workflow_id)
        own_or_404(wf.owner_id if wf else None, user)
    return row


@router.get("")
async def list_artifacts(kind: str = "", limit: int = 100, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    q = select(Artifact).order_by(Artifact.created_at.desc()).limit(min(max(1, limit), 500))
    if kind:
        q = q.where(Artifact.kind == kind)
    if user is not None:
        # v37: keep artifacts of unclaimed or own workflows only
        from ..auth import visible_workflow_ids

        visible = await visible_workflow_ids(db, user)
        q = q.where((Artifact.workflow_id.is_(None)) | (Artifact.workflow_id.in_(visible)))
    rows = (await db.execute(q)).scalars().all()
    return [_out(r) for r in rows]


@router.get("/{artifact_id}")
async def get_artifact(artifact_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    return _out(await _get_or_404(db, artifact_id, user))


@router.get("/{artifact_id}/content")
async def get_content(artifact_id: str, db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, artifact_id)
    data = art_svc.read_bytes(row)
    return Response(content=data, media_type=row.content_type)


@router.delete("/{artifact_id}", status_code=204)
async def delete_artifact(artifact_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, artifact_id, user)
    art_svc.delete_file(row)
    await db.delete(row)
    await db.commit()
