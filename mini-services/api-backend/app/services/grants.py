"""Row-level share grants (v48) - per-viewer row scoping for published apps.

A grant pairs a token with a row filter, so every viewer holding that token
only ever sees (and, for ``eq`` grants, writes) rows inside the filter's
slice of the bound dataset::

    {"column": "region", "op": "eq",  "value": "eu"}
    {"column": "region", "op": "in",  "value": ["eu", "us"]}
    {"column": "region", "op": "neq", "value": "internal"}

Semantics that matter:

- String-normalized comparison: values cross the JSON boundary, parquet
  dtypes do not always round-trip the way a creator typed them, so both
  sides are compared via ``str()`` (``"5000"`` matches ``5000``).
- Fail closed: a filter whose column vanished from the dataset matches
  NOTHING - a reshaped dataset can never silently leak rows.
- ``eq`` grants stamp the scope column onto every created/submitted record,
  so viewers can only write rows inside their own slice. ``in``/``neq``
  grants are read-only by design (there is no single value to stamp).
"""

from __future__ import annotations

import pandas as pd

GRANT_OPS = ("eq", "in", "neq")


def validate_row_filter(filt: dict | None, schema: list[dict]) -> dict:
    """Normalize + validate a row filter; raises ValueError when unusable."""
    if not isinstance(filt, dict):
        raise ValueError("row_filter must be an object {column, op, value}")
    column = (filt.get("column") or "").strip()
    op = (filt.get("op") or "eq").strip()
    value = filt.get("value")
    if not column:
        raise ValueError("row_filter.column is required")
    known = {c.get("name") for c in (schema or [])}
    if known and column not in known:
        raise ValueError(f"row_filter.column {column!r} is not a column of the bound dataset")
    if op not in GRANT_OPS:
        raise ValueError(f"row_filter.op must be one of {', '.join(GRANT_OPS)}")
    if op in ("eq", "neq"):
        if isinstance(value, (list, dict)) or value is None:
            raise ValueError(f"row_filter.op={op!r} needs a scalar value")
    else:  # in
        if not isinstance(value, list) or not value:
            raise ValueError("row_filter.op='in' needs a non-empty list of values")
        if any(isinstance(v, (list, dict)) for v in value):
            raise ValueError("row_filter.op='in' values must be scalars")
    return {"column": column, "op": op, "value": value}


def _scope_values(filt: dict) -> set[str]:
    raw = filt.get("value")
    if isinstance(raw, list):
        return {str(v) for v in raw}
    return {str(raw)}


def scope_mask(df: pd.DataFrame, filt: dict | None) -> pd.Series:
    """Boolean mask of rows the grant may see (empty frame -> all-False)."""
    if not filt:
        return pd.Series(True, index=df.index)
    col = filt.get("column")
    if col not in df.columns:
        return pd.Series(False, index=df.index)  # fail closed
    values = _scope_values(filt)
    norm = df[col].astype("object").where(df[col].notna(), None).map(
        lambda v: None if v is None else str(v)
    )
    if filt.get("op") == "neq":
        mask = norm.map(lambda v: v is not None and v not in values)
    else:  # eq | in - the same set logic, eq is just a one-element set
        mask = norm.map(lambda v: v is not None and v in values)
    return mask


def apply_scope(df: pd.DataFrame, filt: dict | None) -> pd.DataFrame:
    """Rows the grant may see; never mutates the input frame."""
    if not filt:
        return df
    return df[scope_mask(df, filt)]


def stamp_record(filt: dict | None, record: dict) -> tuple[dict, str | None]:
    """Force a created record into the grant's slice.

    Returns (record, error). ``eq`` grants stamp the scope value over
    whatever the viewer submitted (the stamp wins); ``in``/``neq`` grants
    cannot create rows at all - there is no single value to stamp.
    """
    if not filt:
        return record, None
    if filt.get("op") != "eq":
        return record, "This share grant is read-only and cannot create records"
    record = {**record, filt["column"]: filt["value"]}
    return record, None
