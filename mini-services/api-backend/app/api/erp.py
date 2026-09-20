"""ERP reports API (v114) - the books speak.

The ledgers the ERP operator's reactive posters write are datasets; this
door turns the GL entries dataset into the reports a company actually
reads:

* the TRIAL BALANCE - one row per account with its total debits, total
  credits and its balance (debit-normal for assets and expenses,
  credit-normal for liabilities and the rest), plus the grand totals and
  the ``balanced`` flag that proves the posters kept their side of the
  bargain;
* the INCOME summary - revenue accounts vs expense accounts = the net
  income the books currently speak.

v115: the PURCHASE side joined the ledger, so the kinds map carries
Inventory (asset) and Accounts payable (liability) - the trial balance
now shows what the company OWES, not just what it owns and earned.

v116: the books get a FACE - the response carries a per-kind ``summary``
(asset / liability / equity / revenue / expense / memo / other buckets,
each with its account count and net total, always all seven in a fixed
order) so the console can draw the balance sheet against the income
statement without re-deriving the kinds client-side.

v117: the statements SPEAK - GET /erp/statements shapes the same book
into the two documents a company closes with: the INCOME STATEMENT
(revenue lines, expense lines, the net) and the BALANCE SHEET (asset,
liability and equity lines, retained earnings implied by the period's
net) - and the ACCOUNTING EQUATION check (assets == liabilities + equity
+ retained) that proves the books tie out end to end. Both reports read
the dataset through the SAME one-pass reader, so they can never disagree.

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
    "inventory": "asset",
    "accounts payable": "liability",
    "revenue": "revenue",
    "salary expense": "expense",
    "order desk": "memo",
    "vendor desk": "memo",
    # v117: the classic equity accounts - the posters never post them, but
    # a hand-kept book can, and the balance sheet's equation check reads
    # them instead of crying "does not tie out"
    "owner's equity": "equity",
    "owner equity": "equity",
    "retained earnings": "equity",
    "common stock": "equity",
    "paid-in capital": "equity",
}
_DEBIT_NORMAL = {"asset", "expense"}

# the buckets the summary always carries, in reading order - the balance
# sheet (assets, liabilities, equity) against the income statement
# (revenue, expenses), then the desk memos and anything unclassified
_KIND_ORDER = ("asset", "liability", "equity", "revenue", "expense",
               "memo", "other")
_KIND_SET = set(_KIND_ORDER)


def _num(value) -> float:
    """The datasets hold strings ('720.00') - read them honestly."""
    try:
        return float(str(value or "").strip() or 0.0)
    except (TypeError, ValueError):
        return 0.0


async def _book_dataset(dataset_id: str, user, db: AsyncSession):
    """Resolve the report's dataset by id or case-insensitive name -
    ownership honored like every dataset door, unknown is a loud 404."""
    ds = await ds_svc.get_dataset(db, dataset_id,
                                  owner_id=user.id if user else None)
    if ds is None:
        raise HTTPException(status_code=404,
                            detail=f"dataset {dataset_id!r} not found")
    return ds


def _read_book(ds) -> tuple[dict[str, dict], int]:
    """One pass over the dataset's rows into per-account totals - the
    shared reader under both reports, so they can never disagree."""
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
    return accounts, line_count


def _book_rows(accounts: dict[str, dict]) -> list[dict]:
    """One row per account with its kind and its normal-side balance."""
    out = []
    for name in sorted(accounts):
        acc = accounts[name]
        kind = _ACCOUNT_KINDS.get(name.lower(), "other")
        if kind in _DEBIT_NORMAL:
            balance = round(acc["debits"] - acc["credits"], 2)
        elif kind == "memo":
            balance = 0.0
        else:
            balance = round(acc["credits"] - acc["debits"], 2)
        out.append({
            "account": name,
            "kind": kind,
            "debits": acc["debits"],
            "credits": acc["credits"],
            "balance": balance,
        })
    return out


@router.get("/trial-balance")
async def trial_balance(
    dataset_id: str = Query(
        ..., description="the GL entries dataset (id or case-insensitive name)"),
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """The trial balance + income summary over one GL entries dataset."""
    ds = await _book_dataset(dataset_id, user, db)
    accounts, line_count = _read_book(ds)
    out_rows = _book_rows(accounts)

    tot_debits = tot_credits = 0.0
    revenue = expenses = 0.0
    summary = {kind: {"accounts": 0, "total": 0.0}
               for kind in _KIND_ORDER}
    for r in out_rows:
        if r["kind"] == "revenue":
            revenue = round(revenue + r["balance"], 2)
        elif r["kind"] == "expense":
            expenses = round(expenses + r["balance"], 2)
        bucket = summary[r["kind"]]
        bucket["accounts"] += 1
        bucket["total"] = round(bucket["total"] + r["balance"], 2)
        tot_debits = round(tot_debits + r["debits"], 2)
        tot_credits = round(tot_credits + r["credits"], 2)

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
        "summary": [
            {"kind": kind, "accounts": summary[kind]["accounts"],
             "total": round(summary[kind]["total"], 2)}
            for kind in _KIND_ORDER
        ],
        "line_count": line_count,
    }


@router.get("/statements")
async def statements(
    dataset_id: str = Query(
        ..., description="the GL entries dataset (id or case-insensitive name)"),
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """The income statement + the balance sheet over one GL entries dataset.

    The same book the trial balance reads, shaped into the two documents
    a company closes with - and the accounting equation check: assets ==
    liabilities + equity + the period's retained earnings.
    """
    ds = await _book_dataset(dataset_id, user, db)
    accounts, line_count = _read_book(ds)
    rows = _book_rows(accounts)

    def _lines(kind: str) -> list[dict]:
        return [{"account": r["account"], "balance": r["balance"]}
                for r in rows if r["kind"] == kind]

    revenue_lines = _lines("revenue")
    expense_lines = _lines("expense")
    asset_lines = _lines("asset")
    liability_lines = _lines("liability")
    equity_lines = _lines("equity")
    memo_lines = [{"account": r["account"], "balance": r["balance"]}
                  for r in rows if r["kind"] in ("memo", "other")]

    total_revenue = round(sum(r["balance"] for r in revenue_lines), 2)
    total_expenses = round(sum(r["balance"] for r in expense_lines), 2)
    net = round(total_revenue - total_expenses, 2)
    total_assets = round(sum(r["balance"] for r in asset_lines), 2)
    total_liabilities = round(sum(r["balance"] for r in liability_lines), 2)
    total_equity = round(sum(r["balance"] for r in equity_lines), 2)
    # the period's earnings stay in the company (no close entries exist
    # yet), so the balance sheet's retained earnings IS the net - the
    # equation check reads assets == liabilities + equity + retained
    retained = net
    equation_side = round(total_liabilities + total_equity + net, 2)

    return {
        "dataset": {"id": ds.id, "name": ds.name},
        "income": {
            "revenue": revenue_lines,
            "expenses": expense_lines,
            "total_revenue": total_revenue,
            "total_expenses": total_expenses,
            "net": net,
        },
        "balance": {
            "assets": asset_lines,
            "liabilities": liability_lines,
            "equity": equity_lines,
            "memos": memo_lines,
            "total_assets": total_assets,
            "total_liabilities": total_liabilities,
            "total_equity": total_equity,
            "retained_earnings": retained,
            "equation_side": equation_side,
            "balanced": abs(total_assets - equation_side) < 0.005,
        },
        "line_count": line_count,
    }
