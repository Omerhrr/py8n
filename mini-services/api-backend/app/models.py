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
    # v54 governance: steward certification stamp (NULL = uncertified). Set
    # by the owner via POST /datasets/{id}/certify - a human promise that
    # this dataset is what it says it is, surfaced in the catalog.
    certified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # v55 governance layer: who answers for this dataset and how sensitive it
    # is - the catalog and impact engine read these to answer "who owns this",
    # "what breaks if it changes" and to rank risk.
    steward: Mapped[str | None] = mapped_column(String(120), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(80), nullable=True)
    classification: Mapped[str | None] = mapped_column(String(20), nullable=True)  # public|internal|confidential|restricted
    sensitivity: Mapped[str | None] = mapped_column(String(20), nullable=True)  # low|medium|high|critical
    retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    # v47 lineage: which workflow/execution/node produced this version
    # (NULL for API/dashboard-side writes - they carry no engine context).
    workflow_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    execution_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    node_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


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
    # v47: per-feature training distributions captured at fit time (numeric
    # quantiles / categorical counts) - the reference for drift scoring.
    reference_stats: Mapped[dict] = mapped_column(JSONVariant, default=dict)
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
    # v47: when set, the public runtime surface (runtime/records/form-submit)
    # requires this token via ?t= or X-Share-Token; NULL keeps legacy open access.
    share_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
    # v47: share token - same contract as apps.share_token.
    share_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
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


class ScheduledReport(Base):
    """Scheduled export job (v48) - snapshot a dataset or dashboard on a cron.

    When the APScheduler job fires, the service serializes the source
    (dataset -> csv/xlsx/json/parquet, dashboard -> a JSON snapshot of every
    rendered component) and stores it as a regular Artifact; the report row
    keeps the last artifact id so the UI can deep-link the download.

    Cron-only by design: a report export is a time-of-day concern, and a
    single crontab string validates + previews with one code path.
    """

    __tablename__ = "scheduled_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # dataset | dashboard
    source_type: Mapped[str] = mapped_column(String(20), nullable=False, default="dataset")
    source_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # dataset exports: csv | xlsx | json | parquet; dashboard exports: json
    fmt: Mapped[str] = mapped_column(String(10), nullable=False, default="csv")
    cron: Mapped[str] = mapped_column(String(100), nullable=False, default="0 6 * * *")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # v52: outbound delivery channels evaluated after every successful run.
    # {"channels": [{"type": "webhook", "url", "headers", "include_attachment"},
    #               {"type": "email", "to", "cc", "subject", "include_attachment"}]}
    # NULL/{} = artifact-only (pre-v52 behaviour, nothing leaves the instance).
    delivery_json: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fire_count: Mapped[int] = mapped_column(Integer, default=0)
    last_status: Mapped[str | None] = mapped_column(String(10), nullable=True)  # ok|error
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    last_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class ReportDeliveryEvent(Base):
    """Scheduled-report delivery log (v52) - did the push-out succeed?

    One row per (report run x delivery channel): a webhook POST or an
    SMTP send attempted after a report run produced its artifact. The
    log answers "did the 6am email actually go out?" without grepping
    server logs - including the negative space (SMTP not configured,
    oversized attachment skipped, webhook answered 500).

    Capped at the newest REPORT_DELIVERY_CAP events per report (trimmed
    on insert) and never written before the artifact exists - a delivery
    failure NEVER fails the report run it belongs to.
    """

    __tablename__ = "report_delivery_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    report_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # webhook | email
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    target: Mapped[str] = mapped_column(String(300), default="")  # url or comma-joined recipients
    # ok | error | skipped
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="ok")
    detail: Mapped[str | None] = mapped_column(String(300), nullable=True)
    artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    attached: Mapped[bool] = mapped_column(Boolean, default=False)  # file went inline/attached
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class AppShareGrant(Base):
    """Row-level share grant (v48) - a named, scoped door into one app.

    Where apps.share_token opens the whole runtime surface, a grant opens a
    SLICE of it: every viewer holding the grant token only ever sees (and,
    for ``eq`` grants, writes) rows matching ``row_filter``::

        {"column": "region", "op": "eq",  "value": "eu"}
        {"column": "region", "op": "in",  "value": ["eu", "us"]}
        {"column": "region", "op": "neq", "value": "internal"}

    Each grant gets its own token + share URL, so per-viewer links can be
    issued and revoked independently. Grants never widen access for the
    owner (the builder always sees all rows) and the legacy full-access
    share token keeps working exactly as before.
    """

    __tablename__ = "app_share_grants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    row_filter: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class GrantAuditEvent(Base):
    """Share-surface access log (v49) - who used which door, and when.

    One row per runtime-surface request on a PROTECTED app (share token or
    grants exist): grant-token views/lists/submits land with their grant
    snapshot, rejected callers (anonymous once protection exists, or a bad
    token) land as outcome="denied" with no grant. ``grant_name`` is a
    snapshot on purpose - the log must stay readable after a grant is
    revoked and deleted. Legacy open apps (no share_token, no grants) are
    never logged: the log exists to answer "what did shared viewers see?",
    not to track the owner's own traffic.

    Capped at the newest GRANT_AUDIT_CAP events per app (trimmed on insert)
    so a hot public link can never grow the table without bound.
    """

    __tablename__ = "grant_audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    grant_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    grant_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # view_runtime | list_records | create_record | update_record |
    # delete_record | view_form | submit_form | access (unknown attempt)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    # allowed | denied
    outcome: Mapped[str] = mapped_column(String(10), nullable=False, default="allowed")
    detail: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class DashboardAuditEvent(Base):
    """Dashboard share-surface access log (v51) - parity with app grants.

    One row per request through a PROTECTED dashboard share (share_token
    set on the board): token-bearing renders of /d/{slug} land as
    outcome="allowed" (action="view_dashboard"), rejected callers as
    "denied" with the reason (missing/invalid token) BEFORE the 403 -
    the same contract GrantAuditEvent established for apps in v49.
    Boards without a share token are never logged: the log answers
    "what did shared viewers see", not the owner's own traffic.

    Capped at the newest DASHBOARD_AUDIT_CAP events per board (trimmed on
    insert) so a hot public link cannot grow the table without bound.
    """

    __tablename__ = "dashboard_audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    dashboard_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # view_dashboard | access (an unknown/failed attempt)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    # allowed | denied
    outcome: Mapped[str] = mapped_column(String(10), nullable=False, default="allowed")
    detail: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class DatasetContract(Base):
    """Declarative data contract (v50) - the schema a dataset PROMISES.

    One active contract per dataset. ``columns_json`` is a list of
    ``{name, dtype, nullable, allowed}`` entries (dtype in
    text|integer|number|boolean|datetime; ``allowed`` restricts the value
    domain, e.g. status in [active, inactive]). Contracts are enforced at
    WRITE time by the dataset_write node and the rows API, before rows land:

    * ``on_violation="error"`` -> the write fails (pipeline hard-stop, the
      data-quality gate made declarative and persistent);
    * ``on_violation="warn"`` -> the write proceeds and the violations
      report rides along on the output / response.

    Checking is castability-based (``"12"`` IS an integer; ``"abc"`` is
    not) so stringly-typed payloads from HTTP sources do not spuriously
    fail, matching how the dataset engine itself normalizes types.
    """

    __tablename__ = "dataset_contracts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    dataset_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    columns_json: Mapped[list] = mapped_column(JSONVariant, default=list)
    # warn | error
    on_violation: Mapped[str] = mapped_column(String(10), default="warn", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class IngestionState(Base):
    """Incremental-ingestion cursor (v50) - where a pipeline left off.

    One row per (dataset_id, key). ``key`` names the pipeline that feeds
    the dataset (default ``"default"``; dataset_trigger nodes use
    ``trigger:{node_id}``), so several pipelines can incrementally feed
    one dataset without stepping on each other.

    ``watermark`` is the high-water mark of the cursor column (e.g.
    ``last_updated``): dataset_write in incremental mode only writes rows
    STRICTLY beyond it (numeric > numeric when both parse as numbers,
    else ISO-datetime/text comparison), then advances the mark to the
    best value seen. The effect is the classic CDC checkpoint -
    ``WHERE last_updated > {{ checkpoint }}`` - without needing the
    source to remember anything. Resetting the row makes the next run
    ingest from scratch.
    """

    __tablename__ = "ingestion_states"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    dataset_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    key: Mapped[str] = mapped_column(String(200), nullable=False, default="default")
    watermark: Mapped[str | None] = mapped_column(String(120), nullable=True)
    runs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # v53: what the LAST run actually did - {"mode", "rows_in", "written",
    # "skipped", "updated", "inserted", "lookback"} - so the ingestion
    # surface shows behaviour, not just a cursor position.
    stats_json: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True, default=None)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (UniqueConstraint("dataset_id", "key", name="uq_ingestion_state"),)


class DatasetContractRevision(Base):
    """Immutable contract-history snapshot (v54) - what the dataset
    promised BEFORE the current contract.

    One row per superseded contract state, written by :func:`put_contract`
    when a contract is replaced and by the delete endpoint when a contract
    is removed (note="contract removed"), so the promise trail survives
    edits and deletions. Diffing two revisions answers "what changed and
    who must re-check their pipelines" without grepping history.

    Capped at the newest MAX_CONTRACT_REVISIONS per dataset (trimmed on
    insert) - contracts change at human speed, not machine speed.
    """

    __tablename__ = "dataset_contract_revisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    dataset_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    columns_json: Mapped[list] = mapped_column(JSONVariant, default=list)
    on_violation: Mapped[str] = mapped_column(String(10), default="warn", nullable=False)
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class SystemDraft(Base):
    """One AI System Builder session (v59).

    The roadmap's Describe -> Discover -> Clarify -> Design -> Build loop:
    the user describes what they want in plain language, the builder
    synthesizes a SystemSpec (purpose, persona, component checklist with
    selected flags, clarifying questions), the interview + toggles refine
    it, and the build step translates the SELECTED components into real
    py8n primitives - datasets, workflow graphs, contracts, policies,
    dashboards, reports and notification rules.

    ``spec_json`` is the living SystemSpec; ``messages_json`` is the
    interview transcript; ``built_json`` holds the refs the build created
    (so review is one GET). Nothing here is derived - it IS the source of
    truth for the conversation - but every BUILT artifact is a normal
    py8n object owned by the user.
    """

    __tablename__ = "system_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")  # the original ask
    persona: Mapped[str] = mapped_column(String(20), default="business")  # business|data_engineer
    status: Mapped[str] = mapped_column(String(20), default="interview", index=True)  # interview|built
    spec_json: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    messages_json: Mapped[list] = mapped_column(JSONVariant, default=list)
    built_json: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Solution(Base):
    """A marketplace solution (v60) - the outcome-named layer over packs.

    Templates say "Webhook workflow"; solutions say "Customer Support
    Automation" and show WHAT YOU GET (the capability checklist) instead
    of what nodes they contain. The ``pack_json`` payload is a standard
    py8n-pack document (workflows + datasets), so installing a solution
    reuses the exact pack-import machinery - and anyone can author one
    from their own workflows/datasets.

    ``owner_id`` NULL = system-curated (seeded showcase solutions);
    otherwise the author, who may unlist it.
    """

    __tablename__ = "solutions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(140), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(140), nullable=False)
    tagline: Mapped[str] = mapped_column(String(300), default="")
    category: Mapped[str] = mapped_column(String(60), default="Operations", index=True)
    icon: Mapped[str] = mapped_column(String(60), default="package")
    color: Mapped[str] = mapped_column(String(20), default="#22d3ee")
    # the capability checklist - the roadmap's "Includes: ✓ ..." list
    outcomes_json: Mapped[list] = mapped_column(JSONVariant, default=list)
    # a standard py8n-pack document (format "py8n-pack")
    pack_json: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    docs: Mapped[str] = mapped_column(Text, default="")
    installs: Mapped[int] = mapped_column(Integer, default=0)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Py8nSystem(Base):
    """A Py8n System (v61) - the operating unit above workflows.

    Where a workflow automates a TASK, a system RUNS A PART OF THE
    BUSINESS: it binds the workflows, datasets, apps, dashboards, models
    and reports that belong together into one named, health-scored,
    ownable unit. Membership is a curated grouping (like folders), so it
    IS stored - but everything the system REPORTS about itself (health,
    activity, freshness) is derived from the member objects at read
    time and can never drift.
    """

    __tablename__ = "py8n_systems"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(140), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    icon: Mapped[str] = mapped_column(String(60), default="boxes")
    color: Mapped[str] = mapped_column(String(20), default="#f97316")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    components: Mapped[list["SystemComponent"]] = relationship(
        back_populates="system",
        cascade="all, delete-orphan",
        order_by="SystemComponent.added_at",
    )


class SystemComponent(Base):
    """One object bound to a system (v61).

    ``kind`` is one of workflow | dataset | app | dashboard | model |
    report - resolved and validated against the live table on attach, so
    a system can never reference an object that does not exist.
    """

    __tablename__ = "system_components"
    __table_args__ = (UniqueConstraint("system_id", "kind", "ref_id", name="uq_system_component"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    system_id: Mapped[str] = mapped_column(ForeignKey("py8n_systems.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    ref_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    system: Mapped[Py8nSystem] = relationship(back_populates="components")


class SystemMember(Base):
    """System-level membership (v62) - who can touch a system, at what role.

    The CREATOR is not stored here: ``py8n_systems.owner_id`` remains the
    single source of truth for ownership (pre-v62 systems keep working with
    zero migration). This table holds the INVITED members:

    * ``viewer`` - can read the system (detail, health, dependency views)
    * ``editor`` - can also bind/unbind components and edit metadata
    * ownership is never shared - the creator anchor cannot be demoted or
      removed, and invites are editor/viewer only.

    Membership is a permission grant, so it IS stored; everything the
    system reports stays derived.
    """

    __tablename__ = "system_members"
    __table_args__ = (UniqueConstraint("system_id", "user_id", name="uq_system_member"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    system_id: Mapped[str] = mapped_column(ForeignKey("py8n_systems.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(10), nullable=False, default="viewer")  # editor|viewer
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ModelSystem(Base):
    """A Model System (v63) - the AI model-building operating unit.

    Where a Py8n System runs a part of the BUSINESS, a Model System BUILDS
    AND OPERATES A MODEL: it binds the datasets, trained models, training /
    retraining / deployment workflows and reports that one model's life
    belongs to. Membership is curated (stored); every section the model
    system REPORTS (training summary, evaluation, composition, monitoring
    coverage, retraining schedules) is derived from the member objects at
    read time and can never drift. A model system is itself bindable into
    a Py8n System as the ``model_system`` component kind - the Company AI
    System pattern: data systems + model systems + agent workflows in one
    health-scored unit.
    """

    __tablename__ = "model_systems"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(140), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    icon: Mapped[str] = mapped_column(String(60), default="brain-circuit")
    color: Mapped[str] = mapped_column(String(20), default="#818cf8")
    # declared modality focus: text|image|audio|document|tabular|multimodal
    modalities: Mapped[list] = mapped_column(JSONVariant, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    components: Mapped[list["ModelSystemComponent"]] = relationship(
        back_populates="model_system",
        cascade="all, delete-orphan",
        order_by="ModelSystemComponent.added_at",
    )


class ModelSystemComponent(Base):
    """One object bound to a model system (v63).

    ``kind`` is one of dataset | model | workflow | report - resolved and
    validated against the live table on attach with owner scoping.
    """

    __tablename__ = "model_system_components"
    __table_args__ = (UniqueConstraint("model_system_id", "kind", "ref_id", name="uq_model_system_component"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    model_system_id: Mapped[str] = mapped_column(ForeignKey("model_systems.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    ref_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    model_system: Mapped[ModelSystem] = relationship(back_populates="components")


class ModelDeployment(Base):
    """A model deployment (v67) - the DEPLOY verb made first-class.

    A deployment turns a registry row into a LIVE serving endpoint: py8n
    generates the serving workflow (a webhook trigger wired to lm_generate
    for language models, or split_out -> model_predict for tabular ones),
    activates it, and the deployment row is the handle you operate - list,
    inspect, disable, retire. The workflow is a normal py8n object (you can
    watch its executions, edit the graph, add monitoring downstream); the
    deployment row just owns the pairing. Serving statistics are DERIVED
    from the execution log at read time and never stored.
    """

    __tablename__ = "model_deployments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(140), nullable=False)
    # the registry row being served (trained_models.id)
    model_registry_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # serving shape: "generate" (lm_generate) | "predict" (model_predict)
    serving_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="predict")
    # dev | staging | prod
    environment: Mapped[str] = mapped_column(String(20), nullable=False, default="dev")
    workflow_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DeploymentToken(Base):
    """Serving token (v68) - credential for a deployed model's endpoint.

    A deployment that has at least one ACTIVE (non-revoked) token demands
    ``Authorization: Bearer <token>`` (or ``X-Deployment-Token``) on every
    call to its serving workflow; zero active tokens = open endpoint (the
    v67 behavior, kept for backward compatibility). The full token
    (``py8nd_`` + 32 url-safe chars) is shown exactly once at creation;
    storage keeps only the sha256 hash plus a display prefix - the same
    discipline as v41 API keys, but scoped to ONE deployment instead of
    the whole API. Tokens never grant API access: they only open the
    serving webhook they belong to.
    """

    __tablename__ = "deployment_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    deployment_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    prefix: Mapped[str] = mapped_column(String(24), default="")  # display form, e.g. py8nd_ab12cd34
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # sha256 hex
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeploymentRevision(Base):
    """Deployment revision (v68) - the redeploy/rollback ledger.

    Every time a deployment is created, redeployed to another registry
    version, or rolled back, a revision row records WHICH registry row the
    serving endpoint pointed at, when, and why. Exactly one revision is
    active at a time and it mirrors deployment.model_registry_id; the
    ledger itself is append-only event history (like the execution log),
    so rolling back is just activating an older ledger entry and patching
    the serving workflow's model parameter in place.
    """

    __tablename__ = "deployment_revisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    deployment_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # monotonic per deployment: 1 = the initial deploy
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    model_registry_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # snapshot of the registry row at activation time (the row may vanish later)
    model_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    model_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    algorithm: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    action: Mapped[str] = mapped_column(String(20), nullable=False, default="deploy")  # deploy|redeploy|rollback
    note: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    deployed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class InteractionConversation(Base):
    """A conversation (v68) - the interaction layer's unit of continuity.

    Channels (voice, whatsapp, telegram, discord, web, app, api, sms,
    email) are interchangeable ADAPTERS; the conversation is the thing
    that persists underneath them. A participant can move between
    channels mid-conversation (conversation_ref rebinds) and the same
    handler workflow, history and context carry over - one customer, one
    context, one AI, regardless of how they reached us.
    """

    __tablename__ = "interaction_conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # the channel the conversation STARTED on (per-message channels live on the messages)
    channel: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    participant_id: Mapped[str] = mapped_column(String(180), nullable=False, default="")
    participant_name: Mapped[str] = mapped_column(String(180), nullable=False, default="")
    # the workflow that answers inbound text (last node's output supplies the reply)
    handler_workflow_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="open", index=True)  # open|closed
    outcome: Mapped[str] = mapped_column(String(180), nullable=False, default="")
    context: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InteractionMessage(Base):
    """One message inside a conversation (v68).

    ``role`` is user | agent | human_agent | system; ``channel`` records
    the adapter the message actually traveled through, so a transcript
    shows the channel hops (phone -> whatsapp -> app) without the business
    logic ever caring about them.
    """

    __tablename__ = "interaction_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user|agent|human_agent|system
    channel: Mapped[str] = mapped_column(String(30), nullable=False, default="")
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ChannelEndpoint(Base):
    """A channel endpoint (v69) - the REAL provider adapter surface.

    v68's universal ingress accepted already-normalized messages; v69 makes
    py8n itself the adapter: a ChannelEndpoint is a stored handle that turns
    a provider's NATIVE webhook (Meta Cloud API, Telegram Bot API, Discord
    interactions) into interaction-layer ingests, and knows the exact
    outbound request each provider's send API expects.

    One endpoint = one provider connection = one owner = one handler
    workflow. The provider-specific secrets (verify_token, app_secret,
    bot_token, secret_token, public_key) live in ``config`` and are always
    MASKED in API output - the raw values are only used at verification and
    delivery time. The webhook URLs are public (providers can't log in);
    each receiver verifies its provider's credentials before anything runs.
    """

    __tablename__ = "channel_endpoints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(140), nullable=False)
    # provider id inside PROVIDER_ADAPTERS: meta_cloud_api | telegram_bot_api | discord_bot
    provider: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # the interaction channel this provider delivers: whatsapp | telegram | discord
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    handler_workflow_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # provider-specific secrets + settings; masked in every API output
    config: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    # derived-at-write event counters (the transcript lives in interactions)
    events_received: Mapped[int] = mapped_column(Integer, default=0)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class VoiceSession(Base):
    """A voice session (v69) - the call as a first-class primitive.

    One phone conversation as py8n sees it: a call-state machine
    (initiated -> ringing -> in_progress -> ended, with no_answer / busy /
    voicemail endings), an optional link to the interaction-layer
    conversation (so the voice transcript lives in the SAME place as
    whatsapp/app transcripts), and the barge-in bookkeeping. The event
    timeline (VoiceEvent) is the record; everything reported about the
    call (duration, barge-in count, turn count) is derived from it.
    """

    __tablename__ = "voice_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    direction: Mapped[str] = mapped_column(String(10), nullable=False, default="inbound")  # inbound|outbound
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="twilio")
    # provider-side call identifiers (CallSid, provider call id, ...)
    call_ref: Mapped[str] = mapped_column(String(180), nullable=False, default="")
    from_ref: Mapped[str] = mapped_column(String(180), nullable=False, default="")
    to_ref: Mapped[str] = mapped_column(String(180), nullable=False, default="")
    # the answering workflow (voice_turn runs it exactly like the interaction handler)
    handler_workflow_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # optional link into the interaction layer - one customer, one transcript
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # initiated | ringing | in_progress | on_hold | voicemail | ended
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="initiated", index=True)
    end_reason: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    # context holds live call state (active_tts event id) + provider extras
    context: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VoiceEvent(Base):
    """One event on a voice session (v69) - the append-only call timeline.

    Kinds: call.ringing | call.answered | speech.started | speech.ended |
    dtmf | asr.final | tts.started | tts.ended | barge_in | hold | unhold |
    transfer | no_answer | busy | voicemail_detected | hangup | failed.
    Barge-in semantics live here: a barge_in event references the
    tts.started event it interrupted, and the tts.ended carries
    cancelled=true.
    """

    __tablename__ = "voice_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DeploymentTokenPolicy(Base):
    """Rate-shaping/quotas on a serving token (v69, cross-process in v70).

    A token may carry a policy: a per-minute rate cap (sliding window)
    and a UTC calendar-day quota. Enforcement happens right after token
    auth succeeds on the serving webhook / stream endpoints - a
    shape-limited request gets 429 with Retry-After and X-RateLimit-*
    headers; an exhausted quota gets 429 with the UTC reset time. Zero
    policy (the default) keeps the token unlimited - rate shaping is
    opt-in per token, exactly like auth is opt-in per deployment.

    The policy row is the only CONFIG stored part; the counters live in
    ``deployment_token_hits`` (v70): one row per admitted request, in the
    SAME database every process shares, so two uvicorn workers (or two
    boxes behind the balancer) enforce ONE limit instead of each seeing
    its own. v69 counted per-process - correct arithmetic, but N workers
    meant N silent allowances; v70 makes the database the single truth.
    """

    __tablename__ = "deployment_token_policies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    token_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    rate_per_min: Mapped[int | None] = mapped_column(Integer, nullable=True)   # requests/minute, NULL = unlimited
    daily_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)    # requests/UTC day, NULL = unlimited
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DeploymentTokenHit(Base):
    """One admitted request on a serving token (v70) - the shared counter.

    This IS the cross-process limit storage: ``admit`` inserts a row and
    reads the window counts back from the same table every other process
    writes to. The sliding minute window = rows with ``admitted_at``
    inside the last 60 seconds; the daily quota = rows with today's
    ``quota_day``. Rows older than two days are pruned opportunistically
    on admit, so the table stays tiny without a sweeper. Admitted
    requests only (the v69 semantics kept); rejects are answered but not
    counted as traffic.
    """

    __tablename__ = "deployment_token_hits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    token_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # window key for the per-minute sliding count (indexed with token_id)
    admitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    # UTC calendar-day key for the quota count ("2026-09-04")
    quota_day: Mapped[str] = mapped_column(String(10), nullable=False)
