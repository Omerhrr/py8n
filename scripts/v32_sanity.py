"""v32 sanity - verify extraction engines work end-to-end BEFORE writing tests.

Generates: (1) reportlab PDF with a ruled table, (2) PIL image with text.
Verifies: pdfplumber text+table extraction, tesseract OCR, csv/json/xlsx paths.
"""

import io
import json
import sys

sys.path.insert(0, "/home/z/my-project/mini-services/api-backend")

from app.services import documents as doc_svc

# 1) PDF with ruled table via reportlab
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
from reportlab.lib.styles import getSampleStyleSheet

buf = io.BytesIO()
story = [
    Paragraph("ACME Consulting - Invoice INV-2026-004", getSampleStyleSheet()["Title"]),
    Paragraph("Client: Globex Ltd. Terms: Net 30", getSampleStyleSheet()["Normal"]),
]
cell_rows = [
    ["Item", "Qty", "Unit Price", "Amount"],
    ["Data pipeline build", "1", "4200.00", "4200.00"],
    ["Support retainer", "3", "350.00", "1050.00"],
    ["Training workshop", "2", "500.00", "1000.00"],
]
tbl = Table(cell_rows)
tbl.setStyle(
    TableStyle(
        [
            ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ]
    )
)
story.append(tbl)
story.append(Paragraph("Total Due: 6250.00 USD", getSampleStyleSheet()["Normal"]))
SimpleDocTemplate(buf, pagesize=letter).build(story)
pdf_bytes = buf.getvalue()

r = doc_svc.extract_document("invoice.pdf", pdf_bytes)
print("PDF:", r["engine"], "pages", r["pages"], "tables", len(r["tables"]), "chars", len(r["text"]))
print("  text head:", r["text"][:90].replace("\n", " / "))
if r["tables"]:
    t = r["tables"][0]
    print("  table:", t["n_rows"], "x", t["n_cols"], "| header:", t["rows"][0])
best = doc_svc.best_table(r)
print("  best_table items:", json.dumps(doc_svc.table_to_items(best)[:1]) if best else None)

# 2) OCR image via PIL + tesseract
from PIL import Image, ImageDraw, ImageFont

img = Image.new("RGB", (640, 200), "white")
d = ImageDraw.Draw(img)
try:
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 30)
except Exception:
    font = ImageFont.load_default()
d.text((20, 30), "INVOICE TOTAL 6250 USD", fill="black", font=font)
d.text((20, 100), "DUE DATE 2026-09-30", fill="black", font=font)
ibuf = io.BytesIO()
img.save(ibuf, format="PNG")
r2 = doc_svc.extract_document("scan.png", ibuf.getvalue())
print("OCR:", r2["engine"], "chars", len(r2["text"]), "| text:", " ".join(r2["text"].split())[:80])

# 3) csv + json quick paths
r3 = doc_svc.extract_document("t.csv", b"city,units\nLagos,35\nBerlin,25\n")
print("CSV:", r3["engine"], r3["tables"][0]["n_rows"], "rows, header", r3["tables"][0]["rows"][0])
r4 = doc_svc.extract_document("t.json", json.dumps([{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]).encode())
print("JSON:", r4["engine"], r4["tables"][0]["n_rows"], "rows")

# 4) xlsx via openpyxl
import openpyxl

wb = openpyxl.Workbook()
ws = wb.active
ws.append(["region", "revenue"])
ws.append(["Lagos", 12000])
ws.append(["Berlin", 9000])
xb = io.BytesIO()
wb.save(xb)
r5 = doc_svc.extract_document("t.xlsx", xb.getvalue())
print("XLSX:", r5["engine"], "pages", r5["pages"], r5["tables"][0]["n_rows"], "rows")

# 5) guards
try:
    doc_svc.extract_document("virus.exe", b"MZ...")
    print("GUARD: FAIL - .exe accepted")
except ValueError as e:
    print("GUARD ok:", str(e)[:60])
try:
    doc_svc.extract_document("note.pdf", b"")
    print("GUARD: FAIL - empty accepted")
except ValueError as e:
    print("GUARD ok:", str(e)[:60])

print("SANITY DONE")
