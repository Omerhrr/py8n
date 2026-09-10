"""Model classes: Artifact, TrainedModel, App, Dashboard, AppShareGrant, GrantAuditEvent, DashboardAuditEvent.

Split from the original app/models.py (task #3) - moved verbatim, no
behavior change.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._shared import Base, JSONVariant, _now, _uuid

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

