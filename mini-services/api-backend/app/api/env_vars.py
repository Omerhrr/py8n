"""Environment variables API (v15) — global template values, ``{{ env.KEY }}``.

Endpoints
---------
GET    /env-vars        list (secret values masked to null)
POST   /env-vars        create (409 on duplicate key, case-insensitive)
GET    /env-vars/{id}   edit-time detail (secrets masked)
PUT    /env-vars/{id}   update value / is_secret / description ("__keep__" preserves)
DELETE /env-vars/{id}   remove

Keys are stored EXACTLY as typed (validated ``^[A-Za-z_][A-Za-z0-9_]*$``) so
a template's ``{{ env.my_key }}`` matches the key verbatim — Jinja dict
access is case-sensitive. Uniqueness is enforced case-insensitively. Values
are always Fernet-encrypted at rest; every write commits explicitly (v4
lesson: yield-dependency teardown commits run after the response is sent).
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import EnvVariable
from ..schemas import EnvVariableCreate, EnvVariableOut, EnvVariableUpdate
from ..services.crypto import decrypt_value, encrypt_value

router = APIRouter(prefix="/env-vars", tags=["env-vars"])

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
KEEP_MARKER = "__keep__"


def _normalize_key(raw: str) -> str:
    """Trim only — case is preserved (template access is case-sensitive)."""
    return (raw or "").strip()


def _out(row: EnvVariable, include_value: bool) -> EnvVariableOut:
    value: str | None = None
    if include_value and not row.is_secret:
        try:
            value = decrypt_value(row.value_encrypted)
        except ValueError:
            value = ""
    return EnvVariableOut(
        id=row.id,
        key=row.key,
        value=value,
        is_secret=row.is_secret,
        description=row.description or "",
        updated_at=row.updated_at,
    )


async def _get_row(db: AsyncSession, env_id: str) -> EnvVariable:
    row = await db.get(EnvVariable, env_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Environment variable not found")
    return row


@router.get("", response_model=list[EnvVariableOut])
async def list_env_vars(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(EnvVariable).order_by(EnvVariable.key))).scalars().all()
    return [_out(r, include_value=True) for r in rows]


@router.post("", response_model=EnvVariableOut, status_code=201)
async def create_env_var(body: EnvVariableCreate, db: AsyncSession = Depends(get_db)):
    key = _normalize_key(body.key)
    if not KEY_RE.match(key):
        raise HTTPException(
            status_code=400,
            detail="Key must contain only letters, digits and underscores, and cannot start with a digit",
        )
    dup = (
        await db.execute(
            select(EnvVariable).where(func.upper(EnvVariable.key) == key.upper())
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(status_code=409, detail=f"Key {key!r} already exists (case-insensitive match: {dup.key!r})")
    row = EnvVariable(
        key=key,
        value_encrypted=encrypt_value(body.value),
        is_secret=body.is_secret,
        description=body.description.strip(),
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    await db.commit()  # explicit: teardown commit runs after the response
    return _out(row, include_value=True)


@router.get("/{env_id}", response_model=EnvVariableOut)
async def get_env_var(env_id: str, db: AsyncSession = Depends(get_db)):
    row = await _get_row(db, env_id)
    return _out(row, include_value=True)


@router.put("/{env_id}", response_model=EnvVariableOut)
async def update_env_var(env_id: str, body: EnvVariableUpdate, db: AsyncSession = Depends(get_db)):
    row = await _get_row(db, env_id)
    if body.value is not None and body.value != KEEP_MARKER:
        row.value_encrypted = encrypt_value(body.value)
    if body.is_secret is not None:
        row.is_secret = body.is_secret
    if body.description is not None:
        row.description = body.description.strip()
    db.add(row)
    await db.commit()  # explicit: teardown commit runs after the response
    await db.refresh(row)
    return _out(row, include_value=True)


@router.delete("/{env_id}", status_code=204)
async def delete_env_var(env_id: str, db: AsyncSession = Depends(get_db)):
    row = await _get_row(db, env_id)
    await db.delete(row)
    await db.commit()  # explicit: teardown commit runs after the response
