"""System-scoped API keys (v104) - the deployed system's machine identity.

The commercial arc needs its last credential: a company's OTHER software
(their ERP cron job, their monitoring agent, their data warehouse) should
talk to the deployed operations system AS THE SYSTEM - scoped to exactly
one system, never inheriting a human's estate-wide powers. Where v41's
``api_keys`` authenticate as a USER, a system key authenticates as THE
SYSTEM it was minted for:

* format   - ``py8n_sys_`` + 24 url-safe chars. The prefix is RESERVED:
  auth resolves it against ``system_api_keys`` only, never the user-key
  table, so the two credential families can never collide.
* role     - DERIVED from scopes at read time (``role_for``): ``write``
  in scopes -> editor, otherwise viewer - the same ladder the human
  members use (v62). A key can therefore never act above its scope, and
  minting keys / managing members / restructuring the system are human,
  owner-only doors no key can ever reach.
* boundary - a key answers only for ITS system. A request carrying the
  key to a foreign system looks nonexistent (404): auth stamps the
  resolved key on ``request.state.py8n_system_key`` and EVERY system
  door consults it through ``_get_system`` - never a fall-through to
  the anonymous path, because auth-off's anonymous is an owner and a
  machine credential must not inherit that. Owner doors (members,
  keys, dissolve) stay human-only: a key's role tops out at editor.
* storage  - the full key is shown exactly once at mint; the DB keeps
  the sha256 hash and a display prefix. Revoke = stamp revoked_at.
"""

from __future__ import annotations

import hashlib
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import SystemApiKey

KEY_PREFIX = "py8n_sys_"
_PREFIX_DISPLAY_LEN = 20  # e.g. py8n_sys_ab12cd34
ALLOWED_SCOPES = ("read", "write")
DEFAULT_SCOPES = ["read", "write"]

_ROLE_FOR_SCOPES = {"editor": 1, "viewer": 0}


class SystemKeyError(ValueError):
    """Honest 4xx-grade key failures (bad name, unknown scope)."""


def role_for(scopes: list | None) -> str:
    """The role a key speaks with, derived from its scopes - ``write``
    spells editor, anything less is a viewer. Never an owner: credentials
    do not own the business."""
    if "write" in (scopes or []):
        return "editor"
    return "viewer"


def key_out(row: SystemApiKey) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "prefix": row.prefix,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "revoked": row.revoked_at is not None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "scopes": row.scopes or list(DEFAULT_SCOPES),
        "read_only": "write" not in (row.scopes or DEFAULT_SCOPES),
    }


def mint(system_id: str, *, name: str, scopes: list[str] | None,
         created_by: str | None) -> tuple[SystemApiKey, str]:
    """Create the credential row + return (row, FULL_KEY). The full key
    exists in this return value only - storage keeps the hash. Validates
    loudly (the same discipline v41's mint set)."""
    clean = str(name or "").strip()
    if not clean:
        raise SystemKeyError("a key name is required (what is this key for?)")
    if len(clean) > 120:
        raise SystemKeyError("key name is longer than 120 characters")
    picked = list(dict.fromkeys(scopes if scopes else list(DEFAULT_SCOPES)))
    if not picked:
        raise SystemKeyError("a key needs at least one scope")
    unknown = [s for s in picked if s not in ALLOWED_SCOPES]
    if unknown:
        raise SystemKeyError(
            f"Unknown scope(s): {', '.join(unknown)}. Allowed: {', '.join(ALLOWED_SCOPES)}")
    full = KEY_PREFIX + secrets.token_urlsafe(24)
    row = SystemApiKey(
        system_id=system_id,
        name=clean[:120],
        prefix=full[:_PREFIX_DISPLAY_LEN],
        key_hash=hashlib.sha256(full.encode()).hexdigest(),
        scopes=picked,
        created_by=created_by,
    )
    return row, full


async def resolve(db: AsyncSession, key_string: str) -> SystemApiKey | None:
    """The hash lookup behind the auth dependency - revoked keys are
    invisible (None), the same honesty v41's resolution keeps."""
    key_hash = hashlib.sha256((key_string or "").encode()).hexdigest()
    return (
        await db.execute(
            select(SystemApiKey).where(
                SystemApiKey.key_hash == key_hash,
                SystemApiKey.revoked_at.is_(None))
        )
    ).scalar_one_or_none()


async def list_keys(db: AsyncSession, system_id: str) -> list[SystemApiKey]:
    return (
        await db.execute(
            select(SystemApiKey)
            .where(SystemApiKey.system_id == system_id)
            .order_by(SystemApiKey.created_at.desc())
        )
    ).scalars().all()
