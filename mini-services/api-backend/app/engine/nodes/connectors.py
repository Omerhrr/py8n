"""Connector nodes (v50 + v52) - native ingestion primitives.

Instead of one node per SaaS product (the "500 random nodes" trap), these
are primitives that feed the dataset estate from anywhere:

* ``db_source``      - read rows from any SQLAlchemy-addressable database
                       (sqlite built in; postgres/mysql when their driver
                       is installed). Credentials plug in for host/port
                       databases; SELECT-only SQL guard reuses the dataset
                       service's read-only validation.
* ``s3_source``      - read csv/xlsx/json/parquet objects from S3, MinIO,
                       or any S3-compatible store (boto3; endpoint_url
                       makes MinIO/self-hosted a first-class citizen).
* ``google_sheets_source`` (v52) - read a Google Sheet tab as rows: PUBLIC
                       sheets via the no-auth CSV export endpoint, private
                       ones via a service-account credential (google-auth,
                       Sheets REST v4).
* ``ftp_source``     (v52) - read a csv/tsv file over FTP or FTPS (stdlib
                       ``ftplib``, zero extra deps) - the boring-but-real
                       export drop of a thousand legacy systems.

All of them emit plain JSON rows - shape with transform nodes, land them
with dataset_write (incremental mode + contracts), and the estate does
the rest.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, ClassVar
from urllib.parse import quote

from pydantic import BaseModel, Field

from ..context import ExecutionContext
from .base import BaseNode, NodeExecutionError, NodeResult

_READ_ONLY_PREFIXES = ("select", "with")
_FORBIDDEN_DB_KEYWORDS = (
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "grant", "revoke", "attach", "detach", "pragma", "vacuum",
)


def _validate_readonly_sql(sql: str) -> str:
    """Lightweight SELECT-only guard for remote databases."""
    s = sql.strip().rstrip(";").strip()
    if not s:
        raise NodeExecutionError("A SQL statement is required")
    lowered = s.lower()
    if not lowered.startswith(_READ_ONLY_PREFIXES):
        raise NodeExecutionError("Only SELECT/WITH statements are allowed on db_source")
    for kw in _FORBIDDEN_DB_KEYWORDS:
        import re

        if re.search(rf"\b{kw}\b", lowered):
            raise NodeExecutionError(f"db_source is read-only: {kw.upper()} is not allowed")
    return s


def _build_db_url(backend: str, p: "DbSourceNode.ParamsModel", cred: dict | None) -> str:
    """Assemble the connection URL from params + optional credential."""
    if backend == "sqlite":
        conn = (p.connection or "").strip()
        if not conn:
            return "sqlite:///data/py8n.db"
        if "://" in conn:
            return conn  # already a full URL
        # bare filesystem path -> sqlite URL: the path keeps its own leading
        # slash when absolute (4 slashes total), relative gets exactly 3
        return f"sqlite:///{conn}"
    if p.connection and "://" in p.connection:
        return p.connection
    if cred is None:
        raise NodeExecutionError(
            f"{backend} needs a credential (host/port/user/password/database) or a full connection URL"
        )
    host = cred.get("host") or ""
    user = cred.get("user") or cred.get("username") or ""
    password = cred.get("password") or ""
    database = cred.get("database") or ""
    port = cred.get("port")
    if not host or not database:
        raise NodeExecutionError("credential needs at least host and database")
    scheme = {"postgres": "postgresql+psycopg2", "mysql": "mysql+pymysql"}.get(backend)
    if scheme is None:
        raise NodeExecutionError(f"unsupported backend {backend!r}")
    auth = f"{user}:{password}@" if user else ""
    port_part = f":{int(port)}" if port else ""
    return f"{scheme}://{auth}{host}{port_part}/{database}"


class DbSourceNode(BaseNode):
    """Reads rows from a relational database via SQLAlchemy (v50)."""

    type = "db_source"
    name = "DB Source"
    description = (
        "Reads rows from a relational database - sqlite (path), postgres or mysql "
        "(credential or URL). SELECT-only. Pair with dataset_write (incremental) "
        "to land data with checkpoints and contracts."
    )
    category = "actions"
    icon = "plug-zap"
    color = "#0ea5e9"

    class ParamsModel(BaseModel):
        backend: str = Field(
            default="sqlite",
            description="Database kind",
            json_schema_extra={"widget": "select", "options": ["sqlite", "postgres", "mysql"]},
        )
        connection: str = Field(
            default="",
            description="sqlite: path to the .db file; postgres/mysql: optional full SQLAlchemy URL (overrides the credential)",
        )
        credential_id: str | None = Field(
            default=None,
            description="Optional credential of type database (host, port, user, password, database)",
        )
        table: str = Field(default="", description="Table (or view) to read")
        sql: str = Field(default="", description="Optional SELECT statement (overrides table)")
        limit: int = Field(default=1000, ge=1, le=100_000, description="Max rows returned")

    async def execute(self, context: ExecutionContext) -> NodeResult:
        import pandas as pd
        from sqlalchemy import create_engine, text

        p = self.params  # type: DbSourceNode.ParamsModel
        cred: dict | None = None
        if p.credential_id:
            from ...services.crypto import decrypt_credential

            cred = await decrypt_credential(context, p.credential_id, owner_id=context.owner_id)
            if cred.get("type") not in (None, "", "database"):
                raise NodeExecutionError(
                    f"credential type {cred.get('type')!r} is not a database credential"
                )
        url = _build_db_url(p.backend, p, cred)
        try:
            engine = create_engine(url, future=True, pool_pre_ping=True)
        except Exception as exc:  # noqa: BLE001 - driver/import/url errors
            raise NodeExecutionError(f"could not create {p.backend} engine: {exc}") from exc

        try:
            with engine.connect() as conn:
                if p.sql.strip():
                    q = _validate_readonly_sql(p.sql)
                    df = pd.read_sql(text(q), conn)
                elif p.table.strip():
                    table = p.table.strip()
                    if not table.replace("_", "").isalnum():
                        raise NodeExecutionError("table must be a plain (quoted if needed) name")
                    df = pd.read_sql(text(f'SELECT * FROM "{table}"'), conn)
                else:
                    raise NodeExecutionError("db_source needs a table or a SELECT statement")
        except NodeExecutionError:
            raise
        except Exception as exc:  # noqa: BLE001 - driver errors surface cleanly
            raise NodeExecutionError(f"{p.backend} query failed: {exc}") from exc
        finally:
            engine.dispose()

        if p.limit and len(df) > p.limit:
            df = df.head(p.limit)
        import json as _json

        rows = _json.loads(df.to_json(orient="records"))
        return self._single({
            "items": rows,
            "row_count": len(rows),
            "backend": p.backend,
            "columns": list(df.columns),
        })


class S3SourceNode(BaseNode):
    """Reads csv/xlsx/json/parquet objects from S3-compatible storage (v50)."""

    type = "s3_source"
    name = "S3 Source"
    description = (
        "Reads a csv/xlsx/json/parquet object from S3, MinIO or any S3-compatible "
        "store (set endpoint_url for self-hosted). Emits rows - land them with "
        "dataset_write (incremental + contracts)."
    )
    category = "actions"
    icon = "bucket"
    color = "#f59e0b"

    class ParamsModel(BaseModel):
        uri: str = Field(default="s3://bucket/path/file.csv", description="Object URI, s3://bucket/key")
        endpoint_url: str = Field(default="", description="Optional S3-compatible endpoint (e.g. http://minio:9000)")
        credential_id: str | None = Field(
            default=None,
            description="Optional credential of type aws (access_key_id, secret_access_key, region)",
        )
        limit: int = Field(default=10_000, ge=1, le=200_000, description="Max rows returned")

    async def execute(self, context: ExecutionContext) -> NodeResult:
        import json as _json

        try:
            import boto3  # noqa: F401
        except ImportError as exc:  # pragma: no cover - boto3 ships with the API backend
            raise NodeExecutionError("boto3 is not installed (pip install boto3)") from exc

        p = self.params  # type: S3SourceNode.ParamsModel
        uri = p.uri.strip()
        if not uri.startswith("s3://"):
            raise NodeExecutionError("uri must look like s3://bucket/path/object.ext")
        rest = uri[5:]
        bucket, _, key = rest.partition("/")
        if not bucket or not key:
            raise NodeExecutionError("uri must include both bucket and object key")
        ext = key.rsplit(".", 1)[-1].lower()
        if ext not in ("csv", "xlsx", "xls", "json", "parquet"):
            raise NodeExecutionError(f"unsupported object type .{ext} (csv, xlsx, json, parquet)")

        client_kwargs: dict[str, Any] = {}
        if p.credential_id:
            from ...services.crypto import decrypt_credential

            cred = await decrypt_credential(context, p.credential_id, owner_id=context.owner_id)
            if cred.get("type") not in (None, "", "aws"):
                raise NodeExecutionError("s3_source needs an aws-type credential")
            client_kwargs["aws_access_key_id"] = cred.get("access_key_id") or cred.get("access_key") or ""
            client_kwargs["aws_secret_access_key"] = cred.get("secret_access_key") or cred.get("secret_key") or ""
            if cred.get("region"):
                client_kwargs["region_name"] = cred.get("region")
        if p.endpoint_url.strip():
            client_kwargs["endpoint_url"] = p.endpoint_url.strip()

        import boto3
        import io

        try:
            s3 = boto3.client("s3", **client_kwargs)
            obj = s3.get_object(Bucket=bucket, Key=key)
            raw = obj["Body"].read()
        except NodeExecutionError:
            raise
        except Exception as exc:  # noqa: BLE001 - boto/boto errors surface cleanly
            raise NodeExecutionError(f"s3 get_object failed: {exc}") from exc

        import pandas as pd

        try:
            if ext in ("csv",):
                df = pd.read_csv(io.BytesIO(raw))
            elif ext in ("xlsx", "xls"):
                df = pd.read_excel(io.BytesIO(raw))
            elif ext == "json":
                df = pd.read_json(io.BytesIO(raw))
            else:
                df = pd.read_parquet(io.BytesIO(raw))
        except Exception as exc:  # noqa: BLE001
            raise NodeExecutionError(f"could not parse s3 object as {ext}: {exc}") from exc

        if p.limit and len(df) > p.limit:
            df = df.head(p.limit)
        rows = _json.loads(df.to_json(orient="records"))
        return self._single({
            "items": rows,
            "row_count": len(rows),
            "bucket": bucket,
            "key": key,
            "columns": list(df.columns),
        })


# ------------------------------------------------------------------ v52
# Google Sheets: public sheets need NO auth (the gviz CSV export endpoint);
# private ones use a service-account credential and the Sheets REST v4
# values endpoint. Both paths funnel into the same rows emitter.
_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{10,})")
_GID_RE = re.compile(r"[#&?]gid=([0-9]+)")


def _extract_sheet_id(sheet: str) -> tuple[str, int | None]:
    """Accept a full Sheets URL or a bare spreadsheet ID.

    Returns (sheet_id, gid_from_url) - the gid is only picked up when the
    caller did not set one explicitly.
    """
    s = (sheet or "").strip()
    if not s:
        raise NodeExecutionError("A Google Sheet URL or spreadsheet ID is required")
    m = _SHEET_ID_RE.search(s)
    if m:
        gid_m = _GID_RE.search(s)
        return m.group(1), (int(gid_m.group(1)) if gid_m else None)
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", s):
        return s, None
    raise NodeExecutionError("Could not parse a spreadsheet ID from that URL/ID")


def _gviz_url(sheet_id: str, gid: int, tab: str) -> str:
    """No-auth CSV export URL for a PUBLIC sheet (tab name wins over gid)."""
    base = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv"
    if tab.strip():
        return f"{base}&sheet={quote(tab.strip())}"
    return f"{base}&gid={int(gid)}"


async def _fetch_public_csv(sheet_id: str, gid: int, tab: str) -> bytes:
    """Download the public CSV export (also the seam tests monkeypatch)."""
    import httpx

    url = _gviz_url(sheet_id, gid, tab)
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                raise NodeExecutionError(
                    f"Google Sheets export returned HTTP {resp.status_code} "
                    "(is the sheet shared as 'Anyone with the link'?)"
                )
            return resp.content
    except httpx.HTTPError as exc:
        raise NodeExecutionError(f"Google Sheets export failed: {exc}") from exc


def _service_account_credentials(cred: dict):
    """Build google-auth service-account credentials from a vault credential.

    Accepts either a full service-account JSON (credential field ``json`` -
    dict or string) or ``client_email`` + ``private_key`` fields.
    """
    try:
        from google.oauth2 import service_account  # deferred: optional dep
    except ImportError as exc:  # pragma: no cover
        raise NodeExecutionError(
            "google-auth is not installed (pip install google-auth) - "
            "required for service-account Sheets access"
        ) from exc

    import json as _json

    raw = cred.get("json")
    if isinstance(raw, str) and raw.strip():
        try:
            info = _json.loads(raw)
        except ValueError as exc:
            raise NodeExecutionError(f"service-account json is not valid JSON: {exc}") from exc
    elif isinstance(raw, dict):
        info = raw
    elif cred.get("client_email") and cred.get("private_key"):
        info = {
            "type": "service_account",
            "client_email": cred["client_email"],
            "private_key": cred["private_key"],
            "token_uri": cred.get("token_uri") or "https://oauth2.googleapis.com/token",
        }
    else:
        raise NodeExecutionError(
            "service-account credential needs a 'json' blob (or client_email + private_key)"
        )
    if not isinstance(info, dict) or "client_email" not in info or "private_key" not in info:
        raise NodeExecutionError("service-account json is missing client_email/private_key")
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    return service_account.Credentials.from_service_account_info(info, scopes=scopes)


def _refresh_sa_token(credentials) -> str:
    """Blocking OAuth token refresh (call through asyncio.to_thread)."""
    from google.auth.transport.requests import Request

    credentials.refresh(Request())
    return credentials.token


async def _fetch_sa_values(sheet_id: str, tab: str, token: str) -> list[list]:
    """Read the whole tab via Sheets REST v4 (also the monkeypatch seam)."""
    import httpx

    range_a1 = f"'{tab.strip()}'" if tab.strip() else "A1:ZZ"
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{quote(range_a1, safe='')}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code != 200:
                raise NodeExecutionError(
                    f"Sheets API returned HTTP {resp.status_code} "
                    "(check the sheet is shared with the service account)"
                )
            return resp.json().get("values", [])
    except httpx.HTTPError as exc:
        raise NodeExecutionError(f"Sheets API call failed: {exc}") from exc


def _values_to_df(values: list[list]) -> "pd.DataFrame":
    """Sheets values -> DataFrame (first row header, ragged rows padded)."""
    import pandas as pd

    if not values:
        return pd.DataFrame()
    width = max(len(r) for r in values)
    padded = [list(r) + [""] * (width - len(r)) for r in values]
    header = [str(c or f"col_{i + 1}") for i, c in enumerate(padded[0])]
    body = padded[1:] if len(padded) > 1 else []
    return pd.DataFrame(body, columns=header)


class GoogleSheetsSourceNode(BaseNode):
    """Reads a Google Sheet tab as rows (v52) - public or service-account."""

    type = "google_sheets_source"
    name = "Google Sheets Source"
    description = (
        "Reads one Google Sheet tab as rows. Public sheets: no auth (CSV "
        "export). Private sheets: attach a service-account credential and "
        "share the sheet with its client_email. Pair with dataset_write "
        "(incremental + contracts) to land the data."
    )
    category = "actions"
    icon = "sheet"
    color = "#22c55e"

    class ParamsModel(BaseModel):
        sheet: str = Field(default="", description="Full Sheets URL or bare spreadsheet ID")
        tab: str = Field(default="", description="Tab (sheet) name - wins over gid")
        gid: int = Field(default=0, ge=0, description="gid of the tab (public mode, ignored when tab is set)")
        mode: str = Field(
            default="public",
            description="How to authenticate",
            json_schema_extra={"widget": "select", "options": ["public", "service_account"]},
        )
        credential_id: str | None = Field(
            default=None,
            description="Service-account credential (json blob, or client_email + private_key)",
            json_schema_extra={"widget": "credential"},
        )
        limit: int = Field(default=10_000, ge=1, le=200_000, description="Max rows returned")

    async def execute(self, context: ExecutionContext) -> NodeResult:
        import io as _io
        import json as _json

        import pandas as pd

        p = self.params  # type: GoogleSheetsSourceNode.ParamsModel
        sheet_id, url_gid = _extract_sheet_id(p.sheet)
        tab = p.tab

        if p.mode == "service_account":
            if not p.credential_id:
                raise NodeExecutionError("service_account mode needs a credential_id")
            from ...services.crypto import decrypt_credential

            cred = await decrypt_credential(context, p.credential_id, owner_id=context.owner_id)
            if cred.get("type") not in (None, "", "google_service_account"):
                raise NodeExecutionError("Sheets service-account mode needs a google_service_account credential")
            credentials = _service_account_credentials(cred)
            token = await asyncio.to_thread(_refresh_sa_token, credentials)
            values = await _fetch_sa_values(sheet_id, tab, token)
            df = _values_to_df(values)
            auth = "service_account"
        else:
            gid = p.gid if (p.gid or url_gid is None) else url_gid
            raw = await _fetch_public_csv(sheet_id, gid, tab)
            try:
                df = pd.read_csv(_io.BytesIO(raw))
            except Exception as exc:  # noqa: BLE001
                raise NodeExecutionError(f"could not parse the sheet export as CSV: {exc}") from exc
            auth = "public"

        if p.limit and len(df) > p.limit:
            df = df.head(p.limit)
        rows = _json.loads(df.to_json(orient="records"))
        return self._single({
            "items": rows,
            "row_count": len(rows),
            "sheet_id": sheet_id,
            "tab": tab,
            "mode": auth,
            "columns": list(df.columns),
        })


def _ftp_connect(host: str, port: int, username: str, password: str, secure: bool, timeout: int):
    """Open an FTP/FTPS connection (module-level so tests can stub it)."""
    from ftplib import FTP, FTP_TLS

    client = FTP_TLS() if secure else FTP()
    client.connect(host, int(port), timeout=max(5, int(timeout)))
    client.login(username or "anonymous", password or "")
    if secure:
        client.prot_p()  # encrypt the data channel too
    return client


class FtpSourceNode(BaseNode):
    """Reads a csv/tsv file over FTP/FTPS as rows (v52) - stdlib only."""

    type = "ftp_source"
    name = "FTP Source"
    description = (
        "Downloads a csv/tsv file from an FTP or FTPS server (stdlib ftplib "
        "- no extra dependencies) and emits rows. The legacy-export workhorse: "
        "land the file with dataset_write (incremental + contracts)."
    )
    category = "actions"
    icon = "server"
    color = "#a855f7"

    class ParamsModel(BaseModel):
        host: str = Field(default="", description="FTP host, e.g. ftp.example.com")
        port: int = Field(default=21, ge=1, le=65535, description="Port (21 ftp, 990 explicit FTPS is server-dependent)")
        username: str = Field(default="", description="Username (default anonymous)")
        password: str = Field(default="", description="Password", json_schema_extra={"widget": "password"})
        credential_id: str | None = Field(
            default=None,
            description="Optional credential of type ftp (host, port, username, password)",
            json_schema_extra={"widget": "credential"},
        )
        remote_path: str = Field(default="", description="Path to the file on the server, e.g. /exports/customers.csv")
        fmt: str = Field(
            default="csv",
            description="File format",
            json_schema_extra={"widget": "select", "options": ["csv", "tsv"]},
        )
        encoding: str = Field(default="utf-8", description="File encoding")
        secure: bool = Field(default=False, description="Use FTPS (TLS, control + data channels)")
        timeout_seconds: int = Field(default=30, ge=5, le=300, description="Socket timeout")
        limit: int = Field(default=10_000, ge=1, le=200_000, description="Max rows returned")

    async def execute(self, context: ExecutionContext) -> NodeResult:
        import io as _io
        import json as _json

        import pandas as pd

        p = self.params  # type: FtpSourceNode.ParamsModel
        host, port = p.host.strip(), p.port
        username, password = p.username, p.password
        if p.credential_id:
            from ...services.crypto import decrypt_credential

            cred = await decrypt_credential(context, p.credential_id, owner_id=context.owner_id)
            if cred.get("type") not in (None, "", "ftp"):
                raise NodeExecutionError("ftp_source needs an ftp-type credential")
            host = cred.get("host") or host
            port = int(cred.get("port") or port)
            username = cred.get("username") or username
            password = cred.get("password") or password
        if not host:
            raise NodeExecutionError("An FTP host is required (or attach an ftp credential)")
        path = p.remote_path.strip()
        if not path or not path.startswith("/"):
            raise NodeExecutionError("remote_path must be an absolute server path like /exports/customers.csv")
        fmt = (p.fmt or "csv").strip().lower()
        if fmt not in ("csv", "tsv"):
            raise NodeExecutionError("fmt must be csv or tsv")
        delimiter = "\t" if fmt == "tsv" else ","

        buf = _io.BytesIO()

        def _download() -> None:
            client = _ftp_connect(host, port, username, password, p.secure, p.timeout_seconds)
            try:
                client.retrbinary(f"RETR {path}", buf.write)
            finally:
                try:
                    client.quit()
                except Exception:  # noqa: BLE001 - close is best effort
                    client.close()

        try:
            await asyncio.to_thread(_download)
        except NodeExecutionError:
            raise
        except Exception as exc:  # noqa: BLE001 - ftplib/socket errors surface cleanly
            raise NodeExecutionError(f"FTP download of {path} failed: {exc}") from exc

        try:
            df = pd.read_csv(_io.BytesIO(buf.getvalue()), sep=delimiter, encoding=p.encoding or "utf-8")
        except Exception as exc:  # noqa: BLE001
            raise NodeExecutionError(f"could not parse the FTP file as {fmt}: {exc}") from exc

        if p.limit and len(df) > p.limit:
            df = df.head(p.limit)
        rows = _json.loads(df.to_json(orient="records"))
        return self._single({
            "items": rows,
            "row_count": len(rows),
            "host": host,
            "path": path,
            "fmt": fmt,
            "secure": p.secure,
            "columns": list(df.columns),
        })
