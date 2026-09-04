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


async def _resolve_dataset(ref: str, owner_id: str | None = None) -> dict | None:
    """Resolve a dataset reference (id first, then case-insensitive name).

    With ``owner_id`` set, another owner's claimed dataset is not found.
    """
    from ...db import AsyncSessionLocal
    from ...services import datasets as ds_svc

    if not ref or not ref.strip():
        raise NodeExecutionError("A dataset name or id is required")
    ref = ref.strip()
    async with AsyncSessionLocal() as session:
        ds = await ds_svc.get_dataset(session, ref, owner_id=owner_id)
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
        meta = await _resolve_dataset(p.dataset, owner_id=context.owner_id)
        if meta is None:
            raise NodeExecutionError(f"Dataset {p.dataset!r} not found")
        df = ds_svc.read_parquet_df(ds_svc.parquet_path(meta["id"]))
        limit = p.limit if p.limit and p.limit > 0 else len(df)
        rows = ds_svc.jsonable_rows(df.head(limit))
        return self._single({"items": rows, "row_count": meta["row_count"], "returned": len(rows), "dataset": meta["name"]})


class DatasetWriteNode(BaseNode):
    """Writes the incoming items into a dataset (append / replace / upsert /
    incremental), enforcing the dataset's data contract."""

    type = "dataset_write"
    name = "Dataset Write"
    description = (
        "Writes the incoming items into a dataset - appends by default, replaces all rows, "
        "upserts (fresh rows REPLACE rows sharing the key_columns values), or ingests "
        "INCREMENTALLY: only rows whose watermark_column value is beyond the stored "
        "checkpoint are written and the checkpoint advances (v50). UPSERT + a "
        "watermark_column combines both (v53): rows beyond the checkpoint are MERGED "
        "on key_columns, so late-arriving updates never duplicate. lookback rewinds "
        "the comparison window (units for numeric cursors, seconds for ISO) so "
        "boundary rows are re-admitted. A data contract on the dataset is enforced "
        "before anything lands (error = hard-stop)."
    )
    category = "actions"
    icon = "hard-drive-download"
    color = "#a3e635"

    class ParamsModel(BaseModel):
        dataset: str = Field(default="", description="Target dataset name (created when missing)")
        mode: str = Field(
            default="append",
            json_schema_extra={"widget": "select", "options": ["append", "replace", "upsert", "incremental"]},
        )
        key_columns: list[str] = Field(
            default_factory=list,
            description="Upsert key column(s) (JSON array, required for mode=upsert), e.g. [\"email\"]",
            json_schema_extra={"widget": "code", "rows": 3, "language": "json", "hint": '["email"]'},
        )
        create_if_missing: bool = Field(default=True, description="Create the dataset on first write")
        watermark_column: str = Field(
            default="",
            description="Cursor column (e.g. last_updated) compared against the stored checkpoint - in incremental mode OR in upsert mode (incremental upsert)",
        )
        ingestion_key: str = Field(
            default="default",
            description="Names this pipeline's checkpoint so several pipelines can feed one dataset",
        )
        lookback: float = Field(
            default=0.0,
            ge=0.0,
            le=1.0e12,
            description="Rewind the checkpoint comparison by this many units (numeric cursor) or seconds (ISO cursor) so boundary/late rows are re-admitted; pair with mode=upsert to merge instead of duplicate",
        )
        dead_letter_dataset: str = Field(
            default="",
            description="v67: quarantine dataset for rows that violate a dead_letter-mode contract (required when the target's contract is in dead_letter mode) - failing rows land here stamped with _dl_reasons/_dl_at",
        )

    async def execute(self, context: ExecutionContext) -> NodeResult:
        from ...db import AsyncSessionLocal
        from ...services import contracts as contract_svc
        from ...services import datasets as ds_svc
        from ...services import ingestion as ing_svc

        p = self.params  # type: DatasetWriteNode.ParamsModel
        items = _items(_working_data(context.current_input))
        rows = [r for r in items if isinstance(r, dict)]
        if len(rows) < len(items):
            raise NodeExecutionError(f"Dataset Write needs object items - {len(items) - len(rows)} non-object item(s) dropped would lose data; shape upstream instead")
        if not p.dataset or not p.dataset.strip():
            raise NodeExecutionError("A target dataset name is required")
        if p.mode == "upsert" and not p.key_columns:
            raise NodeExecutionError("Dataset Write mode=upsert needs key_columns (the column(s) that identify a row)")
        if p.mode == "incremental" and not p.watermark_column.strip():
            raise NodeExecutionError("Dataset Write mode=incremental needs watermark_column (the cursor column, e.g. last_updated)")
        if p.lookback and p.mode not in ("incremental", "upsert"):
            raise NodeExecutionError("lookback only applies to mode=incremental or mode=upsert (with a watermark_column)")

        async with AsyncSessionLocal() as session:
            # v47 lineage: stamp every version this write produces with the
            # producing workflow/execution/node (cleared on the way out)
            prov_token = ds_svc.set_provenance(
                context.workflow_id, context.execution_id, self.name,
            )
            contract_report = None
            checkpoint_before = checkpoint_after = None
            skipped = 0
            try:
                ds = await ds_svc.get_dataset(session, p.dataset.strip(), owner_id=context.owner_id)
                if ds is None:
                    if not p.create_if_missing:
                        raise NodeExecutionError(f"Dataset {p.dataset!r} not found (create_if_missing is off)")
                    import pandas as pd

                    ds = await ds_svc.create_from_df(session, p.dataset.strip(), pd.DataFrame(), source="workflow")
                    await session.flush()
                # v50 data contract: checked BEFORE anything lands
                try:
                    contract_report = await contract_svc.enforce_on_rows(session, ds, rows, context="dataset_write node")
                except contract_svc.ContractViolation as exc:
                    raise NodeExecutionError(str(exc)) from exc
                # v67 dead-letter routing: a contract in dead_letter mode
                # QUARANTINES failing rows into a side dataset and writes only
                # the rows that pass - the pipeline never hard-stops on data
                # quality, it routes around it (the reject lane is the record).
                dead_lettered = 0
                dead_letter_dataset_name = None
                if rows and contract_report is not None and contract_report.get("on_violation") == "dead_letter":
                    if not p.dead_letter_dataset.strip():
                        raise NodeExecutionError(
                            f"dataset {ds.name!r} has a dead_letter-mode contract - set "
                            "dead_letter_dataset on this node so violating rows have somewhere to go")
                    contract_row = await contract_svc.get_contract(session, ds.id)
                    good, bad = contract_svc.split_rows_dead_letter(rows, contract_row.columns_json or [])
                    if bad:
                        dl_name = p.dead_letter_dataset.strip()
                        dl_ds = await ds_svc.get_dataset(session, dl_name, owner_id=context.owner_id)
                        if dl_ds is None:
                            import pandas as pd

                            dl_ds = await ds_svc.create_from_df(session, dl_name, pd.DataFrame(), source="dead_letter")
                            await session.flush()
                        for q in bad:
                            q["_dl_source"] = ds.name
                            q["_dl_contract_version"] = int(contract_row.version or 1)
                        await ds_svc.append_rows(session, dl_ds, bad)
                        dead_lettered = len(bad)
                        dead_letter_dataset_name = dl_ds.name
                    rows = good
                    contract_report = {
                        **contract_report,
                        "ok": True,
                        "dead_lettered": dead_lettered,
                        "dead_letter_dataset": dead_letter_dataset_name,
                        "passed": len(good),
                    }
                created = ds.row_count == 0 and len(rows) > 0
                updated = inserted = 0
                wm = p.watermark_column.strip()
                if p.mode == "replace":
                    if not rows:
                        raise NodeExecutionError("Refusing to replace a dataset with zero items")
                    written = await ds_svc.replace_rows(session, ds, rows)
                elif p.mode == "upsert":
                    if rows:
                        missing = [c for c in p.key_columns if c not in rows[0]]
                        if missing:
                            raise NodeExecutionError(f"Upsert key column(s) {missing} not present in the incoming items")
                    keys = [str(c) for c in p.key_columns]
                    if wm:
                        # v53 incremental upsert: rows beyond the checkpoint are
                        # MERGED on key_columns - late updates never duplicate.
                        fresh, state, checkpoint_before = await ing_svc.filter_incremental(
                            session, ds, rows, wm, p.ingestion_key, lookback=p.lookback,
                        )
                        skipped = len(rows) - len(fresh)
                        stats = await ds_svc.upsert_rows(session, ds, fresh, keys)
                        written, updated, inserted = stats["written"], stats["updated"], stats["inserted"]
                        checkpoint_after = state.watermark
                        await ing_svc.advance(
                            session, state, checkpoint_after, written,
                            stats={
                                "mode": "upsert", "rows_in": len(rows), "written": written,
                                "skipped": skipped, "updated": updated, "inserted": inserted,
                                "lookback": p.lookback,
                            },
                        )
                    else:
                        stats = await ds_svc.upsert_rows(session, ds, rows, keys)
                        written, updated, inserted = stats["written"], stats["updated"], stats["inserted"]
                elif p.mode == "incremental":
                    fresh, state, checkpoint_before = await ing_svc.filter_incremental(
                        session, ds, rows, wm, p.ingestion_key, lookback=p.lookback,
                    )
                    skipped = len(rows) - len(fresh)
                    written = await ds_svc.append_rows(session, ds, fresh)
                    checkpoint_after = state.watermark
                    await ing_svc.advance(
                        session, state, checkpoint_after, written,
                        stats={
                            "mode": "incremental", "rows_in": len(rows), "written": written,
                            "skipped": skipped, "lookback": p.lookback,
                        },
                    )
                else:
                    written = await ds_svc.append_rows(session, ds, rows)
                total = ds.row_count
                name = ds.name
                await session.commit()
            finally:
                ds_svc.reset_provenance(prov_token)

        payload = {
            "items": rows if written else [],
            "dataset": name,
            "mode": p.mode,
            "written": written,
            "row_count": total,
            "created": created,
        }
        if p.mode == "upsert":
            payload["updated"] = updated
            payload["inserted"] = inserted
            payload["key_columns"] = [str(c) for c in p.key_columns]
        if p.mode == "incremental" or (p.mode == "upsert" and p.watermark_column.strip()):
            payload["rows_in"] = len(rows)
            payload["skipped"] = skipped
            payload["watermark_column"] = p.watermark_column.strip()
            payload["checkpoint_before"] = checkpoint_before
            payload["checkpoint_after"] = checkpoint_after
            if p.lookback:
                payload["lookback"] = p.lookback
        if dead_lettered:
            payload["dead_lettered"] = dead_lettered
            payload["dead_letter_dataset"] = dead_letter_dataset_name
        if contract_report is not None:
            payload["contract"] = contract_report
        return self._single(payload)


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
                result = await ds_svc.run_sql(session, p.sql, owner_id=context.owner_id)
            except ValueError as exc:
                raise NodeExecutionError(str(exc)) from exc
        return self._single({"items": result["rows"], "row_count": result["row_count"], "columns": result["columns"], "duration_ms": result["duration_ms"], "views": result["views"]})


class DatasetExportNode(BaseNode):
    """v45: exports a stored dataset as a downloadable artifact."""

    type = "dataset_export"
    name = "Dataset Export"
    description = (
        "Exports a stored dataset (by name or id) as a downloadable file artifact - "
        "csv (Excel-friendly UTF-8), xlsx, json or parquet."
    )
    category = "actions"
    icon = "download"
    color = "#fbbf24"

    class ParamsModel(BaseModel):
        dataset: str = Field(default="", description="Dataset name (or id)")
        fmt: str = Field(
            default="csv",
            description="Export format",
            json_schema_extra={"widget": "select", "options": ["csv", "xlsx", "json", "parquet"]},
        )

    async def execute(self, context: ExecutionContext) -> NodeResult:
        from ...db import AsyncSessionLocal
        from ...services import artifacts as art_svc
        from ...services import datasets as ds_svc

        p = self.params  # type: DatasetExportNode.ParamsModel
        meta = await _resolve_dataset(p.dataset, owner_id=context.owner_id)
        if meta is None:
            raise NodeExecutionError(f"Dataset {p.dataset!r} not found")
        async with AsyncSessionLocal() as session:
            ds = await ds_svc.get_dataset(session, p.dataset.strip(), owner_id=context.owner_id)
            if ds is None:
                raise NodeExecutionError(f"Dataset {p.dataset!r} not found")
            try:
                data, content_type, ext = ds_svc.export_dataset_bytes(ds, p.fmt)
            except ValueError as exc:
                raise NodeExecutionError(str(exc)) from exc
            saved = await art_svc.save_artifact(
                session,
                kind="file",
                data=data,
                content_type=content_type,
                meta={"dataset": ds.name, "format": p.fmt, "rows": ds.row_count, "node": self.name},
                filename=f"{ds_svc.view_name(ds.name)}.{ext}",
            )
            await session.commit()
        return self._single({
            "dataset": meta["name"],
            "format": p.fmt,
            "rows": meta["row_count"],
            "artifact_id": saved.id,
            "artifact_url": f"/api/v1/artifacts/{saved.id}/content",
            "filename": saved.filename,
            "size_bytes": saved.size_bytes,
        })
