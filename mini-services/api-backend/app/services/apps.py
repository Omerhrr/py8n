"""App builder core (v29) — Excel → App flagship.

An App binds ONE dataset and a component config:

    {"components": [
        {"id": "stat_total", "type": "stat",  "label": "Total records", "agg": "count"},
        {"id": "stat_ltv",   "type": "stat",  "label": "Avg LTV", "agg": "avg", "column": "ltv"},
        {"id": "table_1",    "type": "table", "title": "All records", "columns": [...], "page_size": 10},
        {"id": "form_1",     "type": "form",  "title": "Add record", "fields": [...], "submit_label": "Create"},
        {"id": "chart_1",    "type": "chart", "title": "By plan", "chart_type": "bar", "group_by": "plan", "agg": "count"},
    ]}

One-click generation inspects the bound dataset's schema + values and lays
out a sensible CRM-style app: a count stat + numeric means, a breakdown
chart on the first low-cardinality text column, a full table and a create
form. Records written through a published app land in the dataset's
parquet via the datasets service (v27) — one storage engine, no drift.

Record addressing: rows are index-addressable (parquet order). Mutations
rewrite the parquet atomically; deleting the LAST row preserves the schema
(the empty-with-columns frame is still writable, unlike the fileless
0-column case the v27 tests caught).
"""

from __future__ import annotations

import re

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import App, Dataset
from . import datasets as ds_svc

COMPONENT_TYPES = {"stat", "table", "form", "chart"}
AGGS = {"count", "sum", "avg", "min", "max"}
CHART_TYPES = {"bar", "pie"}
MAX_COMPONENTS = 24


# ----------------------------------------------------------------- slugs
def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:100]
    return s or "app"


async def unique_slug(db: AsyncSession, name: str, exclude_id: str | None = None) -> str:
    base = slugify(name)
    slug, n = base, 2
    q = select(App).where(App.slug == slug)
    if exclude_id:
        q = q.where(App.id != exclude_id)
    while (await db.execute(q)).scalar_one_or_none() is not None:
        slug = f"{base}-{n}"
        n += 1
        q = select(App).where(App.slug == slug)
        if exclude_id:
            q = q.where(App.id != exclude_id)
    return slug


async def name_taken(db: AsyncSession, name: str, exclude_id: str | None = None) -> bool:
    q = select(App).where(func.upper(App.name) == name.strip().upper())
    if exclude_id:
        q = q.where(App.id != exclude_id)
    return (await db.execute(q)).scalar_one_or_none() is not None


async def get_app(db: AsyncSession, ref: str) -> App | None:
    """Resolve an app by id, then by case-insensitive name."""
    row = await db.get(App, ref)
    if row is not None:
        return row
    return (
        await db.execute(select(App).where(func.upper(App.name) == ref.strip().upper()))
    ).scalar_one_or_none()


async def get_by_slug(db: AsyncSession, slug: str) -> App | None:
    return (
        await db.execute(select(App).where(App.slug == slug.strip().lower()))
    ).scalar_one_or_none()


# ----------------------------------------------------------------- helpers
def humanize(col: str) -> str:
    """'ltv' → 'LTV', 'first_name' → 'First Name'."""
    if col.isupper() and len(col) <= 5:
        return col
    return " ".join(w.capitalize() for w in re.split(r"[\s_]+", col) if w)


def _numeric_cols(schema: list[dict]) -> list[str]:
    return [c["name"] for c in schema if c.get("dtype") in ("integer", "number")]


def _text_cols(schema: list[dict]) -> list[str]:
    return [c["name"] for c in schema if c.get("dtype") == "text"]


# ----------------------------------------------------------------- generation
def generate_config(df: pd.DataFrame, schema: list[dict]) -> dict:
    """Inspect the dataset and lay out a sensible default app."""
    components: list[dict] = []

    # stats — row count + up to two numeric means
    components.append(
        {"id": "stat_total", "type": "stat", "label": "Total records", "agg": "count"}
    )
    for col in _numeric_cols(schema)[:2]:
        components.append(
            {
                "id": f"stat_{col}",
                "type": "stat",
                "label": f"Avg {humanize(col)}",
                "agg": "avg",
                "column": col,
            }
        )

    # chart — text column with the LOWEST chartable cardinality (2..12):
    # "plan" (3 values) beats "name" (8) — the breakdown is the point.
    candidates: list[tuple[int, str]] = []
    for col in _text_cols(schema):
        uniq = int(df[col].nunique(dropna=True)) if col in df.columns else 0
        if 2 <= uniq <= 12:
            candidates.append((uniq, col))
    if candidates:
        col = min(candidates)[1]
        components.append(
            {
                "id": "chart_breakdown",
                "type": "chart",
                "title": f"Records by {humanize(col)}",
                "chart_type": "bar",
                "group_by": col,
                "agg": "count",
            }
        )

    # table — every column (cap 8), pagination built in
    components.append(
        {
            "id": "table_main",
            "type": "table",
            "title": "All records",
            "columns": [c["name"] for c in schema][:8],
            "page_size": 10,
        }
    )

    # form — first 6 schema columns become create/edit fields
    components.append(
        {
            "id": "form_main",
            "type": "form",
            "title": "Add record",
            "fields": [c["name"] for c in schema][:6],
            "submit_label": "Create",
        }
    )
    return {"components": components}


def validate_config(config: dict, schema: list[dict]) -> None:
    """Raise ValueError with an end-user message on any bad component."""
    if not isinstance(config, dict) or not isinstance(config.get("components"), list):
        raise ValueError("config.components must be a list")
    comps = config["components"]
    if len(comps) > MAX_COMPONENTS:
        raise ValueError(f"too many components (max {MAX_COMPONENTS})")
    names = {c["name"] for c in schema}
    ids: set[str] = set()
    for i, comp in enumerate(comps):
        ctx = f"component[{i}]"
        if not isinstance(comp, dict):
            raise ValueError(f"{ctx} must be an object")
        ctype = comp.get("type")
        if ctype not in COMPONENT_TYPES:
            raise ValueError(f"{ctx}: unknown type {ctype!r} (stat|table|form|chart)")
        cid = str(comp.get("id") or f"{ctype}_{i}").strip()
        if not cid:
            raise ValueError(f"{ctx}: id must not be empty")
        if cid in ids:
            raise ValueError(f"{ctx}: duplicate component id {cid!r}")
        ids.add(cid)
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
        elif ctype == "table":
            cols = comp.get("columns", [])
            if not isinstance(cols, list):
                raise ValueError(f"{ctx} ({cid}): columns must be a list")
            unknown = [c for c in cols if c not in names]
            if unknown:
                raise ValueError(f"{ctx} ({cid}): columns not in dataset schema: {unknown}")
            size = comp.get("page_size", 10)
            if not isinstance(size, int) or not 1 <= size <= 100:
                raise ValueError(f"{ctx} ({cid}): page_size must be 1..100")
        elif ctype == "form":
            fields = comp.get("fields", [])
            if not fields or not isinstance(fields, list):
                raise ValueError(f"{ctx} ({cid}): form needs at least one field")
            unknown = [f for f in fields if f not in names]
            if unknown:
                raise ValueError(f"{ctx} ({cid}): fields not in dataset schema: {unknown}")
        elif ctype == "chart":
            ctype_chart = comp.get("chart_type", "bar")
            if ctype_chart not in CHART_TYPES:
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


# ----------------------------------------------------------------- aggregates
def compute_stats(components: list[dict], df: pd.DataFrame) -> dict[str, object]:
    """Component id → rendered value, for every stat component."""
    out: dict[str, object] = {}
    for comp in components:
        if comp.get("type") != "stat":
            continue
        agg = comp.get("agg", "count")
        if agg == "count" or not len(df):
            out[comp["id"]] = int(len(df)) if agg == "count" else None
            continue
        col = comp.get("column")
        if col not in df.columns:
            out[comp["id"]] = None
            continue
        nums = pd.to_numeric(df[col], errors="coerce").dropna()
        if not len(nums):
            out[comp["id"]] = None
            continue
        val = {"sum": nums.sum(), "avg": nums.mean(), "min": nums.min(), "max": nums.max()}[agg]
        out[comp["id"]] = round(float(val), 4)
    return out


def compute_chart(components: list[dict], df: pd.DataFrame) -> dict | None:
    """First chart component → {labels, values, title, chart_type}."""
    for comp in components:
        if comp.get("type") != "chart":
            continue
        group_by = comp.get("group_by")
        if group_by not in df.columns or not len(df):
            return {"labels": [], "values": [], "title": comp.get("title", ""), "chart_type": comp.get("chart_type", "bar")}
        series = df[group_by].fillna("(blank)")
        if comp.get("agg", "count") == "count":
            counts = series.value_counts()
            labels = [str(v) for v in counts.index[:12]]
            values = [int(c) for c in counts.values[:12]]
        else:
            col = comp.get("column")
            if col not in df.columns:
                return {"labels": [], "values": [], "title": comp.get("title", ""), "chart_type": comp.get("chart_type", "bar")}
            grouped = (
                pd.to_numeric(df[col], errors="coerce")
                .groupby(series)
                .agg(comp.get("agg", "avg"))
                .dropna()
            )
            labels = [str(v) for v in grouped.index[:12]]
            values = [round(float(v), 4) for v in grouped.values[:12]]
        return {
            "labels": labels,
            "values": values,
            "title": comp.get("title", ""),
            "chart_type": comp.get("chart_type", "bar"),
        }
    return None


# ----------------------------------------------------------------- records
def _load_df(ds: Dataset) -> pd.DataFrame:
    return ds_svc.read_parquet_df(ds_svc.parquet_path(ds.id))


def _save_df(ds: Dataset, df: pd.DataFrame) -> None:
    """Write parquet + sync metadata (schema may drift on edits)."""
    if len(df.columns):
        ds_svc.write_parquet(df, ds_svc.parquet_path(ds.id))
    ds.schema_json = ds_svc.schema_of(df)
    ds.row_count = int(len(df))


def _coerce_values(record: dict, schema: list[dict]) -> dict:
    """Cast form-submitted strings into the column dtype (API-friendly)."""
    out = dict(record)
    for col in schema:
        key, dtype = col["name"], col.get("dtype")
        if key not in out or not isinstance(out[key], str):
            continue
        raw = out[key].strip()
        if raw == "":
            out[key] = None
            continue
        if dtype in ("integer", "number"):
            try:
                out[key] = float(raw) if dtype == "number" else int(float(raw))
            except ValueError:
                pass  # leave as-is; pandas owns the fallout
        elif dtype == "boolean" and raw.lower() in ("true", "false"):
            out[key] = raw.lower() == "true"
    return out


async def append_record(ds: Dataset, record: dict, schema: list[dict]) -> dict:
    """Create one record through an app — schema keys enforced, values coerced."""
    names = {c["name"] for c in schema}
    unknown = [k for k in record if k not in names and names]
    if unknown:
        raise ValueError(f"unknown fields: {unknown}")
    await ds_svc.append_rows(ds, [_coerce_values(record, schema)])
    return record


async def update_record(ds: Dataset, index: int, patch: dict) -> dict:
    """Partially update row ``index``; rewrites the parquet atomically."""
    df = _load_df(ds)
    if index < 0 or index >= len(df):
        raise IndexError(f"record {index} out of range (0..{len(df) - 1})")
    unknown = [k for k in patch if k not in df.columns]
    if unknown:
        raise ValueError(f"unknown fields: {unknown}")
    patch = _coerce_values(patch, ds_svc.schema_of(df))
    for k, v in patch.items():
        df.at[index, k] = v
    _save_df(ds, df)
    return ds_svc.jsonable_rows(df.iloc[[index]])[0]


async def delete_record(ds: Dataset, index: int) -> int:
    """Delete row ``index``; the last row keeps the schema alive."""
    df = _load_df(ds)
    if index < 0 or index >= len(df):
        raise IndexError(f"record {index} out of range (0..{len(df) - 1})")
    df = df.drop(index=df.index[index]).reset_index(drop=True)
    _save_df(ds, df)
    return int(len(df))
