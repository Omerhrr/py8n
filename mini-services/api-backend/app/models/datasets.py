"""Model classes: Dataset, DatasetVersion, DatasetContract, IngestionState, DatasetContractRevision.

Split from the original app/models.py (task #3) - moved verbatim, no
behavior change.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._shared import Base, JSONVariant, _now, _uuid

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
    # warn | error | dead_letter
    on_violation: Mapped[str] = mapped_column(String(20), default="warn", nullable=False)
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
    on_violation: Mapped[str] = mapped_column(String(20), default="warn", nullable=False)
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)

