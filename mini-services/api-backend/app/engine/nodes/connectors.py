"""Connector nodes (v50) - native ingestion primitives.

Instead of one node per SaaS product (the "500 random nodes" trap), these
are TWO primitives that feed the dataset estate from anywhere:

* ``db_source``      - read rows from any SQLAlchemy-addressable database
                       (sqlite built in; postgres/mysql when their driver
                       is installed). Credentials plug in for host/port
                       databases; SELECT-only SQL guard reuses the dataset
                       service's read-only validation.
* ``s3_source``      - read csv/xlsx/json/parquet objects from S3, MinIO,
                       or any S3-compatible store (boto3; endpoint_url
                       makes MinIO/self-hosted a first-class citizen).

Both emit plain JSON rows - shape with transform nodes, land them with
dataset_write (incremental mode + contracts), and the estate does the rest.
"""

from __future__ import annotations

from typing import Any, ClassVar

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
