"""Model registry (v46) - versioned ML models behind ``trained_models``.

Every ``model_train`` run can register its fitted pipeline as a NEW VERSION
of a named model; activating a version makes it the one ``model_predict``
scores with (only one active version per name). The artifact holds the
pickle; this table holds the lineage: algorithm, task, target, features,
metrics, dataset link, row count, owner.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Artifact, TrainedModel


async def next_version(db: AsyncSession, name: str) -> int:
    last = (
        await db.execute(
            select(TrainedModel.version).where(TrainedModel.name == name).order_by(TrainedModel.version.desc()).limit(1)
        )
    ).scalar_one_or_none()
    return int(last or 0) + 1


async def deactivate_others(db: AsyncSession, name: str, keep_id: str | None = None) -> None:
    rows = (
        (await db.execute(select(TrainedModel).where(TrainedModel.name == name)))
        .scalars()
        .all()
    )
    for row in rows:
        if row.id != keep_id:
            row.active = False
            db.add(row)


async def register_model(
    db: AsyncSession,
    *,
    name: str,
    algorithm: str,
    task: str,
    target: str,
    features: list[str],
    metrics: dict,
    artifact_id: str | None,
    owner_id: str | None = None,
    dataset_name: str | None = None,
    row_count: int = 0,
    activate: bool = True,
) -> TrainedModel:
    """Create the next version of ``name``; first version auto-activates."""
    row = TrainedModel(
        name=name,
        version=await next_version(db, name),
        algorithm=algorithm,
        task=task,
        target=target,
        features=features,
        metrics=metrics or {},
        artifact_id=artifact_id,
        dataset_name=dataset_name,
        row_count=row_count,
        owner_id=owner_id,
    )
    row.active = True  # first version of a name is active by definition
    db.add(row)
    await db.flush()
    if activate:
        await deactivate_others(db, name, keep_id=row.id)
    return row


async def get_model(db: AsyncSession, model_id: str) -> TrainedModel | None:
    return await db.get(TrainedModel, model_id)


async def resolve_model(db: AsyncSession, ref: str, owner_id: str | None = None) -> TrainedModel | None:
    """Resolve by registry id first, then by name → ACTIVE version.

    Owner scoping matches the dataset service: another owner's claimed
    model is treated as not found.
    """
    row = await db.get(TrainedModel, ref)
    if row is None:
        # name → active version (newest created active row wins ties)
        q = select(TrainedModel).where(TrainedModel.name == ref.strip(), TrainedModel.active.is_(True))
        row = (await db.execute(q.order_by(TrainedModel.version.desc()))).scalars().first()
    if row is None:
        return None
    if owner_id is not None and row.owner_id is not None and row.owner_id != owner_id:
        return None
    return row


async def list_models(db: AsyncSession, owner_id: str | None = None) -> list[TrainedModel]:
    q = select(TrainedModel).order_by(TrainedModel.name, TrainedModel.version.desc())
    rows = (await db.execute(q)).scalars().all()
    if owner_id is not None:
        rows = [r for r in rows if r.owner_id is None or r.owner_id == owner_id]
    return rows


async def activate_version(db: AsyncSession, row: TrainedModel) -> TrainedModel:
    await deactivate_others(db, row.name, keep_id=row.id)
    row.active = True
    db.add(row)
    await db.flush()
    return row


async def delete_model(db: AsyncSession, row: TrainedModel, *, delete_artifact: bool = True) -> None:
    """Drop the registry row; the artifact (and its file) dies with the last
    reference so orphaned pickles never accumulate."""
    artifact_id = row.artifact_id
    await db.delete(row)
    await db.flush()
    if delete_artifact and artifact_id:
        artifact = await db.get(Artifact, artifact_id)
        if artifact is not None:
            from . import artifacts as art_svc

            art_svc.delete_file(artifact)
            await db.delete(artifact)


def model_out(row: TrainedModel) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "version": row.version,
        "algorithm": row.algorithm,
        "task": row.task,
        "target": row.target,
        "features": row.features or [],
        "metrics": row.metrics or {},
        "artifact_id": row.artifact_id,
        "dataset_name": row.dataset_name,
        "row_count": row.row_count,
        "active": row.active,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
