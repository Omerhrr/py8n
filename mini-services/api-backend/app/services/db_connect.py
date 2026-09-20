"""Connect your database - the guided wizard (v148).

The ``db_source`` workflow node already knows how to reach any
SQLAlchemy-addressable database (sqlite/postgres/mysql, credential or full
URL, SELECT-only). This module wraps the SAME connection/read path
(``_build_db_url`` / ``_validate_readonly_sql`` from
``engine.nodes.connectors``) in three previewable steps a UI can call one
at a time, before anything is committed:

1. **test_connection()** - open a connection, run ``SELECT 1``. Confirms
   credentials/URL before the user picks anything.
2. **list_tables()** - SQLAlchemy inspection: every table/view name.
3. **preview()** - a small page of a table (or custom read-only SQL), so
   the user sees real columns and real rows before importing.

Only the last step, **import_table()**, writes anything - it pulls the
full result (capped at ``row_limit``) and lands it as a real Dataset via
the same ``datasets`` service every other ingestion path uses, then
optionally builds an App on top of it through the shared
``apps.compose_app()`` helper (the exact primitive the Apps builder UI
creates) - so "connect a database" and "build an app" land on the same
real objects in one guided flow.
"""

from __future__ import annotations

from ..engine.nodes.connectors import _build_db_url, _validate_readonly_sql


class DbConnectError(Exception):
    """A user-facing wizard error (bad connection, bad table, etc.)."""


class _Params:
    """Duck-types just the one attribute ``_build_db_url`` reads off
    ``DbSourceNode.ParamsModel`` - the wizard has no node params object,
    only a bare connection string."""

    def __init__(self, connection: str):
        self.connection = connection


async def _resolve_credential(credential_id: str | None, owner_id: str | None) -> dict | None:
    if not credential_id:
        return None
    from .crypto import decrypt_credential

    try:
        cred = await decrypt_credential(None, credential_id, owner_id=owner_id)
    except LookupError as exc:
        raise DbConnectError(str(exc)) from exc
    if cred.get("type") not in (None, "", "database"):
        raise DbConnectError(
            f"credential {cred.get('name')!r} is of type {cred.get('type')!r}, not database"
        )
    return cred


def _engine(backend: str, connection: str, cred: dict | None):
    from sqlalchemy import create_engine

    url = _build_db_url(backend, _Params(connection), cred)
    try:
        return create_engine(url, future=True, pool_pre_ping=True)
    except Exception as exc:  # noqa: BLE001 - driver/import/url errors
        raise DbConnectError(f"could not create {backend} engine: {exc}") from exc


def _check_table_name(table: str) -> str:
    t = table.strip()
    if not t or not t.replace("_", "").isalnum():
        raise DbConnectError("table must be a plain (unquoted) name")
    return t


async def test_connection(backend: str, connection: str, credential_id: str | None,
                          owner_id: str | None) -> dict:
    from sqlalchemy import text

    cred = await _resolve_credential(credential_id, owner_id)
    engine = _engine(backend, connection, cred)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except DbConnectError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DbConnectError(f"{backend} connection failed: {exc}") from exc
    finally:
        engine.dispose()
    return {"ok": True, "backend": backend}


async def list_tables(backend: str, connection: str, credential_id: str | None,
                      owner_id: str | None) -> dict:
    from sqlalchemy import inspect

    cred = await _resolve_credential(credential_id, owner_id)
    engine = _engine(backend, connection, cred)
    try:
        insp = inspect(engine)
        names = list(insp.get_table_names())
        try:
            names += list(insp.get_view_names())
        except Exception:  # noqa: BLE001 - not every backend/driver supports views
            pass
    except DbConnectError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DbConnectError(f"could not list tables: {exc}") from exc
    finally:
        engine.dispose()
    return {"tables": sorted(set(names))}


def _read_df(engine, table: str, sql: str, limit: int | None = None):
    import pandas as pd
    from sqlalchemy import text

    with engine.connect() as conn:
        if sql.strip():
            q = _validate_readonly_sql(sql)
            df = pd.read_sql(text(q), conn)
        elif table.strip():
            t = _check_table_name(table)
            df = pd.read_sql(text(f'SELECT * FROM "{t}"'), conn)
        else:
            raise DbConnectError("table or sql is required")
    if limit and len(df) > limit:
        df = df.head(limit)
    return df


async def preview(backend: str, connection: str, credential_id: str | None, owner_id: str | None,
                  *, table: str = "", sql: str = "", limit: int = 20) -> dict:
    import json

    cred = await _resolve_credential(credential_id, owner_id)
    engine = _engine(backend, connection, cred)
    try:
        df = _read_df(engine, table, sql, limit)
    except DbConnectError:
        raise
    except Exception as exc:  # noqa: BLE001 - driver/SQL errors
        raise DbConnectError(f"{backend} query failed: {exc}") from exc
    finally:
        engine.dispose()
    rows = json.loads(df.to_json(orient="records"))
    return {"columns": list(df.columns), "rows": rows, "row_count": len(rows)}


async def import_table(db, *, backend: str, connection: str, credential_id: str | None,
                       owner_id: str | None, table: str = "", sql: str = "",
                       dataset_name: str, row_limit: int = 100_000,
                       create_app: bool = False) -> dict:
    """Pull the full result (capped at ``row_limit``) and land it as a real
    Dataset; optionally build an App on top via the shared compose_app()
    helper. The caller owns the commit."""
    from . import apps as app_svc
    from . import datasets as ds_svc

    cred = await _resolve_credential(credential_id, owner_id)
    engine = _engine(backend, connection, cred)
    try:
        df = _read_df(engine, table, sql, row_limit)
    except DbConnectError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DbConnectError(f"{backend} import failed: {exc}") from exc
    finally:
        engine.dispose()

    name = (dataset_name or table or "Imported dataset").strip()[:100] or "Imported dataset"
    candidate, n = name, 2
    while await ds_svc.name_taken(db, candidate):
        candidate = f"{name} {n}"
        n += 1

    ds = await ds_svc.create_from_df(
        db, candidate, df,
        source=f"db_connect:{backend}",
        description=(f"Imported from {backend} table {table!r}" if table
                     else f"Imported from {backend} (custom SQL)"),
        owner_id=owner_id,
    )
    out: dict = {"dataset_id": ds.id, "dataset_name": ds.name,
                 "rows": int(len(df)), "columns": list(df.columns)}
    if create_app:
        app_row = await app_svc.compose_app(
            db, f"{ds.name} app", ds,
            description=f"Manage records imported from {backend}.",
            owner_id=owner_id,
            publish=False,
        )
        out["app_id"] = app_row.id
        out["app_slug"] = app_row.slug
    return out
