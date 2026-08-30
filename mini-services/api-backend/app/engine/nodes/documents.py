"""Document nodes (v32) — pull documents into the flow as text + items.

``document_extract`` reads a document from a server path or a URL, runs the
same extraction engines as the Documents API (pdfplumber / OCR / docx /
xlsx / csv / json — see app/services/documents.py) and emits the text plus,
when ``include_items`` is on, the best table as flow items so downstream
nodes (filter, python_transform, dataset_write, chart…) can work with the
document's rows immediately.
"""

from __future__ import annotations

import os
from typing import ClassVar

from pydantic import BaseModel, Field

from ..context import ExecutionContext
from ..nodes.base import BaseNode, NodeExecutionError, NodeResult


class DocumentExtractNode(BaseNode):
    """Extracts text + tabular data from a document (path or URL)."""

    type = "document_extract"
    name = "Document Extract"
    description = (
        "Reads a PDF / image (OCR) / Word / Excel / CSV / JSON document and outputs its "
        "text plus the best table as items — documents in, data out."
    )
    category = "actions"
    icon = "file-text"
    color = "#fb923c"

    class ParamsModel(BaseModel):
        source: str = Field(
            default="path",
            json_schema_extra={"widget": "select", "options": ["path", "url"]},
        )
        path: str = Field(default="", description="File path on the server (Jinja-resolvable)")
        url: str = Field(default="", description="http(s) URL of the document (Jinja-resolvable)")
        include_items: bool = Field(default=True, description="Emit the best table as items")
        coerce_numbers: bool = Field(default=True, description="Cast all-numeric string columns to numbers")

    async def execute(self, context: ExecutionContext) -> NodeResult:
        from ...services import documents as doc_svc

        p = self.params  # type: DocumentExtractNode.ParamsModel
        source = (p.source or "path").lower()
        if source == "url":
            url = (p.url or "").strip()
            if not url:
                raise NodeExecutionError("A document URL is required when source is 'url'")
            import httpx

            try:
                resp = httpx.get(url, timeout=30.0, follow_redirects=True)
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001 — network failures surface as run errors
                raise NodeExecutionError(f"Could not fetch {url!r}: {exc}") from exc
            raw = resp.content
            filename = os.path.basename(url.split("?", 1)[0].rstrip("/")) or "document"
        else:
            path = (p.path or "").strip()
            if not path:
                raise NodeExecutionError("A document path is required when source is 'path'")
            if not os.path.isfile(path):
                raise NodeExecutionError(f"File not found: {path}")
            raw = open(path, "rb").read()
            filename = os.path.basename(path)

        try:
            result = doc_svc.extract_document(filename, raw)
        except ValueError as exc:
            raise NodeExecutionError(str(exc)) from exc

        items: list[dict] = []
        table = doc_svc.best_table(result)
        if p.include_items and table is not None:
            items = doc_svc.table_to_items(table)
            if p.coerce_numbers:
                items = doc_svc.coerce_items_dtypes(items)

        return self._single(
            {
                "items": items,
                "items_count": len(items),
                "source": filename,
                "engine": result["engine"],
                "pages": result["pages"],
                "chars": len(result.get("text", "")),
                "tables_count": len(result.get("tables", [])),
                "text_preview": (result.get("text") or "")[:600],
            }
        )
