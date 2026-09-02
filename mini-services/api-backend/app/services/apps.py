"""App builder core (v29) - Excel → App flagship.

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
parquet via the datasets service (v27) - one storage engine, no drift.

Record addressing: rows are index-addressable (parquet order). Mutations
rewrite the parquet atomically; deleting the LAST row preserves the schema
(the empty-with-columns frame is still writable, unlike the fileless
0-column case the v27 tests caught).

v30 - forms get field options and records get business rules:

* form fields may be plain strings (shorthand) or objects -
  ``{"name": "plan", "label": "Plan", "required": true, "default": "starter",
  "options": ["starter", "pro"], "placeholder": "choose"}`` - both validate;
  ``required`` / ``options`` are enforced server-side on create (and on
  update for touched fields), ``default`` fills empty/absent fields on create.
* ``config["rules"]`` runs through :mod:`.rules` on every record create/update:
  block rejects with 400, warn surfaces messages in the response, set
  computes/overrides a field (constant or safe arithmetic formula).
"""

from __future__ import annotations

import json
import re

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import App, Dataset
from . import datasets as ds_svc
from . import rules as rule_svc

COMPONENT_TYPES = {"stat", "table", "form", "chart", "kpi", "markdown", "filter"}  # v46: +kpi/markdown/filter
AGGS = {"count", "sum", "avg", "min", "max", "count_distinct", "median"}  # v46: +count_distinct/median
CHART_TYPES = {"bar", "pie", "line", "area", "donut", "scatter"}  # v46: +line/area/donut/scatter
MAX_COMPONENTS = 24
MAX_FILTER_OPTIONS = 200  # distinct options kept for filter components


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

    # stats - row count + up to two numeric means
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

    # chart - text column with the LOWEST chartable cardinality (2..12):
    # "plan" (3 values) beats "name" (8) - the breakdown is the point.
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

    # table - every column (cap 8), pagination built in
    components.append(
        {
            "id": "table_main",
            "type": "table",
            "title": "All records",
            "columns": [c["name"] for c in schema][:8],
            "page_size": 10,
        }
    )

    # form - first 6 schema columns become create/edit fields
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
            validate_fields(fields, names, ctx, cid)
        elif ctype == "kpi":  # v46: a stat with presence
            agg = comp.get("agg", "count")
            if agg not in AGGS:
                raise ValueError(f"{ctx} ({cid}): agg must be one of {sorted(AGGS)}")
            if agg != "count":
                col = comp.get("column")
                if not col:
                    raise ValueError(f"{ctx} ({cid}): agg={agg} requires a column")
                if col not in names:
                    raise ValueError(f"{ctx} ({cid}): column {col!r} not in dataset schema")
        elif ctype == "markdown":  # v46
            body = comp.get("body", "")
            if not isinstance(body, str) or not body.strip():
                raise ValueError(f"{ctx} ({cid}): markdown needs a body")
            if len(body) > 5000:
                raise ValueError(f"{ctx} ({cid}): markdown body too long (max 5000 chars)")
        elif ctype == "filter":  # v46: dropdown that filters the whole app
            col = comp.get("column")
            if not col or col not in names:
                raise ValueError(f"{ctx} ({cid}): filter needs a valid column")
            if "multiple" in comp and not isinstance(comp["multiple"], bool):
                raise ValueError(f"{ctx} ({cid}): multiple must be a boolean")
        elif ctype == "chart":
            ctype_chart = comp.get("chart_type", "bar")
            if ctype_chart not in CHART_TYPES:
                raise ValueError(f"{ctx} ({cid}): chart_type must be one of {sorted(CHART_TYPES)}")
            if ctype_chart == "scatter":  # v46: x/y scatter instead of group_by
                x_col = comp.get("x")
                y_col = comp.get("y")
                if not x_col or x_col not in names:
                    raise ValueError(f"{ctx} ({cid}): scatter needs a valid x column")
                if not y_col or y_col not in names:
                    raise ValueError(f"{ctx} ({cid}): scatter needs a valid y column")
                dtypes = {c["name"]: c.get("dtype") for c in schema}
                if dtypes.get(y_col) not in ("integer", "number"):
                    raise ValueError(f"{ctx} ({cid}): scatter y column must be numeric")
            else:
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

    # v30 - rules ride in the same config, validated against the schema
    rule_svc.validate_rules(config.get("rules"), schema)


def validate_fields(fields: list, names: set[str], ctx: str, cid: str) -> None:
    """Form fields: strings (shorthand) or option objects - v30."""
    seen: set[str] = set()
    for j, f in enumerate(fields):
        fctx = f"{ctx} ({cid}) field[{j}]"
        if isinstance(f, str):
            if f not in names:
                raise ValueError(f"{fctx}: field {f!r} not in dataset schema")
            seen.add(f)
            continue
        if not isinstance(f, dict):
            raise ValueError(f"{fctx} must be a column name or an object with a name")
        name = f.get("name")
        if not name or name not in names:
            raise ValueError(f"{fctx}: name {name!r} not in dataset schema")
        if name in seen:
            raise ValueError(f"{ctx} ({cid}): duplicate field {name!r}")
        seen.add(name)
        for key in ("label", "placeholder", "default"):
            if key in f and f[key] is not None and not isinstance(f[key], (str, int, float, bool)):
                raise ValueError(f"{fctx}: {key} must be a scalar")
        if "required" in f and not isinstance(f["required"], bool):
            raise ValueError(f"{fctx}: required must be a boolean")
        if "options" in f and f["options"] is not None:
            opts = f["options"]
            if not isinstance(opts, list) or not opts or not all(isinstance(o, (str, int, float, bool)) for o in opts):
                raise ValueError(f"{fctx}: options must be a non-empty list of scalars")


def normalize_field(f: object) -> dict:
    """String | object field → canonical options dict (UI-facing)."""
    if isinstance(f, str):
        return {"name": f, "label": humanize(f), "required": False, "options": None, "default": None, "placeholder": None}
    f = dict(f)
    name = f.get("name", "")
    f.setdefault("label", humanize(name))
    f.setdefault("required", False)
    f.setdefault("options", None)
    f.setdefault("default", None)
    f.setdefault("placeholder", None)
    return f


def form_fields(form_comp: dict | None) -> list[dict]:
    if not form_comp:
        return []
    return [normalize_field(f) for f in form_comp.get("fields", [])]


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
                .agg("mean" if comp.get("agg", "avg") == "avg" else comp.get("agg", "avg"))
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


# ----------------------------------------------------------------- v46 deep compute
def markdown_to_safe_html(text: str) -> str:
    """Tiny markdown-lite renderer with NO dependency and NO XSS surface.

    The body is HTML-escaped FIRST, then a conservative subset of markdown
    is layered on top: **bold**, *italic*, `code`, [text](http(s) links)
    and line breaks. Everything else passes through as (escaped) text.
    """
    import html as _html

    out = _html.escape(text or "", quote=True)
    out = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", out)
    out = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)
    out = re.sub(
        r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)",
        r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>',
        out,
    )
    out = out.replace("\r\n", "\n").replace("\n", "<br>")
    return out


def _agg_series(df: pd.DataFrame, agg: str, column: str | None):
    """Aggregation over a frame → scalar (v46 aggs)."""
    if agg == "count":
        return int(len(df))
    if column not in df.columns or not len(df):
        return None
    if agg == "count_distinct":
        return int(df[column].nunique(dropna=True))
    if agg == "median":
        nums = pd.to_numeric(df[column], errors="coerce").dropna()
        return round(float(nums.median()), 4) if len(nums) else None
    nums = pd.to_numeric(df[column], errors="coerce").dropna()
    if not len(nums):
        return None
    val = {"sum": nums.sum(), "avg": nums.mean(), "min": nums.min(), "max": nums.max()}[agg]
    return round(float(val), 4)


def apply_filters(df: pd.DataFrame, filters: dict | None) -> pd.DataFrame:
    """Filter-component selections → filtered frame (string-loose matching)."""
    if not filters:
        return df
    out = df
    for col, raw in (filters or {}).items():
        if col not in out.columns:
            continue
        values = raw if isinstance(raw, list) else [raw]
        values = [str(v).strip().lower() for v in values if str(v).strip() != ""]
        if not values:
            continue
        out = out[out[col].astype(str).str.strip().str.lower().isin(values)]
    return out


def compute_components(components: list[dict], df: pd.DataFrame, filters: dict | None = None) -> list[dict]:
    """Render EVERY component server-side (v46) - one source of truth for the
    builder preview and the runtime, ending the client-side drift.

    Chart bucket caps: 8 slices for pie/donut, 12 bars/lines (v29 parity).
    Filter options are distinct values capped at MAX_FILTER_OPTIONS.
    """
    fdf = apply_filters(df, filters)
    rendered: list[dict] = []
    for comp in components:
        ctype = comp.get("type")
        cid = comp.get("id", "")
        if ctype in ("stat", "kpi"):
            value = _agg_series(fdf, comp.get("agg", "count"), comp.get("column"))
            rendered.append({
                "id": cid, "type": ctype, "label": comp.get("label", ""),
                "agg": comp.get("agg", "count"), "column": comp.get("column"),
                "value": value,
            })
        elif ctype == "chart":
            chart_type = comp.get("chart_type", "bar")
            base = {"id": cid, "type": "chart", "title": comp.get("title", ""), "chart_type": chart_type}
            if chart_type == "scatter":
                x_col, y_col = comp.get("x"), comp.get("y")
                if not len(fdf) or x_col not in fdf.columns or y_col not in fdf.columns:
                    rendered.append({**base, "points": []})
                    continue
                pts = fdf[[x_col, y_col]].copy()
                pts[y_col] = pd.to_numeric(pts[y_col], errors="coerce")
                pts = pts.dropna().head(500)
                points = [
                    {"x": json.loads(json.dumps(row[x_col], default=str)), "y": round(float(row[y_col]), 4)}
                    for _, row in pts.iterrows()
                ]
                rendered.append({**base, "x": x_col, "y": y_col, "points": points})
                continue
            group_by = comp.get("group_by")
            if group_by not in fdf.columns or not len(fdf):
                rendered.append({**base, "labels": [], "values": []})
                continue
            series = fdf[group_by].fillna("(blank)")
            agg = comp.get("agg", "count")
            cap = 8 if chart_type in ("pie", "donut") else 12
            if agg == "count":
                counts = series.value_counts()
                labels = [str(v) for v in counts.index[:cap]]
                values = [int(c) for c in counts.values[:cap]]
            else:
                col = comp.get("column")
                if col not in fdf.columns:
                    rendered.append({**base, "labels": [], "values": []})
                    continue
                nums = pd.to_numeric(fdf[col], errors="coerce")
                if agg == "median":
                    grouped = nums.groupby(series).median().dropna()
                elif agg == "count_distinct":
                    grouped = fdf[col].groupby(series).nunique().dropna()
                else:
                    grouped = nums.groupby(series).agg("mean" if agg == "avg" else agg).dropna()
                labels = [str(v) for v in grouped.index[:cap]]
                values = [round(float(v), 4) for v in grouped.values[:cap]]
            if chart_type in ("line", "area"):
                order = sorted(range(len(labels)), key=lambda i: labels[i])
                labels = [labels[i] for i in order]
                values = [values[i] for i in order]
            rendered.append({**base, "labels": labels, "values": values})
        elif ctype == "table":
            cols = [c for c in comp.get("columns", []) if c in fdf.columns]
            page_size = int(comp.get("page_size", 10))
            rows = ds_svc.jsonable_rows(fdf[cols].head(page_size)) if cols else []
            rendered.append({
                "id": cid, "type": "table", "title": comp.get("title", ""),
                "columns": cols, "rows": rows,
                "row_count": len(rows), "total": int(len(fdf)),
            })
        elif ctype == "markdown":
            rendered.append({
                "id": cid, "type": "markdown", "title": comp.get("title", ""),
                "html": markdown_to_safe_html(comp.get("body", "")),
            })
        elif ctype == "filter":
            col = comp.get("column")
            options: list[str] = []
            if col in df.columns:
                options = [str(v) for v in df[col].dropna().unique()[:MAX_FILTER_OPTIONS]]
            rendered.append({
                "id": cid, "type": "filter", "column": col,
                "label": comp.get("label") or humanize(col or ""),
                "multiple": bool(comp.get("multiple", False)),
                "options": sorted(options),
            })
        elif ctype == "form":
            rendered.append({
                "id": cid, "type": "form", "title": comp.get("title", "Submit"),
                "submit_label": comp.get("submit_label", "Submit"),
                "fields": form_fields(comp),
            })
    return rendered


def search_sort_df(df: pd.DataFrame, q: str | None, sort_by: str | None, sort_dir: str = "asc") -> pd.DataFrame:
    """Runtime records search (all columns, case-insensitive substring) + sort."""
    out = df
    if q and q.strip():
        needle = q.strip().lower()
        mask = pd.Series(False, index=out.index)
        for col in out.columns:
            mask |= out[col].astype(str).str.lower().str.contains(needle, regex=False)
        out = out[mask]
    if sort_by and sort_by in out.columns and len(out):
        ascending = sort_dir != "desc"
        col = out[sort_by]
        if col.dtype == object:
            out = out.assign(__k=out[sort_by].astype(str)).sort_values("__k", ascending=ascending, key=lambda s: s.str.lower()).drop(columns="__k")
        else:
            out = out.sort_values(sort_by, ascending=ascending)
    return out


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


def apply_form_options(record: dict, fields: list[dict], event: str, touched: set[str] | None = None) -> dict:
    """Defaults (create) + required/options enforcement (v30).

    * create: empty/absent fields with a ``default`` get it, then EVERY form
      field marked required must be non-empty (absent counts as empty), and
      submitted values must honour ``options`` when configured.
    * update: only TOUCHED fields (patch keys) are validated - legacy rows
      with gaps must not block unrelated edits; a touched required field may
      not land empty and its new value must honour ``options``.
    """
    out = dict(record)

    def check(f: dict) -> None:
        name = f["name"]
        val = out.get(name)
        if f.get("required") and _is_empty_val(val):
            raise ValueError(f"field '{name}' is required")
        opts = f.get("options")
        if opts and not _is_empty_val(val) and not any(_loose(val, o) for o in opts):
            raise ValueError(f"field '{name}' must be one of: {', '.join(str(o) for o in opts)}")

    if event == "create":
        for f in fields:
            if f.get("default") is not None and _is_empty_val(out.get(f["name"])):
                out[f["name"]] = f["default"]
        for f in fields:
            check(f)
        return out
    for f in fields:
        if f["name"] in (touched or set()):
            check(f)
    return out


def _is_empty_val(v: object) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def _loose(a: object, b: object) -> bool:
    na, nb = rule_svc._num(a), rule_svc._num(b)
    if na is not None and nb is not None:
        return na == nb
    return str(a).strip().lower() == str(b).strip().lower()


async def append_record(
    ds: Dataset,
    record: dict,
    schema: list[dict],
    form: dict | None = None,
    rules: list[dict] | None = None,
    db: AsyncSession | None = None,
) -> dict:
    """Create one record through an app - schema keys, form options, rules.

    Order: unknown-field guard → coercion → form defaults/required/options →
    business rules (block raises, set mutates, warn collects) → parquet.
    Returns ``{"record": ..., "warnings": [...]}`"""
    names = {c["name"] for c in schema}
    unknown = [k for k in record if k not in names and names]
    if unknown:
        raise ValueError(f"unknown fields: {unknown}")
    rec = _coerce_values(record, schema)
    rec = apply_form_options(rec, form_fields(form), "create")
    rec, warnings = rule_svc.apply_rules(rules, rec, "create", schema)
    session = db
    if session is None:  # legacy path without a session - no version snapshot
        from ..db import AsyncSessionLocal

        session = AsyncSessionLocal()
    try:
        await ds_svc.append_rows(session, ds, [_coerce_values(rec, schema)])
        if db is None:
            await session.commit()
    finally:
        if db is None:
            await session.close()
    return {"record": rec, "warnings": warnings}


async def update_record(
    ds: Dataset,
    index: int,
    patch: dict,
    form: dict | None = None,
    rules: list[dict] | None = None,
) -> dict:
    """Partially update row ``index``; rewrites the parquet atomically.

    Rules evaluate against the MERGED row (existing + patch) so ``set``
    formulas see the final state. Returns ``{"record": ..., "warnings": [...]}``.
    """
    df = _load_df(ds)
    if index < 0 or index >= len(df):
        raise IndexError(f"record {index} out of range (0..{len(df) - 1})")
    unknown = [k for k in patch if k not in df.columns]
    if unknown:
        raise ValueError(f"unknown fields: {unknown}")
    schema_now = ds_svc.schema_of(df)
    patch = _coerce_values(patch, schema_now)
    existing = ds_svc.jsonable_rows(df.iloc[[index]])[0]
    merged = {**existing, **patch}
    merged = apply_form_options(merged, form_fields(form), "update", touched=set(patch.keys()))
    merged, warnings = rule_svc.apply_rules(rules, merged, "update", schema_now)
    changed = [k for k in merged if not rule_svc._loose_eq(merged[k], existing.get(k))]
    for k in changed:
        if k in df.columns:
            df.at[index, k] = merged[k]
    _save_df(ds, df)
    return {"record": ds_svc.jsonable_rows(df.iloc[[index]])[0], "warnings": warnings}


async def delete_record(ds: Dataset, index: int) -> int:
    """Delete row ``index``; the last row keeps the schema alive."""
    df = _load_df(ds)
    if index < 0 or index >= len(df):
        raise IndexError(f"record {index} out of range (0..{len(df) - 1})")
    df = df.drop(index=df.index[index]).reset_index(drop=True)
    _save_df(ds, df)
    return int(len(df))
