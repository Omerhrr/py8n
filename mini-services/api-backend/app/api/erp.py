"""ERP reports API (v114) - the books speak.

The ledgers the ERP operator's reactive posters write are datasets; these
doors turn the GL entries dataset into the reports a company actually
reads. Since v118 the reading AND the writing live in ONE service
(``app/services/erp_books.py``) - the doors below are thin wrappers so
the harness tools (v119) speak the exact same books:

* the TRIAL BALANCE - one row per account with its total debits, total
  credits and its balance (debit-normal for assets and expenses,
  credit-normal for the rest), plus the grand totals, the ``balanced``
  flag and the per-kind ``summary`` (v116);
* the STATEMENTS - the income statement + the balance sheet + the
  accounting equation check (v117);
* the AGING - who owes the company and who the company owes, bucketed
  current / 31-60 / 61-90 / 90+ / undated (v118);
* the CASH FLOW - the direct-method movement of money, sectioned by a
  stated heuristic into operating / investing / financing / other (v118);
* THE CLOSE - POST the period: revenue and expense sweep into retained
  earnings with real closing entries appended to the same book, the
  close is recorded, and the book LOCKS (a second close is a loud 409;
  the unbalanced are refused; nothing to close is refused) (v118).

The dataset is the caller's to name: the console passes the GL entries
id it already holds, an agent or a dashboard names it (id or
case-insensitive name). Resolution honors ownership like every dataset
door; an unknown or foreign dataset is a loud 404.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..services import erp_books as books
from .auth import get_optional_user

router = APIRouter(prefix="/erp", tags=["erp"])


async def _book(dataset_id: str, user, db: AsyncSession):
    try:
        return await books.book_dataset(db, dataset_id, user)
    except books.BookNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _book_error(exc: books.BookError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("/trial-balance")
async def trial_balance(
    dataset_id: str = Query(
        ..., description="the GL entries dataset (id or case-insensitive name)"),
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """The trial balance + income summary over one GL entries dataset."""
    ds = await _book(dataset_id, user, db)
    return books.trial_balance_payload(ds)


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
    liabilities + equity + the retained earnings.
    """
    ds = await _book(dataset_id, user, db)
    return books.statements_payload(ds)


@router.get("/aging")
async def aging(
    dataset_id: str = Query(
        ..., description="the GL entries dataset (id or case-insensitive name)"),
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """The AR/AP aging (v118) - open money by ref, bucketed by age.

    The collection desk's first grip: every open receivable ref and open
    payable ref with its age (from the journal lines' ``at`` stamps) in
    the fixed buckets current / 31-60 / 61-90 / 90+ / undated.
    """
    ds = await _book(dataset_id, user, db)
    return books.aging_payload(ds)


@router.get("/cash-flow")
async def cash_flow(
    dataset_id: str = Query(
        ..., description="the GL entries dataset (id or case-insensitive name)"),
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """The direct-method cash flow (v118) - where the money moved.

    Every ref that touches a cash account moves money; the counterpart
    rows decide the section (operating / investing / financing / other)
    under the heuristic stated in the service. Movements between the
    company's own cash pockets are excluded.
    """
    ds = await _book(dataset_id, user, db)
    return books.cash_flow_payload(ds)


@router.get("/close")
async def close_history(
    dataset_id: str = Query(
        ..., description="the GL entries dataset (id or case-insensitive name)"),
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """The close history (v118) - whether the book is locked, and every
    close it has been through with its receipt."""
    ds = await _book(dataset_id, user, db)
    return await books.closes_payload(db, ds)


class CloseIn(BaseModel):
    period: str = ""


class BankCsvIn(BaseModel):
    """The bank statement connector's body (v121) - raw CSV rows (one dict
    per line) plus the column mapping; dry_run previews by default."""
    dataset_id: str
    rows: list[dict] = []
    mapping: dict = {}
    dry_run: bool = True


@router.post("/import-csv")
async def import_bank_csv(
    body: BankCsvIn,
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """The bank statement connector (v121): land a CSV statement as REAL
    balanced journal pairs (cash takes the movement, the counterpart
    account the other side). ``dry_run=true`` (the default) previews every
    entry and appends nothing; a closed book refuses (the close locks);
    the books stay balanced by construction."""
    ds = await _book(body.dataset_id, user, db)
    try:
        return await books.import_bank_rows(
            db, ds, body.rows, body.mapping, dry_run=body.dry_run)
    except books.BookError as exc:
        raise _book_error(exc) from exc


@router.post("/close")
async def close_period(
    body: CloseIn | None = None,
    dataset_id: str = Query(
        ..., description="the GL entries dataset (id or case-insensitive name)"),
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Close the period (v118) - THE WRITE.

    Real closing entries land on the same book (revenue and expense sweep
    through Income summary into Retained earnings), the close is recorded,
    and the book locks: a second close, an unbalanced book and an empty
    book are all loud 409s. The numbers can only move forward on a fresh
    book.
    """
    ds = await _book(dataset_id, user, db)
    try:
        return await books.close_book(
            db, ds, period=(body.period if body else "") or "")
    except books.BookError as exc:
        raise _book_error(exc) from exc
