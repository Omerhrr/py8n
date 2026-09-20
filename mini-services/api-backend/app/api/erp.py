"""ERP reports API (v114) - the books speak.

The ledgers the ERP operator's reactive posters write are datasets; this
door turns the GL entries dataset into the reports a company actually
reads:

* the TRIAL BALANCE - one row per account with its total debits, total
  credits and its balance (debit-normal for assets and expenses,
  credit-normal for the rest), plus the grand totals and the ``balanced``
  flag that proves the posters kept their side of the bargain;
* the INCOME summary - revenue accounts vs expense accounts = the net
  income the books currently speak.

The dataset is the caller's to name: the console passes the GL entries
id it already holds, an agent or a dashboard names it (id or
case-insensitive name). Resolution honors ownership like every dataset
door; an unknown or foreign dataset is a loud 404.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..services import datasets as ds_svc
from .auth import get_optional_user

router = APIRouter(prefix="/erp", tags=["erp"])

# the account kinds the shelf posts against - the balance's normal side
# follows the kind (assets and expenses carry a debit balance, the rest a
# credit one); anything the posters invent beyond this map is 'other'
_ACCOUNT_KINDS: dict[str, str] = {
    "accounts receivable": "asset",
    "cash": "asset",
    "revenue": "revenue",
    "salary expense": "expense",
    "order desk": "memo",
}
_DEBIT_NORMAL = {"asset", "expense"}


def _num(value) -> float:
    """The datasets hold strings ('720.00') - read them honestly."""
    try:
        return float(str(value or "").strip() or 0.0)
    except (TypeError, ValueError):
        return 0.0


@router.get("/trial-balance")
async def trial_balance(
    dataset_id: str = Query(
        ..., description="the GL entries dataset (id or case-insensitive name)"),
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """The trial balance + income summary over one GL entries dataset."""
    ds = await ds_svc.get_dataset(db, dataset_id,
                                  owner_id=user.id if user else None)
    if ds is None:
        raise HTTPException(status_code=404,
                            detail=f"dataset {dataset_id!r} not found")
    df = ds_svc.read_parquet_df(ds_svc.parquet_path(ds.id))
    rows = ds_svc.jsonable_rows(df)

    accounts: dict[str, dict] = {}
    line_count = 0
    for r in rows:
        account = str(r.get("account") or "").strip()
        debit = _num(r.get("debit"))
        credit = _num(r.get("credit"))
        if not account:
            account = "(unassigned)"
        line_count += 1
        acc = accounts.setdefault(
            account, {"account": account, "debits": 0.0, "credits": 0.0})
        acc["debits"] = round(acc["debits"] + debit, 2)
        acc["credits"] = round(acc["credits"] + credit, 2)

    out_rows = []
    tot_debits = tot_credits = 0.0
    revenue = expenses = 0.0
    for name in sorted(accounts):
        acc = accounts[name]
        kind = _ACCOUNT_KINDS.get(name.lower(), "other")
        if kind in _DEBIT_NORMAL:
            balance = round(acc["debits"] - acc["credits"], 2)
        elif kind == "memo":
            balance = 0.0
        else:
            balance = round(acc["credits"] - acc["debits"], 2)
        if kind == "revenue":
            revenue = round(revenue + balance, 2)
        elif kind == "expense":
            expenses = round(expenses + balance, 2)
        tot_debits = round(tot_debits + acc["debits"], 2)
        tot_credits = round(tot_credits + acc["credits"], 2)
        out_rows.append({
            "account": name,
            "kind": kind,
            "debits": acc["debits"],
            "credits": acc["credits"],
            "balance": balance,
        })

    return {
        "dataset": {"id": ds.id, "name": ds.name},
        "rows": out_rows,
        "totals": {
            "debits": tot_debits,
            "credits": tot_credits,
            "balanced": abs(tot_debits - tot_credits) < 0.005,
        },
        "income": {
            "revenue": revenue,
            "expenses": expenses,
            "net": round(revenue - expenses, 2),
        },
        "line_count": line_count,
    }
