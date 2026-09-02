"""Artifact storage (v28) - chart PNGs, model pickles, future files.

Bytes live under ``data/artifacts/{id}.{ext}``; an ``artifacts`` row holds
metadata (kind, content_type, size, free-form meta). Nodes call
:func:`save_artifact` with their own session (subflow/agent pattern); the
API serves bytes back at GET /artifacts/{id}/content so the executions
drawer can render charts inline.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Artifact

EXT_BY_TYPE = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/svg+xml": "svg",
    "application/octet-stream": "pkl",
    "application/json": "json",
    "text/csv": "csv",  # v45 dataset exports
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/x-parquet": "parquet",
}


def artifacts_dir() -> Path:
    path = Path(settings.artifacts_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def artifact_path(artifact_id: str, content_type: str) -> Path:
    ext = EXT_BY_TYPE.get(content_type, "bin")
    return artifacts_dir() / f"{artifact_id}.{ext}"


async def save_artifact(
    db: AsyncSession,
    *,
    kind: str,
    data: bytes,
    content_type: str,
    meta: dict | None = None,
    filename: str = "",
    workflow_id: str | None = None,
    execution_id: str | None = None,
) -> Artifact:
    """Persist bytes + metadata; caller commits the session."""
    row = Artifact(
        kind=kind,
        filename=filename,
        content_type=content_type,
        size_bytes=len(data),
        meta=meta or {},
        workflow_id=workflow_id,
        execution_id=execution_id,
    )
    db.add(row)
    await db.flush()  # assigns the id used by the filename
    path = artifact_path(row.id, content_type)
    path.write_bytes(data)
    row.filename = path.name
    await db.flush()
    return row


def read_bytes(row: Artifact) -> bytes:
    return artifact_path(row.id, row.content_type).read_bytes()


def delete_file(row: Artifact) -> None:
    path = artifact_path(row.id, row.content_type)
    if path.exists():
        path.unlink()
