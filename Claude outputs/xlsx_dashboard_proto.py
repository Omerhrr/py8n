"""Prototype for a styled, dashboard-carrying xlsx export.

Tests generic aggregation logic against a synthetic dataset before this
gets folded into services/datasets.py's export_dataset_bytes().
"""
from __future__ import annotations

import json
import io
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.chart import BarChart, PieChart, Reference
from openpyxl.chart.label import DataLabelList

ACCENT = "F97316"
ACCENT_DARK = "C2410C"
INK = "1F2937"
MUTED = "6B7280"
WHITE = "FFFFFF"
TILE_COLORS = ["F97316", "0EA5E9", "22C55E", "A855F7", "EF4444", "0D9488"]

MAX_CHART_CATEGORIES = 12


def jsonable_rows(df: pd.DataFrame) -> list[dict]:
    if len(df) == 0:
        return []
    return json.loads(df.to_json(orient="records", date_format="iso", force_ascii=False))


def _kpi_tile(ws, col0: int, row0: int, width: int, label: str, value: str, fill: str) -> None:
    c0 = get_column_letter(col0)
    c1 = get_column_letter(col0 + width - 1)
    ws.merge_cells(f"{c0}{row0}:{c1}{row0 + 1}")
    vcell = ws[f"{c0}{row0}"]
    vcell.value = value
    vcell.font = Font(size=20, bold=True, color=WHITE)
    vcell.fill = PatternFill("solid", fgColor=fill)
    vcell.alignment = Alignment(horizontal="center", vertical="center")
    ws.merge_cells(f"{c0}{row0 + 2}:{c1}{row0 + 2}")
    lcell = ws[f"{c0}{row0 + 2}"]
    lcell.value = label
    lcell.font = Font(size=10, bold=True, color=WHITE)
    lcell.fill = PatternFill("solid", fgColor=fill)
    lcell.alignment = Alignment(horizontal="center", vertical="center")
    for r in (row0, row0 + 1, row0 + 2):
        for c in range(col0, col0 + width):
            ws.cell(row=r, column=c).fill = PatternFill("solid", fgColor=fill)


def _fmt_num(v: float) -> str:
    if v != v:  # NaN
        return "-"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if float(v).is_integer():
        return f"{int(v):,}"
    return f"{v:,.2f}"


def build_dashboard_xlsx(name: str, df: pd.DataFrame) -> bytes:
    columns = [str(c) for c in df.columns]
    rows = jsonable_rows(df)

    wb = Workbook()
    data_ws = wb.active
    data_ws.title = "Data"
    data_ws.append(columns)
    for r in rows:
        data_ws.append([r.get(c) for c in columns])

    header_font = Font(bold=True, color=WHITE)
    header_fill = PatternFill("solid", fgColor=ACCENT)
    for cell in data_ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    n_rows = len(rows) + 1
    n_cols = len(columns)
    if n_rows > 1 and n_cols > 0:
        last_col = get_column_letter(n_cols)
        table = Table(displayName="DatasetTable", ref=f"A1:{last_col}{n_rows}")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium9", showRowStripes=True)
        data_ws.add_table(table)
    data_ws.freeze_panes = "A2"
    for i, col in enumerate(columns, start=1):
        sample = [len(str(r.get(col, ""))) for r in rows[:200]]
        width = max(10, min(40, max([len(str(col))] + sample) + 2))
        data_ws.column_dimensions[get_column_letter(i)].width = width

    # ---- Dashboard sheet ----
    dash = wb.create_sheet("Dashboard", 0)
    wb.active = 0
    dash.sheet_view.showGridLines = False
    dash.column_dimensions["A"].width = 2

    dash["B2"] = name or "Dataset"
    dash["B2"].font = Font(size=18, bold=True, color=INK)
    dash["B3"] = f"{len(df):,} rows  •  {len(columns)} columns"
    dash["B3"].font = Font(size=11, color=MUTED)

    numeric_cols = [c for c in columns if pd.api.types.is_numeric_dtype(df[c])]
    non_numeric = [c for c in columns if c not in numeric_cols]

    # KPI tiles: row/col counts + up to 3 numeric summaries
    tiles = [("Rows", f"{len(df):,}"), ("Columns", f"{len(columns):,}")]
    for c in numeric_cols[:3]:
        total = df[c].sum()
        tiles.append((f"Sum of {c}"[:24], _fmt_num(total)))
    col0, row0, width = 2, 5, 3
    for i, (label, value) in enumerate(tiles[:5]):
        _kpi_tile(dash, col0 + i * (width + 1), row0, width, label, value, TILE_COLORS[i % len(TILE_COLORS)])

    chart_row = row0 + 5
    helper_col = 20  # far right, hidden helper columns backing the charts

    def cardinality_ok(col):
        n = df[col].nunique(dropna=True)
        return 2 <= n <= MAX_CHART_CATEGORIES

    chart_cat_col = next((c for c in non_numeric if cardinality_ok(c)), None)
    charts_added = 0

    if chart_cat_col:
        if numeric_cols:
            agg = df.groupby(chart_cat_col)[numeric_cols[0]].sum().sort_values(ascending=False)
            metric_label = f"sum of {numeric_cols[0]}"
        else:
            agg = df[chart_cat_col].value_counts()
            metric_label = "count"
        agg = agg.head(MAX_CHART_CATEGORIES)

        hc = helper_col
        dash.cell(row=1, column=hc, value=chart_cat_col)
        dash.cell(row=1, column=hc + 1, value=metric_label)
        for i, (k, v) in enumerate(agg.items(), start=2):
            dash.cell(row=i, column=hc, value=str(k))
            dash.cell(row=i, column=hc + 1, value=float(v))
        dash.column_dimensions[get_column_letter(hc)].hidden = True
        dash.column_dimensions[get_column_letter(hc + 1)].hidden = True

        bar = BarChart()
        bar.type = "col"
        bar.title = f"{metric_label} by {chart_cat_col}"
        bar.y_axis.title = metric_label
        bar.x_axis.title = chart_cat_col
        data_ref = Reference(dash, min_col=hc + 1, min_row=1, max_row=len(agg) + 1)
        cats_ref = Reference(dash, min_col=hc, min_row=2, max_row=len(agg) + 1)
        bar.add_data(data_ref, titles_from_data=True)
        bar.set_categories(cats_ref)
        bar.height, bar.width = 9, 16
        dash.add_chart(bar, f"B{chart_row}")
        charts_added += 1

        if len(agg) >= 2:
            pie = PieChart()
            pie.title = f"{metric_label} share by {chart_cat_col}"
            pie.add_data(data_ref, titles_from_data=True)
            pie.set_categories(cats_ref)
            pie.dataLabels = DataLabelList()
            pie.dataLabels.showPercent = True
            pie.height, pie.width = 9, 12
            dash.add_chart(pie, f"J{chart_row}")
            charts_added += 1
    elif len(numeric_cols) >= 1:
        # no usable categorical column: chart per-column aggregate stats instead
        stats = ["sum", "mean", "max"]
        hc = helper_col
        dash.cell(row=1, column=hc, value="column")
        for j, s in enumerate(stats):
            dash.cell(row=1, column=hc + 1 + j, value=s)
        use_cols = numeric_cols[:8]
        for i, c in enumerate(use_cols, start=2):
            dash.cell(row=i, column=hc, value=c)
            desc = df[c].describe()
            dash.cell(row=i, column=hc + 1, value=float(desc.get("sum", df[c].sum())))
            dash.cell(row=i, column=hc + 2, value=float(desc.get("mean", 0)))
            dash.cell(row=i, column=hc + 3, value=float(desc.get("max", 0)))
        for j in range(4):
            dash.column_dimensions[get_column_letter(hc + j)].hidden = True
        bar = BarChart()
        bar.type = "col"
        bar.title = "Numeric column summary"
        data_ref = Reference(dash, min_col=hc + 1, max_col=hc + 3, min_row=1, max_row=len(use_cols) + 1)
        cats_ref = Reference(dash, min_col=hc, min_row=2, max_row=len(use_cols) + 1)
        bar.add_data(data_ref, titles_from_data=True)
        bar.set_categories(cats_ref)
        bar.height, bar.width = 9, 18
        dash.add_chart(bar, f"B{chart_row}")
        charts_added += 1

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), charts_added


if __name__ == "__main__":
    import numpy as np
    rng = np.random.default_rng(0)
    n = 500
    df = pd.DataFrame({
        "region": rng.choice(["North", "South", "East", "West"], n),
        "product": rng.choice(["Widget", "Gadget", "Gizmo"], n),
        "revenue": rng.normal(1000, 250, n).round(2),
        "units": rng.integers(1, 50, n),
        "signup_date": pd.date_range("2025-01-01", periods=n, freq="h"),
    })
    data, charts = build_dashboard_xlsx("Sales Demo", df)
    with open("/tmp/test_dashboard.xlsx", "wb") as f:
        f.write(data)
    print("wrote", len(data), "bytes,", charts, "charts")

    # numeric-only fallback path
    df2 = pd.DataFrame({"a": rng.normal(size=100), "b": rng.normal(size=100) * 5})
    data2, charts2 = build_dashboard_xlsx("Numeric Only", df2)
    with open("/tmp/test_dashboard_numeric.xlsx", "wb") as f:
        f.write(data2)
    print("wrote", len(data2), "bytes,", charts2, "charts")

    # empty dataframe edge case
    df3 = pd.DataFrame({"x": pd.Series(dtype="float64"), "y": pd.Series(dtype="object")})
    data3, charts3 = build_dashboard_xlsx("Empty", df3)
    with open("/tmp/test_dashboard_empty.xlsx", "wb") as f:
        f.write(data3)
    print("wrote", len(data3), "bytes,", charts3, "charts")
