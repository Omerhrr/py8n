"""Storage migration tooling (v52) - copy dataset blobs between backends.

v51 made the dataset estate backend-pluggable (local | s3/minio | gcs),
but switching backends on an EXISTING install still faced an empty bucket:
every dataset blob lived on the old backend. This service is the one-shot
copy-over that closes that gap:

1. Enumerate every dataset's blobs - the live parquet (``{id}.parquet``)
   plus every point-in-time snapshot (``versions/{id}/v{N}.parquet``,
   straight from the dataset_versions table).
2. For each blob: read from the CURRENT backend, write to the TARGET
   backend, then read the copy back and byte-compare (integrity is the
   whole point of a migration).
3. Idempotent by default - a blob already present on the target (same
   size) is skipped, so re-runs after an interruption only move the
   remainder. ``overwrite`` forces a re-copy; ``delete_source`` turns the
   copy-over into a move AFTER a verified copy.

Design contracts:

- The ACTIVE backend never changes here. Switching is a deployment
  decision (``PY8N_STORAGE_BACKEND`` + the per-backend env), exactly as
  the v51 storage status endpoint documents; this tool only moves bytes
  and returns a per-dataset report plus a cutover hint.
- Reads go through the current backend, writes through a backend built
  from the request (so any source -> any target works, including
  remote -> remote). Nothing is written through the dataset service, so
  contracts, versions and lineage metadata are untouched.
- Every blob copy (a blocking network round trip) runs in a worker
  thread; the event loop keeps serving while megabytes move.
"""

from __future__ import annotations

import asyncio
import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Dataset, DatasetVersion
from .storage import S3Backend, StorageBackend, StorageError, get_backend

logger = logging.getLogger("py8n.storage_migration")

#: hard cap on blobs moved per request - a runaway migration must not
#: occupy the worker forever; larger estates pass dataset_ids in batches.
MAX_BLOBS_PER_RUN = 500


class MigrationError(Exception):
    """Raised for request-shaped problems (bad target config, etc.)."""


def build_target_backend(cfg: dict) -> StorageBackend:
    """Build the target backend from request config (never from env)."""
    kind = (cfg.get("kind") or "").strip().lower()
    if kind in ("s3", "minio"):
        return S3Backend(
            bucket=(cfg.get("bucket") or "").strip(),
            prefix=cfg.get("prefix") or "",
            endpoint_url=cfg.get("endpoint_url") or "",
            region=cfg.get("region") or "",
            access_key_id=cfg.get("access_key_id") or "",
            secret_access_key=cfg.get("secret_access_key") or "",
        )
    if kind == "gcs":
        from .storage import GCSBackend

        return GCSBackend(
            bucket=(cfg.get("bucket") or "").strip(),
            prefix=cfg.get("prefix") or "",
        )
    if kind == "local":
        from .storage import LocalBackend

        return LocalBackend()
    raise MigrationError(f"unknown target kind {kind!r} (use s3|minio|gcs|local)")


def target_summary(backend: StorageBackend) -> dict:
    """Target description safe to return to the client (no secrets)."""
    d = backend.describe()
    d.pop("root", None)
    return d


async def _enumerate_keys(db: AsyncSession, dataset: Dataset, include_versions: bool) -> list[str]:
    """Live blob key + every snapshot key for one dataset."""
    keys = [f"{dataset.id}.parquet"]
    if include_versions:
        rows = (
            await db.execute(
                select(DatasetVersion.version)
                .where(DatasetVersion.dataset_id == dataset.id)
                .order_by(DatasetVersion.version.asc())
            )
        ).scalars().all()
        keys.extend(f"versions/{dataset.id}/v{v}.parquet" for v in rows)
    return keys


def _copy_one(source: StorageBackend, target: StorageBackend, key: str) -> bytes:
    """Blocking copy + verify - runs in a worker thread."""
    data = source.read_bytes(key)
    target.write_bytes(key, data)
    if target.read_bytes(key) != data:
        raise StorageError(f"verification failed for {key} (read-back mismatch)")
    return data


def _delete_one(source: StorageBackend, key: str) -> None:
    try:
        source.delete(key)
    except Exception:  # noqa: BLE001 - a failed source delete is a warning, not a failure
        logger.warning("could not delete source blob %s after verified copy", key)


async def migrate_blobs(
    db: AsyncSession,
    *,
    target_cfg: dict,
    dataset_ids: list[str] | None = None,
    include_versions: bool = True,
    dry_run: bool = False,
    overwrite: bool = False,
    delete_source: bool = False,
) -> dict:
    """Copy every dataset blob from the current backend to the target.

    Returns a per-dataset report (copied | skipped | missing per blob) plus
    a summary. Raises MigrationError for bad configs. Never touches the
    active backend or any metadata - bytes only.
    """
    target = build_target_backend(target_cfg)
    source = get_backend()

    q = select(Dataset).order_by(Dataset.created_at.asc())
    if dataset_ids:
        q = q.where(Dataset.id.in_(dataset_ids))
    datasets = (await db.execute(q)).scalars().all()
    if dataset_ids:
        found = {d.id for d in datasets}
        missing = [i for i in dataset_ids if i not in found]
        if missing:
            raise MigrationError(f"dataset(s) not found: {', '.join(missing[:5])}")

    report: list[dict] = []
    total_copied = total_skipped = total_missing = total_bytes = 0
    plan_overflow = False

    for ds in datasets:
        keys = await _enumerate_keys(db, ds, include_versions)
        if len(keys) > MAX_BLOBS_PER_RUN:
            plan_overflow = True
        entries: list[dict] = []
        copied = skipped = missing = 0
        ds_bytes = 0
        for key in keys[:MAX_BLOBS_PER_RUN]:
            try:
                data = await asyncio.to_thread(source.read_bytes, key)
            except FileNotFoundError:
                entries.append({"key": key, "bytes": 0, "status": "missing"})
                missing += 1
                continue
            except StorageError as exc:
                entries.append({"key": key, "bytes": 0, "status": "missing", "detail": str(exc)[:120]})
                missing += 1
                continue

            if not dry_run and not overwrite:
                already = await asyncio.to_thread(target.exists, key)
                if already:
                    tlen = len(await asyncio.to_thread(target.read_bytes, key))
                    if tlen == len(data):
                        entries.append({"key": key, "bytes": len(data), "status": "skipped"})
                        skipped += 1
                        continue

            if dry_run:
                entries.append({"key": key, "bytes": len(data), "status": "would_copy"})
                copied += 1  # counts what WOULD be copied
                ds_bytes += len(data)
                continue

            await asyncio.to_thread(_copy_one, source, target, key)
            if delete_source:
                await asyncio.to_thread(_delete_one, source, key)
            entries.append({"key": key, "bytes": len(data), "status": "copied"})
            copied += 1
            ds_bytes += len(data)

        report.append({
            "dataset_id": ds.id,
            "name": ds.name,
            "blobs": entries,
            "copied": copied,
            "skipped": skipped,
            "missing": missing,
            "bytes": ds_bytes,
        })
        total_copied += copied
        total_skipped += skipped
        total_missing += missing
        total_bytes += ds_bytes

    return {
        "dry_run": dry_run,
        "target": target_summary(target),
        "source": {"kind": source.kind},
        "datasets": report,
        "summary": {
            "datasets": len(report),
            "blobs_copied": total_copied,
            "blobs_skipped": total_skipped,
            "blobs_missing": total_missing,
            "bytes_copied": total_bytes,
            "truncated": plan_overflow,
        },
        "cutover_hint": (
            "Blobs are copied; the ACTIVE backend is unchanged. Set "
            "PY8N_STORAGE_BACKEND (+ the bucket/credentials env for the "
            "target) and restart to serve the estate from the new backend."
        ),
    }
