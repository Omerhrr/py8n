"""Authentication + multi-user scoping primitives (v37).

Passwords: PBKDF2-HMAC-SHA256 (240k iterations, 16-byte salt, stdlib only).
Tokens: hand-rolled HS256 JWTs (stdlib hmac/base64) - no third-party auth
dependencies to pin or audit. The signing secret is a 64-hex key auto-created
once at data/.jwt.key (same pattern as the fernet credential key).

Mode: settings.require_auth (PY8N_REQUIRE_AUTH, default false).
  false = single-user legacy mode: anonymous requests work everywhere; tokens
          still work and scope whatever they touch.
  true  = enforced mode: anonymous requests get 401 on every build/admin
          surface; machine + published-runtime surfaces (webhooks, chat, app
          and dashboard runtimes, dataset SQL for embedded components) stay
          reachable without a token.

Scoping: every user-facing resource carries owner_id (NULL = unclaimed).
  - anonymous callers see everything (legacy mode behavior)
  - a token-ed caller sees unclaimed rows plus their own; rows owned by
    other users 404 on direct access and are filtered out of lists
  - the FIRST registered user claims all unclaimed rows, so an existing
    install flipping auth on later keeps its data under a real owner
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from typing import Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db

_PBKDF2_ITERATIONS = 240_000
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Paths that never require a token even in enforced mode. Suffixes cover the
# published runtime surfaces (app records/forms, dashboard data, embedded
# dataset SQL) and artifact content (<img> tags cannot send auth headers).
_PUBLIC_PREFIXES = (
    "/api/v1/health",
    "/api/v1/auth",
    "/api/v1/webhooks",
    "/api/v1/chat",
    "/api/v1/ws",
    # "/api/v1/_spawn" removed (audit hardening): the dev-only spawn helper
    # is token-gated inside the route itself; it must never be treated as a
    # public surface, and it does not exist at all unless debug+enabled.
)
_PUBLIC_SUFFIXES = (
    "/query",  # POST /datasets/query powers embedded app/dashboard components
    "/runtime",
    "/form",
    "/form-submit",
    "/records",
    "/content",
)


def is_public_path(path: str) -> bool:
    return path.startswith(_PUBLIC_PREFIXES) or path.endswith(_PUBLIC_SUFFIXES)


# ----------------------------------------------------------------------
# JWT secret (file-backed, auto-created)
# ----------------------------------------------------------------------
def _load_jwt_secret() -> bytes:
    path = settings.jwt_secret_file
    try:
        raw = path.read_text().strip()
        if len(raw) >= 32:
            try:
                # Audit hardening: repair perms on secrets written by older,
                # looser versions (umask-dependent writes).
                os.chmod(path, 0o600)
            except OSError:  # pragma: no cover - best effort
                pass
            return raw.encode()
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = secrets.token_hex(32)
    # Audit hardening: create with owner-only permissions (no world-readable
    # window between create and chmod).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(raw)
    os.chmod(path, 0o600)  # umask-independent guarantee
    return raw.encode()


# ----------------------------------------------------------------------
# Password hashing (PBKDF2-HMAC-SHA256)
# ----------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _algo, iters, salt_hex, digest_hex = encoded.split("$")
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False


# ----------------------------------------------------------------------
# HS256 JWT (stdlib only)
# ----------------------------------------------------------------------
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64url_decode(segment: str) -> bytes:
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def make_token(user_id: str) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps(
            {"sub": user_id, "exp": int(time.time()) + settings.token_ttl_seconds}
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    sig = hmac.new(_load_jwt_secret(), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(sig)}"


def decode_token(token: str) -> str | None:
    """Return the user id for a valid, unexpired token - else None."""
    try:
        header, payload, sig = token.split(".")
        expected = hmac.new(
            _load_jwt_secret(), f"{header}.{payload}".encode(), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, _b64url_decode(sig)):
            return None
        claims = json.loads(_b64url_decode(payload))
        if int(claims.get("exp", 0)) < int(time.time()):
            return None
        sub = claims.get("sub")
        return str(sub) if sub else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


# ----------------------------------------------------------------------
# FastAPI dependencies
# ----------------------------------------------------------------------
async def get_optional_user(request: Request, db: AsyncSession = Depends(get_db)):
    """Resolve the caller to a User, or None (anonymous is legal).

    Two credential channels (v41 added the second):
      1. ``Authorization: Bearer <jwt>`` - interactive sessions
      2. ``X-API-Key: py8n_...`` - machine access (scripts, CI); the key
         authenticates AS ITS OWNER with the same scoping
    """
    from .models import ApiKey, User

    header = request.headers.get("authorization") or ""
    if header.startswith("Bearer "):
        user_id = decode_token(header.removeprefix("Bearer ").strip())
        if not user_id:
            return None
        user = await db.get(User, user_id)
        if user is None:
            return None
        return user

    api_key = (request.headers.get("x-api-key") or "").strip()
    if api_key:
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        row = (
            await db.execute(
                select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None))
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        # sqlite stores naive datetimes - keep arithmetic in naive UTC (v38 GOTCHA)
        now = datetime.utcnow()
        if row.last_used_at is None or (now - row.last_used_at) > timedelta(seconds=60):
            row.last_used_at = now  # throttled touch
            await db.commit()
        if row.owner_id is None:
            return None
        user = await db.get(User, row.owner_id)
        if user is None:
            return None
        # v43: scopes ride request.state so enforce_key_scopes can gate
        # writes; NULL scopes = legacy pre-v43 key, treated as unrestricted.
        request.state.py8n_key_scopes = list(row.scopes) if row.scopes else None
        return user

    return None


def enforce_auth(request: Request, user=Depends(get_optional_user)) -> None:
    """Router-level dependency: 401 anonymous requests when auth is enforced.

    Public/machine surfaces (see is_public_path) stay reachable so webhooks,
    chat widgets and published app/dashboard runtimes keep working.
    """
    if user is not None or not settings.require_auth:
        return
    if is_public_path(request.url.path):
        return
    raise HTTPException(status_code=401, detail="Authentication required")


def enforce_key_scopes(request: Request, user=Depends(get_optional_user)) -> None:
    """Router-level dependency (v43): gate API-key callers by their scopes.

    A key whose scopes lack "write" may only use safe methods (GET/HEAD/
    OPTIONS) - every mutating call (create, update, delete, run, trigger)
    gets a 403. JWT sessions and anonymous traffic are unaffected; pre-v43
    keys (scopes NULL) are unrestricted. get_optional_user is cached per
    request, so the user resolution happens exactly once.
    """
    scopes = getattr(request.state, "py8n_key_scopes", None)
    if scopes is None or "write" in scopes:
        return
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    raise HTTPException(
        status_code=403,
        detail="This API key is read-only (scope: read); use a key with write access for this action",
    )


# ----------------------------------------------------------------------
# Scoping helpers (shared by the resource routers)
# ----------------------------------------------------------------------
def own_or_404(owner_id: str | None, user) -> None:
    """Direct access to a row owned by someone else must look nonexistent."""
    if user is not None and owner_id is not None and owner_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")


def scope_rows(rows: list, user):
    """List filter: authed callers see unclaimed + their own rows."""
    if user is None:
        return rows
    return [r for r in rows if r.owner_id is None or r.owner_id == user.id]


async def visible_workflow_ids(db: AsyncSession, user) -> list[str] | None:
    """Workflow ids the caller may see; None means "no filter" (anonymous)."""
    if user is None:
        return None
    from .models import Workflow

    rows = (
        await db.execute(
            select(Workflow.id).where(
                (Workflow.owner_id.is_(None)) | (Workflow.owner_id == user.id)
            )
        )
    ).scalars().all()
    return list(rows)


async def claim_orphans(db: AsyncSession, user_id: str) -> dict[str, int]:
    """First registered user inherits every unclaimed resource row."""
    from .models import App, Credential, Dashboard, Dataset, EnvVariable, Folder, Workflow

    counts: dict[str, int] = {}
    for model in (Workflow, Dataset, Folder, Credential, EnvVariable, App, Dashboard):
        result = await db.execute(
            update(model)
            .where(model.owner_id.is_(None))
            .values(owner_id=user_id)
        )
        counts[model.__tablename__] = result.rowcount or 0
    return counts


async def user_count(db: AsyncSession) -> int:
    from .models import User

    return len((await db.execute(select(User.id))).all())


def validate_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    return email


def validate_password(password: str) -> str:
    if not password or len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    return password


def public_user(user) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name or "",
        "role": user.role,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }
