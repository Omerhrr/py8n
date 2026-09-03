"""The data catalog (v50) - one inventory of every dataset and its people.

For each dataset visible to the caller the catalog assembles the card a
data platform owes you:

* identity:   name, description, owner, tags, source
* shape:      row/column counts, latest version + version count
* freshness:  age of the last write (from the version timeline)
* governance: contract presence/version
* graph:      PRODUCERS (workflows that wrote it, from version lineage)
              and CONSUMERS (workflows whose dataset_read / dataset_write /
              sql_query nodes reference it), resolved to workflow names

The catalog is DERIVED - no new truth is stored - so it can never drift
from what actually happened. Consumer scanning is a plain substring match
of the dataset's view name over active workflow graphs, which is exactly
how the engine itself resolves datasets at runtime (id-first, then
case-insensitive name), so a workflow that reads "Customers" shows up as
a consumer of the dataset called "Customers".
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Dataset, DatasetContract, DatasetVersion, User, Workflow

_CONSUMER_NODE_TYPES = {"dataset_read", "dataset_write", "sql_query", "dataset_export"}


def _view_names(ds: Dataset) -> set[str]:
    """Names a workflow might use to reference this dataset (view-name form)."""
    view = "".join(ch if ch.isalnum() else "_" for ch in ds.name.lower())
    return {ds.name, ds.name.lower(), view}


def _graph_mentions_dataset(graph: dict | None, names: set[str]) -> list[dict]:
    """Nodes in a graph whose params/SQL reference any of the names."""
    hits: list[dict] = []
    for node in (graph or {}).get("nodes", []):
        if node.get("type") not in _CONSUMER_NODE_TYPES:
            continue
        params = node.get("parameters") or {}
        blob = " ".join(str(v) for v in params.values()).lower()
        if any(n.lower() in blob for n in names):
            hits.append({"node_id": node.get("id"), "node_name": node.get("name"), "type": node.get("type")})
    return hits


async def _producer_map(db: AsyncSession, dataset_ids: list[str]) -> dict[str, list[str]]:
    """dataset_id -> distinct producing workflow names (from version lineage)."""
    if not dataset_ids:
        return {}
    rows = (
        await db.execute(
            select(DatasetVersion.dataset_id, DatasetVersion.workflow_id)
            .where(
                DatasetVersion.dataset_id.in_(dataset_ids),
                DatasetVersion.workflow_id.is_not(None),
            )
            .distinct()
        )
    ).all()
    wf_ids = sorted({r[1] for r in rows})
    names: dict[str, str] = {}
    if wf_ids:
        for wf in (
            await db.execute(select(Workflow).where(Workflow.id.in_(wf_ids)))
        ).scalars().all():
            names[wf.id] = wf.name
    out: dict[str, list[str]] = {}
    for ds_id, wf_id in rows:
        out.setdefault(ds_id, [])
        nm = names.get(wf_id)
        if nm and nm not in out[ds_id]:
            out[ds_id].append(nm)
    return out


async def _consumer_map(db: AsyncSession, datasets: list[Dataset]) -> dict[str, list[str]]:
    """dataset_id -> workflow names whose nodes reference it (active graphs)."""
    workflows = (
        await db.execute(select(Workflow).where(Workflow.is_active.is_(True)))
    ).scalars().all()
    out: dict[str, list[str]] = {ds.id: [] for ds in datasets}
    for wf in workflows:
        for ds in datasets:
            hits = _graph_mentions_dataset(wf.graph, _view_names(ds))
            if hits and wf.name not in out[ds.id]:
                out[ds.id].append(wf.name)
    return out


async def _version_stats(db: AsyncSession, dataset_ids: list[str]) -> dict[str, dict]:
    """dataset_id -> {version_count, latest_version, latest_at}."""
    if not dataset_ids:
        return {}
    counts = (
        await db.execute(
            select(
                DatasetVersion.dataset_id,
                func.count(DatasetVersion.id),
                func.max(DatasetVersion.version),
                func.max(DatasetVersion.created_at),
            )
            .where(DatasetVersion.dataset_id.in_(dataset_ids))
            .group_by(DatasetVersion.dataset_id)
        )
    ).all()
    return {
        r[0]: {
            "version_count": int(r[1]),
            "latest_version": int(r[2] or 0),
            "latest_at": r[3].isoformat() if r[3] else None,
        }
        for r in counts
    }


def _freshness_tier(age_seconds: float | None) -> str:
    if age_seconds is None:
        return "never"
    if age_seconds < 3600:
        return "fresh"
    if age_seconds < 86400:
        return "hours"
    if age_seconds < 7 * 86400:
        return "stale"
    return "cold"


async def build_catalog(
    db: AsyncSession,
    owner_id: str | None = None,
    q: str = "",
    tag: str = "",
) -> list[dict]:
    """Catalog entries for every dataset visible to the caller.

    Visibility mirrors the rest of the estate: with ``owner_id`` set,
    another owner's datasets are invisible (NULL owner_id stays public).
    """
    stmt = select(Dataset).order_by(Dataset.updated_at.desc())
    if owner_id is not None:
        stmt = stmt.where(Dataset.owner_id.is_(None) | (Dataset.owner_id == owner_id))
    datasets = (await db.execute(stmt)).scalars().all()

    q_l = (q or "").strip().lower()
    tag_l = (tag or "").strip().lower()
    if q_l:
        datasets = [
            ds for ds in datasets
            if q_l in ds.name.lower() or q_l in (ds.description or "").lower()
        ]
    if tag_l:
        datasets = [ds for ds in datasets if tag_l in [str(t).lower() for t in (ds.tags or [])]]

    ids = [ds.id for ds in datasets]
    producers = await _producer_map(db, ids)
    consumers = await _consumer_map(db, datasets)
    vstats = await _version_stats(db, ids)

    contract_rows = (
        await db.execute(
            select(DatasetContract).where(DatasetContract.dataset_id.in_(ids or ["-"]))
        )
    ).scalars().all()
    contracts = {c.dataset_id: c for c in contract_rows}

    owner_names: dict[str, str] = {}
    owner_ids = sorted({ds.owner_id for ds in datasets if ds.owner_id})
    if owner_ids:
        for u in (
            await db.execute(select(User).where(User.id.in_(owner_ids)))
        ).scalars().all():
            owner_names[u.id] = u.name or u.email

    now = datetime.now(timezone.utc)
    entries: list[dict] = []
    for ds in datasets:
        vs = vstats.get(ds.id, {})
        latest_at = vs.get("latest_at")
        age_seconds = None
        if latest_at:
            dt = datetime.fromisoformat(latest_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_seconds = max(0.0, (now - dt).total_seconds())
        contract = contracts.get(ds.id)
        entries.append({
            "id": ds.id,
            "name": ds.name,
            "description": ds.description or "",
            "owner": owner_names.get(ds.owner_id) if ds.owner_id else None,
            "tags": ds.tags or [],
            "source": ds.source,
            "rows": int(ds.row_count or 0),
            "columns": len(ds.schema_json or []),
            "schema_preview": [
                {"name": c.get("name"), "dtype": c.get("dtype")}
                for c in (ds.schema_json or [])[:8]
            ],
            "freshness": {
                "last_write_at": latest_at,
                "age_minutes": round(age_seconds / 60, 1) if age_seconds is not None else None,
                "tier": _freshness_tier(age_seconds),
            },
            "versions": {
                "count": vs.get("version_count", 0),
                "latest": vs.get("latest_version", 0),
            },
            "contract": {
                "present": contract is not None,
                "on_violation": contract.on_violation if contract else None,
                "version": int(contract.version or 1) if contract else 0,
            },
            # v54 governance: steward certification + ownership status
            "certified": ds.certified_at is not None,
            "certified_at": ds.certified_at.isoformat() if ds.certified_at else None,
            "owner_id": ds.owner_id,
            "claimable": ds.owner_id is None,
            "producers": producers.get(ds.id, []),
            "consumers": consumers.get(ds.id, []),
        })
    return entries
