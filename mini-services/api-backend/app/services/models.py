"""Model registry (v46) - versioned ML models behind ``trained_models``.

Every ``model_train`` run can register its fitted pipeline as a NEW VERSION
of a named model; activating a version makes it the one ``model_predict``
scores with (only one active version per name). The artifact holds the
pickle; this table holds the lineage: algorithm, task, target, features,
metrics, dataset link, row count, owner.

v47 adds drift monitoring: at training time we snapshot each feature's
distribution (numeric quantile bins / categorical counts) as REFERENCE
STATS on the registry row; ``score_drift`` compares a candidate batch
against that reference with the Population Stability Index (PSI), the
classic production drift metric (< 0.1 stable, 0.1-0.25 moderate,
> 0.25 significant). The same code path backs the ``drift_check`` node
and the ``GET /models/{ref}/drift`` endpoint, so canvas gates and the
registry page can never disagree.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Artifact, TrainedModel


async def next_version(db: AsyncSession, name: str) -> int:
    last = (
        await db.execute(
            select(TrainedModel.version).where(TrainedModel.name == name).order_by(TrainedModel.version.desc()).limit(1)
        )
    ).scalar_one_or_none()
    return int(last or 0) + 1


async def deactivate_others(db: AsyncSession, name: str, keep_id: str | None = None) -> None:
    rows = (
        (await db.execute(select(TrainedModel).where(TrainedModel.name == name)))
        .scalars()
        .all()
    )
    for row in rows:
        if row.id != keep_id:
            row.active = False
            db.add(row)


async def register_model(
    db: AsyncSession,
    *,
    name: str,
    algorithm: str,
    task: str,
    target: str,
    features: list[str],
    metrics: dict,
    artifact_id: str | None,
    owner_id: str | None = None,
    dataset_name: str | None = None,
    row_count: int = 0,
    activate: bool = True,
    reference_stats: dict | None = None,
) -> TrainedModel:
    """Create the next version of ``name``; first version auto-activates."""
    row = TrainedModel(
        name=name,
        version=await next_version(db, name),
        algorithm=algorithm,
        task=task,
        target=target,
        features=features,
        metrics=metrics or {},
        reference_stats=reference_stats or {},
        artifact_id=artifact_id,
        dataset_name=dataset_name,
        row_count=row_count,
        owner_id=owner_id,
    )
    row.active = True  # first version of a name is active by definition
    db.add(row)
    await db.flush()
    if activate:
        await deactivate_others(db, name, keep_id=row.id)
    return row


async def get_model(db: AsyncSession, model_id: str) -> TrainedModel | None:
    return await db.get(TrainedModel, model_id)


async def resolve_model(db: AsyncSession, ref: str, owner_id: str | None = None) -> TrainedModel | None:
    """Resolve by registry id first, then by name → ACTIVE version.

    Owner scoping matches the dataset service: another owner's claimed
    model is treated as not found.
    """
    row = await db.get(TrainedModel, ref)
    if row is None:
        # name → active version (newest created active row wins ties)
        q = select(TrainedModel).where(TrainedModel.name == ref.strip(), TrainedModel.active.is_(True))
        row = (await db.execute(q.order_by(TrainedModel.version.desc()))).scalars().first()
    if row is None:
        return None
    if owner_id is not None and row.owner_id is not None and row.owner_id != owner_id:
        return None
    return row


async def list_models(db: AsyncSession, owner_id: str | None = None) -> list[TrainedModel]:
    q = select(TrainedModel).order_by(TrainedModel.name, TrainedModel.version.desc())
    rows = (await db.execute(q)).scalars().all()
    if owner_id is not None:
        rows = [r for r in rows if r.owner_id is None or r.owner_id == owner_id]
    return rows


async def activate_version(db: AsyncSession, row: TrainedModel) -> TrainedModel:
    await deactivate_others(db, row.name, keep_id=row.id)
    row.active = True
    db.add(row)
    await db.flush()
    return row


async def delete_model(db: AsyncSession, row: TrainedModel, *, delete_artifact: bool = True) -> None:
    """Drop the registry row; the artifact (and its file) dies with the last
    reference so orphaned pickles never accumulate."""
    artifact_id = row.artifact_id
    await db.delete(row)
    await db.flush()
    if delete_artifact and artifact_id:
        artifact = await db.get(Artifact, artifact_id)
        if artifact is not None:
            from . import artifacts as art_svc

            art_svc.delete_file(artifact)
            await db.delete(artifact)


def model_out(row: TrainedModel) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "version": row.version,
        "algorithm": row.algorithm,
        "task": row.task,
        "target": row.target,
        "features": row.features or [],
        "metrics": row.metrics or {},
        "reference_stats": row.reference_stats or {},
        "has_reference_stats": bool(row.reference_stats),
        "artifact_id": row.artifact_id,
        "dataset_name": row.dataset_name,
        "row_count": row.row_count,
        "active": row.active,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


# ----------------------------------------------------------------------
# Drift monitoring (v47) - reference stats + Population Stability Index
# ----------------------------------------------------------------------
PSI_STABLE = 0.10
PSI_MODERATE = 0.25
N_BINS = 5
TOP_CATEGORIES = 20
SMOOTHING = 0.5  # pseudo-count per bucket so ln() never sees zero


def _psi(expected: list[float], actual: list[float]) -> float:
    """PSI between two bucketed distributions (same buckets, both sum ~1).

    Buckets are pre-smoothed by the caller so proportions are never 0.
    """
    return float(sum((a - e) * math.log(a / e) for e, a in zip(expected, actual)))


def _proportions(counts: list[float]) -> list[float]:
    total = sum(counts)
    if total <= 0:
        return [1.0 / len(counts)] * len(counts)
    return [c / total for c in counts]


def compute_reference_stats(df: pd.DataFrame, features: list[str]) -> dict:
    """Snapshot each feature's training distribution for later PSI scoring.

    numeric     -> N_BINS quantile buckets: stored edges + expected proportions
    categorical -> top TOP_CATEGORIES training values (counts) + __other__

    Everything is JSON-serializable by construction (floats/lists/strs).
    """
    stats: dict = {
        "_meta": {
            "n_rows": int(len(df)),
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "psi_bins": N_BINS,
        }
    }
    for col in features:
        if col not in df.columns:
            continue
        series = df[col]
        if pd.api.types.is_numeric_dtype(series):
            values = pd.to_numeric(series, errors="coerce").dropna()
            if values.empty:
                continue
            qs = [float(values.quantile(i / N_BINS)) for i in range(N_BINS + 1)]
            edges = sorted(set(qs))
            if len(edges) < 2:
                # constant column - a single bucket at 100%; scoring treats
                # any batch value != c as drift via the two-bucket path below
                stats[col] = {"type": "numeric", "constant": edges[0], "expected": [1.0], "n": int(values.size)}
                continue
            # expected proportions measured ON the reference itself
            cats = pd.cut(values, bins=edges, include_lowest=True, duplicates="drop")
            counts = cats.value_counts().reindex(cats.cat.categories).fillna(0).tolist()
            expected = _proportions([c + SMOOTHING for c in counts])
            stats[col] = {
                "type": "numeric",
                "edges": edges,
                "expected": expected,
                "n": int(values.size),
            }
        else:
            values = series.dropna().astype(str)
            if values.empty:
                continue
            top = values.value_counts().head(TOP_CATEGORIES)
            counts = {str(k): int(v) for k, v in top.items()}
            other = int(values.size - sum(counts.values()))
            if other > 0:
                counts["__other__"] = other
            stats[col] = {"type": "categorical", "counts": counts, "n": int(values.size)}
    return stats


def score_drift(
    reference_stats: dict,
    df: pd.DataFrame,
    features: list[str],
    threshold: float = PSI_MODERATE,
) -> dict:
    """PSI-score a candidate batch against a model's reference stats.

    Returns ``{drift_detected, threshold, overall_psi, max_feature, rows,
    features: [{feature, type, psi, status, missing_in_batch}]}``.

    A feature counts as missing when the batch lacks the column or every
    value is null - missing features flag drift (they break prediction too).
    """
    per_feature: list[dict] = []
    for col in features:
        ref = reference_stats.get(col)
        if not ref:
            continue  # feature trained before stats existed - skip silently
        if col not in df.columns:
            per_feature.append({"feature": col, "type": ref.get("type"), "psi": None, "status": "missing", "missing_in_batch": True})
            continue
        series = df[col]
        if ref.get("type") == "numeric":
            values = pd.to_numeric(series, errors="coerce").dropna()
            if values.empty:
                per_feature.append({"feature": col, "type": "numeric", "psi": None, "status": "missing", "missing_in_batch": True})
                continue
            if "constant" in ref:
                # reference was a constant c - score against {== c, != c}
                c = float(ref["constant"])
                expected = _proportions([1.0 * float(ref.get("n") or 1) + SMOOTHING, SMOOTHING])
                eq = int((values == c).sum())
                actual = _proportions([eq + SMOOTHING, int(values.size - eq) + SMOOTHING])
                psi = _psi(expected, actual)
            else:
                edges = [float(e) for e in ref.get("edges") or []]
                if len(edges) < 2:
                    per_feature.append({"feature": col, "type": "numeric", "psi": None, "status": "missing", "missing_in_batch": True})
                    continue
                # open-ended outer buckets: batch values beyond the training
                # min/max land in the first/last bucket instead of NaN
                score_edges = sorted(set([-math.inf, *edges[1:-1], math.inf]))
                cats = pd.cut(values, bins=score_edges)
                actual_counts = cats.value_counts().reindex(cats.cat.categories).fillna(0).tolist()
                expected_raw = ref.get("expected") or []
                # rebuild expected from raw counts so bucket counts align
                # (duplicates="drop" may have collapsed bins on either side)
                k = min(len(expected_raw), len(actual_counts))
                expected = _proportions([expected_raw[i] * float(ref.get("n") or 1) + SMOOTHING for i in range(k)])
                actual = _proportions([actual_counts[i] + SMOOTHING for i in range(k)])
                psi = _psi(expected, actual)
        else:
            values = series.dropna().astype(str)
            if values.empty:
                per_feature.append({"feature": col, "type": "categorical", "psi": None, "status": "missing", "missing_in_batch": True})
                continue
            ref_counts: dict = ref.get("counts") or {}
            batch_counts = values.value_counts()
            categories = [c for c in ref_counts if c != "__other__"]
            expected_counts = [ref_counts[c] + SMOOTHING for c in categories]
            actual_counts_cat = [int(batch_counts.get(c, 0)) + SMOOTHING for c in categories]
            other_ref = ref_counts.get("__other__", 0)
            other_batch = int(len(values) - sum(int(batch_counts.get(c, 0)) for c in categories))
            if other_ref > 0 or other_batch > 0:
                expected_counts.append(other_ref + SMOOTHING)
                actual_counts_cat.append(other_batch + SMOOTHING)
            expected = _proportions(expected_counts)
            actual = _proportions(actual_counts_cat)
            psi = _psi(expected, actual)
        status = "stable" if psi < PSI_STABLE else ("moderate" if psi < threshold else "drifted")
        per_feature.append({"feature": col, "type": ref.get("type"), "psi": round(psi, 6), "status": status, "missing_in_batch": False})

    scored = [f["psi"] for f in per_feature if f["psi"] is not None]
    overall = max(scored) if scored else None
    drifted = (overall is not None and overall > threshold) or any(f["status"] == "missing" for f in per_feature)
    max_feature = None
    if scored:
        max_feature = next(f["feature"] for f in per_feature if f["psi"] == overall)
    return {
        "drift_detected": bool(drifted),
        "threshold": threshold,
        "overall_psi": round(overall, 6) if overall is not None else None,
        "max_feature": max_feature,
        "rows": int(len(df)),
        "features": per_feature,
    }
