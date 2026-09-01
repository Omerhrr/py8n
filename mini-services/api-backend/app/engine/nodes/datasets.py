"""Dataset nodes (v27) - read / write stored datasets and run SQL over them.

These nodes make the dataset store (app/services/datasets.py) part of the
engine: workflows can pull rows from a dataset, push their items into one,
and run DuckDB SQL across ALL datasets (each registered as a view named by
its lowercased name). Datasets referenced by NAME resolve via
get_dataset (id-first, then case-insensitive name) so canvas params stay
human-friendly; Jinja ``{{ }}`` is resolved in every parameter first, so
dataset names/SQL can be dynamic (e.g. ``{{ nodes.upstream.output.name }}``).
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from ..context import ExecutionContext
from ..nodes.base import BaseNode, NodeExecutionError
from .data import _items, _working_data


async def _resolve_dataset(ref: str) -> dict | None:
    """Resolve a dataset reference (id first, then case-insensitive name)."""
    from ...db import AsyncSessionLocal
    from ...services import datasets as ds_svc

    if not ref or not ref.strip():
        raise NodeExecutionError("A dataset name or id is required")
    ref = ref.strip()
    async with AsyncSessionLocal() as session:
        ds = await ds_svc.get_dataset(session, ref)
        if ds is None:
            return None
        return {
            "id": ds.id,
            "name": ds.name,
            "row_count": ds.row_count,
            "schema_json": ds.schema_json or [],
        }


class DatasetReadNode(BaseNode):
    """Reads rows from a stored dataset into the flow."""

    type = "dataset_read"
    name = "Dataset Read"
    description = "Reads rows from a stored dataset (by name or id) into the flow as items."
    category = "actions"
    icon = "database"
    color = "#38bdf8"

    class ParamsModel(BaseModel):
        dataset: str = Field(default="", description="Dataset name (or id)")
        limit: int = Field(default=200, ge=0, le=10_000, description="Max rows returned (0 = all, hard cap 10000)")

    async def execute(self, context: ExecutionContext) -> NodeResult:
        from ...services import datasets as ds_svc

        p = self.params  # type: DatasetReadNode.ParamsModel
        meta = await _resolve_dataset(p.dataset)
        if meta is None:
            raise NodeExecutionError(f"Dataset {p.dataset!r} not found")
        df = ds_svc.read_parquet_df(ds_svc.parquet_path(meta["id"]))
        limit = p.limit if p.limit and p.limit > 0 else len(df)
        rows = ds_svc.jsonable_rows(df.head(limit))
        return self._single({"items": rows, "row_count": meta["row_count"], "returned": len(rows), "dataset": meta["name"]})


class DatasetWriteNode(BaseNode):
    """Writes the incoming items into a dataset (append or replace)."""

    type = "dataset_write"
    name = "Dataset Write"
    description = "Writes the incoming items into a dataset - appends by default, or replaces all rows."
    category = "actions"
    icon = "hard-drive-download"
    color = "#a3e635"

    class ParamsModel(BaseModel):
        dataset: str = Field(default="", description="Target dataset name (created when missing)")
        mode: str = Field(
            default="append",
            json_schema_extra={"widget": "select", "options": ["append", "replace"]},
        )
        create_if_missing: bool = Field(default=True, description="Create the dataset on first write")

    async def execute(self, context: ExecutionContext) -> NodeResult:
        from ...db import AsyncSessionLocal
        from ...services import datasets as ds_svc

        p = self.params  # type: DatasetWriteNode.ParamsModel
        items = _items(_working_data(context.current_input))
        rows = [r for r in items if isinstance(r, dict)]
        if len(rows) < len(items):
            raise NodeExecutionError(f"Dataset Write needs object items - {len(items) - len(rows)} non-object item(s) dropped would lose data; shape upstream instead")
        if not p.dataset or not p.dataset.strip():
            raise NodeExecutionError("A target dataset name is required")

        async with AsyncSessionLocal() as session:
            ds = await ds_svc.get_dataset(session, p.dataset.strip())
            if ds is None:
                if not p.create_if_missing:
                    raise NodeExecutionError(f"Dataset {p.dataset!r} not found (create_if_missing is off)")
                import pandas as pd

                ds = await ds_svc.create_from_df(session, p.dataset.strip(), pd.DataFrame(), source="workflow")
                await session.flush()
            created = ds.row_count == 0 and len(rows) > 0
            if p.mode == "replace":
                if not rows:
                    raise NodeExecutionError("Refusing to replace a dataset with zero items")
                written = await ds_svc.replace_rows(session, ds, rows)
            else:
                written = await ds_svc.append_rows(session, ds, rows)
            total = ds.row_count
            name = ds.name
            await session.commit()

        return self._single({
            "items": rows if written else [],
            "dataset": name,
            "mode": p.mode,
            "written": written,
            "row_count": total,
            "created": created,
        })


class SqlQueryNode(BaseNode):
    """Runs DuckDB SQL across every stored dataset (each is a view)."""

    type = "sql_query"
    name = "SQL Query"
    description = "Runs SQL over all datasets with DuckDB - each dataset is a view named after it (lowercased)."
    category = "actions"
    icon = "table-2"
    color = "#c084fc"

    class ParamsModel(BaseModel):
        sql: str = Field(
            default="",
            description="DuckDB SQL - datasets appear as views, e.g. SELECT * FROM customers",
            json_schema_extra={"widget": "code", "rows": 6, "language": "sql", "hint": "SELECT name, count(*) FROM customers GROUP BY name"},
        )

    async def execute(self, context: ExecutionContext) -> NodeResult:
        from ...db import AsyncSessionLocal
        from ...services import datasets as ds_svc

        p = self.params  # type: SqlQueryNode.ParamsModel
        if not p.sql or not p.sql.strip():
            raise NodeExecutionError("A SQL statement is required")
        async with AsyncSessionLocal() as session:
            try:
                result = await ds_svc.run_sql(session, p.sql)
            except ValueError as exc:
                raise NodeExecutionError(str(exc)) from exc
        return self._single({"items": result["rows"], "row_count": result["row_count"], "columns": result["columns"], "duration_ms": result["duration_ms"], "views": result["views"]})
