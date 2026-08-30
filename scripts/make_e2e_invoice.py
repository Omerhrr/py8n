"""Generate a realistic invoice PDF for the v32 E2E (ruled table + totals)."""

import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OUT = Path("/home/z/my-project/download/e2e-v32-invoice.pdf")

doc_buf = Path("/home/z/my-project/scripts/.e2e_invoice.pdf")
buf = doc_buf.read_bytes() if False else None

story = []
title = ParagraphStyle("t", parent=getSampleStyleSheet()["Title"], fontSize=18, spaceAfter=4)
meta = getSampleStyleSheet()["Normal"]

rows = [
    ["Item", "Qty", "Unit Price", "Amount"],
    ["Data pipeline build", "1", "4200.00", "4200.00"],
    ["Support retainer (months)", "3", "350.00", "1050.00"],
    ["Training workshop", "2", "500.00", "1000.00"],
    ["Dashboard customization", "1", "1450.00", "1450.00"],
]
tbl = Table(rows, colWidths=[220, 60, 100, 100])
tbl.setStyle(
    TableStyle(
        [
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#333333")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
            ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#111111")),
        ]
    )
)
story.append(Paragraph("ACME Consulting — Invoice INV-2026-0417", title))
story.append(Paragraph("Client: Globex Ltd. · Issued: 2026-08-30 · Terms: Net 30", meta))
story.append(Spacer(1, 14))
story.append(tbl)
story.append(Spacer(1, 12))
story.append(Paragraph("<b>Total Due: 7700.00 USD</b>", meta))

SimpleDocTemplate(str(OUT), pagesize=letter).build(story)
print(f"invoice written: {OUT} ({OUT.stat().st_size} bytes)")
