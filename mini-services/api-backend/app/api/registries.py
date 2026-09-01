"""Pack registries (v43) - point Py8n at a URL to sync gallery packs.

A registry is any HTTP(S) endpoint serving a ``py8n-pack`` document: another
instance's ``/api/v1/templates/gallery/pack``, a teammate's shared pack file
on a static host, a CI artifact. ``check`` dry-runs the fetched pack against
the local estate (validity, collisions, rename previews) without writing;
``sync`` imports it through the ordinary pack pipeline (workflows inactive +
version-snapshotted, datasets via the parquet pipeline, invalid entries
skipped with reasons) and records the outcome on the registry row.

Endpoints (all under /registries, enforced like the rest of the build surface):
  GET    /registries            list the caller's registries
  POST   /registries            {name, url} - scheme must be http/https
  DELETE /registries/{id}       forget the registry (imported data stays)
  POST   /registries/{id}/check fetch + dry-run, nothing recorded
  POST   /registries/{id}/sync  fetch + import + stamp last_sync outcome
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user, own_or_404
from ..db import get_db
from ..models import PackRegistry
from ..api.packs import PACK_FORMAT, PackDocument, _import_pack_doc, _inspect_pack_doc

router = APIRouter(prefix="/registries", tags=["registries"])

FETCH_TIMEOUT_SECONDS = 30.0
# A registry pack larger than this is refused before parsing - a misconfigured
# URL pointing at a huge binary should fail fast with a clear message.
MAX_REGISTRY_BYTES = 64 * 1024 * 1024


class RegistryFetchError(Exception):
    """The URL could not be fetched or did not contain a Py8n pack."""


class RegistryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=2000)


class RegistryOut(BaseModel):
    id: str
    name: str
    url: str
    created_at: datetime | None = None
    last_sync_at: datetime | None = None
    last_status: str | None = None
    last_summary: dict | None = None


def _out(row: PackRegistry) -> RegistryOut:
    return RegistryOut(
        id=row.id,
        name=row.name,
        url=row.url,
        created_at=row.created_at,
        last_sync_at=row.last_sync_at,
        last_status=row.last_status,
        last_summary=row.last_summary,
    )


def _make_client() -> httpx.AsyncClient:
    """Separate factory so tests can inject an httpx.MockTransport."""
    return httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True)


def _validate_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="Registry URL cannot be empty")
    if len(url) > 2000:
        raise HTTPException(status_code=400, detail="Registry URL is too long")
    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    if scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Registry URL must start with http:// or https://")
    return url


async def _fetch_pack_doc(url: str) -> PackDocument:
    """Fetch a registry URL and parse it into a PackDocument.

    Raises RegistryFetchError with a human-clean message on transport errors,
    non-2xx responses, oversized bodies, invalid JSON or a wrong format
    marker - callers translate that into a 502.
    """
    try:
        async with _make_client() as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        raise RegistryFetchError(f"Could not reach the registry URL: {exc.__class__.__name__}") from exc

    if resp.status_code >= 400:
        raise RegistryFetchError(f"Registry URL returned HTTP {resp.status_code}")
    declared = resp.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_REGISTRY_BYTES:
        raise RegistryFetchError(f"Registry pack exceeds the {MAX_REGISTRY_BYTES // (1024 * 1024)} MB cap")
    body = resp.content
    if len(body) > MAX_REGISTRY_BYTES:
        raise RegistryFetchError(f"Registry pack exceeds the {MAX_REGISTRY_BYTES // (1024 * 1024)} MB cap")
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise RegistryFetchError("Registry URL did not return valid JSON") from exc
    if not isinstance(doc, dict):
        raise RegistryFetchError("Registry URL must return a JSON object")
    fmt = doc.get("format")
    if fmt != PACK_FORMAT:
        raise RegistryFetchError(f"URL did not return a Py8n pack (format {fmt!r}, expected {PACK_FORMAT!r})")
    try:
        return PackDocument.model_validate(doc)
    except Exception as exc:  # pydantic validation of a malformed pack body
        raise RegistryFetchError(f"Pack structure is invalid: {exc}") from exc


@router.get("", response_model=list[RegistryOut])
async def list_registries(user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(PackRegistry)
            .where(PackRegistry.owner_id == (user.id if user else None))
            .order_by(PackRegistry.created_at.desc())
        )
    ).scalars().all()
    return [_out(r) for r in rows]


@router.post("", response_model=RegistryOut, status_code=201)
async def create_registry(body: RegistryCreate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = PackRegistry(
        name=body.name.strip()[:120],
        url=_validate_url(body.url),
    )
    row.owner_id = user.id if user else None  # v37
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.delete("/{registry_id}", status_code=204)
async def delete_registry(registry_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await db.get(PackRegistry, registry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Registry not found")
    own_or_404(row.owner_id, user)  # v37: other users' registries look nonexistent
    await db.delete(row)
    await db.commit()
    return None


@router.post("/{registry_id}/check")
async def check_registry(registry_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Fetch the pack and dry-run it against the local estate. Nothing is
    written and nothing is recorded - check as often as you like."""
    row = await db.get(PackRegistry, registry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Registry not found")
    own_or_404(row.owner_id, user)
    try:
        pack = await _fetch_pack_doc(row.url)
    except RegistryFetchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    preview = await _inspect_pack_doc(pack, db)
    return {
        "url": row.url,
        "generated_at": pack.generated_at,
        "py8n_version": pack.py8n_version,
        **preview,
    }


@router.post("/{registry_id}/sync")
async def sync_registry(registry_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Fetch the pack and import it through the ordinary pack pipeline. The
    outcome (created/skipped, or the fetch error) is stamped on the registry
    row so the list view shows the last sync state at a glance."""
    row = await db.get(PackRegistry, registry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Registry not found")
    own_or_404(row.owner_id, user)

    try:
        pack = await _fetch_pack_doc(row.url)
    except RegistryFetchError as exc:
        row.last_sync_at = datetime.now(timezone.utc)
        row.last_status = "error"
        row.last_summary = {"error": str(exc)}
        await db.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not pack.workflows and not pack.datasets:
        row.last_sync_at = datetime.now(timezone.utc)
        row.last_status = "error"
        row.last_summary = {"error": "the pack contains no workflows or datasets"}
        await db.commit()
        raise HTTPException(status_code=502, detail="The pack contains no workflows or datasets")

    owner = user.id if user else None  # v37
    summary = await _import_pack_doc(pack, owner, db)
    row.last_sync_at = datetime.now(timezone.utc)
    row.last_status = "ok"
    row.last_summary = {
        "workflows_created": len(summary["workflows"]),
        "datasets_created": len(summary["datasets"]),
        "skipped": summary["skipped"],
        "warnings": summary["warnings"],
        "workflow_names": [w["name"] for w in summary["workflows"]],
        "dataset_names": [d["name"] for d in summary["datasets"]],
    }
    await db.commit()
    await db.refresh(row)
    return {"registry": _out(row), "import": summary}
