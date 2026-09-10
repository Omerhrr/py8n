"""Model classes: Credential, CredentialEvent, EnvVariable, AppSetting, Folder, AgentMemory.

Split from the original app/models.py (task #3) - moved verbatim, no
behavior change.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._shared import Base, JSONVariant, _now, _uuid

class Credential(Base):
    """Encrypted-at-rest credential (API keys, tokens) for node parameters."""

    __tablename__ = "credentials"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # v37
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[str] = mapped_column(String(60), default="generic")  # header_auth|openai_compatible|generic
    data_encrypted: Mapped[str] = mapped_column(Text, nullable=False)  # Fernet token
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # v43 last secret rotation


class CredentialEvent(Base):
    """Vault audit trail (v43) - one row per lifecycle action on a credential.

    Written on created / renamed / updated (payload via PATCH) / rotated /
    tested / used (a node resolved the secret during an execution) / deleted.
    ``credential_name`` is snapshotted so the trail stays meaningful after the
    credential itself is gone. Detail dicts carry FIELD NAMES only - secret
    values never touch the audit log.
    """

    __tablename__ = "credential_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    credential_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    credential_name: Mapped[str] = mapped_column(String(200), default="")
    action: Mapped[str] = mapped_column(String(20), nullable=False)  # created|renamed|updated|rotated|tested|used|deleted
    detail: Mapped[dict] = mapped_column(JSON, default=dict)  # field names / workflow refs, never values
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class EnvVariable(Base):
    """Global environment variable (v15) - exposed to templates as ``env.KEY``.

    Values are ALWAYS Fernet-encrypted at rest (uniform code path, no
    plaintext in the DB). ``is_secret`` rows are additionally masked in the
    API (write-only) so they can hold tokens / passwords.
    """

    __tablename__ = "env_variables"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # v37
    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    value_encrypted: Mapped[str] = mapped_column(Text, nullable=False)  # Fernet token
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class AppSetting(Base):
    """Global key/value platform setting (v19) - retention policies etc.

    Values are JSON documents; a missing row means "use the built-in default"
    (declared next to the consumer, e.g. services/retention.py).
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Folder(Base):
    """Organizational folder for workflows (v16).

    Supports nesting up to MAX_FOLDER_DEPTH (enforced in the API layer with
    a cycle-safe ancestor walk). Deleting a folder is refused while it still
    has subfolders; workflows inside fall back to the root (folder_id=None).
    """

    __tablename__ = "folders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # v37
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class AgentMemory(Base):
    """Persisted conversation buffer for AI Agent nodes (v23).

    One row per session key; ``messages`` holds the rolling chat history
    (alternating user/assistant turns) that gets injected into the next
    agent run sharing the same key.
    """

    __tablename__ = "agent_memories"

    session_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    messages: Mapped[list] = mapped_column(JSONVariant, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

