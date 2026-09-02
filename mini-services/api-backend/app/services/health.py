"""Dataset health (v50) - the observability primitive.

One call answers "is this dataset okay right now?" for a specific dataset
by composing the primitives the platform already keeps:

* **Freshness**  - age of the last write (from the version timeline).
* **Volume**     - current rows vs the previous version's rows.
* **Schema**     - contract presence + castability of the live data.
* **Quality**    - null rates, duplicate rows, completeness (from profile).
* **Score**      - one 0-100 number + healthy/degraded/unhealthy status.

The health endpoint is read-only and cheap enough to poll: profiling runs
over the live parquet (capped rows), the contract check runs over the
same frame, and version comparisons are two SQL rows. No background
workers, no extra state - health is always DERIVED, so it can never lie.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Dataset, DatasetVersion
from . import contracts as contract_svc
from .datasets import profile_df, read_parquet_df, parquet_path

PROFILE_ROW_CAP = 50_000  # health profiling is a sample of this size at most


def _freshness_tier(age_seconds: float) -> str:
    if age_seconds < 3600:
        return "fresh"
    if age_seconds < 86400:
        return "hours"
    if age_seconds < 7 * 86400:
        return "stale"
    return "cold"


def _grade(score: float) -> str:
    if score >= 90:
        return "healthy"
    if score >= 70:
        return "degraded"
    return "unhealthy"


async def compute_health(db: AsyncSession, ds: Dataset) -> dict:
    """Full health report for one dataset (read-only; caller owns nothing)."""
    now = datetime.now(timezone.utc)

    # --- freshness: the version timeline is the write log -----------------
    versions = (
        (
            await db.execute(
                select(DatasetVersion)
                .where(DatasetVersion.dataset_id == ds.id)
                .order_by(DatasetVersion.version.desc())
                .limit(2)
            )
        )
        .scalars()
        .all()
    )
    last_write = versions[0].created_at if versions else ds.updated_at
    if last_write is None:
        last_write = ds.created_at
    if last_write.tzinfo is None:
        last_write = last_write.replace(tzinfo=timezone.utc)
    age_seconds = max(0.0, (now - last_write).total_seconds())
    tier = _freshness_tier(age_seconds)
    if tier == "fresh":
        freshness_score = 100.0
    elif tier == "hours":
        freshness_score = 80.0
    elif tier == "stale":
        freshness_score = 50.0
    else:
        freshness_score = 20.0

    # --- volume: current vs previous version ------------------------------
    prev_rows = versions[1].row_count if len(versions) > 1 else None
    rows = int(ds.row_count or 0)
    delta = (rows - prev_rows) if prev_rows is not None else None
    delta_pct = (
        round(delta / prev_rows * 100, 2)
        if prev_rows not in (None, 0)
        else (100.0 if prev_rows == 0 and rows > 0 else None)
    )
    volume_score = 100.0
    if delta_pct is not None and prev_rows:
        if delta_pct < -50:
            volume_score = 30.0
        elif delta_pct < -10:
            volume_score = 70.0

    # --- quality + schema: one profile over the live frame ----------------
    df = read_parquet_df(parquet_path(ds.id)) if ds.row_count else None
    if df is not None and len(df) > PROFILE_ROW_CAP:
        df = df.head(PROFILE_ROW_CAP)
    checked_rows = int(len(df)) if df is not None else 0

    quality_score = 100.0
    null_rate = None
    worst_null_column = None
    duplicate_pct = None
    completeness = None
    if df is not None and len(df) and len(df.columns):
        prof = profile_df(df)
        cols = prof.get("columns", [])
        cells = sum(c["non_null"] + c["nulls"] for c in cols) or 1
        nulls = sum(c["nulls"] for c in cols)
        null_rate = round(nulls / cells * 100, 2)
        worst = max(cols, key=lambda c: c["null_pct"]) if cols else None
        if worst and worst["null_pct"]:
            worst_null_column = {"column": worst["name"], "null_pct": worst["null_pct"]}
        dupes = int(prof.get("duplicate_rows") or 0)
        duplicate_pct = round(dupes / len(df) * 100, 2)
        completeness = prof.get("completeness_pct")
        # score: completeness dominates, duplicates subtract
        quality_score = float(completeness or 100.0)
        quality_score -= min(quality_score, duplicate_pct * 2)
        quality_score = round(max(0.0, quality_score), 1)
    elif df is None:
        # empty dataset: nothing to score yet - neutral, not broken
        quality_score = 100.0

    # --- contract ----------------------------------------------------------
    row = await contract_svc.get_contract(db, ds.id)
    contract = contract_svc.contract_report(row)
    contract["ok"] = None
    contract["violations"] = []
    if row is not None and df is not None and len(df):
        report = contract_svc.check_rows(
            df.to_dict(orient="records"), row.columns_json or []
        )
        contract["ok"] = report["ok"]
        contract["violations"] = report["violations"]
        if not report["ok"]:
            quality_score = round(quality_score * 0.6, 1)
    schema_score = 100.0 if (row is None or contract["ok"] is not False) else 40.0

    score = round(
        quality_score * 0.45 + freshness_score * 0.2 + schema_score * 0.2 + volume_score * 0.15,
        1,
    )
    return {
        "dataset_id": ds.id,
        "name": ds.name,
        "status": _grade(score),
        "score": score,
        "checked_rows": checked_rows,
        "freshness": {
            "last_write_at": last_write.isoformat(),
            "age_seconds": round(age_seconds, 1),
            "age_minutes": round(age_seconds / 60, 1),
            "tier": tier,
        },
        "volume": {
            "rows": rows,
            "previous_rows": prev_rows,
            "delta": delta,
            "delta_pct": delta_pct,
            "versions": len(versions),
        },
        "schema": {
            "columns": len(ds.schema_json or []),
            "contract_present": contract["present"],
            "contract_ok": contract["ok"],
            "contract_violations": contract["violations"],
            "contract_version": contract.get("version", 0),
        },
        "quality": {
            "score": quality_score,
            "null_rate_pct": null_rate,
            "worst_null_column": worst_null_column,
            "duplicate_rows_pct": duplicate_pct,
            "completeness_pct": completeness,
        },
        "signals": {
            "fresh": tier == "fresh",
            "schema_valid": contract["ok"] is not False,
            "no_volume_shock": volume_score >= 70.0,
        },
    }
