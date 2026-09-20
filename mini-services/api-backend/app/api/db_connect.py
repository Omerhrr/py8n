"""Connect your database - the guided wizard API (v148).

* ``POST /db-connect/test``    - open a connection, confirm it's live
* ``POST /db-connect/tables``  - list tables/views (SQLAlchemy inspection)
* ``POST /db-connect/preview`` - a small page of a table or custom SQL
* ``POST /db-connect/import``  - land the full result as a real Dataset,
                                 optionally with an App on top (same door
                                 the Apps builder UI and the other AI
                                 builders use - ``apps.compose_app()``)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user
from ..db import get_db
from ..services import db_connect as svc

router = APIRouter(prefix="/db-connect", tags=["db-connect"])


class ConnBody(BaseModel):
    backend: str = Field(default="sqlite", description="sqlite | postgres | mysql")
    connection: str = Field(
        default="",
        description="sqlite: path to the .db file; postgres/mysql: optional full "
                    "SQLAlchemy URL (overrides the credential)")
    credential_id: str | None = Field(
        default=None, description="A vault credential of type database")


class PreviewBody(ConnBody):
    table: str = ""
    sql: str = Field(default="", description="Optional read-only SELECT/WITH (overrides table)")
    limit: int = Field(default=20, ge=1, le=500)


class ImportBody(ConnBody):
    table: str = ""
    sql: str = Field(default="", description="Optional read-only SELECT/WITH (overrides table)")
    dataset_name: str = Field(..., min_length=1, max_length=100)
    row_limit: int = Field(default=100_000, ge=1, le=100_000)
    create_app: bool = Field(
        default=False,
        description="Also build an App (forms, records, Excel export) over the imported dataset")


def _http(exc: Exception, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=str(exc))


@router.post("/test")
async def db_connect_test(body: ConnBody, user=Depends(get_optional_user)):
    owner = getattr(user, "id", None)
    try:
        return await svc.test_connection(body.backend, body.connection, body.credential_id, owner)
    except svc.DbConnectError as exc:
        raise _http(exc) from exc


@router.post("/tables")
async def db_connect_tables(body: ConnBody, user=Depends(get_optional_user)):
    owner = getattr(user, "id", None)
    try:
        return await svc.list_tables(body.backend, body.connection, body.credential_id, owner)
    except svc.DbConnectError as exc:
        raise _http(exc) from exc


@router.post("/preview")
async def db_connect_preview(body: PreviewBody, user=Depends(get_optional_user)):
    owner = getattr(user, "id", None)
    try:
        return await svc.preview(
            body.backend, body.connection, body.credential_id, owner,
            table=body.table, sql=body.sql, limit=body.limit)
    except svc.DbConnectError as exc:
        raise _http(exc) from exc


@router.post("/import", status_code=201)
async def db_connect_import(body: ImportBody, user=Depends(get_optional_user),
                            db: AsyncSession = Depends(get_db)):
    owner = getattr(user, "id", None)
    try:
        out = await svc.import_table(
            db, backend=body.backend, connection=body.connection,
            credential_id=body.credential_id, owner_id=owner,
            table=body.table, sql=body.sql, dataset_name=body.dataset_name,
            row_limit=body.row_limit, create_app=body.create_app)
    except svc.DbConnectError as exc:
        raise _http(exc) from exc
    await db.commit()
    return out
