"""Model classes: Workflow, ExecutionLog, WorkflowVersion, NotificationRule, PackRegistry.

Split from the original app/models.py (task #3) - moved verbatim, no
behavior change.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._shared import Base, JSONVariant, _now, _uuid

class Workflow(Base):
    """A saved automation workflow (graph document + metadata)."""

    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # v37: owning user (NULL = unclaimed / pre-auth era, visible to everyone)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    # The visual graph document: {"nodes": [...], "edges": [...]}
    graph: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)  # enables triggers
    # Error-workflow routing: dispatched with a structured payload when an
    # execution of this workflow ends in an unhandled error (v8).
    error_workflow_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Organizational folder (v16) - plain id validated in the API layer (same
    # pattern as error_workflow_id, keeps SQLite FK enforcement out of scope).
    folder_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # Organizational labels (v12): normalized lowercase strings, max 10.
    tags: Mapped[list] = mapped_column(JSONVariant, default=list)
    # Per-workflow retention override (v20): NULL = inherit the global policy,
    # 0 = keep forever, N = purge this workflow's finished logs after N days.
    retention_days: Mapped[int | None] = mapped_column(nullable=True)
    # v51: data-DAG execution policy - defaults applied to EVERY node in the
    # workflow that has not set its own retry/timeout. Shape (all optional):
    # {"retries": 0-5, "backoff_ms": ms, "backoff_multiplier": >=1,
    #  "backoff_max_ms": ms, "timeout_seconds": s, "retry_on": "all"|"transient"}
    # NULL = no workflow-level policy (node settings alone, pre-v51 behavior).
    policy_json: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
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

    def dataset_trigger_nodes(self) -> list[dict]:
        """v50: dataset watchers - fire when a watched dataset version advances."""
        return [n for n in (self.graph or {}).get("nodes", []) if n.get("type") == "dataset_trigger"]

    def event_trigger_nodes(self) -> list[dict]:
        """v80: event watchers - fire when a system event matches the pattern."""
        return [n for n in (self.graph or {}).get("nodes", []) if n.get("type") == "event_trigger"]


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
    change (graph / name / description) via services.versions. Bounded -
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


class NotificationRule(Base):
    """Webhook-on-event rule (v44) - POST a JSON payload when runs finish.

    Events: execution_failed | execution_succeeded | execution_cancelled |
    drift_detected (v48, fired by the drift_check node).
    A rule may scope to one workflow (NULL = every workflow). Dispatch is
    fire-and-forget: a slow or dead webhook never slows or breaks a run.
    """

    __tablename__ = "notification_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    events: Mapped[list] = mapped_column(JSONVariant, default=list)  # subset of NOTIFICATION_EVENTS
    webhook_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    headers: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)  # extra headers, e.g. auth
    workflow_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # scope filter
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fire_count: Mapped[int] = mapped_column(Integer, default=0)
    last_status: Mapped[str | None] = mapped_column(String(10), nullable=True)  # ok|error
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)


class PackRegistry(Base):
    """Remote pack source (v43) - a URL Py8n can pull py8n-pack documents from.

    Point it at another instance's ``/templates/gallery/pack``, a teammate's
    shared pack file or any static JSON endpoint; ``check`` dry-runs the pack
    against the local estate and ``sync`` imports it through the ordinary
    pack pipeline (inactive workflows, snapshot versions, skip-with-reason).
    """

    __tablename__ = "pack_registries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(10), nullable=True)  # ok|error
    last_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # import summary or {"error": ...}

