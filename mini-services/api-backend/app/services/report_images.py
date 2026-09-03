"""Dashboard report images (v49) - server-side PNG snapshots.

``services.reports`` already snapshots a dashboard as a JSON blob of every
rendered component; this module turns the SAME ``compute_config`` output
into a single shareable image so a scheduled board report lands in
Artifacts as a picture instead of a payload.

Design contracts:

- One vertical figure, ``Agg`` backend, no display dependency: the renderer
  must be safe inside the APScheduler worker thread.
- Every component render is wrapped in try/except - one broken chart
  degrades to a placeholder, never fails the report run.
- Layout lives entirely in a single ``constrained_layout`` gridspec (the
  header is its own row; no ``add_axes``, no ``tight_layout``, no
  ``bbox_inches``) so long tick labels cannot clip.
- Bounded work: at most MAX_COMPONENTS components, and the data is already
  aggregated by ``compute_config`` (charts carry <= 12 buckets), so image
  size stays predictable.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timezone

import matplotlib

matplotlib.use("Agg")  # must precede pyplot - scheduler threads have no display

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

logger = logging.getLogger("py8n.reports.images")

MAX_COMPONENTS = 12
PALETTE = plt.get_cmap("tab10").colors
ACCENT = "#0ea5e9"  # the platform cyan
INK = "#0f172a"
MUTED = "#64748b"

plt.rcParams["font.family"] = "DejaVu Sans"


def _fmt_num(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(f) >= 1000:
        return f"{f:,.0f}"
    return f"{f:,.4g}"


def _no_data(ax) -> None:
    ax.text(0.5, 0.5, "no data", ha="center", va="center", fontsize=9, color=MUTED)
    ax.axis("off")


def _stat_card(ax, comp: dict) -> None:
    ax.axis("off")
    ax.text(0.02, 0.62, str(comp.get("value", "-"))[:24], fontsize=26, fontweight="bold", color=ACCENT, va="center")
    label = comp.get("label") or comp.get("agg", "count")
    if comp.get("column"):
        label = f"{label} {comp['column']}"
    ax.text(0.02, 0.16, str(label)[:60], fontsize=10, color=MUTED, va="center")


def _bar_line(ax, comp: dict, chart_type: str) -> None:
    labels = [str(x)[:16] for x in comp.get("labels") or []]
    values = [float(v) for v in comp.get("values") or []]
    if not labels:
        _no_data(ax)
        return
    x = np.arange(len(labels))
    if chart_type in ("line", "area"):
        ax.plot(x, values, color=ACCENT, linewidth=2.2, marker="o", markersize=4)
        if chart_type == "area":
            ax.fill_between(x, values, color=ACCENT, alpha=0.18)
    else:
        ax.bar(x, values, color=ACCENT, width=0.62, alpha=0.9)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=7.5, rotation=30, ha="right", color=INK)
    ax.tick_params(axis="y", labelsize=7.5, colors=MUTED)
    ax.grid(axis="y", alpha=0.25, linewidth=0.6)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.margins(y=0.18)


def _pie(ax, comp: dict, chart_type: str) -> None:
    labels = [str(x)[:18] for x in comp.get("labels") or []]
    values = [float(v) for v in comp.get("values") or []]
    if not labels or sum(values) <= 0:
        _no_data(ax)
        return
    wedges, _texts, auto = ax.pie(
        values,
        labels=labels,
        autopct="%1.0f%%",
        pctdistance=0.78,
        colors=[PALETTE[i % len(PALETTE)] for i in range(len(labels))],
        wedgeprops={"width": 0.42} if chart_type == "donut" else {},
        textprops={"fontsize": 7.5, "color": INK},
    )
    for t in auto:
        t.set_fontsize(6.5)
        t.set_color("white")


def _scatter(ax, comp: dict) -> None:
    pts = comp.get("points") or []
    if not pts:
        _no_data(ax)
        return
    xs = [p.get("x") for p in pts]
    ys = [p.get("y") for p in pts]
    ax.scatter(xs, ys, s=16, color=ACCENT, alpha=0.75, edgecolors="none")
    ax.set_xlabel(str(comp.get("x", "x")), fontsize=8, color=MUTED)
    ax.set_ylabel(str(comp.get("y", "y")), fontsize=8, color=MUTED)
    ax.tick_params(labelsize=7, colors=MUTED)
    ax.grid(alpha=0.25, linewidth=0.6)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)


def _table(ax, comp: dict) -> None:
    cols = comp.get("columns") or []
    rows = comp.get("rows") or []
    ax.axis("off")
    if not cols or not rows:
        _no_data(ax)
        return
    cell = [[_fmt_num(r.get(c)) for c in cols] for r in rows]
    tab = ax.table(cellText=cell, colLabels=cols, loc="center", cellLoc="left")
    tab.auto_set_font_size(False)
    tab.set_fontsize(7.5)
    tab.scale(1, 1.25)
    for (r, _c), cell_ref in tab.get_celld().items():
        cell_ref.set_edgecolor("#e2e8f0")
        if r == 0:
            cell_ref.set_facecolor("#f1f5f9")
            cell_ref.set_text_props(fontweight="bold", color=INK)
        else:
            cell_ref.set_text_props(color=INK)


def _placeholder(ax, title: str) -> None:
    ax.axis("off")
    ax.text(0.02, 0.5, f"{title} - not renderable in the image snapshot", fontsize=9, color=MUTED, va="center")


def render_dashboard_png(
    name: str,
    components: list[dict],
    *,
    generated_at: datetime | None = None,
    component_id: str | None = None,
    drilldown: dict | None = None,
) -> bytes:
    """Rendered component list -> PNG bytes (never raises for bad components).

    v54 drilldowns: with ``drilldown`` metadata set, every component gets a
    caption strip beneath it - ``source: <dataset> · drilldown: <url>`` -
    where the URL points at the runtime board with ``?c=<component id>``
    (the runtime highlights and scrolls to that component). With
    ``component_id`` set, ONLY that component renders (plus the header
    band) - the drilldown target rendered standalone.
    """
    stamp = (generated_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    comps = [c for c in (components or []) if isinstance(c, dict)]
    if component_id:
        comps = [c for c in comps if str(c.get("id") or "") == str(component_id)]
    comps = comps[:MAX_COMPONENTS]
    dd = drilldown or {}
    base = str(dd.get("base_url") or "").rstrip("/")
    slug = str(dd.get("slug") or "")
    ds_names = dd.get("datasets") or {}

    def _ref(comp: dict) -> str:
        cid = comp.get("id") or ""
        return f"{base}/d/{slug}?c={cid}" if slug else (f"?c={cid}" if cid else "")

    def _caption(comp: dict) -> str:
        parts = []
        src = ds_names.get(comp.get("dataset_id") or comp.get("id"))
        if src:
            parts.append(f"source: {src}")
        ref = _ref(comp)
        if ref:
            parts.append(f"drilldown: {ref}")
        return "  ·  ".join(parts)

    stats = [c for c in comps if c.get("type") in ("stat", "kpi")]
    body = [c for c in comps if c.get("type") not in ("stat", "kpi")]

    # One gridspec owns the whole figure: header row + one row per component
    # (v54: each row may carry a thin caption strip for its drilldown link).
    cap_h = 0.34 if dd else 0.0
    units = [0.9]  # header band
    if stats:
        units.append(1.7 + (cap_h if any(_caption(c) for c in stats) else 0.0))
    for comp in body:
        units.append(3.1 + (cap_h if _caption(comp) else 0.0))
    if not comps:
        units.append(2.0)
    fig_h = 0.55 + sum(units)
    fig = plt.figure(figsize=(12.5, fig_h), dpi=110, constrained_layout=True)
    gs = fig.add_gridspec(len(units), 1, height_ratios=units)

    # header band (a regular subplot row so constrained_layout reserves space)
    hdr = fig.add_subplot(gs[0])
    hdr.axis("off")
    hdr.add_patch(plt.Rectangle((0, 0), 1, 1, transform=hdr.transAxes, color=INK, zorder=0))
    hdr.text(0.03, 0.6, str(name)[:80], fontsize=17, fontweight="bold", color="white", va="center", zorder=1)
    hdr.text(0.03, 0.18, f"dashboard report - generated {stamp}", fontsize=9, color="#7dd3fc", va="center", zorder=1)

    def _title(ax, comp: dict) -> None:
        title = str(comp.get("title") or comp.get("id") or "")[:70]
        ax.set_title(title, fontsize=10, fontweight="bold", color=INK, loc="left")

    row = 1
    if stats:
        n = max(len(stats), 1)
        sgs = gs[row].subgridspec(2 if any(_caption(c) for c in stats) else 1, 1, height_ratios=[1.7, cap_h] if any(_caption(c) for c in stats) else None)
        chart_area = sgs[0].subgridspec(1, n)
        for i, comp in enumerate(stats):
            ax = fig.add_subplot(chart_area[0, i])
            try:
                _stat_card(ax, comp)
            except Exception:  # noqa: BLE001 - degrade, never fail the report
                logger.warning("stat card %s failed to render", comp.get("id"), exc_info=True)
                _placeholder(ax, comp.get("label") or "stat")
        if any(_caption(c) for c in stats):
            cap_ax = fig.add_subplot(sgs[1])
            cap_ax.axis("off")
            joined = "   |   ".join(filter(None, (_caption(c) for c in stats)))
            cap_ax.text(0.0, 0.5, joined[:200], fontsize=7.5, color=MUTED, va="center", ha="left", family="monospace")
        row += 1

    for comp in body:
        has_cap = bool(_caption(comp))
        if has_cap:
            rgs = gs[row].subgridspec(2, 1, height_ratios=[3.1, cap_h])
            ax = fig.add_subplot(rgs[0])
        else:
            ax = fig.add_subplot(gs[row])
        try:
            ctype = comp.get("type")
            if ctype == "chart":
                ct = comp.get("chart_type", "bar")
                if ct in ("pie", "donut"):
                    _pie(ax, comp, ct)
                elif ct == "scatter":
                    _scatter(ax, comp)
                else:
                    _bar_line(ax, comp, ct)
                _title(ax, comp)
            elif ctype == "table":
                _table(ax, comp)
                _title(ax, comp)
            else:  # markdown / filter / future types
                _placeholder(ax, str(comp.get("title") or ctype or "component"))
        except Exception:  # noqa: BLE001 - degrade, never fail the report
            logger.warning("component %s failed to render", comp.get("id"), exc_info=True)
            _placeholder(ax, str(comp.get("title") or "component"))
        if has_cap:
            cap_ax = fig.add_subplot(rgs[1])
            cap_ax.axis("off")
            cap_ax.text(0.0, 0.5, _caption(comp)[:180], fontsize=7.5, color=MUTED, va="center", ha="left", family="monospace")
        row += 1

    if not comps:
        ax = fig.add_subplot(gs[row])
        ax.axis("off")
        ax.text(0.5, 0.5, "no components configured" if not component_id else f"component {component_id!r} not found", ha="center", va="center", fontsize=11, color=MUTED)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="white")
    plt.close(fig)
    return buf.getvalue()
