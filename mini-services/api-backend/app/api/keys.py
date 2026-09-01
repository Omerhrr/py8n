"""API keys (v41) - machine access credentials for the Py8n REST API.

A key authenticates AS ITS OWNER: requests carrying ``X-API-Key: py8n_...``
resolve to the key's user and inherit the same owner scoping as that user's
JWT, so scripts and CI pipelines can hit every build surface even when
``PY8N_REQUIRE_AUTH`` is on. The full key is returned exactly once at
creation; storage keeps only a sha256 hash plus a display prefix.

Endpoints (all under /keys, enforced like the rest of the build surface):
  GET    /keys        list the caller's keys (masked)
  POST   /keys        mint a key {name} -> {key: "py8n_..."} (shown once)
  DELETE /keys/{id}   revoke (stamps revoked_at; history stays)
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user, own_or_404
from ..db import get_db
from ..models import ApiKey

router = APIRouter(prefix="/keys", tags=["keys"])

KEY_PREFIX = "py8n_"
_PREFIX_DISPLAY_LEN = 12  # e.g. py8n_ab12cd34


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


def _out(row: ApiKey) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "prefix": row.prefix,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "revoked": row.revoked_at is not None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
    }


@router.get("")
async def list_keys(user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    if user is None:  # keys always belong to a user
        return []
    rows = (
        await db.execute(
            select(ApiKey)
            .where(ApiKey.owner_id == user.id)
            .order_by(ApiKey.created_at.desc())
        )
    ).scalars().all()
    return [_out(r) for r in rows]


@router.post("", status_code=201)
async def create_key(body: KeyCreate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    if user is None:
        raise HTTPException(status_code=401, detail="Register or sign in before creating API keys")
    full = KEY_PREFIX + secrets.token_urlsafe(24)
    row = ApiKey(
        owner_id=user.id,
        name=body.name.strip()[:120],
        prefix=full[:_PREFIX_DISPLAY_LEN],
        key_hash=hashlib.sha256(full.encode()).hexdigest(),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {**_out(row), "key": full}  # the ONLY response carrying the full key


@router.delete("/{key_id}", status_code=204)
async def revoke_key(key_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    if user is None:
        raise HTTPException(status_code=404, detail="Not found")
    row = await db.get(ApiKey, key_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    own_or_404(row.owner_id, user)  # v37: other users' keys look nonexistent
    if row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        await db.commit()
    return None
