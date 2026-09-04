"""Data contracts (v50) - declarative, persistent schema enforcement.

A contract is the schema a dataset PROMISES: per column, a dtype, a
nullability flag and an optional allowed-value domain. Contracts are
checked at WRITE time (dataset_write node, rows API) BEFORE rows land:

* ``on_violation="error"``       -> the write raises (pipeline hard-stop);
* ``on_violation="warn"``        -> the write proceeds with a violations report;
* ``on_violation="dead_letter"`` -> (v67) failing rows are QUARANTINED into a
  dead-letter dataset (stamped ``_dl_reasons`` / ``_dl_at``) and only passing
  rows land - the medallion architecture's reject lane.

Checking is CASTABILITY-based: ``"12"`` satisfies an integer column and
``"true"`` satisfies a boolean column, because stringly-typed payloads
(HTTP, OCR, sheets) are the norm and the dataset engine normalizes types
on write anyway. What fails is data that could never be the promised
type (``"abc"`` as integer) or outside the allowed domain.

This module is engine-independent (plain dicts in / dicts out) so both
the FastAPI layer and the node layer can call it without ceremony.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Dataset, DatasetContract, DatasetContractRevision

CONTRACT_DTYPES = ("text", "integer", "number", "boolean", "datetime")

_DT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$"
)


class ContractError(ValueError):
    """Raised when a contract definition itself is invalid (API 400)."""


def validate_contract_def(columns: list, on_violation: str = "warn") -> list[dict]:
    """Validate a contract definition; returns the normalized columns.

    Raises ContractError (-> API 400) when the definition is malformed:
    unknown dtype, empty/duplicate names, non-list allowed values.
    """
    if on_violation not in ("warn", "error", "dead_letter"):
        raise ContractError("on_violation must be 'warn', 'error' or 'dead_letter'")
    if not isinstance(columns, list) or not columns:
        raise ContractError("a contract needs at least one column definition")
    out: list[dict] = []
    seen: set[str] = set()
    for col in columns:
        if not isinstance(col, dict):
            raise ContractError("each column must be an object with a name")
        name = str(col.get("name") or "").strip()
        if not name:
            raise ContractError("each column needs a non-empty name")
        if name in seen:
            raise ContractError(f"duplicate contract column {name!r}")
        seen.add(name)
        dtype = str(col.get("dtype") or "text").strip().lower()
        if dtype not in CONTRACT_DTYPES:
            raise ContractError(
                f"column {name!r}: dtype must be one of {', '.join(CONTRACT_DTYPES)}"
            )
        entry: dict = {"name": name, "dtype": dtype}
        entry["nullable"] = bool(col.get("nullable", True))
        allowed = col.get("allowed")
        if allowed is not None:
            if not isinstance(allowed, list) or not allowed:
                raise ContractError(
                    f"column {name!r}: allowed must be a non-empty list of values"
                )
            entry["allowed"] = [str(v) if isinstance(v, (dict, list)) else v for v in allowed]
        out.append(entry)
    return out


def _castable(value, dtype: str) -> bool:
    """Could ``value`` be this dtype (after the engine's own normalization)?"""
    if value is None:
        return True
    if isinstance(value, bool):
        return dtype in ("boolean", "text")
    if isinstance(value, (int, float)):
        if isinstance(value, int):
            return dtype in ("integer", "number", "text")
        return dtype in ("number", "text")
    s = str(value).strip()
    if s == "":
        return True  # blanks behave like null for nullability purposes
    if dtype == "text":
        return True
    if dtype == "integer":
        try:
            int(s)
            return True
        except ValueError:
            return False
    if dtype == "number":
        try:
            float(s)
            return True
        except ValueError:
            return False
    if dtype == "boolean":
        return s.lower() in ("true", "false", "1", "0", "yes", "no", "t", "f", "y", "n")
    if dtype == "datetime":
        if _DT_RE.match(s):
            return True
        try:
            datetime.fromisoformat(s)
            return True
        except ValueError:
            return False
    return False


def check_rows(rows: list[dict], columns: list[dict], max_samples: int = 5) -> dict:
    """Check JSON rows against contract columns; returns a violations report.

    Report shape::

        {"ok": bool, "checked_rows": n, "checked_columns": n,
         "violations": [{"column", "rule", "count", "samples"}]}

    Rules: ``missing_column`` (declared column absent from the payload),
    ``not_null`` (null in a non-nullable column), ``dtype`` (not castable),
    ``allowed`` (outside the declared domain).
    """
    violations: list[dict] = []

    def _record(column: str, rule: str, bad: list) -> None:
        violations.append({
            "column": column,
            "rule": rule,
            "count": len(bad),
            "samples": [str(v)[:80] for v in bad[:max_samples]],
        })

    for col in columns:
        name = col["name"]
        present = [r for r in rows if isinstance(r, dict) and name in r]
        if rows and not present:
            _record(name, "missing_column", ["(column absent from payload)"])
            continue
        if not col.get("nullable", True):
            # absent keys count as nulls - a row without the key has no value
            bad = [
                r.get(name) for r in rows
                if isinstance(r, dict) and (r.get(name) is None or str(r.get(name) or "").strip() == "")
            ]
            if bad:
                _record(name, "not_null", bad)
        bad_dtype = [r.get(name) for r in present if not _castable(r.get(name), col["dtype"])]
        if bad_dtype:
            _record(name, "dtype", bad_dtype)
        allowed = col.get("allowed")
        if allowed:
            norm = {str(v) for v in allowed}
            bad = [
                r.get(name)
                for r in present
                if r.get(name) is not None
                and str(r.get(name)) not in norm
                and str(r.get(name)).strip() != ""
            ]
            if bad:
                _record(name, "allowed", bad)

    return {
        "ok": not violations,
        "checked_rows": len(rows),
        "checked_columns": len(columns),
        "violations": violations,
    }


def split_rows_dead_letter(rows: list[dict], columns: list[dict]) -> tuple[list[dict], list[dict]]:
    """v67 dead-letter routing: evaluate EVERY row against the contract
    individually and split the batch into passing and failing rows.

    Per-row rules mirror ``check_rows`` (not_null incl. absent keys, dtype
    castability, allowed domain); a column absent from the WHOLE payload
    (``missing_column``) marks every row bad - the shape itself is broken.
    Failing rows come back as copies stamped with ``_dl_reasons`` (the
    ``column:rule`` list) and ``_dl_at`` (ISO timestamp) so the quarantine
    dataset is self-describing. Pure function - the caller owns the writes.
    """
    good: list[dict] = []
    bad: list[dict] = []
    stamped_at = datetime.now(timezone.utc).isoformat()
    for row in rows:
        if not isinstance(row, dict):  # defensive: callers pre-filter non-dicts
            bad.append({"_dl_reasons": ["row:not_an_object"], "_dl_at": stamped_at, "row": row})
            continue
        reasons: list[str] = []
        for col in columns:
            name = col["name"]
            if name not in row:
                if not col.get("nullable", True):
                    reasons.append(f"{name}:not_null")
                continue  # absent key on a nullable column is fine
            val = row.get(name)
            if col.get("allowed"):
                norm = {str(v) for v in col["allowed"]}
                if val is not None and str(val).strip() != "" and str(val) not in norm:
                    reasons.append(f"{name}:allowed")
            if not col.get("nullable", True) and (val is None or str(val or "").strip() == ""):
                reasons.append(f"{name}:not_null")
                continue
            if not _castable(val, col["dtype"]):
                reasons.append(f"{name}:dtype")
        if reasons:
            quarantined = dict(row)
            quarantined["_dl_reasons"] = reasons
            quarantined["_dl_at"] = stamped_at
            bad.append(quarantined)
        else:
            good.append(row)
    return good, bad


async def get_contract(db: AsyncSession, dataset_id: str) -> DatasetContract | None:
    return (
        await db.execute(
            select(DatasetContract).where(DatasetContract.dataset_id == dataset_id)
        )
    ).scalar_one_or_none()


async def put_contract(
    db: AsyncSession,
    ds: Dataset,
    columns: list,
    on_violation: str = "warn",
) -> DatasetContract:
    """Create or replace the dataset's contract (version bumps on replace).

    v54: the OUTGOING state is snapshotted into ``dataset_contract_revisions``
    before it is overwritten, so the promise trail survives every edit and a
    diff between any two versions is always available.
    """
    normalized = validate_contract_def(columns, on_violation)
    row = await get_contract(db, ds.id)
    if row is None:
        row = DatasetContract(dataset_id=ds.id, owner_id=ds.owner_id)
        db.add(row)
    else:
        await snapshot_revision(db, ds, row, note=f"superseded by v{int(row.version or 1) + 1}")
        row.version = int(row.version or 1) + 1
    row.columns_json = normalized
    row.on_violation = on_violation
    await db.flush()
    return row


MAX_CONTRACT_REVISIONS = 20  # per dataset; oldest beyond the cap are trimmed


async def snapshot_revision(
    db: AsyncSession, ds: Dataset, contract: DatasetContract, note: str = ""
) -> DatasetContractRevision | None:
    """Preserve one contract state in the revision history (v54).

    A no-op when the contract carries no columns (never persisted) or when
    an identical revision already exists (double-snapshot guard). Trims the
    oldest revisions beyond :data:`MAX_CONTRACT_REVISIONS`.
    """
    cols = contract.columns_json or []
    if not cols:
        return None
    existing = (
        await db.execute(
            select(DatasetContractRevision)
            .where(
                DatasetContractRevision.dataset_id == ds.id,
                DatasetContractRevision.version == int(contract.version or 1),
            )
        )
    ).scalars().first()
    if existing is not None:
        return None  # same version already captured - nothing to add
    rev = DatasetContractRevision(
        dataset_id=ds.id,
        owner_id=ds.owner_id,
        version=int(contract.version or 1),
        columns_json=cols,
        on_violation=contract.on_violation or "warn",
        note=note[:200],
    )
    db.add(rev)
    await db.flush()
    stale = (
        (
            await db.execute(
                select(DatasetContractRevision)
                .where(DatasetContractRevision.dataset_id == ds.id)
                .order_by(DatasetContractRevision.version.desc(), DatasetContractRevision.created_at.desc())
                .offset(MAX_CONTRACT_REVISIONS)
            )
        )
        .scalars()
        .all()
    )
    for old in stale:
        await db.delete(old)
    return rev


async def delete_contract(db: AsyncSession, ds: Dataset) -> bool:
    """Remove the dataset's contract, snapshotting the final state first.

    Returns True when a contract existed. The revision trail keeps the
    removed promise (note='contract removed') so history survives deletion.
    """
    row = await get_contract(db, ds.id)
    if row is None:
        return False
    await snapshot_revision(db, ds, row, note="contract removed")
    await db.execute(sa_delete(DatasetContract).where(DatasetContract.dataset_id == ds.id))
    await db.flush()
    return True


async def list_revisions(db: AsyncSession, dataset_id: str) -> list[DatasetContractRevision]:
    """The dataset's contract history, newest version first."""
    return (
        (
            await db.execute(
                select(DatasetContractRevision)
                .where(DatasetContractRevision.dataset_id == dataset_id)
                .order_by(DatasetContractRevision.version.desc())
            )
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------- diff (v54)


def _allowed_equal(a: list | None, b: list | None) -> bool:
    """Allowed-value domains compare as sets (order is presentation)."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return {str(v) for v in a} == {str(v) for v in b}


def diff_contract_columns(old: list[dict], new: list[dict]) -> dict:
    """Two contract column lists -> a human-readable change report.

    Shape::

        {"added": [col], "removed": [col],
         "changed": [{"name", "field", "old", "new"}],
         "same": [names], "summary": "1 added, 2 removed, 1 changed"}

    ``changed`` walks dtype / nullable / allowed per column, so a reviewer
    sees exactly which promise loosened or tightened.
    """
    old_by = {c["name"]: c for c in (old or []) if isinstance(c, dict) and c.get("name")}
    new_by = {c["name"]: c for c in (new or []) if isinstance(c, dict) and c.get("name")}
    added = [new_by[n] for n in new_by if n not in old_by]
    removed = [old_by[n] for n in old_by if n not in new_by]
    changed: list[dict] = []
    same: list[str] = []
    for name, nc in new_by.items():
        oc = old_by.get(name)
        if oc is None:
            continue
        if nc.get("dtype") != oc.get("dtype"):
            changed.append({"name": name, "field": "dtype", "old": oc.get("dtype"), "new": nc.get("dtype")})
        if bool(nc.get("nullable", True)) != bool(oc.get("nullable", True)):
            changed.append({"name": name, "field": "nullable", "old": bool(oc.get("nullable", True)), "new": bool(nc.get("nullable", True))})
        if not _allowed_equal(oc.get("allowed"), nc.get("allowed")):
            changed.append({
                "name": name, "field": "allowed",
                "old": oc.get("allowed"), "new": nc.get("allowed"),
            })
        if not any(ch["name"] == name for ch in changed):
            same.append(name)
    parts = []
    if added:
        parts.append(f"{len(added)} added")
    if removed:
        parts.append(f"{len(removed)} removed")
    if changed:
        parts.append(f"{len(changed)} changed")
    summary = ", ".join(parts) if parts else "no changes"
    return {"added": added, "removed": removed, "changed": changed, "same": same, "summary": summary}


def contract_report(row: DatasetContract | None) -> dict:
    """JSON shape of a contract row (or the 'no contract' stub)."""
    if row is None:
        return {"present": False, "columns": [], "on_violation": None, "version": 0}
    return {
        "present": True,
        "columns": row.columns_json or [],
        "on_violation": row.on_violation,
        "version": int(row.version or 1),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def enforce_on_rows(
    db: AsyncSession, ds: Dataset, rows: list[dict], context: str = "write"
) -> dict | None:
    """Check ``rows`` against the dataset's contract; enforce on_violation.

    Returns the violations report (falsy dict ``{"ok": True}`` dict when a
    contract exists and the rows pass; ``None`` when no contract exists).
    Raises ``ContractViolation`` on an error-mode violation - the caller
    turns that into a node failure or a 422.
    """
    row = await get_contract(db, ds.id)
    if row is None or not rows:
        return None
    report = check_rows(rows, row.columns_json or [])
    report["on_violation"] = row.on_violation
    report["contract_version"] = int(row.version or 1)
    if not report["ok"] and row.on_violation == "error":
        top = ", ".join(f"{v['column']}:{v['rule']}x{v['count']}" for v in report["violations"][:3])
        raise ContractViolation(
            f"data contract violated during {context} (on_violation=error): {top}"
        )
    return report


class ContractViolation(ValueError):
    """A write violated an error-mode contract (node failure / API 422)."""
