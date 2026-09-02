"""Auth API (v37): register / login / me / status.

POST /api/v1/auth/register  create an account (first user becomes admin
                            and claims all unclaimed resources)
POST /api/v1/auth/login     exchange email + password for a JWT
GET  /api/v1/auth/me        the token's account (401 without a valid token)
GET  /api/v1/auth/status    mode probe for the UI: {require_auth, has_users}
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import (
    claim_orphans,
    get_optional_user,
    hash_password,
    make_token,
    public_user,
    user_count,
    validate_email,
    validate_password,
    verify_password,
)
from ..config import settings
from ..db import get_db
from ..models import User
from ._ratelimit import rate_limit

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=8, max_length=200)
    name: str = Field(default="", max_length=120)


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


@router.get("/status")
async def auth_status(db: AsyncSession = Depends(get_db)):
    """Anonymous mode probe: should the UI force a login, and can anyone register."""
    return {
        "require_auth": settings.require_auth,
        "has_users": await user_count(db) > 0,
        "version": settings.version,
    }


@router.post("/register", status_code=201, dependencies=[Depends(rate_limit("auth"))])
async def register(body: RegisterIn, db: AsyncSession = Depends(get_db)):
    email = validate_email(body.email)
    validate_password(body.password)
    existing = (
        await db.execute(select(User.id).where(User.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    first = await user_count(db) == 0
    # Audit hardening: 240k PBKDF2 iterations are ~100ms+ of pure CPU - run
    # them on a worker thread so register/login never blocks the event loop.
    password_hash = await asyncio.to_thread(hash_password, body.password)
    user = User(
        email=email,
        name=(body.name or "").strip(),
        password_hash=password_hash,
        role="admin" if first else "member",
    )
    db.add(user)
    await db.flush()
    claimed = await claim_orphans(db, user.id) if first else {}
    await db.commit()
    return {"token": make_token(user.id), "user": public_user(user), "claimed": claimed}


@router.post("/login", dependencies=[Depends(rate_limit("auth"))])
async def login(body: LoginIn, db: AsyncSession = Depends(get_db)):
    email = (body.email or "").strip().lower()
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    ok = user is not None and await asyncio.to_thread(
        verify_password, body.password, user.password_hash
    )
    if not ok:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return {"token": make_token(user.id), "user": public_user(user)}


@router.get("/me")
async def me(user=Depends(get_optional_user)):
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return public_user(user)
