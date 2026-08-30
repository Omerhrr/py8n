"""SQLAlchemy ORM models: workflows, execution logs, credentials.

Graphs are stored as JSON (JSONB on PostgreSQL via variant) so the visual
canvas document maps 1:1 to a database row — the same design used by n8n.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


JSONVariant = JSON().with_variant(JSONB(), "postgresql")


class Workflow(Base):
    """A saved automation workflow (graph document + metadata)."""

    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    # The visual graph document: {"nodes": [...], "edges": [...]}
    graph: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)  # enables triggers
    # n8n-style error workflow: dispatched with a structured payload when an
    # execution of this workflow ends in an unhandled error (v8).
    error_workflow_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Organizational folder (v16) — plain id validated in the API layer (same
    # pattern as error_workflow_id, keeps SQLite FK enforcement out of scope).
    folder_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # Organizational labels (v12): normalized lowercase strings, max 10.
    tags: Mapped[list] = mapped_column(JSONVariant, default=list)
    # Per-workflow retention override (v20): NULL = inherit the global policy,
    # 0 = keep forever, N = purge this workflow's finished logs after N days.
    retention_days: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    executions: Mapped[list["ExecutionLog"]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="ExecutionLog.started_at.desc()",
    )

    versions: Mapped[list["WorkflowVersion"]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="WorkflowVersion.version.desc()",
    )

    def webhook_nodes(self) -> list[dict]:
        return [n for n in (self.graph or {}).get("nodes", []) if n.get("type") == "webhook_trigger"]

    def chat_nodes(self) -> list[dict]:
        return [n for n in (self.graph or {}).get("nodes", []) if n.get("type") == "chat_trigger"]

    def schedule_nodes(self) -> list[dict]:
        return [n for n in (self.graph or {}).get("nodes", []) if n.get("type") == "schedule_trigger"]


class ExecutionLog(Base):
    """Persisted record of one workflow execution, including per-node runs."""

    __tablename__ = "workflow_execution_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)  # running|success|error|cancelled
    trigger_type: Mapped[str] = mapped_column(String(40), default="manual")          # manual|webhook|schedule
    trigger_payload: Mapped[dict] = mapped_column(JSONVariant, default=dict)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Ordered list of node run records:
    # [{"node_id","node_type","node_name","status","started_at","finished_at",
    #   "duration_ms","output":...,"error":str|None}]
    node_runs: Mapped[list] = mapped_column(JSONVariant, default=list)
    # Final execution context snapshot (resolutions of {{ nodes.*.output.* }})
    context_snapshot: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    workflow: Mapped[Workflow] = relationship(back_populates="executions")

    def to_summary(self) -> dict:
        return {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "status": self.status,
            "trigger_type": self.trigger_type,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


class WorkflowVersion(Base):
    """One immutable snapshot of a workflow's content (v13).

    Created automatically on create/import/duplicate and on every content
    change (graph / name / description) via services.versions. Bounded —
    the newest MAX_VERSIONS rows are kept per workflow.
    """

    __tablename__ = "workflow_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)  # per-workflow, 1-based

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    graph: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    tags: Mapped[list] = mapped_column(JSONVariant, default=list)
    node_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    workflow: Mapped[Workflow] = relationship(back_populates="versions")


class Credential(Base):
    """Encrypted-at-rest credential (API keys, tokens) for node parameters."""

    __tablename__ = "credentials"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[str] = mapped_column(String(60), default="generic")  # header_auth|openai_compatible|generic
    data_encrypted: Mapped[str] = mapped_column(Text, nullable=False)  # Fernet token
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Folder(Base):
    """Organizational folder for workflows (v16).

    Supports nesting up to MAX_FOLDER_DEPTH (enforced in the API layer with
    a cycle-safe ancestor walk). Deleting a folder is refused while it still
    has subfolders; workflows inside fall back to the root (folder_id=None).
    """

    __tablename__ = "folders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class EnvVariable(Base):
    """Global environment variable (v15) — exposed to templates as ``env.KEY``.

    Values are ALWAYS Fernet-encrypted at rest (uniform code path, no
    plaintext in the DB). ``is_secret`` rows are additionally masked in the
    API (write-only) so they can hold tokens / passwords.
    """

    __tablename__ = "env_variables"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    value_encrypted: Mapped[str] = mapped_column(Text, nullable=False)  # Fernet token
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class AppSetting(Base):
    """Global key/value platform setting (v19) — retention policies etc.

    Values are JSON documents; a missing row means "use the built-in default"
    (declared next to the consumer, e.g. services/retention.py).
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONVariant, default=dict)
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
