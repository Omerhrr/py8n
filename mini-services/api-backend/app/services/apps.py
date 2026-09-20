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

COMPONENT_TYPES = {"stat", "table", "form", "chart", "kpi", "markdown", "filter", "button"}  # v46: +kpi/markdown/filter; v137: +button
AGGS = {"count", "sum", "avg", "min", "max", "count_distinct", "median"}  # v46: +count_distinct/median
CHART_TYPES = {"bar", "pie", "line", "area", "donut", "scatter", "funnel", "treemap", "gauge", "heatmap", "radar"}  # v46: +line/area/donut/scatter; v138: +funnel/treemap/gauge/heatmap/radar (ECharts)
GROUPED_CHART_TYPES = {"bar", "pie", "line", "area", "donut", "funnel", "treemap"}  # v138: share the group_by/agg/labels-values pipeline
BUTTON_STYLES = {"primary", "secondary", "danger"}  # v137
BUTTON_ACTIONS = {"link", "navigate", "webhook"}  # v137
BUTTON_METHODS = {"GET", "POST"}  # v137
WIDTHS = {"full", "half", "third", "two_thirds"}  # v139: grid layout slot widths
THEME_RADII = {"sharp", "rounded", "soft"}  # v140
THEME_DENSITIES = {"compact", "comfortable", "spacious"}  # v140
MAX_COMPONENTS = 24
MAX_FILTER_OPTIONS = 200  # distinct options kept for filter components
_HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")  # v136: config.theme.accent

# v134: multi-page apps - an optional `page` string on any component groups
# it under a named section, rendered as sidebar/tab navigation in the
# published runtime. Absent/blank `page` falls back to DEFAULT_PAGE, so every
# app saved before this feature existed renders exactly as before (one page,
# no migration needed).
DEFAULT_PAGE = "Dashboard"


def component_page(comp: dict) -> str:
    return (comp.get("page") or "").strip() or DEFAULT_PAGE


def pages_of(components: list[dict]) -> list[str]:
    """Ordered, deduped page names across a component list - DEFAULT_PAGE
    first (even if empty), then every other page in first-seen order."""
    seen: list[str] = [DEFAULT_PAGE]
    for comp in components:
        p = component_page(comp)
        if p not in seen:
            seen.append(p)
    return seen


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


async def compose_app(
    db: AsyncSession,
    name: str,
    dataset: Dataset,
    description: str = "",
    config: dict | None = None,
    rules: list[dict] | None = None,
    owner_id: str | None = None,
    publish: bool = True,
    extra_datasets: list[dict] | None = None,
) -> App:
    """Build a real App row bound to ``dataset`` - the one non-UI path into
    the Apps builder, shared by the AI System Builder and AI Composer (v147)
    so "build me an app" from either one lands on the EXACT same primitive a
    person gets from the builder UI, not a parallel one-off.

    ``config`` omitted/None auto-generates one from the dataset's own shape
    via :func:`generate_config` - deterministic, so a caller that hands it
    nothing still gets a sane app rather than an empty shell. Name collisions
    get de-duped the same way the API does (``unique_slug`` off App.name, not
    off the caller's exact string), so callers never need to pre-check.

    ``extra_datasets`` (v149: multi-dataset apps) is an optional list of
    ``{"page": "Sales", "dataset": <Dataset>}`` entries - one extra dataset
    per page/module, bound onto the SAME App alongside ``dataset`` (which
    stays the app's primary/legacy dataset). When ``config`` is also None,
    a full multi-page layout is auto-generated via
    :func:`generate_multi_page_config` (one page per dataset, ``dataset``
    first). When ``config`` is supplied explicitly (e.g. a hand-crafted
    AI Composer spec), it is validated with each page's own schema via
    ``extra_schemas`` and stored as-is - callers are expected to have
    stamped ``dataset_id``/``page`` on each component themselves.
    """
    app_name, n = name, 2
    while await name_taken(db, app_name):
        app_name = f"{name} {n}"
        n += 1
    extra_datasets = extra_datasets or []
    extra_schemas = {e["dataset"].id: (e["dataset"].schema_json or []) for e in extra_datasets}
    if extra_datasets:
        extra_schemas[dataset.id] = dataset.schema_json or []  # so components stamped with the primary dataset_id also validate
    if config is None:
        df = ds_svc.read_parquet_df(ds_svc.parquet_path(dataset.id))
        if extra_datasets:
            pages = [{"page": humanize(dataset.name), "dataset": dataset, "df": df}]
            for e in extra_datasets:
                pages.append({
                    "page": e.get("page") or humanize(e["dataset"].name),
                    "dataset": e["dataset"],
                    "df": ds_svc.read_parquet_df(ds_svc.parquet_path(e["dataset"].id)),
                })
            config = generate_multi_page_config(pages)
        else:
            config = generate_config(df, dataset.schema_json or [])
    validate_config(config, dataset.schema_json or [], extra_schemas=extra_schemas or None)
    if rules:
        rule_svc.validate_rules(rules, dataset.schema_json or [])
        config = {**config, "rules": rules}
    if extra_datasets:
        config = {
            **config,
            "datasets": [
                {"dataset_id": dataset.id, "page": humanize(dataset.name)},
                *[
                    {"dataset_id": e["dataset"].id, "page": e.get("page") or humanize(e["dataset"].name)}
                    for e in extra_datasets
                ],
            ],
        }
    row = App(
        name=app_name,
        slug=await unique_slug(db, app_name),
        description=(description or "").strip(),
        dataset_id=dataset.id,
        config=config,
        status="published" if publish else "draft",
    )
    row.owner_id = owner_id
    db.add(row)
    await db.flush()
    return row


def dataset_id_for_page(config: dict | None, page: str | None, default_dataset_id: str | None) -> str | None:
    """Which dataset a given page's records/CRUD should hit (v149).

    ``config["datasets"]`` (written by :func:`compose_app`/the multi-dataset
    editor) is a list of ``{"dataset_id", "page"}`` entries - one per EXTRA
    page. A page not listed there (including every page of a legacy,
    single-dataset app, and the app's own primary page) falls back to
    ``default_dataset_id`` (``App.dataset_id``), so this is a no-op for
    every app that predates the feature.
    """
    if not page or not config:
        return default_dataset_id
    for entry in config.get("datasets") or []:
        if entry.get("page") == page:
            return entry.get("dataset_id") or default_dataset_id
    return default_dataset_id


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


def generate_multi_page_config(pages: list[dict]) -> dict:
    """Multi-dataset sibling of :func:`generate_config` (v149).

    ``pages`` is ``[{"page": "Inventory", "dataset": <Dataset>, "df": <DataFrame>}, ...]``.
    Builds one page's worth of components per entry (same stat/chart/table/
    form layout ``generate_config`` uses for a single dataset), each
    component stamped with ``page`` and ``dataset_id`` so
    :func:`compute_components`/:func:`validate_config` route it to the
    right dataset, and each component id prefixed with a per-page slug so
    ids stay unique across the whole app.
    """
    all_components: list[dict] = []
    for entry in pages:
        page_name = entry["page"]
        ds = entry["dataset"]
        df = entry["df"]
        schema = ds.schema_json or []
        page_slug = re.sub(r"[^a-z0-9]+", "_", page_name.strip().lower()).strip("_") or "page"
        base = generate_config(df, schema)["components"]
        for comp in base:
            comp = dict(comp)
            comp["id"] = f"{page_slug}_{comp['id']}"
            comp["page"] = page_name
            comp["dataset_id"] = ds.id
            all_components.append(comp)
    return {"components": all_components}


def validate_config(
    config: dict,
    schema: list[dict],
    extra_schemas: dict[str, list[dict]] | None = None,
) -> None:
    """Raise ValueError with an end-user message on any bad component.

    ``extra_schemas`` (v149: multi-dataset apps) maps ``dataset_id -> schema``
    for the app's EXTRA (non-primary) datasets, mirroring Dashboard's
    ``validate_config``. A component tagged with ``dataset_id`` is validated
    against ``extra_schemas[dataset_id]`` instead of the primary ``schema``;
    every other component validates against ``schema`` exactly as before.
    """
    if not isinstance(config, dict) or not isinstance(config.get("components"), list):
        raise ValueError("config.components must be a list")
    comps = config["components"]
    if len(comps) > MAX_COMPONENTS:
        raise ValueError(f"too many components (max {MAX_COMPONENTS})")
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
        if "page" in comp and comp["page"] is not None:  # v134: multi-page apps
            if not isinstance(comp["page"], str) or len(comp["page"]) > 60:
                raise ValueError(f"{ctx} ({cid}): page must be a string (max 60 chars)")
        local_schema = schema  # v149: per-component dataset scoping
        if extra_schemas:
            comp_ds_id = comp.get("dataset_id")
            if comp_ds_id is not None:
                if comp_ds_id not in extra_schemas:
                    raise ValueError(f"{ctx} ({cid}): dataset {comp_ds_id!r} not found")
                local_schema = extra_schemas[comp_ds_id]
        names = {c["name"] for c in local_schema}
        dtypes = {c["name"]: c.get("dtype") for c in local_schema}
        if "width" in comp and comp["width"] is not None:  # v139: grid layout - how wide a slot this component fills
            if comp["width"] not in WIDTHS:
                raise ValueError(f"{ctx} ({cid}): width must be one of {sorted(WIDTHS)}")
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
        elif ctype == "button":  # v137: link / in-app navigate / fire a webhook
            label = comp.get("label", "")
            if not isinstance(label, str) or not label.strip():
                raise ValueError(f"{ctx} ({cid}): button needs a label")
            style = comp.get("style", "primary")
            if style not in BUTTON_STYLES:
                raise ValueError(f"{ctx} ({cid}): style must be one of {sorted(BUTTON_STYLES)}")
            action = comp.get("action")
            if action not in BUTTON_ACTIONS:
                raise ValueError(f"{ctx} ({cid}): action must be one of {sorted(BUTTON_ACTIONS)}")
            if action == "link":
                url = comp.get("url", "")
                if not isinstance(url, str) or not re.match(r"^https?://", url or ""):
                    raise ValueError(f"{ctx} ({cid}): link needs an http(s) url")
            elif action == "navigate":
                target = comp.get("target_page")
                if not isinstance(target, str) or not target.strip():
                    raise ValueError(f"{ctx} ({cid}): navigate needs a target_page")
            elif action == "webhook":
                url = comp.get("webhook_url", "")
                if not isinstance(url, str) or not re.match(r"^https?://", url or ""):
                    raise ValueError(f"{ctx} ({cid}): webhook needs an http(s) webhook_url")
                method = comp.get("method", "POST")
                if method not in BUTTON_METHODS:
                    raise ValueError(f"{ctx} ({cid}): method must be one of {sorted(BUTTON_METHODS)}")
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
                if dtypes.get(y_col) not in ("integer", "number"):
                    raise ValueError(f"{ctx} ({cid}): scatter y column must be numeric")
            elif ctype_chart == "gauge":  # v138: single aggregated value, like a stat
                agg = comp.get("agg", "count")
                if agg not in AGGS:
                    raise ValueError(f"{ctx} ({cid}): agg must be one of {sorted(AGGS)}")
                if agg != "count":
                    col = comp.get("column")
                    if not col or col not in names:
                        raise ValueError(f"{ctx} ({cid}): agg={agg} requires a valid column")
                gmax = comp.get("max")
                if gmax is not None and not isinstance(gmax, (int, float)):
                    raise ValueError(f"{ctx} ({cid}): gauge max must be a number")
            elif ctype_chart == "heatmap":  # v138: two dimensions + an aggregated value per cell
                row_col = comp.get("row")
                col_col = comp.get("col")
                if not row_col or row_col not in names:
                    raise ValueError(f"{ctx} ({cid}): heatmap needs a valid row column")
                if not col_col or col_col not in names:
                    raise ValueError(f"{ctx} ({cid}): heatmap needs a valid col column")
                if row_col == col_col:
                    raise ValueError(f"{ctx} ({cid}): heatmap row and col must be different columns")
                agg = comp.get("agg", "count")
                if agg not in AGGS:
                    raise ValueError(f"{ctx} ({cid}): agg must be one of {sorted(AGGS)}")
                if agg != "count":
                    col = comp.get("column")
                    if not col or col not in names:
                        raise ValueError(f"{ctx} ({cid}): agg={agg} requires a valid column")
            elif ctype_chart == "radar":  # v138: one series across several numeric metrics
                metrics = comp.get("metrics")
                if not metrics or not isinstance(metrics, list) or len(metrics) < 3:
                    raise ValueError(f"{ctx} ({cid}): radar needs at least 3 metric columns")
                unknown = [m for m in metrics if m not in names]
                if unknown:
                    raise ValueError(f"{ctx} ({cid}): radar metrics not in dataset schema: {unknown}")
                non_numeric = [m for m in metrics if dtypes.get(m) not in ("integer", "number")]
                if non_numeric:
                    raise ValueError(f"{ctx} ({cid}): radar metrics must be numeric: {non_numeric}")
                agg = comp.get("agg", "avg")
                if agg not in AGGS or agg == "count":
                    raise ValueError(f"{ctx} ({cid}): radar agg must be one of {sorted(AGGS - {'count'})}")
            else:  # bar / pie / line / area / donut / funnel / treemap: group_by + agg
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

    # v136 - optional per-app accent color, e.g. {"accent": "#8b5cf6"}
    # v140 - + corner radius / spacing density (light/dark is the platform-wide toggle, not per-app)
    theme = config.get("theme")
    if theme is not None:
        if not isinstance(theme, dict):
            raise ValueError("config.theme must be an object")
        accent = theme.get("accent")
        if accent is not None and not (isinstance(accent, str) and _HEX_COLOR_RE.fullmatch(accent)):
            raise ValueError("config.theme.accent must be a hex color like #8b5cf6")
        radius = theme.get("radius")
        if radius is not None and radius not in THEME_RADII:
            raise ValueError(f"config.theme.radius must be one of {sorted(THEME_RADII)}")
        density = theme.get("density")
        if density is not None and density not in THEME_DENSITIES:
            raise ValueError(f"config.theme.density must be one of {sorted(THEME_DENSITIES)}")

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
        if "multiple" in f and not isinstance(f["multiple"], bool):  # v136: checkbox-group fields
            raise ValueError(f"{fctx}: multiple must be a boolean")
        if "relation" in f and f["relation"] is not None:  # v141: field links to another dataset
            rel = f["relation"]
            if not isinstance(rel, dict):
                raise ValueError(f"{fctx}: relation must be an object")
            if not isinstance(rel.get("dataset_id"), str) or not rel["dataset_id"]:
                raise ValueError(f"{fctx}: relation.dataset_id is required")
            if not isinstance(rel.get("display_column"), str) or not rel["display_column"]:
                raise ValueError(f"{fctx}: relation.display_column is required")
            vc = rel.get("value_column")
            if vc is not None and not isinstance(vc, str):
                raise ValueError(f"{fctx}: relation.value_column must be a string")


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
    f.setdefault("multiple", False)  # v136: options rendered as a checkbox group
    f.setdefault("relation", None)  # v141: {dataset_id, display_column, value_column?} - links to another dataset
    return f


def form_fields(form_comp: dict | None) -> list[dict]:
    if not form_comp:
        return []
    return [normalize_field(f) for f in form_comp.get("fields", [])]


def relation_fields(form_comp: dict | None) -> list[dict]:
    """Normalized fields of a form component that link to another dataset (v141)."""
    return [f for f in form_fields(form_comp) if f.get("relation")]


def build_relation_lookup(df: pd.DataFrame, value_col: str, display_col: str, limit: int = 5000) -> dict[str, str]:
    """str(value) -> display label, over up to ``limit`` rows of the related
    dataset (v141). Used both to resolve a table cell's friendly label and to
    build the create/edit dropdown's option list from the SAME source.

    ``value_col == display_col`` is the common case (link by the same column
    you show, e.g. a customer's name) - selected separately below because
    ``df[[col, col]]`` would otherwise select TWO columns sharing one label,
    turning ``sub[col]`` into a 2-column frame instead of a Series and
    silently collapsing every row into one bogus entry when iterated.
    """
    if value_col not in df.columns or display_col not in df.columns:
        return {}
    if value_col == display_col:
        sub = df[[value_col]].dropna(subset=[value_col]).head(limit)
        out: dict[str, str] = {}
        for v in sub[value_col]:
            out.setdefault(str(v), str(v))
        return out
    sub = df[[value_col, display_col]].dropna(subset=[value_col]).head(limit)
    out = {}
    for v, d in zip(sub[value_col], sub[display_col]):
        key = str(v)
        if key not in out:
            out[key] = str(d) if pd.notna(d) else key
    return out


def relation_options_list(lookup: dict[str, str], limit: int = MAX_FILTER_OPTIONS) -> list[dict]:
    """{value,label} pairs for a <select>, alphabetized by label and capped."""
    items = [{"value": v, "label": l} for v, l in lookup.items()]
    items.sort(key=lambda o: o["label"].lower())
    return items[:limit]


EXPORT_FORMATS = {"csv", "xlsx", "json"}  # v142: app-level export - see export_records_bytes


def _hex_or(color: str | None, fallback: str) -> str:
    """A '#rrggbb' (or bare 'rrggbb') accent as the ARGB-less hex openpyxl
    fills want - falls back to ``fallback`` for anything malformed/missing."""
    c = (color or "").lstrip("#").upper()
    return c if re.fullmatch(r"[0-9A-F]{6}", c) else fallback


_XLSX_CHART_SHAPES = {"bar", "line", "area", "pie", "donut", "funnel"}  # labels/values shape
_XLSX_LIVE_AGGS = {"count", "sum", "avg", "min", "max"}  # have a clean live-formula translation


def _xlsx_stat_formula(table: str, agg: str, column: str | None, first_column: str | None = None) -> str | None:
    """A live formula against the Excel Table's structured reference - edit,
    add, or delete a row on the Data sheet and this recalculates on its own,
    same as any other Excel formula. None for an agg with no clean live
    translation (median, count_distinct) - the caller falls back to the
    plain computed number in that case, just not a self-updating one.

    Deliberately never emits a BARE table reference like ``=ROWS(AppRecords)``:
    real Excel only reliably resolves a Table name written straight into the
    XML (rather than typed through the formula bar, which auto-qualifies it)
    when it's used as ``Table[Column]`` - the bare form comes back #NAME? on
    load. So "count" uses ``ROWS(table[some column])`` same as every other
    agg, falling back to ``first_column`` (the Data sheet's first column,
    always present) when no specific column applies."""
    col = column or first_column
    if agg == "count":
        return f"=ROWS({table}[{col}])" if col else None
    if not column:
        return None
    ref = f"{table}[{column}]"
    return {"sum": f"=SUM({ref})", "avg": f"=AVERAGE({ref})", "min": f"=MIN({ref})", "max": f"=MAX({ref})"}.get(agg)


def _xlsx_group_formula(table: str, agg: str, group_col: str, column: str | None, label_cell: str) -> str | None:
    """Same idea as :func:`_xlsx_stat_formula` but per chart category - the
    label cell (e.g. "A5") is the fixed, export-time category; COUNTIF/
    SUMIF/etc. re-tally it live against the Table whenever the Data sheet
    changes. New categories added after export don't grow a new bar (the
    label list itself is static) - re-export to pick those up."""
    gref = f"{table}[{group_col}]"
    if agg == "count":
        return f"=COUNTIF({gref},{label_cell})"
    if not column:
        return None
    vref = f"{table}[{column}]"
    return {
        "sum": f"=SUMIF({gref},{label_cell},{vref})",
        "avg": f"=AVERAGEIF({gref},{label_cell},{vref})",
        "min": f"=MINIFS({vref},{gref},{label_cell})",
        "max": f"=MAXIFS({vref},{gref},{label_cell})",
    }.get(agg)


def _xlsx_kpi_tile(ws, col0: int, row0: int, width: int, label: str, value, number_format: str, fill: str) -> None:
    """Local twin of ds_svc._xlsx_kpi_tile that accepts a live FORMULA (or a
    plain fallback number) plus an explicit number_format, instead of a
    pre-stringified display value - ds_svc's own copy stays untouched since
    the dataset-level export still uses it as-is."""
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    c0, c1 = get_column_letter(col0), get_column_letter(col0 + width - 1)
    ws.merge_cells(f"{c0}{row0}:{c1}{row0 + 1}")
    vcell = ws[f"{c0}{row0}"]
    vcell.value = value
    if number_format:
        vcell.number_format = number_format
    vcell.font = Font(size=20, bold=True, color=ds_svc._XLSX_WHITE)
    vcell.alignment = Alignment(horizontal="center", vertical="center")
    ws.merge_cells(f"{c0}{row0 + 2}:{c1}{row0 + 2}")
    lcell = ws[f"{c0}{row0 + 2}"]
    lcell.value = label
    lcell.font = Font(size=10, bold=True, color=ds_svc._XLSX_WHITE)
    lcell.alignment = Alignment(horizontal="center", vertical="center")
    for r in (row0, row0 + 1, row0 + 2):
        for c in range(col0, col0 + width):
            ws.cell(row=r, column=c).fill = PatternFill("solid", fgColor=fill)


def _xlsx_nav_button(ws, cell_ref: str, label: str, target_sheet: str, accent: str, span: int = 2) -> None:
    """A clickable, in-workbook 'button' - a button-styled cell carrying an
    internal hyperlink to another sheet. A real VBA CommandButton needs a
    macro-enabled .xlsm (Trust Center friction for whoever opens it, and
    openpyxl has no safe way to author a VBA project) - this is how native,
    macro-free Excel dashboards do in-workbook navigation, and it's what
    answers "where's the button that opens the Add Record tab": click it,
    Excel jumps you straight to that sheet."""
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import column_index_from_string, get_column_letter

    col_letters = "".join(ch for ch in cell_ref if ch.isalpha())
    row = int("".join(ch for ch in cell_ref if ch.isdigit()))
    if span > 1:
        col_idx = column_index_from_string(col_letters)
        end_col = get_column_letter(col_idx + span - 1)
        ws.merge_cells(f"{cell_ref}:{end_col}{row}")
    cell = ws[cell_ref]
    cell.value = f"▸ {label}"
    cell.hyperlink = f"#'{target_sheet}'!A1"
    cell.font = Font(bold=True, color=ds_svc._XLSX_WHITE, size=10)
    cell.fill = PatternFill("solid", fgColor=accent)
    cell.alignment = Alignment(horizontal="center", vertical="center")
    thin = Side(style="thin", color=accent)
    cell.border = Border(top=thin, bottom=thin, left=thin, right=thin)
    if ws.row_dimensions[row].height is None or ws.row_dimensions[row].height < 20:
        ws.row_dimensions[row].height = 20


def _add_record_sheet(wb, form_comp: dict | None, accent: str) -> bool:
    """v144.2: a template sheet mirroring the app's Add Record form - field
    labels as the header row (in the form's own order), a dropdown for any
    field with fixed options, 200 blank templated rows to fill in. This is
    NOT wired back into py8n (there's no import path from a bare workbook
    into a running app) - it exists so the workbook reflects the WHOLE app,
    not just its table and dashboard, addressing "the form didn't appear".
    Returns whether the sheet was actually created (nothing to fill in for
    an app with no form component means nothing to add here)."""
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    fields = form_fields(form_comp) if form_comp else []
    if not fields:
        return False
    ws = wb.create_sheet("Add Record")
    n = max(1, len(fields))
    ws["A1"] = "Template only - matches this app's Add Record form. Not synced back to py8n; use it to prep rows, then paste the values into the Data sheet or re-enter them in the live app."
    ws["A1"].font = Font(size=9, italic=True, color=ds_svc._XLSX_MUTED)
    ws.merge_cells(f"A1:{get_column_letter(n)}1")
    _xlsx_nav_button(ws, f"{get_column_letter(n + 2)}1", "Dashboard", "Dashboard", accent)

    header_font = Font(bold=True, color=ds_svc._XLSX_WHITE)
    header_fill = PatternFill("solid", fgColor=accent)
    for i, f in enumerate(fields, start=1):
        cell = ws.cell(row=3, column=i, value=f.get("label") or humanize(f.get("name", "")))
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        width = max(12, min(30, len(str(cell.value)) + 4))
        ws.column_dimensions[get_column_letter(i)].width = width

        options = [str(o) for o in (f.get("options") or [])]
        if options:
            joined = ",".join(options)
            if len(joined) <= 240:  # Excel's inline list formula has a ~255-char ceiling
                dv = DataValidation(type="list", formula1=f'"{joined}"', allow_blank=True)
                col = get_column_letter(i)
                dv.add(f"{col}4:{col}203")
                ws.add_data_validation(dv)
    ws.freeze_panes = "A4"
    return True


def _app_dashboard_xlsx_bytes(app: App, df: pd.DataFrame) -> bytes:
    """The APP, not just its data (v144, fixing v142's bare df.to_excel()).

    Sheet "Dashboard" (opened first) mirrors what the live /run/{slug} page
    actually shows: one KPI tile per stat/kpi component, one native Excel
    chart per bar/line/area/pie/donut/funnel chart component. Both are
    driven by live formulas against the Data sheet's Excel Table wherever
    the aggregation allows it (count/sum/avg/min/max) - edit, add, or
    delete a row there and the tiles and charts recalculate on their own,
    the way a real spreadsheet is expected to behave; an agg with no clean
    formula translation (median, count_distinct) falls back to the plain
    number computed at export time. Sheet "Add Record" mirrors the app's
    own form (see :func:`_add_record_sheet`). Sheet "Data" is the full
    frame as a banded, filterable Excel Table.

    v144.1 briefly rendered charts as matplotlib images after native chart
    OBJECTS showed up as empty boxes in Google Sheets - reverted: this
    workbook is an Excel artifact (py8n's separate Sheets Sync feature is
    the live-Sheets path), and native charts are what let the dashboard
    stay interactive and recalculate at all once downloaded.
    """
    import io

    from openpyxl import Workbook
    from openpyxl.chart import BarChart, DoughnutChart, LineChart, PieChart, Reference
    from openpyxl.chart.label import DataLabelList
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo

    accent = _hex_or(((app.config or {}).get("theme") or {}).get("accent"), ds_svc._XLSX_ACCENT)
    columns = [str(c) for c in df.columns]
    rows = ds_svc.jsonable_rows(df)
    TABLE_NAME = "AppRecords"

    wb = Workbook()
    data_ws = wb.active
    data_ws.title = "Data"
    data_ws.append(columns)
    for r in rows:
        data_ws.append([r.get(c) for c in columns])

    header_font = Font(bold=True, color=ds_svc._XLSX_WHITE)
    header_fill = PatternFill("solid", fgColor=accent)
    for cell in data_ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    n_rows, n_cols = len(rows) + 1, len(columns)
    has_table = n_rows > 1 and n_cols > 0
    if has_table:
        last_col = get_column_letter(n_cols)
        table = Table(displayName=TABLE_NAME, ref=f"A1:{last_col}{n_rows}")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium9", showRowStripes=True)
        data_ws.add_table(table)
    data_ws.freeze_panes = "A2"
    for i, col in enumerate(columns, start=1):
        sample = [len(str(r.get(col, ""))) for r in rows[:200]]
        data_ws.column_dimensions[get_column_letter(i)].width = max(10, min(40, max([len(str(col))] + sample) + 2))
    _xlsx_nav_button(data_ws, f"{get_column_letter(max(n_cols, 1) + 2)}1", "Dashboard", "Dashboard", accent)

    # Add Record sheet is built now (right after Data, before Dashboard) so
    # the Dashboard's "+ Add Record" button below knows whether that sheet
    # actually exists - an app with no form component gets no such sheet,
    # and a button pointing at a sheet that isn't there would be broken.
    components = (app.config or {}).get("components", [])
    form_comp = next((c for c in components if c.get("type") == "form"), None)
    has_add_record = _add_record_sheet(wb, form_comp, accent)

    # ---- Dashboard sheet - the app itself, placed first -------------------
    dash = wb.create_sheet("Dashboard", 0)
    wb.active = 0
    dash.sheet_view.showGridLines = False
    dash.column_dimensions["A"].width = 2

    dash["B2"] = app.name or "App"
    dash["B2"].font = Font(size=18, bold=True, color=ds_svc._XLSX_INK)
    dash["B3"] = app.description or f"{len(df):,} rows  •  {len(columns)} columns"
    dash["B3"].font = Font(size=11, color=ds_svc._XLSX_MUTED)

    _xlsx_nav_button(dash, "F2", "View All Records", "Data", accent)
    if has_add_record:
        _xlsx_nav_button(dash, "F3", "Add Record", "Add Record", accent)

    rendered = compute_components(components, df)

    stats = [c for c in rendered if c.get("type") in ("stat", "kpi")]
    col0, row0, width = 2, 5, 3
    for i, c in enumerate(stats[:5]):
        agg, column = c.get("agg", "count"), c.get("column")
        formula = (
            _xlsx_stat_formula(TABLE_NAME, agg, column, columns[0] if columns else None)
            if has_table and agg in _XLSX_LIVE_AGGS
            else None
        )
        value = formula if formula else (c.get("value") if isinstance(c.get("value"), (int, float)) else 0)
        number_format = "#,##0" if agg == "count" else "#,##0.##"
        _xlsx_kpi_tile(
            dash, col0 + i * (width + 1), row0, width,
            (c.get("label") or "Value")[:24], value, number_format,
            ds_svc._XLSX_TILE_COLORS[i % len(ds_svc._XLSX_TILE_COLORS)],
        )

    chart_row = row0 + 5
    helper_col = 20  # far right, hidden helper columns backing every chart
    for comp in rendered:
        if comp.get("type") != "chart" or comp.get("chart_type") not in _XLSX_CHART_SHAPES:
            continue
        labels, values = comp.get("labels") or [], comp.get("values") or []
        if not labels or not values:
            continue
        chart_type = comp["chart_type"]
        title = comp.get("title") or "Chart"
        agg = comp.get("agg", "count")
        group_col, value_col = comp.get("group_by"), comp.get("column")

        hc = helper_col
        dash.cell(row=1, column=hc, value="label")
        dash.cell(row=1, column=hc + 1, value=title)
        for i, (lbl, val) in enumerate(zip(labels, values), start=2):
            label_cell = dash.cell(row=i, column=hc, value=str(lbl))
            formula = (
                _xlsx_group_formula(TABLE_NAME, agg, group_col, value_col, f"${get_column_letter(hc)}${i}")
                if has_table and agg in _XLSX_LIVE_AGGS and group_col
                else None
            )
            value_cell = dash.cell(row=i, column=hc + 1, value=formula if formula else float(val))
        dash.column_dimensions[get_column_letter(hc)].hidden = True
        dash.column_dimensions[get_column_letter(hc + 1)].hidden = True
        helper_col += 2

        data_ref = Reference(dash, min_col=hc + 1, min_row=1, max_row=len(labels) + 1)
        cats_ref = Reference(dash, min_col=hc, min_row=2, max_row=len(labels) + 1)

        chart = {
            "bar": BarChart, "line": LineChart, "area": LineChart,
            "pie": PieChart, "donut": DoughnutChart, "funnel": BarChart,
        }[chart_type]()
        if isinstance(chart, BarChart):
            chart.type = "bar" if chart_type == "funnel" else "col"
        chart.title = title
        chart.add_data(data_ref, titles_from_data=True)
        chart.set_categories(cats_ref)
        if chart_type in ("pie", "donut"):
            # Explicit False on every OTHER flag - PieChart's implicit
            # defaults otherwise stack category name + series name + value
            # + percent into one label ("Stage Breakdown, Closed Lost, 1,
            # 12%"), which is what showed up instead of a clean "12%".
            dl = DataLabelList()
            dl.showPercent = True
            dl.showCatName = False
            dl.showSerName = False
            dl.showVal = False
            dl.showLegendKey = False
            chart.dataLabels = dl
        # Excel's default chart behaviour is to plot ONLY visible cells, and
        # the whole point of the helper columns is that they're hidden - so
        # without this every chart renders as an empty box with no bars/
        # slices at all (this, not a Google Sheets interop quirk, is what
        # actually caused the empty-chart bug).
        chart.visible_cells_only = False
        chart.height, chart.width = 9, 16
        dash.add_chart(chart, f"B{chart_row}")
        chart_row += 20

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_records_bytes(app: App, df: pd.DataFrame, fmt: str) -> tuple[bytes, str, str]:
    """Serialize EXACTLY the rows in ``df`` (bytes, content_type, ext) - v142,
    upgraded in v144 so xlsx exports the APP (dashboard + native charts,
    mirroring the live runtime) rather than a bare data dump.

    This is the scope-safe counterpart to ds_svc.export_dataset_bytes, which
    always re-reads the dataset's OWN full parquet from disk: that would leak
    every row to a grant-scoped viewer whose /run/{slug} only ever shows
    their slice. The app runtime's Export button calls this instead, over
    whatever df it already filtered for that viewer.
    """
    fmt = (fmt or "csv").strip().lower()
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"unsupported export format {fmt!r} (use csv|xlsx|json)")
    if fmt == "csv":
        return df.to_csv(index=False).encode("utf-8-sig"), "text/csv", "csv"
    if fmt == "json":
        return df.to_json(orient="records").encode("utf-8"), "application/json", "json"
    return (
        _app_dashboard_xlsx_bytes(app, df),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xlsx",
    )


# ------------------------------------------------------- Google Sheets sync (v143)
# Owner-side push of an app's dataset into a Google Sheet tab - reuses the
# workflow engine's GoogleSheetsWriteNode plumbing directly rather than
# duplicating it. This is a builder/owner action (like Export), NOT part of
# the public runtime surface, so it always pushes the WHOLE bound dataset -
# there is no grant-scoping concern here the way there is for /export.

SHEETS_WRITE_MODES = {"overwrite", "append"}


def sheets_sync_config(app_config: dict | None) -> dict:
    """The app's saved Sheets-sync settings, or the empty defaults."""
    cfg = (app_config or {}).get("sheets_sync") or {}
    return {
        "sheet": cfg.get("sheet") or "",
        "tab": cfg.get("tab") or "",
        "credential_id": cfg.get("credential_id"),
        "write_mode": cfg.get("write_mode") or "overwrite",
    }


def validate_sheets_sync(body: dict) -> dict:
    """Normalize+validate a sheets_sync payload before it's saved to config."""
    sheet = (body.get("sheet") or "").strip()
    tab = (body.get("tab") or "").strip()
    credential_id = (body.get("credential_id") or "").strip() or None
    write_mode = (body.get("write_mode") or "overwrite").strip().lower()
    if write_mode not in SHEETS_WRITE_MODES:
        raise ValueError(f"write_mode must be one of {sorted(SHEETS_WRITE_MODES)}")
    if sheet and not credential_id:
        raise ValueError("A Google service-account credential is required to sync to a sheet")
    if credential_id and not sheet:
        raise ValueError("A sheet URL or ID is required")
    return {"sheet": sheet, "tab": tab, "credential_id": credential_id, "write_mode": write_mode}


async def run_sheets_sync(df: pd.DataFrame, cfg: dict, owner_id: str | None) -> dict:
    """Push every row of ``df`` into the configured Sheet tab (v143).

    Reuses the connector node's private helpers directly instead of routing
    through the workflow engine - ``decrypt_credential`` only touches its
    ``context`` argument for audit metadata (workflow_id/name), both of which
    are happily None here, so passing None is safe and avoids constructing a
    fake ExecutionContext just to satisfy the signature.
    """
    import asyncio

    from ..engine.nodes.connectors import (
        _append_sa_values,
        _clear_sa_values,
        _extract_sheet_id,
        _refresh_sa_token,
        _service_account_credentials,
        _sheets_cell,
        _update_sa_values,
    )
    from .crypto import decrypt_credential

    sheet = (cfg.get("sheet") or "").strip()
    tab = (cfg.get("tab") or "").strip()
    credential_id = cfg.get("credential_id")
    write_mode = (cfg.get("write_mode") or "overwrite").strip().lower()
    if not sheet:
        raise ValueError("No Google Sheet configured yet - set one up first")
    if not credential_id:
        raise ValueError("No service-account credential configured yet")
    if not tab:
        raise ValueError("A tab (sheet) name is required")
    if write_mode not in SHEETS_WRITE_MODES:
        raise ValueError(f"write_mode must be one of {sorted(SHEETS_WRITE_MODES)}")

    sheet_id, _ = _extract_sheet_id(sheet)
    cred = await decrypt_credential(None, credential_id, owner_id=owner_id)
    if cred.get("type") not in (None, "", "google_service_account"):
        raise ValueError("Sheets sync needs a google_service_account credential")
    credentials = _service_account_credentials(cred, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    token = await asyncio.to_thread(_refresh_sa_token, credentials)

    columns = [str(c) for c in df.columns]
    records = df.to_dict(orient="records")
    data_rows = [[_sheets_cell(r.get(c)) for c in columns] for r in records]

    if write_mode == "append":
        written = await _append_sa_values(sheet_id, tab, data_rows, token)
    else:
        await _clear_sa_values(sheet_id, tab, token)
        written = await _update_sa_values(sheet_id, tab, [columns] + data_rows, token)

    return {
        "sheet_id": sheet_id,
        "tab": tab,
        "write_mode": write_mode,
        "rows_written": len(records),
        "updated_rows": written,
        "columns": columns,
    }


async def compute_relation_lookups(db: AsyncSession, components: list[dict]) -> dict[str, dict[str, str]]:
    """Field name -> {str(value): display label} for every relation-linked
    form field across ``components`` (v141). Computed once per request so
    the create/edit dropdown and the table's cell display resolve the exact
    same labels; a broken/missing linked dataset just yields no lookup for
    that field rather than failing the whole request."""
    out: dict[str, dict[str, str]] = {}
    for comp in components:
        if comp.get("type") != "form":
            continue
        for f in relation_fields(comp):
            name = f["name"]
            if name in out:
                continue
            rel = f["relation"]
            ds = await db.get(Dataset, rel["dataset_id"])
            if ds is None:
                continue
            value_col = rel.get("value_column") or rel["display_column"]
            try:
                rdf = ds_svc.read_parquet_df(ds_svc.parquet_path(ds.id))
            except Exception:  # noqa: BLE001 - a bad/missing parquet must not break the app
                continue
            out[name] = build_relation_lookup(rdf, value_col, rel["display_column"])
    return out


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


def compute_components(
    components: list[dict],
    df: pd.DataFrame,
    filters: dict | None = None,
    relation_lookups: dict[str, dict[str, str]] | None = None,
    loaders: dict[str, pd.DataFrame] | None = None,
) -> list[dict]:
    """Render EVERY component server-side (v46) - one source of truth for the
    builder preview and the runtime, ending the client-side drift.

    Chart bucket caps: 8 slices for pie/donut, 12 bars/lines (v29 parity).
    Filter options are distinct values capped at MAX_FILTER_OPTIONS.
    ``relation_lookups`` (v141, precomputed by the caller via
    :func:`compute_relation_lookups` - it needs DB access this function
    doesn't have) attaches live dropdown options to any form field that
    links to another dataset.

    ``loaders`` (v149: multi-dataset apps) maps ``dataset_id -> DataFrame``
    for the app's EXTRA (non-primary) datasets, mirroring Dashboard's
    ``compute_config``. A component tagged with ``dataset_id`` reads from
    ``loaders[dataset_id]`` when present there; every other component (no
    ``dataset_id``, or one not found in ``loaders``) reads from the primary
    ``df`` exactly as before - so single-dataset apps (the overwhelming
    majority, and every app saved before this feature existed) are byte-for-
    byte unaffected by this parameter.
    """
    rendered: list[dict] = []
    for comp in components:
        ctype = comp.get("type")
        cid = comp.get("id", "")
        # v134: every rendered component is stamped with its page/section so
        # the runtime can group components into sidebar/tab navigation - see
        # component_page()/pages_of() above.
        page_name = component_page(comp)
        ds_id = comp.get("dataset_id")
        comp_df = loaders.get(ds_id, df) if (loaders and ds_id) else df
        fdf = apply_filters(comp_df, filters)

        def _add(d: dict) -> None:
            rendered.append({**d, "page": page_name, "width": comp.get("width") or "full"})  # v139: grid layout

        if ctype in ("stat", "kpi"):
            value = _agg_series(fdf, comp.get("agg", "count"), comp.get("column"))
            _add({
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
                    _add({**base, "points": []})
                    continue
                pts = fdf[[x_col, y_col]].copy()
                pts[y_col] = pd.to_numeric(pts[y_col], errors="coerce")
                pts = pts.dropna().head(500)
                points = [
                    {"x": json.loads(json.dumps(row[x_col], default=str)), "y": round(float(row[y_col]), 4)}
                    for _, row in pts.iterrows()
                ]
                _add({**base, "x": x_col, "y": y_col, "points": points})
                continue
            if chart_type == "gauge":  # v138: single aggregated value, like a stat
                value = _agg_series(fdf, comp.get("agg", "count"), comp.get("column"))
                _add({**base, "value": value, "max": comp.get("max")})
                continue
            if chart_type == "radar":  # v138: one series across several numeric metrics
                metrics = [m for m in (comp.get("metrics") or []) if m in fdf.columns]
                agg = comp.get("agg", "avg")
                values = [_agg_series(fdf, agg, m) or 0 for m in metrics]
                _add({**base, "metrics": metrics, "values": values})
                continue
            if chart_type == "heatmap":  # v138: two dimensions + an aggregated value per cell
                row_col, col_col = comp.get("row"), comp.get("col")
                if row_col not in fdf.columns or col_col not in fdf.columns or not len(fdf):
                    _add({**base, "rows": [], "cols": [], "cells": []})
                    continue
                rseries = fdf[row_col].fillna("(blank)").astype(str)
                cseries = fdf[col_col].fillna("(blank)").astype(str)
                agg = comp.get("agg", "count")
                col = comp.get("column")
                if agg == "count":
                    grouped = fdf.groupby([rseries, cseries]).size()
                else:
                    nums = pd.to_numeric(fdf[col], errors="coerce") if col in fdf.columns else pd.Series(dtype=float)
                    if agg == "median":
                        grouped = nums.groupby([rseries, cseries]).median().dropna()
                    elif agg == "count_distinct":
                        grouped = fdf[col].groupby([rseries, cseries]).nunique().dropna() if col in fdf.columns else pd.Series(dtype=float)
                    else:
                        grouped = nums.groupby([rseries, cseries]).agg("mean" if agg == "avg" else agg).dropna()
                rows = [str(v) for v in rseries.value_counts().index[:15]]
                cols = [str(v) for v in cseries.value_counts().index[:15]]
                cells = []
                for (r, c), v in grouped.items():
                    if r in rows and c in cols:
                        cells.append([rows.index(r), cols.index(c), round(float(v), 4)])
                _add({**base, "rows": rows, "cols": cols, "cells": cells})
                continue
            group_by = comp.get("group_by")
            if group_by not in fdf.columns or not len(fdf):
                _add({**base, "labels": [], "values": []})
                continue
            series = fdf[group_by].fillna("(blank)")
            agg = comp.get("agg", "count")
            cap = 8 if chart_type in ("pie", "donut", "funnel") else 12
            if agg == "count":
                counts = series.value_counts()
                labels = [str(v) for v in counts.index[:cap]]
                values = [int(c) for c in counts.values[:cap]]
            else:
                col = comp.get("column")
                if col not in fdf.columns:
                    _add({**base, "labels": [], "values": []})
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
            _add({**base, "labels": labels, "values": values})
        elif ctype == "table":
            cols = [c for c in comp.get("columns", []) if c in fdf.columns]
            page_size = int(comp.get("page_size", 10))
            rows = ds_svc.jsonable_rows(fdf[cols].head(page_size)) if cols else []
            _add({
                "id": cid, "type": "table", "title": comp.get("title", ""),
                "columns": cols, "rows": rows,
                "row_count": len(rows), "total": int(len(fdf)),
            })
        elif ctype == "markdown":
            _add({
                "id": cid, "type": "markdown", "title": comp.get("title", ""),
                "html": markdown_to_safe_html(comp.get("body", "")),
            })
        elif ctype == "filter":
            col = comp.get("column")
            options: list[str] = []
            if col in comp_df.columns:
                options = [str(v) for v in comp_df[col].dropna().unique()[:MAX_FILTER_OPTIONS]]
            _add({
                "id": cid, "type": "filter", "column": col,
                "label": comp.get("label") or humanize(col or ""),
                "multiple": bool(comp.get("multiple", False)),
                "options": sorted(options),
            })
        elif ctype == "form":
            fields = form_fields(comp)
            if relation_lookups:  # v141: attach live {value,label} options per linked field
                for f in fields:
                    if f.get("relation"):
                        f["relation_options"] = relation_options_list(relation_lookups.get(f["name"], {}))
            _add({
                "id": cid, "type": "form", "title": comp.get("title", "Submit"),
                "submit_label": comp.get("submit_label", "Submit"),
                "fields": fields,
            })
        elif ctype == "button":  # v137: static config, no data dependency
            _add({
                "id": cid, "type": "button",
                "label": comp.get("label", "Button"),
                "style": comp.get("style", "primary"),
                "action": comp.get("action"),
                "url": comp.get("url"),
                "new_tab": bool(comp.get("new_tab", True)),
                "target_page": comp.get("target_page"),
                "webhook_url": comp.get("webhook_url"),
                "method": comp.get("method", "POST"),
                "confirm_message": comp.get("confirm_message") or None,
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
        opts = f.get("options")
        if f.get("multiple") and isinstance(val, list):  # v136: checkbox-group field
            if f.get("required") and not val:
                raise ValueError(f"field '{name}' is required")
            if opts:
                bad = [v for v in val if not any(_loose(v, o) for o in opts)]
                if bad:
                    raise ValueError(f"field '{name}' must be one of: {', '.join(str(o) for o in opts)}")
            # store as a delimited string - the underlying dataset column is scalar
            out[name] = "; ".join(str(v) for v in val)
            return
        if f.get("required") and _is_empty_val(val):
            raise ValueError(f"field '{name}' is required")
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
