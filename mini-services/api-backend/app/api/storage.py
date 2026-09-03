"""Storage API (v51 + v52) - status of the dataset storage backend, plus
the one-shot blob migration between backends.

GET  /storage            which backend holds dataset parquet today + liveness
PUT  /storage            NOT exposed: switching backends is a deployment
                         decision (PY8N_STORAGE_BACKEND + bucket/credentials
                         env), not a runtime toggle - mixing backends
                         mid-flight would strand blobs. The status endpoint
                         exists so operators (and the datasets UI badge) can
                         SEE what is configured.
POST /storage/migrate    (v52) copy every dataset blob from the CURRENT
                         backend to a target backend (s3|minio|gcs|local) -
                         idempotent (same-size blobs on the target are
                         skipped), verify-by-readback, optional dry run and
                         optional source cleanup. The active backend is
                         NEVER switched here; the response carries the
                         cutover hint.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..services import storage as storage_svc
from ..services import storage_migration as migration_svc

# Auth: the router is registered with the ENFORCED dependency pair in
# main.py (like every other admin surface), so anonymous callers get 401
# when PY8N_REQUIRE_AUTH=true and read access stays open in single-user
# mode - exactly the contract of the datasets router itself.
router = APIRouter(prefix="/storage", tags=["storage"])


@router.get("")
async def storage_status():
    """Configured dataset storage backend + live liveness probe."""
    return storage_svc.describe_storage(include_ping=True)


class MigrateTarget(BaseModel):
    kind: str = Field(default="s3", description="Target backend kind: s3|minio|gcs|local")
    bucket: str = Field(default="", max_length=200, description="Target bucket (s3|minio|gcs)")
    prefix: str = Field(default="", max_length=200, description="Optional key prefix inside the target")
    endpoint_url: str = Field(default="", max_length=500, description="S3-compatible endpoint (MinIO/Wasabi/R2)")
    region: str = Field(default="", max_length=64)
    access_key_id: str = Field(default="", max_length=200)
    secret_access_key: str = Field(default="", max_length=200)


class MigrateRequest(BaseModel):
    target: MigrateTarget
    dataset_ids: list[str] = Field(default_factory=list, description="Empty = every dataset")
    include_versions: bool = Field(default=True, description="Also migrate versions/{id}/v{N}.parquet snapshots")
    dry_run: bool = Field(default=False, description="Report what would move without moving anything")
    overwrite: bool = Field(default=False, description="Re-copy even when the target already holds the blob")
    delete_source: bool = Field(default=False, description="Remove source blobs AFTER a verified copy (move)")


@router.post("/migrate")
async def migrate_storage(body: MigrateRequest, db: AsyncSession = Depends(get_db)):
    """Copy dataset blobs from the current backend to a target backend."""
    try:
        return await migration_svc.migrate_blobs(
            db,
            target_cfg=body.target.model_dump(),
            dataset_ids=[i for i in body.dataset_ids if i],
            include_versions=body.include_versions,
            dry_run=body.dry_run,
            overwrite=body.overwrite,
            delete_source=body.delete_source,
        )
    except migration_svc.MigrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - backend failures surface as 502
        raise HTTPException(status_code=502, detail=f"migration failed: {exc}") from exc
