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
    decode_mfa_token,
    generate_totp_secret,
    get_optional_user,
    hash_password,
    make_token,
    otpauth_uri,
    public_user,
    user_count,
    validate_email,
    validate_password,
    verify_password,
    verify_totp,
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


class TwoFactorCodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=10)


class TwoFactorVerifyIn(BaseModel):
    mfa_token: str = Field(min_length=10)
    code: str = Field(min_length=6, max_length=10)


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
    # v120: two-factor accounts get the CHALLENGE step - a short-lived
    # mfa-scoped token that is NOT a bearer (decode_token refuses it); the
    # real session mints only after the code verifies at /auth/2fa/verify
    if user.totp_enabled:
        return {"mfa_required": True, "mfa_token": make_token(user.id, scope="mfa")}
    return {"token": make_token(user.id), "user": public_user(user)}


@router.get("/me")
async def me(user=Depends(get_optional_user)):
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return public_user(user)


# ----------------------------------------------------------------------
# v120: TOTP two-factor - setup, enable, disable, and the login verify
# ----------------------------------------------------------------------

@router.post("/2fa/setup")
async def twofa_setup(
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Enrollment step 1: mint the base32 secret + the otpauth URI.
    The secret stays PENDING (totp_enabled stays False) until a code
    verifies at /2fa/enable."""
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if user.totp_enabled:
        raise HTTPException(status_code=409, detail="Two-factor is already enabled")
    secret = generate_totp_secret()
    user.totp_secret = secret
    await db.commit()
    return {"secret": secret, "otpauth_uri": otpauth_uri(user.email, secret)}


@router.post("/2fa/enable")
async def twofa_enable(
    body: TwoFactorCodeIn,
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Enrollment step 2: the first code proves the app is enrolled."""
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if user.totp_enabled:
        raise HTTPException(status_code=409, detail="Two-factor is already enabled")
    if not user.totp_secret:
        raise HTTPException(status_code=400, detail="Run /auth/2fa/setup first")
    if not verify_totp(user.totp_secret, body.code):
        raise HTTPException(status_code=400, detail="Invalid code - check your authenticator")
    user.totp_enabled = True
    await db.commit()
    return {"enabled": True}


@router.post("/2fa/disable")
async def twofa_disable(
    body: TwoFactorCodeIn,
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Turn two-factor off - the code is required (a stolen session
    cannot quietly unprotect the account)."""
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not user.totp_enabled or not user.totp_secret:
        raise HTTPException(status_code=409, detail="Two-factor is not enabled")
    if not verify_totp(user.totp_secret, body.code):
        raise HTTPException(status_code=400, detail="Invalid code")
    user.totp_enabled = False
    user.totp_secret = ""
    await db.commit()
    return {"enabled": False}


@router.post("/2fa/verify")
async def twofa_verify(
    body: TwoFactorVerifyIn,
    db: AsyncSession = Depends(get_db),
):
    """The login challenge's second half: the short-lived mfa token (from
    /auth/login's ``mfa_required`` answer) plus a valid code mint the REAL
    session. A challenge token is never a bearer and never re-usable."""
    sub = decode_mfa_token(body.mfa_token)
    if not sub:
        raise HTTPException(status_code=401,
                            detail="The two-factor challenge expired - sign in again")
    user = await db.get(User, sub)
    if user is None or not user.totp_enabled or not user.totp_secret:
        raise HTTPException(status_code=400, detail="Two-factor is not active on this account")
    if not verify_totp(user.totp_secret, body.code):
        raise HTTPException(status_code=401, detail="Invalid code")
    return {"token": make_token(user.id), "user": public_user(user)}
