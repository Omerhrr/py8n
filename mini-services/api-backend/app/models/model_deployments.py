"""Model classes: ModelSystem, ModelSystemComponent, ModelDeployment, DeploymentToken, DeploymentRevision, DeploymentTokenPolicy, DeploymentTokenHit.

Split from the original app/models.py (task #3) - moved verbatim, no
behavior change.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._shared import Base, JSONVariant, _now, _uuid

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

