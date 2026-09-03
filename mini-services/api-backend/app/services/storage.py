"""Pluggable dataset storage backends (v51) - where dataset parquet lives.

Every dataset's ROWS are one parquet BLOB addressed by a KEY relative to
the datasets root::

    {dataset_id}.parquet                   the live rows
    versions/{dataset_id}/v{N}.parquet     point-in-time snapshots

The backend decides where those blobs physically live:

* ``local`` (default) - ``data/datasets/`` on the instance disk, exactly
  as every version before v51 stored them. Writes stay atomic (temp file
  + ``os.replace``) and reads are direct DuckDB reads; nothing changes
  for existing installs.
* ``s3`` - AWS S3 or any S3-compatible object store (MinIO, Wasabi,
  R2, ...) via ``endpoint_url``. boto3 is already a dependency (the v50
  s3_source connector uses it).
* ``gcs`` - Google Cloud Storage via ``google-cloud-storage`` (imported
  lazily and only when selected; credentials come from ADC or
  ``GOOGLE_APPLICATION_CREDENTIALS`` as usual).

Design rules the whole feature hangs on:

1. **DuckDB stays the compute engine.** The backend only moves BYTES:
   DuckDB writes a local temp parquet, the backend uploads it; reads
   download to a short-lived local temp that DuckDB queries. Queries
   never run against the network directly, so ``run_sql``, dashboards,
   apps and the engine keep one identical code path.
2. **Keys, not paths.** Service code keeps addressing blobs with the
   familiar ``Path`` objects (``parquet_path`` / ``version_file``); the
   key is derived relative to the datasets root, so a backend switch is
   an env change, not a code change.
3. **No read cache in v51.** Every remote read downloads fresh, which
   keeps multi-worker (Celery) semantics trivially correct; re-downloads
   of the same blob inside one request are bounded by DuckDB's own speed
   on local files.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from abc import ABC, abstractmethod
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)


class StorageError(Exception):
    """Raised when a backend operation fails (wrapped, user-safe message)."""


class StorageBackend(ABC):
    """Minimal object-store surface the dataset service needs.

    Six verbs: read, write, exists, delete, copy (server-side when the
    backend can), delete_prefix (version wipe). Keys are POSIX-style
    relative paths (``"abc.parquet"``, ``"versions/abc/v3.parquet"``).
    """

    kind: str = "abstract"

    @abstractmethod
    def read_bytes(self, key: str) -> bytes:
        """Blob bytes; raises FileNotFoundError when the key is absent."""

    @abstractmethod
    def write_bytes(self, key: str, data: bytes) -> None:
        """Create or overwrite the blob."""

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def delete(self, key: str) -> None:
        """Delete one blob; deleting an absent key is a no-op."""

    @abstractmethod
    def copy(self, src_key: str, dst_key: str) -> None:
        """Copy within the backend (server-side when supported)."""

    @abstractmethod
    def delete_prefix(self, prefix: str) -> None:
        """Delete every blob under a prefix (used by delete_versions)."""

    def describe(self) -> dict:
        return {"kind": self.kind}

    def ping(self) -> bool:
        """Cheap liveness probe: write/read/delete a probe blob."""
        key = f".ping/{uuid.uuid4().hex}.txt"
        try:
            self.write_bytes(key, b"py8n")
            ok = self.read_bytes(key) == b"py8n"
            self.delete(key)
            return ok
        except Exception:  # noqa: BLE001 - probe must never raise
            return False


# ----------------------------------------------------------------- local
class LocalBackend(StorageBackend):
    """Instance-disk storage under ``settings.datasets_dir`` (pre-v51 layout)."""

    kind = "local"

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or settings.datasets_dir)

    def _resolve(self, key: str) -> Path:
        clean = (key or "").strip().lstrip("/")
        if not clean or ".." in Path(clean).parts or Path(clean).is_absolute():
            raise StorageError(f"invalid storage key {key!r}")
        return self.root / clean

    def read_bytes(self, key: str) -> bytes:
        path = self._resolve(key)
        if not path.exists():
            raise FileNotFoundError(key)
        return path.read_bytes()

    def write_bytes(self, key: str, data: bytes) -> None:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def exists(self, key: str) -> bool:
        return self._resolve(key).exists()

    def delete(self, key: str) -> None:
        path = self._resolve(key)
        if path.exists():
            path.unlink()

    def copy(self, src_key: str, dst_key: str) -> None:
        src = self._resolve(src_key)
        if not src.exists():
            raise FileNotFoundError(src_key)
        dst = self._resolve(dst_key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)

    def delete_prefix(self, prefix: str) -> None:
        clean = (prefix or "").strip("/")
        if not clean or ".." in Path(clean).parts:
            raise StorageError(f"invalid storage prefix {prefix!r}")
        target = self.root / clean
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)

    def describe(self) -> dict:
        return {"kind": self.kind, "root": str(self.root)}


# ----------------------------------------------------------------- s3/minio
class S3Backend(StorageBackend):
    """AWS S3 or any S3-compatible store (MinIO via ``endpoint_url``)."""

    kind = "s3"

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        endpoint_url: str = "",
        region: str = "",
        access_key_id: str = "",
        secret_access_key: str = "",
    ) -> None:
        if not bucket:
            raise StorageError("S3 storage needs PY8N_S3_BUCKET")
        self.bucket = bucket
        self.prefix = (prefix or "").strip().strip("/")
        self.endpoint_url = (endpoint_url or "").strip() or None
        self.region = (region or "").strip() or None
        self._creds = (access_key_id or "", secret_access_key or "")
        self._client = None

    @property
    def client(self):
        """Lazy boto3 client - importing boto3 costs ~200ms, so only pay it
        when the S3 backend is actually selected (and once per process)."""
        if self._client is None:
            import boto3  # deferred: not needed by local installs

            params: dict = {"service_name": "s3"}
            if self.endpoint_url:
                params["endpoint_url"] = self.endpoint_url
            if self.region:
                params["region_name"] = self.region
            if all(self._creds):
                params["aws_access_key_id"] = self._creds[0]
                params["aws_secret_access_key"] = self._creds[1]
            self._client = boto3.client(**params)
        return self._client

    def _key(self, key: str) -> str:
        clean = (key or "").strip().lstrip("/")
        if not clean or ".." in Path(clean).parts:
            raise StorageError(f"invalid storage key {key!r}")
        return f"{self.prefix}/{clean}" if self.prefix else clean

    def read_bytes(self, key: str) -> bytes:
        from botocore.exceptions import ClientError

        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
            return resp["Body"].read()
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404", "NotFound"):
                raise FileNotFoundError(key) from exc
            raise StorageError(f"S3 read failed: {code or exc}") from exc

    def write_bytes(self, key: str, data: bytes) -> None:
        try:
            self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)
        except Exception as exc:  # noqa: BLE001 - wrap for the service layer
            raise StorageError(f"S3 write failed: {exc}") from exc

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise StorageError(f"S3 head failed: {code or exc}") from exc

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"S3 delete failed: {exc}") from exc

    def copy(self, src_key: str, dst_key: str) -> None:
        from botocore.exceptions import ClientError

        try:
            self.client.copy_object(
                Bucket=self.bucket,
                Key=self._key(dst_key),
                CopySource={"Bucket": self.bucket, "Key": self._key(src_key)},
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                raise FileNotFoundError(src_key) from exc
            raise StorageError(f"S3 copy failed: {code or exc}") from exc

    def delete_prefix(self, prefix: str) -> None:
        clean = (prefix or "").strip("/")
        if not clean or ".." in Path(clean).parts:
            raise StorageError(f"invalid storage prefix {prefix!r}")
        full = f"{self.prefix}/{clean}" if self.prefix else clean
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=f"{full}/"):
                objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if objects:
                    self.client.delete_objects(
                        Bucket=self.bucket, Delete={"Objects": objects, "Quiet": True}
                    )
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"S3 prefix delete failed: {exc}") from exc

    def describe(self) -> dict:
        out = {"kind": self.kind, "bucket": self.bucket}
        if self.prefix:
            out["prefix"] = self.prefix
        if self.endpoint_url:
            out["endpoint_url"] = self.endpoint_url  # MinIO etc.
        return out


# ----------------------------------------------------------------- gcs
class GCSBackend(StorageBackend):
    """Google Cloud Storage (ADC / GOOGLE_APPLICATION_CREDENTIALS)."""

    kind = "gcs"

    def __init__(self, bucket: str, prefix: str = "") -> None:
        if not bucket:
            raise StorageError("GCS storage needs PY8N_GCS_BUCKET")
        self.bucket_name = bucket
        self.prefix = (prefix or "").strip().strip("/")
        self._bucket = None

    @property
    def bucket(self):
        if self._bucket is None:
            from google.cloud import storage  # deferred heavy import

            self._bucket = storage.Client().bucket(self.bucket_name)
        return self._bucket

    def _key(self, key: str) -> str:
        clean = (key or "").strip().lstrip("/")
        if not clean or ".." in Path(clean).parts:
            raise StorageError(f"invalid storage key {key!r}")
        return f"{self.prefix}/{clean}" if self.prefix else clean

    def read_bytes(self, key: str) -> bytes:
        blob = self.bucket.blob(self._key(key))
        if not blob.exists():
            raise FileNotFoundError(key)
        return blob.download_as_bytes()

    def write_bytes(self, key: str, data: bytes) -> None:
        self.bucket.blob(self._key(key)).upload_from_string(data)

    def exists(self, key: str) -> bool:
        return self.bucket.blob(self._key(key)).exists()

    def delete(self, key: str) -> None:
        blob = self.bucket.blob(self._key(key))
        if blob.exists():
            blob.delete()

    def copy(self, src_key: str, dst_key: str) -> None:
        src = self.bucket.blob(self._key(src_key))
        if not src.exists():
            raise FileNotFoundError(src_key)
        self.bucket.copy_blob(src, self.bucket, self._key(dst_key))

    def delete_prefix(self, prefix: str) -> None:
        clean = (prefix or "").strip("/")
        if not clean or ".." in Path(clean).parts:
            raise StorageError(f"invalid storage prefix {prefix!r}")
        full = f"{self.prefix}/{clean}" if self.prefix else clean
        for blob in list(self.bucket.list_blobs(prefix=f"{full}/")):
            blob.delete()

    def describe(self) -> dict:
        out = {"kind": self.kind, "bucket": self.bucket_name}
        if self.prefix:
            out["prefix"] = self.prefix
        return out


# ----------------------------------------------------------------- accessor
_backend: StorageBackend | None = None


def build_backend() -> StorageBackend:
    """Construct the configured backend from settings (per process)."""
    chosen = (getattr(settings, "storage_backend", "") or "local").strip().lower()
    if chosen in ("s3", "minio"):
        return S3Backend(
            bucket=settings.s3_bucket,
            prefix=settings.s3_prefix,
            endpoint_url=settings.s3_endpoint_url,
            region=settings.s3_region,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
        )
    if chosen in ("gcs", "gs"):
        return GCSBackend(bucket=settings.gcs_bucket, prefix=settings.gcs_prefix)
    if chosen != "local":
        raise StorageError(
            f"unknown PY8N_STORAGE_BACKEND {chosen!r} (use local|s3|minio|gcs)"
        )
    return LocalBackend()


def get_backend() -> StorageBackend:
    """Process-wide backend instance (built once, resettable for tests)."""
    global _backend
    if _backend is None:
        _backend = build_backend()
    return _backend


def set_backend(backend: StorageBackend | None) -> None:
    """Override the process backend (tests, admin tooling)."""
    global _backend
    _backend = backend


def describe_storage(include_ping: bool = False) -> dict:
    """Status payload for the storage status endpoint."""
    backend = get_backend()
    out = backend.describe()
    if include_ping:
        out["ping"] = backend.ping()
    return out
