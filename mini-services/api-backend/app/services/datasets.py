"""Dataset storage core (v27) - first-class tabular data objects.

Storage layout
==============
* Every dataset's ROWS live in a Parquet file under ``data/datasets/``,
  written and read through DuckDB (parquet support is built in - no pyarrow).
* Every dataset's METADATA (name, schema, row_count, source) lives in the
  ``datasets`` SQLite table. The parquet file is named ``{id}.parquet`` and
  writes are atomic (temp file + os.replace).

Ingestion rules
===============
* Column names are coerced to strings; empties become ``col_N`` and
  duplicates get ``_2``/``_3`` suffixes.
* Cells that are dicts/lists are JSON-encoded (keeps parquet flat and every
  downstream consumer JSON-friendly).
* Row payloads going back out are JSON-native (NaN → null, datetimes → ISO)
  via ``json.loads(df.to_json(...))``.

SQL
===
``run_sql`` registers every stored dataset as a DuckDB view named by
:func:`view_name` (lowercase, non-alphanumerics folded to ``_``), so a
dataset called "Customers" is queryable as ``SELECT * FROM customers`` -
including joins across datasets.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

import duckdb
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Dataset, DatasetVersion


# ----------------------------------------------------------------- paths
def datasets_dir() -> Path:
    path = Path(settings.datasets_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def parquet_path(dataset_id: str) -> Path:
    return datasets_dir() / f"{dataset_id}.parquet"


# ----------------------------------------------------------------- naming
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,118}$")


def view_name(name: str) -> str:
    """SQL-facing view name for a dataset: 'Q4 Sales!' → 'q4_sales'."""
    v = re.sub(r"[^a-z0-9_]", "_", (name or "").strip().lower())
    v = re.sub(r"_+", "_", v).strip("_")
    if not v or v[0].isdigit():
        v = f"ds_{v}"
    return v


def _sanitize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Stringify column names; fill empties; dedupe with _2/_3 suffixes."""
    seen: dict[str, bool] = {}
    cols: list[str] = []
    for i, c in enumerate(df.columns):
        name = str(c).strip() if not isinstance(c, str) else c.strip()
        if not name:
            name = f"col_{i + 1}"
        base, n = name, 2
        while name.lower() in seen:
            name = f"{base}_{n}"
            n += 1
        seen[name.lower()] = True
        cols.append(name)
    df.columns = cols
    return df


def _flatten_cells(df: pd.DataFrame) -> pd.DataFrame:
    """JSON-encode dict/list cells so parquet stays flat & JSON-friendly."""
    for col in df.columns:
        if df[col].dtype == object:
            mask = df[col].map(lambda v: isinstance(v, (dict, list)))
            if mask.any():
                df.loc[mask, col] = df.loc[mask, col].map(
                    lambda v: json.dumps(v, ensure_ascii=False, default=str)
                )
    return df


def normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    """Canonical ingest pipeline: columns → cells → dtypes."""
    df = _sanitize_columns(df)
    df = _flatten_cells(df)
    return df.reset_index(drop=True)


# ----------------------------------------------------------------- serde
def jsonable_rows(df: pd.DataFrame) -> list[dict]:
    """pandas → JSON-native rows (NaN→null, datetimes→ISO, numpy→python)."""
    if len(df) == 0:
        return []
    return json.loads(df.to_json(orient="records", date_format="iso", force_ascii=False))


def schema_of(df: pd.DataFrame) -> list[dict]:
    labels = {
        "integer": "integer", "int64": "integer", "int32": "integer",
        "number": "number", "float64": "number", "float32": "number",
        "boolean": "boolean", "bool": "boolean",
        "datetime": "datetime", "datetime64[ns]": "datetime",
    }
    out = []
    for col in df.columns:
        raw = str(df[col].dtype)
        out.append({"name": col, "dtype": labels.get(raw, "text")})
    return out


# ----------------------------------------------------------------- parquet io
def write_parquet(df: pd.DataFrame, path: Path) -> None:
    """Atomic parquet write via DuckDB (temp file + replace)."""
    tmp = path.with_suffix(f".parquet.tmp-{uuid.uuid4().hex[:8]}")
    con = duckdb.connect()
    try:
        con.register("df_view", df)
        con.execute(f"COPY df_view TO '{tmp.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()
    os.replace(tmp, path)


def read_parquet_df(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    con = duckdb.connect()
    try:
        return con.execute(f"SELECT * FROM read_parquet('{path.as_posix()}')").df()
    finally:
        con.close()


# ----------------------------------------------------------------- db helpers
async def name_taken(db: AsyncSession, name: str, exclude_id: str | None = None) -> bool:
    q = select(Dataset).where(func.upper(Dataset.name) == name.strip().upper())
    if exclude_id:
        q = q.where(Dataset.id != exclude_id)
    return (await db.execute(q)).scalar_one_or_none() is not None


async def get_dataset(db: AsyncSession, ref: str) -> Dataset | None:
    """Resolve a dataset by id first, then by case-insensitive name."""
    row = await db.get(Dataset, ref)
    if row is not None:
        return row
    return (
        await db.execute(select(Dataset).where(func.upper(Dataset.name) == ref.strip().upper()))
    ).scalar_one_or_none()


async def create_from_df(
    db: AsyncSession,
    name: str,
    df: pd.DataFrame,
    source: str = "api",
    description: str = "",
    owner_id: str | None = None,
) -> Dataset:
    ds = Dataset(
        name=name.strip(),
        description=description.strip(),
        file_path="",
        schema_json=[],
        row_count=0,
        source=source,
    )
    ds.owner_id = owner_id  # stamped pre-flush so the v1 snapshot inherits it
    db.add(ds)
    await db.flush()  # assigns the id used by the parquet filename
    ds.file_path = f"{ds.id}.parquet"
    if len(df.columns):  # empty schemas stay fileless until first real write
        write_parquet(df, parquet_path(ds.id))
    ds.schema_json = schema_of(df)
    ds.row_count = int(len(df))
    await db.flush()
    await snapshot_version(db, ds, source=source)
    return ds


# ----------------------------------------------------------------- versions (v44)
MAX_DATASET_VERSIONS = 20  # per dataset; oldest snapshots beyond the cap are pruned


def versions_root() -> Path:
    path = datasets_dir() / "versions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def version_dir(dataset_id: str) -> Path:
    return versions_root() / dataset_id


def version_file(dataset_id: str, version: int) -> Path:
    return version_dir(dataset_id) / f"v{version}.parquet"


async def snapshot_version(
    db: AsyncSession, ds: Dataset, source: str, note: str = ""
) -> DatasetVersion:
    """Record the dataset's CURRENT parquet state as the next version.

    The snapshot copies the live file (empty/fileless datasets record a
    version with row_count 0 and no file), so the versions list is a full
    timeline whose newest entry always equals the current state. Snapshots
    beyond MAX_DATASET_VERSIONS are pruned together with their files.
    Caller owns the commit.
    """
    last = (
        await db.execute(
            select(DatasetVersion.version)
            .where(DatasetVersion.dataset_id == ds.id)
            .order_by(DatasetVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    next_v = int(last or 0) + 1

    vdir = version_dir(ds.id)
    vdir.mkdir(parents=True, exist_ok=True)
    src = parquet_path(ds.id)
    if src.exists():
        shutil.copyfile(src, version_file(ds.id, next_v))

    row = DatasetVersion(
        dataset_id=ds.id,
        owner_id=ds.owner_id,
        version=next_v,
        row_count=int(ds.row_count or 0),
        source=source[:20],
        note=note[:300],
    )
    db.add(row)
    await db.flush()

    stale = (
        (
            await db.execute(
                select(DatasetVersion)
                .where(DatasetVersion.dataset_id == ds.id)
                .order_by(DatasetVersion.version.desc())
                .offset(MAX_DATASET_VERSIONS)
            )
        )
        .scalars()
        .all()
    )
    for old in stale:
        f = version_file(ds.id, old.version)
        if f.exists():
            f.unlink()
        await db.delete(old)
    return row


async def restore_version(db: AsyncSession, ds: Dataset, version: int) -> DatasetVersion:
    """Roll the dataset back to a snapshot; the restored state itself is
    recorded as a new version, so a restore is always undoable."""
    f = version_file(ds.id, version)
    if not f.exists():
        raise ValueError(f"snapshot v{version} has no file to restore")
    df = read_parquet_df(f)
    write_parquet(df, parquet_path(ds.id))
    ds.schema_json = schema_of(df)
    ds.row_count = int(len(df))
    return await snapshot_version(db, ds, source="restore", note=f"restored from v{version}")


def delete_versions(dataset_id: str) -> None:
    """Remove every snapshot file of a dataset (called on dataset delete)."""
    vdir = version_dir(dataset_id)
    if vdir.exists():
        shutil.rmtree(vdir, ignore_errors=True)


async def append_rows(db: AsyncSession, ds: Dataset, items: list[dict]) -> int:
    """Append JSON rows; returns rows written."""
    if not items:
        return 0
    existing = read_parquet_df(parquet_path(ds.id)) if ds.row_count else pd.DataFrame()
    fresh = normalize_df(pd.DataFrame(items))
    combined = pd.concat([existing, fresh], ignore_index=True) if len(existing) else fresh
    write_parquet(combined, parquet_path(ds.id))
    ds.schema_json = schema_of(combined)
    ds.row_count = int(len(combined))
    await snapshot_version(db, ds, source="append")
    return len(fresh)


async def replace_rows(db: AsyncSession, ds: Dataset, items: list[dict]) -> int:
    """Replace the whole row set (schema may change)."""
    if not items:
        raise ValueError("refusing to replace a dataset with zero items (schema would be unknown)")
    fresh = normalize_df(pd.DataFrame(items))
    write_parquet(fresh, parquet_path(ds.id))
    ds.schema_json = schema_of(fresh)
    ds.row_count = int(len(fresh))
    await snapshot_version(db, ds, source="replace")
    return len(fresh)


def delete_file(ds: Dataset) -> None:
    path = parquet_path(ds.id)
    if path.exists():
        path.unlink()


# ----------------------------------------------------------------- sql
async def run_sql(db: AsyncSession, sql: str) -> dict:
    """Run SQL over ALL datasets (each registered as a view)."""
    rows = (await db.execute(select(Dataset).order_by(Dataset.name))).scalars().all()
    views: dict[str, str] = {}
    con = duckdb.connect()
    try:
        for ds in rows:
            view = view_name(ds.name)
            base, n = view, 2
            while view in views:
                view = f"{base}_{n}"
                n += 1
            views[view] = ds.name
            con.execute(
                f"CREATE VIEW {view} AS SELECT * FROM read_parquet('{parquet_path(ds.id).as_posix()}')"
            )
        started = time.perf_counter()
        result = con.execute(sql)
        columns = [d[0] for d in (result.description or [])]
        records = result.fetchall()
        duration_ms = int((time.perf_counter() - started) * 1000)
    except duckdb.Error as exc:
        raise ValueError(f"SQL error: {exc}") from exc
    finally:
        con.close()

    out_rows: list[dict] = []
    for rec in records:
        row = {}
        for i, col in enumerate(columns):
            v = rec[i]
            row[col] = json.loads(json.dumps(v, default=str, ensure_ascii=False))
        out_rows.append(row)
    return {
        "columns": columns,
        "rows": out_rows,
        "row_count": len(out_rows),
        "duration_ms": duration_ms,
        "views": views,
    }


# ----------------------------------------------------------------- profiling
def profile_df(df: pd.DataFrame) -> dict:
    """Per-column profile: counts, ranges for numerics, top values for text."""
    columns = []
    for col in df.columns:
        s = df[col]
        entry: dict = {
            "name": col,
            "dtype": schema_of(df[[col]])[0]["dtype"],
            "non_null": int(s.notna().sum()),
            "nulls": int(s.isna().sum()),
            "unique": int(s.nunique(dropna=True)),
        }
        if entry["dtype"] in ("integer", "number"):
            nums = pd.to_numeric(s, errors="coerce").dropna()
            if len(nums):
                stats = json.loads(
                    pd.DataFrame(
                        [{"min": nums.min(), "max": nums.max(), "mean": nums.mean()}]
                    ).to_json(orient="records")
                )[0]
                entry.update(stats)
        elif entry["dtype"] == "text" and entry["unique"] <= 50:
            tops = s.value_counts(dropna=True).head(5)
            entry["top_values"] = [
                {"value": str(v), "count": int(c)} for v, c in tops.items()
            ]
        columns.append(entry)
    return {"row_count": int(len(df)), "columns": columns}
