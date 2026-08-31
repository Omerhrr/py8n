"""Documents API (v32) - extract structured data from uploaded documents.

Endpoints
---------
GET  /documents/engines       capability probe (OCR availability, formats)
POST /documents/extract       multipart file → {engine, pages, text, tables} (no persistence)
POST /documents/to-dataset    multipart file + name → best table becomes a DATASET

``to-dataset`` is the payload of the wave: a PDF/invoice/workbook lands as
a first-class v27 dataset - SQL-queryable, app-buildable, dashboard-able.
Extraction is read-only; the only persisted object is the dataset.
"""

from __future__ import annotations

import pandas as pd
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user
from ..db import get_db
from ..schemas import DatasetOut
from ..services import datasets as ds_svc
from ..services import documents as doc_svc

router = APIRouter(prefix="/documents", tags=["documents"])

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB - same cap as dataset uploads


async def _read_upload(file: UploadFile) -> tuple[str, bytes]:
    filename = file.filename or "document"
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 25 MB)")
    if not doc_svc.supported(filename):
        raise HTTPException(
            status_code=415,
            detail="Unsupported document type - supported: " + ", ".join(doc_svc.SUPPORTED_EXTS),
        )
    return filename, raw


def _extract_or_400(filename: str, raw: bytes) -> dict:
    try:
        return doc_svc.extract_document(filename, raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/engines")
async def engines():
    ocr: dict = {"available": False, "version": None}
    try:
        import pytesseract

        ocr = {"available": True, "version": str(pytesseract.get_tesseract_version())}
    except Exception:  # noqa: BLE001 - binary missing or pytesseract absent
        pass
    return {
        "ocr": ocr,
        "formats": doc_svc.SUPPORTED_EXTS,
        "max_bytes": MAX_UPLOAD_BYTES,
    }


@router.post("/extract")
async def extract(file: UploadFile = File(...)):
    filename, raw = await _read_upload(file)
    result = _extract_or_400(filename, raw)
    return {
        "filename": filename,
        "size_bytes": len(raw),
        **result,
    }


@router.post("/to-dataset", status_code=201)
async def to_dataset(
    file: UploadFile = File(...),
    name: str = Form(""),
    description: str = Form(""),
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    filename, raw = await _read_upload(file)
    result = _extract_or_400(filename, raw)
    table = doc_svc.best_table(result)
    if table is None:
        raise HTTPException(
            status_code=400,
            detail="No extractable table found in this document - it contains text only. "
            "Use /documents/extract to inspect the text instead.",
        )
    items = doc_svc.table_to_items(table)
    items = doc_svc.coerce_items_dtypes(items)

    final_name = (name or "").strip() or filename.rsplit(".", 1)[0].strip() or "Extracted Data"
    if not ds_svc.NAME_RE.match(final_name):
        raise HTTPException(status_code=400, detail="Invalid dataset name")
    if await ds_svc.name_taken(db, final_name):
        raise HTTPException(status_code=409, detail=f"Dataset {final_name!r} already exists")

    df = ds_svc.normalize_df(pd.DataFrame(items))
    if df.empty:
        raise HTTPException(status_code=400, detail="The extracted table has no data rows")
    row = await ds_svc.create_from_df(
        db, final_name, df, source="document", description=description
    )
    row.owner_id = user.id if user else None  # v37
    await db.commit()
    await db.refresh(row)

    out = DatasetOut(
        id=row.id,
        name=row.name,
        description=row.description or "",
        schema_json=row.schema_json or [],
        row_count=row.row_count,
        source=row.source,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
    return {
        "dataset": out,
        "extraction": {
            "engine": result["engine"],
            "pages": result["pages"],
            "chars": result["chars"] if "chars" in result else len(result.get("text", "")),
            "tables_count": len(result.get("tables", [])),
            "table_page": table["page"],
            "rows_imported": len(items),
        },
    }
