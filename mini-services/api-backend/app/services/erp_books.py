"""The books service (v118) - every read and every write over a GL entries
dataset lives here, ONCE.

Until v117 the reports logic lived inside ``app/api/erp.py`` as door-local
helpers. v118 adds three more readers (aging, cash flow) and - for the
first time - a WRITE (the period close, which appends real closing entries
to the book and locks it). Write-capable logic cannot live in a door: the
harness's toolchest needs the same close (v119) and it must be the SAME
close, not a parallel path. So the whole library moved here verbatim and
every consumer - the ERP doors, the harness tools, the smokes - reads
through this one reader, so they can never disagree.

Shape of the world:

* a BOOK is a dataset whose rows are journal lines:
  ``{ref, account, debit, credit, memo, at}`` - strings in, strings out;
  ``at`` is an ISO timestamp the posters stamp from the event that
  triggered them (hand-seeded rows may omit it);
* the account kinds map decides the normal side of every balance
  (assets and expenses are debit-normal, the rest credit-normal, memos
  read flat zero, anything unclassified is ``other`` and rides the
  credit-normal side - the door's standing convention since v114).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ErpClose
from . import datasets as ds_svc

# the account kinds the shelf posts against - the balance's normal side
# follows the kind (assets and expenses carry a debit balance, the rest a
# credit one); anything the posters invent beyond this map is 'other'
ACCOUNT_KINDS: dict[str, str] = {
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
    # v118: the close speaks these three - income summary is the temporary
    # account the closing entries sweep through (it always ends at zero,
    # and it is equity for the reading); equipment is a classic investing
    # asset the hand-kept books carry; loan payable is a financing
    # liability (accounts payable stays the operating one)
    "income summary": "equity",
    "equipment": "asset",
    "loan payable": "liability",
}
DEBIT_NORMAL = {"asset", "expense"}

# the buckets the summary always carries, in reading order - the balance
# sheet (assets, liabilities, equity) against the income statement
# (revenue, expenses), then the desk memos and anything unclassified
KIND_ORDER = ("asset", "liability", "equity", "revenue", "expense",
              "memo", "other")
KIND_SET = set(KIND_ORDER)

# the aging buckets, in reading order (v118)
AGE_BUCKETS = ("current", "d31_60", "d61_90", "d90_plus", "undated")

# the cash-flow sections, in reading order (v118) - a stated heuristic:
# anything touching receivables/payables or revenue/expense is OPERATING,
# non-cash assets are INVESTING, equity and the other liabilities are
# FINANCING, the rest lands in OTHER
CF_SECTIONS = ("operating", "investing", "financing", "other")

_INCOME_SUMMARY = "Income summary"
_RETAINED = "Retained earnings"


class BookNotFound(Exception):
    """The dataset the caller named does not exist (or is not theirs)."""


class BookError(Exception):
    """The books refused a move (unbalanced, already closed, empty)."""

    def __init__(self, detail: str, status_code: int = 409):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def num(value) -> float:
    """The datasets hold strings ('720.00') - read them honestly."""
    try:
        return float(str(value or "").strip() or 0.0)
    except (TypeError, ValueError):
        return 0.0


def kind_of(account: str) -> str:
    return ACCOUNT_KINDS.get((account or "").strip().lower(), "other")


async def book_dataset(db: AsyncSession, dataset_ref: str, user):
    """Resolve the book's dataset by id or case-insensitive name -
    ownership honored like every dataset door; unknown is BookNotFound."""
    ds = await ds_svc.get_dataset(db, dataset_ref,
                                  owner_id=user.id if user else None)
    if ds is None:
        raise BookNotFound(f"dataset {dataset_ref!r} not found")
    return ds


def raw_rows(ds) -> list[dict]:
    """The book's journal lines, one pass, strings as they lie."""
    df = ds_svc.read_parquet_df(ds_svc.parquet_path(ds.id))
    return ds_svc.jsonable_rows(df)


def read_book(ds) -> tuple[dict[str, dict], int]:
    """One pass over the dataset's rows into per-account totals - the
    shared reader under every report, so they can never disagree."""
    accounts: dict[str, dict] = {}
    line_count = 0
    for r in raw_rows(ds):
        account = str(r.get("account") or "").strip()
        debit = num(r.get("debit"))
        credit = num(r.get("credit"))
        if not account:
            account = "(unassigned)"
        line_count += 1
        acc = accounts.setdefault(
            account, {"account": account, "debits": 0.0, "credits": 0.0})
        acc["debits"] = round(acc["debits"] + debit, 2)
        acc["credits"] = round(acc["credits"] + credit, 2)
    return accounts, line_count


def book_rows(accounts: dict[str, dict]) -> list[dict]:
    """One row per account with its kind and its normal-side balance."""
    out = []
    for name in sorted(accounts):
        acc = accounts[name]
        kind = kind_of(name)
        if kind in DEBIT_NORMAL:
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


def _kind_totals(rows: list[dict]) -> dict[str, float]:
    totals = {kind: 0.0 for kind in KIND_ORDER}
    for r in rows:
        totals[r["kind"]] = round(totals[r["kind"]] + r["balance"], 2)
    return totals


def trial_balance_payload(ds) -> dict:
    """The trial balance body - rows, totals, income, per-kind summary."""
    accounts, line_count = read_book(ds)
    rows = book_rows(accounts)

    tot_debits = tot_credits = 0.0
    revenue = expenses = 0.0
    summary = {kind: {"accounts": 0, "total": 0.0} for kind in KIND_ORDER}
    for r in rows:
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
        "rows": rows,
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
            for kind in KIND_ORDER
        ],
        "line_count": line_count,
    }


def statements_payload(ds) -> dict:
    """The income statement + balance sheet + the accounting equation."""
    accounts, line_count = read_book(ds)
    rows = book_rows(accounts)

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
    # the earnings live in one of two honest worlds: BEFORE a close they
    # stay implied by the net (no retained account exists, so the balance
    # sheet reads assets == liabilities + equity + net); AFTER a close -
    # or on a hand-kept capital book - a REAL retained earnings account
    # exists and is already INSIDE the equity total, so the equation reads
    # assets == liabilities + equity and the retained line is a display
    retained_acct = next(
        (r for r in rows if r["account"].strip().lower() == "retained earnings"),
        None)
    if retained_acct is not None:
        retained = retained_acct["balance"]
        equation_side = round(total_liabilities + total_equity, 2)
    else:
        retained = net
        equation_side = round(total_liabilities + total_equity + retained, 2)

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


# ---------------------------------------------------------------------------
# v118: the aging - who owes the company, and who the company owes
# ---------------------------------------------------------------------------

def _parse_at(value) -> datetime | None:
    """The ``at`` column is an ISO string the posters stamped - read the
    honest subset (Z suffix, naive), refuse the rest silently."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _age_bucket(age_days: int) -> str:
    if age_days <= 30:
        return "current"
    if age_days <= 60:
        return "d31_60"
    if age_days <= 90:
        return "d61_90"
    return "d90_plus"


def _aging_side(rows: list[dict], needle: str, debit_normal: bool,
                now: datetime) -> dict:
    """One side of the aging: open refs on the receivable/payable
    accounts, bucketed by the ref's earliest stamped date."""
    per_ref: dict[str, dict] = {}
    for r in rows:
        name = str(r.get("account") or "").strip().lower()
        if needle not in name:
            continue
        ref = str(r.get("ref") or "").strip() or "(no ref)"
        state = per_ref.setdefault(ref, {"open": 0.0, "at": None})
        debit = num(r.get("debit"))
        credit = num(r.get("credit"))
        state["open"] = round(
            state["open"] + (debit - credit if debit_normal else credit - debit),
            2)
        stamped = _parse_at(r.get("at"))
        if stamped is not None and (state["at"] is None or stamped < state["at"]):
            state["at"] = stamped

    lines = []
    for ref, state in per_ref.items():
        if abs(state["open"]) < 0.005:
            continue  # settled - the aging only speaks about open money
        if state["at"] is None:
            bucket, age = "undated", None
        else:
            age = max((now - state["at"]).days, 0)
            bucket = _age_bucket(age)
        lines.append({"ref": ref, "open": state["open"], "at": state["at"],
                      "age_days": age, "bucket": bucket})
    lines.sort(key=lambda x: (-(x["age_days"] if x["age_days"] is not None
                                else -1)), )
    buckets = {b: {"refs": 0, "total": 0.0} for b in AGE_BUCKETS}
    for line in lines:
        buckets[line["bucket"]]["refs"] += 1
        buckets[line["bucket"]]["total"] = round(
            buckets[line["bucket"]]["total"] + line["open"], 2)
    return {
        "open_refs": len(lines),
        "total": round(sum(line["open"] for line in lines), 2),
        "buckets": [{"bucket": b, "refs": buckets[b]["refs"],
                     "total": buckets[b]["total"]} for b in AGE_BUCKETS],
        "lines": [
            {**line, "at": line["at"].isoformat() if line["at"] else ""}
            for line in lines
        ],
    }


def aging_payload(ds, now: datetime | None = None) -> dict:
    """Who owes the company (receivables) and who the company owes
    (payables), bucketed by age - the collection desk's first grip."""
    now = now or datetime.now(timezone.utc)
    rows = raw_rows(ds)
    return {
        "dataset": {"id": ds.id, "name": ds.name},
        "as_of": now.isoformat(),
        "receivables": _aging_side(rows, "receivable", True, now),
        "payables": _aging_side(rows, "payable", False, now),
    }


# ---------------------------------------------------------------------------
# v118: the cash flow - where the money moved, by stated heuristic
# ---------------------------------------------------------------------------

def _is_cash(account: str) -> bool:
    name = account.strip().lower()
    return kind_of(name) == "asset" and ("cash" in name or "bank" in name)


def _classify_ref(group: list[dict]) -> str:
    """The stated heuristic, in priority order - the counterpart rows of
    a cash movement decide its section."""
    names = [str(r.get("account") or "").strip() for r in group]
    counterparts = [n for n in names if n and not _is_cash(n)]
    lowers = [n.lower() for n in counterparts]
    # 1. receivables/payables are the operating treadmill
    if any("receivable" in n or n == "accounts payable" for n in lowers):
        return "operating"
    kinds = {kind_of(n) for n in counterparts}
    # 2. the income statement is operating
    if kinds & {"revenue", "expense"}:
        return "operating"
    # 3. a non-cash asset bought or sold is investing
    if "asset" in kinds:
        return "investing"
    # 4. equity and the other liabilities are financing
    if kinds & {"equity", "liability"}:
        return "financing"
    return "other"


def cash_flow_payload(ds) -> dict:
    """The direct-method cash flow: every ref that touches a cash account
    moves the company's money; the counterpart rows say WHERE it moved."""
    rows = raw_rows(ds)
    cash_names = sorted({str(r.get("account") or "").strip()
                         for r in rows if _is_cash(str(r.get("account") or ""))})

    # pass one: which refs touch the company's cash at all
    cash_refs = {str(r.get("ref") or "").strip() or "(no ref)"
                 for r in rows if _is_cash(str(r.get("account") or ""))}
    # pass two: the ref's WHOLE group classifies the movement - the
    # counterpart rows may sit before or after the cash line
    per_ref: dict[str, list[dict]] = {}
    for r in rows:
        ref = str(r.get("ref") or "").strip() or "(no ref)"
        if ref in cash_refs:
            per_ref.setdefault(ref, []).append(r)

    sections = {name: {"inflows": 0.0, "outflows": 0.0, "net": 0.0}
                for name in CF_SECTIONS}
    for ref, group in per_ref.items():
        delta = round(sum(num(r.get("debit")) - num(r.get("credit"))
                          for r in group if _is_cash(str(r.get("account") or ""))),
                      2)
        if abs(delta) < 0.005:
            continue  # money that moved between the company's own pockets
        section = sections[_classify_ref(group)]
        if delta > 0:
            section["inflows"] = round(section["inflows"] + delta, 2)
        else:
            section["outflows"] = round(section["outflows"] - delta, 2)
        section["net"] = round(section["net"] + delta, 2)

    inflow_total = round(sum(s["inflows"] for s in sections.values()), 2)
    outflow_total = round(sum(s["outflows"] for s in sections.values()), 2)
    return {
        "dataset": {"id": ds.id, "name": ds.name},
        "cash_accounts": cash_names,
        "sections": [{"name": name, **sections[name]} for name in CF_SECTIONS],
        "inflow_total": inflow_total,
        "outflow_total": outflow_total,
        "net_cash_movement": round(inflow_total - outflow_total, 2),
    }


# ---------------------------------------------------------------------------
# v118: THE CLOSE - the period's earnings move into the company for keeps
# ---------------------------------------------------------------------------

async def is_closed(db: AsyncSession, ds) -> bool:
    row = (await db.execute(
        select(ErpClose).where(ErpClose.dataset_id == ds.id)
        .order_by(ErpClose.closed_at.desc()).limit(1))).scalar_one_or_none()
    return row is not None


async def closes_payload(db: AsyncSession, ds) -> dict:
    rows = (await db.execute(
        select(ErpClose).where(ErpClose.dataset_id == ds.id)
        .order_by(ErpClose.closed_at.desc()))).scalars().all()
    return {
        "dataset": {"id": ds.id, "name": ds.name},
        "closed": len(rows) > 0,
        "closes": [
            {"id": c.id, "period": c.period, "net": c.net,
             "retained_after": c.retained_after, "entries": c.entries,
             "closed_at": c.closed_at.isoformat(),
             "lines": (c.detail_json or {}).get("lines", [])}
            for c in rows
        ],
    }


async def close_book(db: AsyncSession, ds, period: str = "",
                     now: datetime | None = None) -> dict:
    """Close the period: sweep revenue and expense into retained earnings
    with REAL closing entries appended to the same book, then lock it.

    The classic close, four moves:
      1. every revenue account (credit balance) is debited to zero, the
         credit lands on Income summary;
      2. every expense account (debit balance) is credited to zero, the
         debit lands on Income summary;
      3. Income summary's remainder - the period's net - moves to
         Retained earnings;
      4. the close is recorded and the book locks (a second close is a
         loud 409; the numbers can only move forward on a fresh book).
    """
    now = now or datetime.now(timezone.utc)
    if await is_closed(db, ds):
        raise BookError("the period is already closed - the book is locked; "
                        "open a fresh book for the next period")

    rows = book_rows(read_book(ds)[0])
    tot_debits = round(sum(r["debits"] for r in rows), 2)
    tot_credits = round(sum(r["credits"] for r in rows), 2)
    if abs(tot_debits - tot_credits) >= 0.005:
        raise BookError(
            f"the books do not balance (debits {tot_debits:.2f} vs credits "
            f"{tot_credits:.2f}) - the close refuses to post")

    stamp = now.strftime("%Y%m%d-%H%M%S")
    ref = f"CLOSE-{stamp}"
    memo_at = now.isoformat()
    entries: list[dict] = []

    revenue_total = expense_total = 0.0
    for r in rows:
        if r["kind"] == "revenue" and abs(r["balance"]) >= 0.005:
            revenue_total = round(revenue_total + r["balance"], 2)
            entries.append({"ref": ref, "account": r["account"],
                            "debit": f"{r['balance']:.2f}", "credit": "",
                            "memo": f"close: {r['account']} swept to income summary",
                            "at": memo_at})
            entries.append({"ref": ref, "account": _INCOME_SUMMARY,
                            "debit": "", "credit": f"{r['balance']:.2f}",
                            "memo": f"close: {r['account']} swept to income summary",
                            "at": memo_at})
        elif r["kind"] == "expense" and abs(r["balance"]) >= 0.005:
            expense_total = round(expense_total + r["balance"], 2)
            entries.append({"ref": ref, "account": _INCOME_SUMMARY,
                            "debit": f"{r['balance']:.2f}", "credit": "",
                            "memo": f"close: {r['account']} swept to income summary",
                            "at": memo_at})
            entries.append({"ref": ref, "account": r["account"],
                            "debit": "", "credit": f"{r['balance']:.2f}",
                            "memo": f"close: {r['account']} swept to income summary",
                            "at": memo_at})

    net = round(revenue_total - expense_total, 2)
    if not entries:
        raise BookError("nothing to close - the books carry no revenue or "
                        "expense yet")
    if abs(net) >= 0.005:
        if net > 0:
            entries.append({"ref": ref, "account": _INCOME_SUMMARY,
                            "debit": f"{net:.2f}", "credit": "",
                            "memo": "close: the period's net moves to retained earnings",
                            "at": memo_at})
            entries.append({"ref": ref, "account": _RETAINED,
                            "debit": "", "credit": f"{net:.2f}",
                            "memo": "close: the period's net moves to retained earnings",
                            "at": memo_at})
        else:
            entries.append({"ref": ref, "account": _RETAINED,
                            "debit": f"{abs(net):.2f}", "credit": "",
                            "memo": "close: the period's loss moves against retained earnings",
                            "at": memo_at})
            entries.append({"ref": ref, "account": _INCOME_SUMMARY,
                            "debit": "", "credit": f"{abs(net):.2f}",
                            "memo": "close: the period's loss moves against retained earnings",
                            "at": memo_at})

    await ds_svc.append_rows(db, ds, entries)

    after_rows = book_rows(read_book(ds)[0])
    retained_after = next(
        (r["balance"] for r in after_rows
         if r["account"].strip().lower() == "retained earnings"),
        round(net, 2))
    record = ErpClose(
        owner_id=ds.owner_id, dataset_id=ds.id, period=period or stamp,
        net=net, retained_after=retained_after, entries=len(entries),
        closed_at=now, detail_json={"lines": entries})
    db.add(record)
    await db.commit()
    await db.refresh(record)

    return {
        "dataset": {"id": ds.id, "name": ds.name},
        "period": record.period,
        "ref": ref,
        "closed_at": record.closed_at.isoformat(),
        "net": net,
        "revenue": revenue_total,
        "expenses": expense_total,
        "entries": len(entries),
        "retained_after": retained_after,
        "locked": True,
        "lines": entries,
    }


# ---------------------------------------------------------------------------
# v121: the bank connector - a CSV statement lands as REAL balanced entries
# ---------------------------------------------------------------------------

async def import_bank_rows(db: AsyncSession, ds, rows, mapping,
                           dry_run: bool = True,
                           now: datetime | None = None) -> dict:
    """The bank statement connector: raw CSV rows (one dict per line) land
    as balanced journal pairs - the cash account takes the movement, the
    counterpart account (``default_account``, or per-row via
    ``account_col``) takes the other side, and the books stay balanced by
    construction. ``dry_run=True`` (the default) previews every entry and
    appends NOTHING; a closed book refuses loudly (the close locks)."""
    now = now or datetime.now(timezone.utc)
    if await is_closed(db, ds):
        raise BookError("the book is closed - the period is locked; "
                        "import into a fresh open book")

    if not isinstance(rows, list) or not rows:
        raise BookError("pass the statement as a list of row objects", 400)

    mapping = mapping or {}
    amount_col = str(mapping.get("amount_col") or "amount")
    date_col = str(mapping.get("date_col") or "")
    description_col = str(mapping.get("description_col") or "")
    ref_col = str(mapping.get("ref_col") or "")
    account_col = str(mapping.get("account_col") or "")
    cash_account = str(mapping.get("cash_account") or "Cash").strip() or "Cash"
    default_account = str(mapping.get("default_account") or "Uncategorized").strip() \
        or "Uncategorized"

    stamp = now.isoformat()
    entries: list[dict] = []
    skipped: list[dict] = []
    cash_net = 0.0
    counterpart_totals: dict[str, float] = {}
    for i, raw in enumerate(rows):
        if not isinstance(raw, dict):
            skipped.append({"row": i, "reason": "not an object"})
            continue
        amount = round(num(raw.get(amount_col)), 2)
        if abs(amount) < 0.005:
            skipped.append({"row": i, "reason": f"no usable {amount_col!r} value"})
            continue
        ref = str(raw.get(ref_col) or "").strip() or f"BANK-{i + 1:04d}"
        memo = str(raw.get(description_col) or "").strip() or "bank import"
        at = str(raw.get(date_col) or "").strip() or stamp
        counterpart = str(raw.get(account_col) or "").strip() or default_account
        side = "in" if amount > 0 else "out"
        pair = [
            {"ref": ref, "account": cash_account,
             "debit": f"{amount:.2f}" if amount > 0 else "",
             "credit": "" if amount > 0 else f"{abs(amount):.2f}",
             "memo": f"{memo} ({side})", "at": at},
            {"ref": ref, "account": counterpart,
             "debit": "" if amount > 0 else f"{abs(amount):.2f}",
             "credit": f"{amount:.2f}" if amount > 0 else "",
             "memo": f"{memo} ({side})", "at": at},
        ]
        entries.extend(pair)
        cash_net = round(cash_net + amount, 2)
        counterpart_totals[counterpart] = round(
            counterpart_totals.get(counterpart, 0.0) + amount, 2)

    appended = False
    if not dry_run and entries:
        await ds_svc.append_rows(db, ds, entries)
        appended = True

    return {
        "dataset": {"id": ds.id, "name": ds.name},
        "dry_run": dry_run,
        "rows_in": len(rows),
        "imported": len(rows) - len(skipped),
        "skipped": skipped,
        "entries": len(entries),
        "cash_net": cash_net,
        "counterpart_totals": counterpart_totals,
        "cash_account": cash_account,
        "default_account": default_account,
        "entries_preview": entries[:10],
        "appended": appended,
    }
