"""SQLAlchemy ORM models: workflows, execution logs, credentials.

Graphs are stored as JSON (JSONB on PostgreSQL via variant) so the visual
canvas document maps 1:1 to a database row - the same design used by n8n.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


JSONVariant = JSON().with_variant(JSONB(), "postgresql")


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
    # n8n-style error workflow: dispatched with a structured payload when an
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


class Dataset(Base):
    """First-class tabular dataset (v27) - the data platform foundation.

    Rows live in a Parquet file (``{id}.parquet`` under data/datasets/,
    written/read via DuckDB); this row holds metadata only. Workflows and
    apps read/write datasets through the dataset_* nodes and REST API.
    """

    __tablename__ = "datasets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # v37
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    file_path: Mapped[str] = mapped_column(String(200), default="")  # relative filename
    # [{"name": "...", "dtype": "text|integer|number|boolean|datetime"}]
    schema_json: Mapped[list] = mapped_column(JSONVariant, default=list)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(20), default="api")  # api|upload|workflow
    tags: Mapped[list | None] = mapped_column(JSONVariant, nullable=True)  # v44 tag strings
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class DatasetVersion(Base):
    """Point-in-time snapshot of a dataset's parquet file (v44).

    Every mutation (create, append, replace, restore) writes the current
    state to ``{versions_dir}/{dataset_id}/v{N}.parquet`` before/after the
    fact and records a row here, so any dataset can be rolled back to an
    earlier shape. Capped per dataset (MAX_DATASET_VERSIONS) - the oldest
    snapshots beyond the cap are pruned with their files.
    """

    __tablename__ = "dataset_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    dataset_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(20), default="append")  # create|import|append|replace|restore|workflow
    note: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class NotificationRule(Base):
    """Webhook-on-event rule (v44) - POST a JSON payload when runs finish.

    Events: execution_failed | execution_succeeded | execution_cancelled.
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


class Artifact(Base):
    """Binary artifact produced by workflow runs (v28) - charts, ML models.

    Bytes live under data/artifacts/ (``{id}.{ext}``); metadata here. Chart
    PNGs are rendered inline in the executions drawer; model pickles are
    re-loadable for prediction. A retention/purge policy may come later.
    """

    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(20), default="file", index=True)  # chart|model|file
    filename: Mapped[str] = mapped_column(String(200), default="")
    content_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    # free-form context: {title, chart_type, model, target, features, node, workflow_id, ...}
    meta: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    workflow_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    execution_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class TrainedModel(Base):
    """Model registry (v46) - first-class, versioned ML models.

    One row per trained version: name + version identify the model
    (highest version = latest; exactly one ACTIVE version per name is what
    model_predict scores with by default). The fitted pipeline (preprocessing
    included - imputer/scaler/one-hot + estimator, plus the label encoder)
    lives in the referenced artifact's pickle. owner scoping matches every
    other v37 surface.
    """

    __tablename__ = "trained_models"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_model_name_version"), )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    algorithm: Mapped[str] = mapped_column(String(60), nullable=False)
    task: Mapped[str] = mapped_column(String(20), nullable=False, default="classification")  # classification|regression
    target: Mapped[str] = mapped_column(String(120), default="")
    features: Mapped[list] = mapped_column(JSONVariant, default=list)
    metrics: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    dataset_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class App(Base):
    """First-class application (v29) - the Excel → App builder flagship.

    An app binds ONE dataset and a component ``config`` (stat cards, data
    table, form, chart). Drafts live in the builder; published apps are
    served at ``/run/{slug}`` where end users browse, create, edit and
    delete records - every mutation lands in the bound dataset's parquet.
    """

    __tablename__ = "apps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # v37
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    slug: Mapped[str] = mapped_column(String(140), nullable=False, unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    dataset_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # {"components": [{id, type: stat|table|form|chart, ...type params}]}
    config: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)  # draft|published
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Dashboard(Base):
    """First-class dashboard (v31) - the analytical face of the Data OS.

    Where an App binds ONE dataset and owns the write path, a Dashboard is
    read-only analytics over MANY datasets: every component carries its own
    ``dataset_id``, so one board can mix KPIs from a CRM dataset with
    breakdown charts from a billing dataset. Published boards are served at
    ``/d/{slug}``.

    config = {"components": [
        {"id", "type": "stat",  "dataset_id", "label", "agg", "column"?},
        {"id", "type": "chart", "dataset_id", "title", "chart_type": bar|line|pie,
         "group_by", "agg", "column"?},
        {"id", "type": "table", "dataset_id", "title", "columns", "limit"?},
        {"id", "type": "text",  "title", "body"},
    ]}
    """

    __tablename__ = "dashboards"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # v37
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    slug: Mapped[str] = mapped_column(String(140), nullable=False, unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    # {"components": [{id, type: stat|chart|table|text, ...type params}]}
    config: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)  # draft|published
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


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
