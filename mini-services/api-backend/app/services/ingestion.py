"""Incremental ingestion cursors (v50, deepened in v53) - the checkpoint primitive.

One ``IngestionState`` row per (dataset_id, key) remembers how far a
pipeline has read into a source. ``dataset_write`` in incremental mode
filters incoming rows down to those strictly beyond the stored watermark
before appending, then advances the mark to the best value seen - the
CDC checkpoint pattern (``WHERE last_updated > {{ checkpoint }}``)
without requiring the source system to remember anything.

v53 deepening:
* ``lookback`` rewinds the comparison baseline (numeric units or ISO
  seconds) so late-arriving rows near the cursor are re-admitted -
  pair it with ``mode=upsert`` + ``key_columns`` so re-admitted rows
  MERGE on key instead of duplicating (the incremental-upsert combo).
* every run records what it did (rows in/written/skipped/updated/
  inserted) on the state row (``stats_json``), so the ingestion
  surface shows behaviour, not just a cursor position.
* an empty payload is a clean no-op (scheduled sources legitimately
  return nothing); only a NON-empty payload missing the cursor column
  fails loudly.

Watermark comparison rules (pragmatic, no magic):
* both sides parse as numbers  -> numeric comparison;
* otherwise                    -> string comparison (ISO timestamps and
  zero-padded text both sort correctly; arbitrary text falls back to
  lexicographic order, which is still stable and resumable).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Dataset, IngestionState

RESERVED_KEY_PREFIX = "trigger:"  # dataset_trigger cursors live here too


def state_key(node_key: str | None) -> str:
    key = (node_key or "default").strip() or "default"
    return key[:200]


def watermark_gt(candidate: str | None, current: str | None) -> bool:
    """True when ``candidate`` is strictly beyond ``current``."""
    if candidate is None:
        return False
    if current is None:
        return True
    try:
        return float(candidate) > float(current)
    except (TypeError, ValueError):
        pass
    return str(candidate) > str(current)


def best_watermark(a: str | None, b: str | None) -> str | None:
    """The further-along of two watermark values (None-safe)."""
    if watermark_gt(b or "", a or ""):
        return b
    return a


def rewind_watermark(current: str | None, lookback: float) -> str | None:
    """Move the comparison baseline BACK by ``lookback`` units (v53).

    The lookback window re-admits rows near the cursor so late-arriving
    data is not skipped forever - pair it with ``mode=upsert`` so the
    re-admitted rows MERGE instead of duplicate:

    * numeric watermark -> rewound numerically (``100`` - 5 -> ``95.0``);
    * ISO-datetime watermark -> rewound by ``lookback`` SECONDS;
    * anything else -> unchanged (arbitrary text cannot rewind).
    """
    if not lookback or lookback <= 0 or current is None:
        return current
    try:
        return str(float(current) - float(lookback))
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(str(current).replace("Z", "+00:00"))
        return (dt - timedelta(seconds=float(lookback))).isoformat()
    except (TypeError, ValueError):
        return current


async def get_state(db: AsyncSession, dataset_id: str, key: str) -> IngestionState | None:
    return (
        await db.execute(
            select(IngestionState).where(
                IngestionState.dataset_id == dataset_id,
                IngestionState.key == state_key(key),
            )
        )
    ).scalar_one_or_none()


async def filter_incremental(
    db: AsyncSession,
    ds: Dataset,
    rows: list[dict],
    column: str,
    key: str,
    lookback: float = 0.0,
) -> tuple[list[dict], IngestionState, str | None]:
    """Split ``rows`` down to those beyond the stored watermark.

    Returns ``(fresh_rows, state, checkpoint_before)``. ``state`` is the
    live row (created on first use); the caller writes ``fresh_rows``,
    then calls :func:`advance` with the best watermark it saw. Rows
    missing the watermark column are dropped (counted in the node's
    output) - an unwatermarked row cannot be resumed safely. An EMPTY
    payload is a clean no-op (a scheduled source may legitimately return
    nothing) - only a NON-empty payload missing the cursor column fails.

    ``lookback`` (v53) rewinds the comparison baseline so rows near the
    cursor are re-admitted (see :func:`rewind_watermark`); the stored
    checkpoint itself only ever moves FORWARD.
    """
    st = await get_state(db, ds.id, key)
    if st is None:
        st = IngestionState(dataset_id=ds.id, owner_id=ds.owner_id, key=state_key(key))
        db.add(st)
        await db.flush()
    before = st.watermark
    if not rows:
        return [], st, before
    if column not in rows[0]:
        # watermark column absent: nothing can be safely filtered - treat as
        # a contract of the pipeline and fail loudly instead of double-ingest
        raise ValueError(
            f"watermark column {column!r} not present in the incoming rows"
        )
    baseline = rewind_watermark(before, lookback)
    fresh = []
    seen_values: list[str] = []
    for r in rows:
        raw = r.get(column)
        if raw is None or str(raw).strip() == "":
            continue  # unwatermarked rows cannot be resumed - skip them
        sval = str(raw)
        if watermark_gt(sval, baseline):
            fresh.append(r)
        seen_values.append(sval)
    best = before
    for v in seen_values:
        best = best_watermark(best, v)
    st.watermark = best[:120] if best is not None else None
    return fresh, st, before


async def advance(
    db: AsyncSession,
    st: IngestionState,
    watermark: str | None,
    rows_written: int,
    stats: dict | None = None,
) -> None:
    st.watermark = best_watermark(st.watermark, watermark)[:120] if watermark is not None else st.watermark
    st.runs = int(st.runs or 0) + 1
    st.rows_total = int(st.rows_total or 0) + int(rows_written)
    st.last_run_at = datetime.now(timezone.utc)
    if stats is not None:
        st.stats_json = {k: v for k, v in stats.items() if k in _STAT_KEYS}
    await db.flush()


_STAT_KEYS = frozenset({"mode", "rows_in", "written", "skipped", "updated", "inserted", "lookback"})


def state_out(st: IngestionState) -> dict:
    return {
        "key": st.key,
        "watermark": st.watermark,
        "runs": int(st.runs or 0),
        "rows_total": int(st.rows_total or 0),
        "last_run_at": st.last_run_at.isoformat() if st.last_run_at else None,
        "updated_at": st.updated_at.isoformat() if st.updated_at else None,
        "stats": st.stats_json or None,
    }
