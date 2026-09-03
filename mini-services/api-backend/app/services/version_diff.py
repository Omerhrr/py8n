"""Dataset version diff (v55) - "what changed, why does it matter, what was affected?".

Versioning answered "can I go back?"; the diff answers "what happened
between two snapshots and who should care". One call compares two dataset
versions across four lenses:

* **Schema**  - columns added / removed / retyped (from each frame's schema).
* **Rows**    - the accurate version-record counts + delta.
* **Quality** - completeness / null rate / duplicate rate per snapshot
  (profiled over a bounded sample of each frame) + the score delta.
* **Changed** - row-level truth: with a ``key`` column, matched-by-key
  inserted / updated / removed counts and sample updates; without one, a
  multiset row-hash diff (added / removed / unchanged) - honest about the
  fact that without an identity there is no "update", only adds and drops.

And then the third question - **what was affected** - rides along from the
impact engine (:mod:`services.impact`): the workflows, dashboards, apps
and models downstream of this dataset, ranked by risk.

Everything is derived from the versioned parquet snapshots, so a diff of
the past can never be rewritten by the present.
"""

from __future__ import annotations

from collections import Counter

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from . import impact as impact_svc
from .datasets import (
    file_exists,
    profile_df,
    read_parquet_df,
    schema_of,
    version_file,
)

PROFILE_ROW_CAP = 50_000  # quality profiling is a bounded sample per frame
MAX_UPDATED_SAMPLES = 5
MAX_CHANGED_FIELDS = 3


class VersionDiffError(ValueError):
    """A diff request that cannot be answered (missing version / bad key)."""


def _norm(df: pd.DataFrame) -> pd.DataFrame:
    """Bounded, order-stable frame for comparisons (no mutation of source)."""
    out = df.head(PROFILE_ROW_CAP)
    return out


def _quality(df: pd.DataFrame | None) -> dict:
    """Light quality snapshot of one frame (None-safe)."""
    if df is None or not len(df) or not len(df.columns):
        return {"completeness_pct": None, "null_rate_pct": None, "duplicate_rows_pct": None, "score": None}
    prof = profile_df(df)
    cols = prof.get("columns", [])
    cells = sum(c["non_null"] + c["nulls"] for c in cols) or 1
    null_rate = round(sum(c["nulls"] for c in cols) / cells * 100, 2)
    completeness = prof.get("completeness_pct")
    dupes = int(prof.get("duplicate_rows") or 0)
    dup_pct = round(dupes / len(df) * 100, 2)
    score = round(max(0.0, float(completeness or 100.0) - min(float(completeness or 100.0), dup_pct * 2)), 1)
    return {
        "completeness_pct": completeness,
        "null_rate_pct": null_rate,
        "duplicate_rows_pct": dup_pct,
        "score": score,
    }


def _schema_diff(old_df: pd.DataFrame, new_df: pd.DataFrame) -> dict:
    old_by = {c["name"]: c["dtype"] for c in schema_of(old_df)}
    new_by = {c["name"]: c["dtype"] for c in schema_of(new_df)}
    added = [{"name": n, "dtype": t} for n, t in new_by.items() if n not in old_by]
    removed = [{"name": n, "dtype": t} for n, t in old_by.items() if n not in new_by]
    changed = [
        {"name": n, "from": old_by[n], "to": new_by[n]}
        for n in new_by
        if n in old_by and old_by[n] != new_by[n]
    ]
    return {"added": added, "removed": removed, "changed": changed}


def _canon(v):
    """Canonical token for row hashing: real numbers hash by float value so a
    retype (int -> float) does not fake a change; text keeps its identity."""
    if isinstance(v, bool):
        return ("b", v)
    if isinstance(v, (int, float)):
        try:
            return ("n", repr(float(v)))
        except (TypeError, ValueError):
            return ("s", str(v))
    return ("s", str(v))


def _row_key(row: dict) -> tuple:
    return tuple((c, _canon(row.get(c))) for c in sorted(row.keys()))


def _val_eq(a, b) -> bool:
    """Cell equality across frames whose dtypes may have shifted (int 20 vs
    float 20.0 is the SAME value after a retype); string compare fallback."""
    if str(a) == str(b):
        return True
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return False


def _changed_diff(old_df: pd.DataFrame, new_df: pd.DataFrame, key: str) -> dict:
    """Keyed diff: inserted / updated / removed counts + sample updates."""
    key = key.strip()
    if key not in old_df.columns or key not in new_df.columns:
        raise VersionDiffError(f"key column {key!r} must exist in BOTH versions")
    old_rows = _norm(old_df).to_dict(orient="records")
    new_rows = _norm(new_df).to_dict(orient="records")

    def _by_key(rows: list[dict]) -> dict:
        out: dict = {}
        for r in rows:
            k = str(r.get(key))
            out[k] = r  # last write wins on duplicate keys
        return out

    old_by, new_by = _by_key(old_rows), _by_key(new_rows)
    inserted = [k for k in new_by if k not in old_by]
    removed = [k for k in old_by if k not in new_by]
    updated_count = 0
    unchanged = 0
    updated_samples: list[dict] = []
    for k, nr in new_by.items():
        orow = old_by.get(k)
        if orow is None:
            continue
        cols = sorted((set(orow.keys()) | set(nr.keys())) - {key})
        diffs = [
            {"column": c, "from": jsonable_scalar(orow.get(c)), "to": jsonable_scalar(nr.get(c))}
            for c in cols
            if not _val_eq(orow.get(c), nr.get(c))
        ]
        if diffs:
            updated_count += 1
            if len(updated_samples) < MAX_UPDATED_SAMPLES:
                updated_samples.append({"key": k, "changes": diffs[:MAX_CHANGED_FIELDS], "changed_fields": len(diffs)})
        else:
            unchanged += 1
    return {
        "key": key,
        "inserted": len(inserted),
        "updated": updated_count,
        "removed": len(removed),
        "unchanged": unchanged,
        "updated_samples": updated_samples,
        "sample_inserted": inserted[:MAX_UPDATED_SAMPLES],
        "sample_removed": removed[:MAX_UPDATED_SAMPLES],
    }


def jsonable_scalar(v):
    """NaN/numpy -> JSON-native for the samples (single value)."""
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(v, "item"):
        try:
            return v.item()
        except (ValueError, AttributeError):
            pass
    return v


def _hash_diff(old_df: pd.DataFrame, new_df: pd.DataFrame) -> dict:
    """Keyless diff: multiset row-hash - honest adds/drops, no 'updated'."""
    old_rows = [_row_key(r) for r in _norm(old_df).to_dict(orient="records")]
    new_rows = [_row_key(r) for r in _norm(new_df).to_dict(orient="records")]
    old_c, new_c = Counter(old_rows), Counter(new_rows)
    added = sum((new_c - old_c).values())
    removed = sum((old_c - new_c).values())
    unchanged = sum((old_c & new_c).values())
    return {
        "key": None,
        "added": int(added),
        "removed": int(removed),
        "unchanged": int(unchanged),
        "note": "no key column - rows compared by full-row hash; provide ?key= for inserted/updated/removed",
    }


async def diff_versions(
    db: AsyncSession,
    ds,
    from_version: int,
    to_version: int,
    key: str | None = None,
) -> dict:
    """Compare two dataset versions: schema / rows / quality / changed + impact."""
    from ..models import DatasetVersion
    from sqlalchemy import select

    rows = (
        await db.execute(
            select(DatasetVersion)
            .where(
                DatasetVersion.dataset_id == ds.id,
                DatasetVersion.version.in_([from_version, to_version]),
            )
        )
    ).scalars().all()
    by_v = {int(v.version): v for v in rows}
    missing = [v for v in (from_version, to_version) if v not in by_v]
    if missing:
        raise VersionDiffError(f"unknown version(s): {', '.join(map(str, missing))}")

    frames: dict[int, pd.DataFrame | None] = {}
    for v in (from_version, to_version):
        f = version_file(ds.id, v)
        if not file_exists(f):
            raise VersionDiffError(f"snapshot v{v}'s parquet is pruned or fileless - cannot diff it")
        df = read_parquet_df(f)
        frames[v] = df

    old_v, new_v = by_v[from_version], by_v[to_version]
    old_df, new_df = frames[from_version], frames[to_version]

    schema = _schema_diff(old_df, new_df)
    q_from = _quality(_norm(old_df))
    q_to = _quality(_norm(new_df))

    if key and key.strip():
        changed = _changed_diff(old_df, new_df, key)
    else:
        changed = _hash_diff(old_df, new_df)

    impact = await impact_svc.compute_impact(db, ds)

    return {
        "dataset": {"id": ds.id, "name": ds.name, "ref": f"/datasets/{ds.id}"},
        "from": {
            "version": from_version,
            "row_count": int(old_v.row_count or 0),
            "created_at": old_v.created_at.isoformat() if old_v.created_at else None,
            "source": old_v.source,
        },
        "to": {
            "version": to_version,
            "row_count": int(new_v.row_count or 0),
            "created_at": new_v.created_at.isoformat() if new_v.created_at else None,
            "source": new_v.source,
        },
        "schema": schema,
        "rows": {
            "from": int(old_v.row_count or 0),
            "to": int(new_v.row_count or 0),
            "delta": int(new_v.row_count or 0) - int(old_v.row_count or 0),
        },
        "quality": {
            "from": q_from,
            "to": q_to,
            "score_delta": (
                round((q_to["score"] or 0) - (q_from["score"] or 0), 1)
                if q_from["score"] is not None and q_to["score"] is not None
                else None
            ),
        },
        "changed": changed,
        "impact": impact,
    }
