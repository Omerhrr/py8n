"""Data-science nodes (v28) - Python transforms, charts, ML training.

* ``python_transform`` - real pandas/numpy against the incoming items as a
  DataFrame (``df``); set ``result`` to a DataFrame / list / dict. Imports
  are whitelisted (pandas, numpy, sklearn + a few stdlib modules); stdout is
  captured and surfaced as ``logs``.
* ``chart`` - no-code matplotlib chart over the input items, saved as a PNG
  artifact and rendered inline in the executions drawer.
* ``model_train`` - trains a curated sklearn model on the input items,
  evaluates on a held-out split, pickles the model as an artifact, and
  returns metrics + a prediction sample.

All three follow the item model (``items`` in the payload; single dict
wrapped) and resolve Jinja in their parameters like every other node.
"""

from __future__ import annotations

import asyncio
import io
import json
import pickle
from typing import Any, ClassVar

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from ..context import ExecutionContext
from .base import BaseNode, NodeExecutionError
from .data import _items, _working_data
from .logic import SAFE_BUILTINS


# ----------------------------------------------------------------- sandbox
ALLOWED_IMPORTS = {
    "pandas": pd,
    "numpy": np,
    "math": __import__("math"),
    "statistics": __import__("statistics"),
    "datetime": __import__("datetime"),
    "random": __import__("random"),
    "itertools": __import__("itertools"),
    "collections": __import__("collections"),
}
_IMPORT_ERROR = (
    "import of {name!r} is not allowed in python_transform - "
    "allowed: pandas, numpy, sklearn, math, statistics, datetime, random, itertools, collections"
)


def _make_import_hook(code_ns: dict[str, Any]):
    """Whitelisted __import__: top-level modules plus any sklearn submodule."""

    def _import(name: str, *args: Any, **kwargs: Any):
        root = name.split(".")[0]
        if root == "sklearn":
            return __import__(name, *args, **kwargs)
        if name in ALLOWED_IMPORTS:
            return ALLOWED_IMPORTS[name]
        raise NodeExecutionError(_IMPORT_ERROR.format(name=name))

    return _import


DS_BUILTINS = {k: v for k, v in SAFE_BUILTINS.items()}
DS_BUILTINS["print"] = print  # stdout is captured per-run (logic.py discards it)
DS_BUILTINS["__import__"] = _make_import_hook({})


def _jsonable_out(obj: Any) -> list[dict]:
    from ...services.datasets import jsonable_rows

    if isinstance(obj, pd.DataFrame):
        return jsonable_rows(obj)
    if isinstance(obj, pd.Series):
        return jsonable_rows(obj.to_frame())
    if isinstance(obj, dict):
        return [obj]
    if isinstance(obj, list):
        return obj
    raise NodeExecutionError(f"result must be a DataFrame, list or dict - got {type(obj).__name__}")


def _input_df(context: ExecutionContext) -> pd.DataFrame:
    items = _items(_working_data(context.current_input))
    rows = [r for r in items if isinstance(r, dict)]
    return pd.DataFrame(rows)


# ----------------------------------------------------------------- node 1
class PythonTransformNode(BaseNode):
    """Runs pandas/numpy code over the input items as a DataFrame."""

    type = "python_transform"
    name = "Python Transform"
    description = "Data-frame code: the input items arrive as `df` (pandas) - set `result` to a DataFrame/list/dict. pd, np and sklearn are preloaded."
    category = "actions"
    icon = "file-code"
    color = "#2dd4bf"

    class ParamsModel(BaseModel):
        code: str = Field(
            default="result = df",
            description="Python code with `df` (pandas DataFrame) in scope - set `result`",
            json_schema_extra={"widget": "code", "rows": 12, "language": "python",
                               "hint": "result = df[df.ltv > 100]"},
        )
        timeout_seconds: float = Field(default=30, ge=1, le=120)

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: PythonTransformNode.ParamsModel
        if not p.code or not p.code.strip():
            raise NodeExecutionError("Python code is required")
        df = _input_df(context)

        buf = io.StringIO()
        user_globals: dict[str, Any] = {"__builtins__": dict(DS_BUILTINS)}
        user_globals.update(ALLOWED_IMPORTS)
        user_globals["df"] = df
        user_globals["result"] = None

        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, self._exec_sync, p.code, user_globals, buf),
                timeout=p.timeout_seconds,
            )
        except asyncio.TimeoutError:
            raise NodeExecutionError(f"python_transform timed out after {p.timeout_seconds}s") from None
        except NodeExecutionError:
            raise

        out = user_globals.get("result")
        if out is None:
            out = user_globals.get("df")  # in-place mutation fallback
        rows = _jsonable_out(out)
        logs = buf.getvalue()[-4000:]
        payload: dict[str, Any] = {"items": rows, "rows_in": int(len(df)), "rows_out": len(rows)}
        if logs.strip():
            payload["logs"] = logs
        return self._single(payload)

    @staticmethod
    def _exec_sync(code: str, user_globals: dict[str, Any], buf: io.StringIO) -> None:
        import contextlib

        try:
            with contextlib.redirect_stdout(buf):
                exec(code, user_globals)  # noqa: S102 (sandboxed namespace, self-hosted tool)
        except NodeExecutionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise NodeExecutionError(f"{type(exc).__name__}: {exc}") from exc


# ----------------------------------------------------------------- helpers
def _require_columns(df: pd.DataFrame, wanted: list[str]) -> None:
    missing = [c for c in wanted if c not in df.columns]
    if missing:
        raise NodeExecutionError(
            f"column(s) {missing} not found - available: {[str(c) for c in df.columns]}"
        )


async def _save_artifact_row(context: ExecutionContext, **kwargs) -> Any:
    from ...db import AsyncSessionLocal
    from ...services import artifacts as art_svc

    async with AsyncSessionLocal() as session:
        row = await art_svc.save_artifact(
            session,
            workflow_id=getattr(context, "workflow_id", None),
            execution_id=getattr(context, "execution_id", None),
            **kwargs,
        )
        await session.commit()
        return {"id": row.id, "filename": row.filename, "content_type": row.content_type}


# ----------------------------------------------------------------- node 2
class ChartNode(BaseNode):
    """Renders a matplotlib chart over the input items and stores the PNG."""

    type = "chart"
    name = "Chart"
    description = "Plots the input items (bar / line / scatter / hist / pie) and saves a PNG artifact - rendered inline in the execution drawer."
    category = "actions"
    icon = "bar-chart-3"
    color = "#fb923c"

    class ParamsModel(BaseModel):
        chart_type: str = Field(
            default="bar",
            json_schema_extra={"widget": "select", "options": ["bar", "line", "scatter", "hist", "pie"]},
        )
        x: str = Field(default="", description="X column (categories / time). Empty = row index")
        y: str = Field(default="", description="Y column(s), comma-separated for multi-series")
        title: str = Field(default="Chart", description="Chart title")
        color: str = Field(default="#f97316", description="Hex color for single-series charts")

    async def execute(self, context: ExecutionContext) -> NodeResult:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        p = self.params  # type: ChartNode.ParamsModel
        df = _input_df(context)
        if df.empty:
            raise NodeExecutionError("Chart needs input items - connect a source (dataset_read, sql_query, …)")

        ys = [s.strip() for s in (p.y or "").split(",") if s.strip()]
        if not ys:
            raise NodeExecutionError("A y column is required")
        if p.chart_type == "pie" and len(ys) > 1:
            raise NodeExecutionError("Pie charts take exactly one y column")
        _require_columns(df, ys)
        if p.x:
            _require_columns(df, [p.x])

        fig, ax = plt.subplots(figsize=(8, 4.8), constrained_layout=True)
        try:
            x_vals = df[p.x] if p.x else df.index
            if p.chart_type in ("bar", "line"):
                for col in ys:
                    series = pd.to_numeric(df[col], errors="coerce")
                    if p.chart_type == "bar":
                        ax.bar([str(v) for v in x_vals], series, label=col, color=p.color if len(ys) == 1 else None)
                    else:
                        ax.plot(x_vals, series, label=col, marker="o", markersize=3,
                                color=p.color if len(ys) == 1 else None)
                if len(ys) > 1:
                    ax.legend(loc="upper left", bbox_to_anchor=(1.02, 1), frameon=False)
            elif p.chart_type == "scatter":
                ycol = ys[0]
                ax.scatter(x_vals, pd.to_numeric(df[ycol], errors="coerce"), s=28, color=p.color)
            elif p.chart_type == "hist":
                ycol = ys[0]
                ax.hist(pd.to_numeric(df[ycol], errors="coerce").dropna(), bins=min(30, max(5, len(df) // 3)), color=p.color, edgecolor="white")
            elif p.chart_type == "pie":
                ycol = ys[0]
                vals = pd.to_numeric(df[ycol], errors="coerce").fillna(0)
                ax.pie(vals, labels=[str(v) for v in x_vals], autopct="%1.1f%%",
                       colors=plt.cm.tab20.colors if len(df) > 1 else None)
            else:
                raise NodeExecutionError(f"Unknown chart_type {p.chart_type!r}")

            ax.set_title(p.title or "Chart", fontsize=11)
            if p.chart_type in ("bar", "line") and p.x:
                ax.set_xlabel(p.x)
                ax.tick_params(axis="x", rotation=30, labelsize=8)
            ax.grid(True, axis="y" if p.chart_type in ("bar", "line", "hist") else "both", alpha=0.25)
        finally:
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=110)
            plt.close(fig)

        saved = await _save_artifact_row(
            context,
            kind="chart",
            data=buf.getvalue(),
            content_type="image/png",
            meta={"title": p.title, "chart_type": p.chart_type, "node": self.name},
            filename="chart.png",
        )
        return self._single({
            "items": _items(_working_data(context.current_input)),
            "artifact_id": saved["id"],
            "artifact_url": f"/api/v1/artifacts/{saved['id']}/content",
            "title": p.title,
            "chart_type": p.chart_type,
            "points": int(len(df)),
        })


# ----------------------------------------------------------------- node 3
class ModelTrainNode(BaseNode):
    """Trains a curated sklearn model on the input items."""

    type = "model_train"
    name = "Model Train"
    description = "Trains an sklearn model (random forest / logistic regression / linear regression) on the input items - returns metrics, a prediction sample and a pickled model artifact."
    category = "ai"
    icon = "network"
    color = "#818cf8"

    class ParamsModel(BaseModel):
        model: str = Field(
            default="random_forest_classifier",
            json_schema_extra={"widget": "select", "options": [
                "random_forest_classifier", "logistic_regression", "linear_regression",
            ]},
        )
        target: str = Field(default="", description="Target column to predict")
        features: str = Field(default="", description="Comma-separated feature columns (empty = all other numeric columns)")
        test_size: float = Field(default=0.2, ge=0.1, le=0.5, description="Held-out fraction for evaluation")

    async def execute(self, context: ExecutionContext) -> NodeResult:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.linear_model import LinearRegression, LogisticRegression
        from sklearn.metrics import accuracy_score, f1_score, mean_absolute_error, mean_squared_error, r2_score
        from sklearn.model_selection import train_test_split
        from sklearn.preprocessing import LabelEncoder

        p = self.params  # type: ModelTrainNode.ParamsModel
        if not p.target:
            raise NodeExecutionError("A target column is required")
        df = _input_df(context)
        if len(df) < 10:
            raise NodeExecutionError(f"Model training needs at least 10 rows (got {len(df)})")
        if p.target not in df.columns:
            raise NodeExecutionError(f"Target column {p.target!r} not found - available: {[str(c) for c in df.columns]}")

        if p.features.strip():
            feats = [f.strip() for f in p.features.split(",") if f.strip()]
            _require_columns(df, feats)
        else:
            feats = [c for c in df.select_dtypes(include=["number", "bool"]).columns if c != p.target]
        if not feats:
            raise NodeExecutionError("No numeric feature columns available - pass `features` or add numeric columns")

        data = df[feats + [p.target]].dropna()
        if len(data) < 10:
            raise NodeExecutionError(f"Only {len(data)} complete rows after dropping NaNs - need 10+")

        is_classifier = p.model in ("random_forest_classifier", "logistic_regression")
        y_raw = data[p.target]
        labeler = None
        if is_classifier and (y_raw.dtype == object or y_raw.dtype == bool):
            labeler = LabelEncoder()
            y_enc = pd.Series(labeler.fit_transform(y_raw), index=y_raw.index)
        else:
            y_enc = pd.Series(pd.to_numeric(y_raw, errors="coerce").fillna(0).values, index=y_raw.index)
        X = data[feats].astype(float)
        if is_classifier and y_enc.nunique() < 2:
            raise NodeExecutionError("Classification needs at least 2 distinct target classes")

        X_tr, X_te, y_tr, y_te = train_test_split(X, y_enc, test_size=p.test_size, random_state=42)
        if p.model == "random_forest_classifier":
            mdl = RandomForestClassifier(n_estimators=100, random_state=42)
        elif p.model == "logistic_regression":
            mdl = LogisticRegression(max_iter=1000)
        elif p.model == "linear_regression":
            mdl = LinearRegression()
        else:
            raise NodeExecutionError(f"Unknown model {p.model!r}")

        mdl.fit(X_tr, y_tr)
        pred = mdl.predict(X_te)

        def _readable(series: pd.Series) -> list[Any]:
            if labeler is not None:
                return [str(v) for v in labeler.inverse_transform(series.astype(int))]
            return [json.loads(json.dumps(float(v))) for v in series]

        metrics: dict[str, Any] = {}
        if is_classifier:
            metrics = {
                "accuracy": round(float(accuracy_score(y_te, pred)), 4),
                "f1_weighted": round(float(f1_score(y_te, pred, average="weighted", zero_division=0)), 4),
            }
        else:
            metrics = {
                "r2": round(float(r2_score(y_te, pred)), 4),
                "mae": round(float(mean_absolute_error(y_te, pred)), 4),
                "mse": round(float(mean_squared_error(y_te, pred)), 4),
            }
        if is_classifier and hasattr(mdl, "feature_importances_"):
            metrics["feature_importances"] = {f: round(float(v), 4) for f, v in sorted(zip(feats, mdl.feature_importances_), key=lambda kv: -kv[1])}
        if p.model == "linear_regression" and hasattr(mdl, "coef_") and getattr(mdl.coef_, "ndim", 1) == 1:
            metrics["coefficients"] = {f: round(float(v), 4) for f, v in zip(feats, mdl.coef_)}

        sample = pd.DataFrame({"actual": y_te, "predicted": pred}).head(20)
        predictions = [
            {"actual": a, "predicted": b}
            for a, b in zip(_readable(sample["actual"]), _readable(sample["predicted"]))
        ]

        saved = await _save_artifact_row(
            context,
            kind="model",
            data=pickle.dumps(mdl),
            content_type="application/octet-stream",
            meta={"model": p.model, "target": p.target, "features": feats, "metrics": metrics, "node": self.name},
            filename="model.pkl",
        )
        return self._single({
            "items": predictions,
            "metrics": metrics,
            "model_id": saved["id"],
            "artifact_url": f"/api/v1/artifacts/{saved['id']}/content",
            "model": p.model,
            "target": p.target,
            "features": feats,
            "rows_used": int(len(data)),
        })
