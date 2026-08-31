"""API request/response schemas (Pydantic v2)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, Field


def _normalize_tags(value: Any) -> list[str]:
    """Canonical tag form: trimmed, whitespace-collapsed, lowercase, deduped.

    Caps at 10 tags x 32 chars so the dashboard chip row stays sane; junk
    entries (empty strings, non-strings) are dropped silently.
    """
    if not value:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            continue
        tag = " ".join(raw.split()).lower()[:32]
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out[:10]


# Normalized on input AND output so legacy rows / API clients get one shape.
Tags = Annotated[list[str], BeforeValidator(_normalize_tags)]


class WorkflowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    graph: dict = Field(default_factory=lambda: {"nodes": [], "edges": []})
    is_active: bool = True
    error_workflow_id: str | None = None
    tags: Tags = Field(default_factory=list)
    folder_id: str | None = Field(default=None, max_length=36)  # v16


class WorkflowUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    graph: dict | None = None
    is_active: bool | None = None
    # Tri-state: omitted (None) = untouched; "" = clear binding; str = bind.
    error_workflow_id: str | None = Field(default=None, max_length=36)
    # v12: omitted (None) = untouched; list (possibly empty) = replace.
    tags: Tags | None = None
    # v16 tri-state: omitted (None) = untouched; "" = move to root; str = assign.
    folder_id: str | None = Field(default=None, max_length=36)
    # v20 tri-state: omitted = untouched; null = inherit global policy;
    # 0 = keep forever; N = purge this workflow's logs after N days.
    retention_days: int | None = Field(default=None, ge=0, le=3650)


class WorkflowOut(BaseModel):
    id: str
    name: str
    description: str
    graph: dict
    is_active: bool
    error_workflow_id: str | None = None
    tags: Tags = Field(default_factory=list)
    folder_id: str | None = None
    retention_days: int | None = None
    owner_id: str | None = None  # v37: owning user (NULL = unclaimed)
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class WorkflowListItem(BaseModel):
    id: str
    name: str
    description: str
    is_active: bool
    node_count: int
    trigger_types: list[str]
    schedule_summary: str | None = None
    next_run_at: str | None = None
    error_workflow_id: str | None = None
    error_workflow_name: str | None = None
    tags: Tags = Field(default_factory=list)
    folder_id: str | None = None
    folder_name: str | None = None
    retention_days: int | None = None
    created_at: datetime
    updated_at: datetime


class FolderCreate(BaseModel):
    """v16 - new folder; parent_id nests it (max depth enforced server-side)."""

    name: str = Field(min_length=1, max_length=120)
    parent_id: str | None = Field(default=None, max_length=36)


class FolderUpdate(BaseModel):
    """v16 - rename and/or re-parent. parent_id tri-state like WorkflowUpdate:
    omitted = untouched, "" = move to root, str = move under that folder."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    parent_id: str | None = Field(default=None, max_length=36)


class FolderOut(BaseModel):
    id: str
    name: str
    parent_id: str | None = None
    workflow_count: int = 0      # direct workflows
    total_count: int = 0         # workflows in this folder + all descendants
    created_at: datetime
    updated_at: datetime


class ScheduleEntryOut(BaseModel):
    """One schedule_trigger node with a human summary + next fire previews."""

    node_id: str
    node_name: str
    mode: str
    cron: str | None = None
    interval_seconds: Any = None
    summary: str
    next_runs: list[str] = Field(default_factory=list)
    error: str | None = None


class WorkflowScheduleOut(BaseModel):
    """Schedule introspection for one workflow (GET / activate / deactivate)."""

    workflow_id: str
    is_active: bool
    schedules: list[ScheduleEntryOut] = Field(default_factory=list)
    next_run_at: str | None = None


class GlobalScheduleEntryOut(ScheduleEntryOut):
    """Row of the cross-workflow schedules overview."""

    workflow_id: str
    workflow_name: str
    is_active: bool


class RunRequest(BaseModel):
    trigger_node_id: str | None = None
    payload: dict | None = Field(default=None, description="Optional payload injected into the trigger output")


class NodeTestRequest(BaseModel):
    """Body for POST /workflows/{id}/nodes/{node_id}/test (v17 test step)."""

    items: Any = Field(
        default=None,
        description="Input data for the node - exposed as {{ input }} / input_data. None = no input.",
    )


class ResumeRequest(BaseModel):
    token: str = Field(description="Resume token emitted by the Wait for Resume node")
    payload: Any = Field(default=None, description="Data delivered to the resumed flow (becomes the wait node's output)")


class RunAccepted(BaseModel):
    execution_id: str
    status: str = "queued"
    message: str = "Execution dispatched"


class CredentialCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type: str = Field(default="generic", description="header_auth | openai_compatible | generic")
    data: dict = Field(default_factory=dict, description="Secret payload - encrypted at rest, never returned")


class CredentialOut(BaseModel):
    id: str
    name: str
    type: str
    masked_hint: str
    created_at: datetime


class CredentialDetail(CredentialOut):
    """Edit-time view: non-secret fields visible, secrets blanked.

    The client re-sends untouched secrets as ``"__keep__"`` in PATCH data;
    the server substitutes the stored value (secrets never leave the vault).
    """

    data: dict = Field(default_factory=dict, description="Non-secret fields visible; secret fields are empty strings")


class CredentialUpdate(BaseModel):
    """PATCH payload - name and/or full replacement data (secrets are never
    echoed back, so the client re-sends the complete field set)."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    data: dict | None = None


class CredentialTestRequest(BaseModel):
    test_url: str | None = Field(
        default=None,
        description="Override target for HTTP-based probes (header_auth/basic_auth/generic)",
    )


class CredentialTestResult(BaseModel):
    ok: bool
    message: str
    latency_ms: int
    probed_at: datetime


class CredentialUsageWorkflow(BaseModel):
    id: str
    name: str
    active: bool
    nodes: list[str] = Field(default_factory=list, description="Node ids referencing the credential")


class CredentialUsage(BaseModel):
    credential_id: str
    workflow_count: int
    workflows: list[CredentialUsageWorkflow] = Field(default_factory=list)


class WebhookInfo(BaseModel):
    workflow_id: str
    url: str
    methods: list[str]
    response_mode: str | None = None


class WorkflowExportDoc(BaseModel):
    """Portable workflow document (download / share between instances)."""

    format: str = "py8n-workflow"
    version: int = 1
    name: str
    description: str = ""
    graph: dict
    exported_at: datetime | None = None


class WorkflowImportRequest(BaseModel):
    data: WorkflowExportDoc | None = Field(default=None, description="Wrapped export document")
    # Convenience: also accept a bare export document at the top level
    name: str | None = None
    description: str | None = None
    graph: dict | None = None


# ------------------------------------------------------------------ v15 env vars
class EnvVariableOut(BaseModel):
    """List/detail view. Secrets never carry a value back to any client."""

    id: str
    key: str
    value: str | None = Field(default=None, description="Plaintext value - null for secrets")
    is_secret: bool
    description: str = ""
    updated_at: datetime


class EnvVariableCreate(BaseModel):
    key: str = Field(min_length=1, max_length=64, description="Stored as typed; letters/digits/underscores, no leading digit")
    value: str = Field(default="", max_length=20_000)
    is_secret: bool = False
    description: str = Field(default="", max_length=500)


class EnvVariableUpdate(BaseModel):
    value: str | None = Field(default=None, max_length=20_000, description='"__keep__" preserves the stored value')
    is_secret: bool | None = None
    description: str | None = Field(default=None, max_length=500)


# ------------------------------------------------------------------ v27 datasets
class DatasetOut(BaseModel):
    """Metadata view (never carries rows - fetch /rows for data)."""

    id: str
    name: str
    description: str = ""
    schema_json: list = Field(default_factory=list)
    row_count: int = 0
    source: str = "api"
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DatasetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    rows: list[dict] = Field(default_factory=list, description="Flat JSON objects; nested values are JSON-encoded")


class DatasetRowsIn(BaseModel):
    rows: list[dict] = Field(min_length=1, max_length=10_000)


class DatasetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)


class DatasetQueryIn(BaseModel):
    sql: str = Field(min_length=1, max_length=20_000, description="DuckDB SQL; datasets appear as views (lowercased name)")


# ------------------------------------------------------------------ v29 apps
class AppCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    dataset_id: str | None = Field(default=None, max_length=36)
    generate: bool = Field(default=True, description="Auto-lay-out components from the bound dataset")
    config: dict | None = Field(default=None, description="Explicit config wins over generate")


class AppUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    # Tri-state (v20 pattern): omitted = untouched; "" = unbind; str = bind.
    dataset_id: str | None = Field(default=None, max_length=36)
    config: dict | None = None


class AppOut(BaseModel):
    id: str
    name: str
    slug: str
    description: str = ""
    dataset_id: str | None = None
    dataset_name: str | None = None
    config: dict = Field(default_factory=dict)
    status: str = "draft"
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AppRecordIn(BaseModel):
    record: dict = Field(min_length=1, max_length=200)


# ------------------------------------------------------------------ v30 rules/forms
class RulesPut(BaseModel):
    # Bound generously here; the service owns the real cap (50) with a 400+message.
    rules: list[dict] = Field(default_factory=list, max_length=200, description="Full replacement ruleset")


class RulesTestIn(BaseModel):
    record: dict = Field(min_length=1, max_length=200)
    event: str = Field(default="create", description="create | update")


# ------------------------------------------------------------------ v31 dashboards
class DashboardCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    dataset_ids: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Generate the layout from these datasets (order matters); empty + generate=false → blank",
    )
    generate: bool = Field(default=True, description="Auto-lay-out components from the given datasets")
    config: dict | None = Field(default=None, description="Explicit config wins over generate")


class DashboardUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    config: dict | None = None


class DashboardOut(BaseModel):
    id: str
    name: str
    slug: str
    description: str = ""
    config: dict = Field(default_factory=dict)
    status: str = "draft"
    created_at: datetime | None = None
    updated_at: datetime | None = None
