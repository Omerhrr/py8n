"""Model classes: User, ApiKey.

Split from the original app/models.py (task #3) - moved verbatim, no
behavior change.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._shared import Base, JSONVariant, _now, _uuid

class User(Base):
    """Platform account (v37) - the unit of authentication and ownership.

    The first registered user becomes ``admin`` and claims every unclaimed
    resource row (bootstrap story for installs that flip auth on later).
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(300), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="member", index=True)  # admin|member
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ApiKey(Base):
    """Machine access credential (v41) - long random bearer for API callers.

    The full key (``py8n_`` + 32 url-safe chars) is shown exactly once at
    creation; only its sha256 hash and a display prefix live in the DB. Keys
    authenticate as their OWNER (same scoping as the owner's JWT) and work
    through the ``X-API-Key`` header, so scripts and CI can talk to Py8n even
    when auth enforcement is on. Revoke = stamp revoked_at (history stays).
    """

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # v37 user
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    prefix: Mapped[str] = mapped_column(String(20), default="")  # display form, e.g. py8n_ab12cd34
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # sha256 hex
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # v43 scopes: list of "read" | "write". NULL/empty = legacy unrestricted
    # key (pre-v43 rows keep working); new keys always store explicit scopes.
    scopes: Mapped[list | None] = mapped_column(JSON, nullable=True)

