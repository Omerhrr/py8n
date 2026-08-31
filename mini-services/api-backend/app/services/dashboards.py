"""Dashboard core (v31) - the analytical face of the Data OS.

Where apps (v29) bind ONE dataset and own the write path, a dashboard is
read-only analytics over MANY datasets. Every component carries its own
``dataset_id``, so one board mixes KPIs from a CRM dataset with breakdown
charts from a billing dataset - the "aggregate view across the business"
that a single-dataset app cannot show.

config = {"components": [
    {"id": "kpi",       "type": "stat",  "dataset_id": "...", "label": "Total",
     "agg": "count"}                                                    # or sum/avg/min/max + column
    {"id": "breakdown", "type": "chart", "dataset_id": "...", "title": "By plan",
     "chart_type": "bar",  "group_by": "plan", "agg": "count"}          # bar|line|pie
    {"id": "trend",     "type": "chart", "chart_type": "line", ...}      # labels sorted ascending
    {"id": "recent",    "type": "table", "dataset_id": "...", "title": "Latest",
     "columns": [...], "limit": 8}
    {"id": "note",      "type": "text",  "title": "Read me", "body": "..."}
]}

Chart difference vs apps: a dashboard renders EVERY chart component (apps
render the first only), ``line`` sorts labels ascending for trend reading,
and ``pie`` stays capped at 8 slices. compute_config() returns the rendered
payload for the whole board - the single source of truth used by both the
draft preview endpoint and the published runtime.
"""

from __future__ import annotations

import re

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Dashboard, Dataset
from . import datasets as ds_svc

COMPONENT_TYPES = {"stat", "chart", "table", "text"}
CHART_TYPES = {"bar", "line", "pie"}
AGGS = {"count", "sum", "avg", "min", "max"}
MAX_COMPONENTS = 32
PIE_SLICES = 8
DEFAULT_TABLE_LIMIT = 8


# ----------------------------------------------------------------- slugs
def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:100]
    return s or "dashboard"


async def unique_slug(db: AsyncSession, name: str, exclude_id: str | None = None) -> str:
    base = slugify(name)
    slug, n = base, 2
    q = select(Dashboard).where(Dashboard.slug == slug)
    if exclude_id:
        q = q.where(Dashboard.id != exclude_id)
    while (await db.execute(q)).scalar_one_or_none() is not None:
        slug = f"{base}-{n}"
        n += 1
        q = select(Dashboard).where(Dashboard.slug == slug)
        if exclude_id:
            q = q.where(Dashboard.id != exclude_id)
    return slug


async def name_taken(db: AsyncSession, name: str, exclude_id: str | None = None) -> bool:
    q = select(Dashboard).where(func.upper(Dashboard.name) == name.strip().upper())
    if exclude_id:
        q = q.where(Dashboard.id != exclude_id)
    return (await db.execute(q)).scalar_one_or_none() is not None


async def get_dashboard(db: AsyncSession, ref: str) -> Dashboard | None:
    """Resolve a dashboard by id, then by case-insensitive name."""
    row = await db.get(Dashboard, ref)
    if row is not None:
        return row
    return (
        await db.execute(
            select(Dashboard).where(func.upper(Dashboard.name) == ref.strip().upper())
        )
    ).scalar_one_or_none()


async def get_by_slug(db: AsyncSession, slug: str) -> Dashboard | None:
    return (
        await db.execute(
            select(Dashboard).where(Dashboard.slug == slug.strip().lower())
        )
    ).scalar_one_or_none()


# ----------------------------------------------------------------- helpers
def humanize(col: str) -> str:
    if col.isupper() and len(col) <= 5:
        return col
    return " ".join(w.capitalize() for w in re.split(r"[\s_]+", col) if w)


def _numeric_cols(schema: list[dict]) -> list[str]:
    return [c["name"] for c in schema if c.get("dtype") in ("integer", "number")]


def _text_cols(schema: list[dict]) -> list[str]:
    return [c["name"] for c in schema if c.get("dtype") == "text"]


def _load_df(ds: Dataset) -> pd.DataFrame:
    if not ds.row_count:
        return pd.DataFrame()
    return ds_svc.read_parquet_df(ds_svc.parquet_path(ds.id))


# ----------------------------------------------------------------- generation
def generate_config(datasets: list[tuple[Dataset, pd.DataFrame]]) -> dict:
    """Inspect the datasets and lay out a sensible default board.

    Layout recipe (top to bottom):
      * one KPI row - count stat per dataset + avg of the first numeric
        column per dataset (max 6 stat cards total),
      * one breakdown chart per dataset on its lowest-cardinality text
        column (2..12 unique values), bar + count,
      * one table for the FIRST dataset only (top 8 rows) - boards are
        about shape, not row dumps.
    Text components are the builder author's job, not the generator's.
    """
    components: list[dict] = []

    for ds, df in datasets[:6]:
        components.append(
            {
                "id": f"stat_{ds.id[:6]}_count",
                "type": "stat",
                "dataset_id": ds.id,
                "label": f"{ds.name} - records",
                "agg": "count",
            }
        )
        schema = ds.schema_json or []
        if schema:
            numeric = _numeric_cols(schema)
            if numeric:
                col = numeric[0]
                components.append(
                    {
                        "id": f"stat_{ds.id[:6]}_{col}",
                        "type": "stat",
                        "dataset_id": ds.id,
                        "label": f"{ds.name} - avg {humanize(col)}",
                        "agg": "avg",
                        "column": col,
                    }
                )

    for ds, df in datasets[:4]:
        schema = ds.schema_json or []
        candidates: list[tuple[int, str]] = []
        for col in _text_cols(schema):
            uniq = int(df[col].nunique(dropna=True)) if col in df.columns else 0
            if 2 <= uniq <= 12:
                candidates.append((uniq, col))
        if candidates:
            col = min(candidates)[1]
            components.append(
                {
                    "id": f"chart_{ds.id[:6]}_{col}",
                    "type": "chart",
                    "dataset_id": ds.id,
                    "title": f"{ds.name} by {humanize(col)}",
                    "chart_type": "bar",
                    "group_by": col,
                    "agg": "count",
                }
            )

    if datasets:
        first_ds, _ = datasets[0]
        schema = first_ds.schema_json or []
        if schema:
            components.append(
                {
                    "id": "table_main",
                    "type": "table",
                    "dataset_id": first_ds.id,
                    "title": f"{first_ds.name} - latest",
                    "columns": [c["name"] for c in schema][:8],
                    "limit": 8,
                }
            )
    return {"components": components[:MAX_COMPONENTS]}


# ----------------------------------------------------------------- validation
def validate_config(config: dict, datasets: dict[str, list[dict]]) -> None:
    """Raise ValueError with an end-user message on any bad component.

    ``datasets`` maps dataset_id → schema (list of {name, dtype}); the API
    layer builds it from the rows the components actually reference.
    """
    if not isinstance(config, dict) or not isinstance(config.get("components"), list):
        raise ValueError("config.components must be a list")
    comps = config["components"]
    if len(comps) > MAX_COMPONENTS:
        raise ValueError(f"too many components (max {MAX_COMPONENTS})")
    if not comps:
        raise ValueError("a dashboard needs at least one component")
    ids: set[str] = set()
    for i, comp in enumerate(comps):
        ctx = f"component[{i}]"
        if not isinstance(comp, dict):
            raise ValueError(f"{ctx} must be an object")
        ctype = comp.get("type")
        if ctype not in COMPONENT_TYPES:
            raise ValueError(
                f"{ctx}: unknown type {ctype!r} (stat|chart|table|text)"
            )
        cid = str(comp.get("id") or f"{ctype}_{i}").strip()
        if not cid:
            raise ValueError(f"{ctx}: id must not be empty")
        if cid in ids:
            raise ValueError(f"{ctx}: duplicate component id {cid!r}")
        ids.add(cid)

        if ctype == "text":
            if not str(comp.get("title", "")).strip() and not str(comp.get("body", "")).strip():
                raise ValueError(f"{ctx} ({cid}): text needs a title or a body")
            continue

        ds_id = comp.get("dataset_id")
        if not ds_id:
            raise ValueError(f"{ctx} ({cid}): dataset_id is required")
        schema = datasets.get(ds_id)
        if schema is None:
            raise ValueError(f"{ctx} ({cid}): dataset {ds_id!r} not found")
        names = {c["name"] for c in schema}

        if ctype == "stat":
            agg = comp.get("agg", "count")
            if agg not in AGGS:
                raise ValueError(f"{ctx} ({cid}): agg must be one of {sorted(AGGS)}")
            if agg != "count":
                col = comp.get("column")
                if not col:
                    raise ValueError(f"{ctx} ({cid}): agg={agg} requires a column")
                if col not in names:
                    raise ValueError(f"{ctx} ({cid}): column {col!r} not in dataset schema")
            if not str(comp.get("label", "")).strip():
                raise ValueError(f"{ctx} ({cid}): stat needs a label")
        elif ctype == "chart":
            chart_type = comp.get("chart_type", "bar")
            if chart_type not in CHART_TYPES:
                raise ValueError(f"{ctx} ({cid}): chart_type must be one of {sorted(CHART_TYPES)}")
            group_by = comp.get("group_by")
            if not group_by:
                raise ValueError(f"{ctx} ({cid}): group_by is required")
            if group_by not in names:
                raise ValueError(f"{ctx} ({cid}): group_by {group_by!r} not in dataset schema")
            agg = comp.get("agg", "count")
            if agg not in AGGS:
                raise ValueError(f"{ctx} ({cid}): agg must be one of {sorted(AGGS)}")
            if agg != "count":
                col = comp.get("column")
                if not col or col not in names:
                    raise ValueError(f"{ctx} ({cid}): agg={agg} requires a valid column")
            if not str(comp.get("title", "")).strip():
                raise ValueError(f"{ctx} ({cid}): chart needs a title")
        elif ctype == "table":
            cols = comp.get("columns", [])
            if not isinstance(cols, list) or not cols:
                raise ValueError(f"{ctx} ({cid}): table needs a non-empty columns list")
            unknown = [c for c in cols if c not in names]
            if unknown:
                raise ValueError(f"{ctx} ({cid}): columns not in dataset schema: {unknown}")
            limit = comp.get("limit", DEFAULT_TABLE_LIMIT)
            if not isinstance(limit, int) or not 1 <= limit <= 100:
                raise ValueError(f"{ctx} ({cid}): limit must be 1..100")


# ----------------------------------------------------------------- computation
def _stat_value(df: pd.DataFrame, agg: str, column: str | None, present: bool = True):
    if not present:  # dataset gone - no data is not the same as zero
        return None
    if agg == "count" or not len(df):
        return int(len(df)) if agg == "count" else None
    if column not in df.columns:
        return None
    nums = pd.to_numeric(df[column], errors="coerce").dropna()
    if not len(nums):
        return None
    val = {"sum": nums.sum(), "avg": nums.mean(), "min": nums.min(), "max": nums.max()}[agg]
    return round(float(val), 4)


def _chart_data(comp: dict, df: pd.DataFrame) -> dict:
    """group_by aggregation → {labels, values}; line sorts ascending."""
    empty = {"labels": [], "values": []}
    group_by = comp.get("group_by")
    if group_by not in df.columns or not len(df):
        return empty
    series = df[group_by].fillna("(blank)")
    agg = comp.get("agg", "count")
    if agg == "count":
        counts = series.value_counts()
        cap = PIE_SLICES if comp.get("chart_type") == "pie" else 12
        labels = [str(v) for v in counts.index[:cap]]
        values = [int(c) for c in counts.values[:cap]]
    else:
        col = comp.get("column")
        if col not in df.columns:
            return empty
        # pandas has no "avg" alias - translate ("avg" is our API vocabulary)
        grouped = (
            pd.to_numeric(df[col], errors="coerce")
            .groupby(series)
            .agg("mean" if agg == "avg" else agg)
            .dropna()
        )
        cap = PIE_SLICES if comp.get("chart_type") == "pie" else 12
        labels = [str(v) for v in grouped.index[:cap]]
        values = [round(float(v), 4) for v in grouped.values[:cap]]
    if comp.get("chart_type") == "line":
        pairs = sorted(zip(labels, values), key=lambda p: p[0])
        labels = [p[0] for p in pairs]
        values = [p[1] for p in pairs]
    return {"labels": labels, "values": values}


def compute_config(
    components: list[dict], loaders: dict[str, pd.DataFrame]
) -> list[dict]:
    """Render EVERY component → the board payload (preview + runtime share this).

    ``loaders`` maps dataset_id → DataFrame (preloaded by the API layer).
    Broken references degrade to empty content, never 500s - a board must
    stay renderable if a component points at a dataset deleted later.
    """
    out: list[dict] = []
    for comp in components:
        ctype = comp.get("type")
        cid = comp.get("id", "")
        if ctype == "text":
            out.append({"id": cid, "type": "text", "title": comp.get("title", ""), "body": comp.get("body", "")})
            continue
        present = comp.get("dataset_id") in loaders
        df = loaders.get(comp.get("dataset_id"), pd.DataFrame())
        if ctype == "stat":
            out.append(
                {
                    "id": cid,
                    "type": "stat",
                    "label": comp.get("label", ""),
                    "value": _stat_value(df, comp.get("agg", "count"), comp.get("column"), present),
                }
            )
        elif ctype == "chart":
            data = _chart_data(comp, df) if present else {"labels": [], "values": []}
            out.append(
                {
                    "id": cid,
                    "type": "chart",
                    "title": comp.get("title", ""),
                    "chart_type": comp.get("chart_type", "bar"),
                    "labels": data["labels"],
                    "values": data["values"],
                }
            )
        elif ctype == "table":
            if not present or not len(df):
                cols = [] if not present else [c for c in comp.get("columns", [])]
                out.append(
                    {
                        "id": cid,
                        "type": "table",
                        "title": comp.get("title", ""),
                        "columns": cols if present else [],
                        "rows": [],
                        "row_count": 0,
                    }
                )
                continue
            cols = [c for c in comp.get("columns", []) if c in df.columns]
            limit = comp.get("limit", DEFAULT_TABLE_LIMIT)
            page = df.iloc[:limit]
            rows = ds_svc.jsonable_rows(page[cols]) if cols and len(page) else []
            out.append(
                {
                    "id": cid,
                    "type": "table",
                    "title": comp.get("title", ""),
                    "columns": cols,
                    "rows": rows,
                    "row_count": int(len(df)),
                }
            )
    return out
