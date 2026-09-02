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
from .. import sandbox
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
    "allowed: pandas, numpy, math, statistics, datetime, random, itertools, collections"
)


def _make_import_hook(code_ns: dict[str, Any]):
    """Whitelisted __import__: top-level modules plus any sklearn submodule."""

    def _import(name: str, *args: Any, **kwargs: Any):
        root = name.split(".")[0]
        if root == "sklearn":
            return __import__(name, *args, **kwargs)
        if name in ALLOWED_IMPORTS:
            # Return the PROXIED module so an explicit import can never
            # rebind the raw module object over the sandbox proxy.
            from ..sandbox import ModuleProxy

            return ModuleProxy(ALLOWED_IMPORTS[name], name)
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

        # Audit hardening: AST guard + module proxies + bounded pool
        # (see app/engine/sandbox.py). The working DataFrame stays shared by
        # reference on purpose - in-place mutation is a documented feature.
        try:
            code_obj = sandbox.guard(p.code, ALLOWED_IMPORTS, extra_roots={"sklearn"})
        except sandbox.SandboxViolation as exc:
            raise NodeExecutionError(f"python_transform rejected by sandbox: {exc}") from exc
        sandbox.make_proxies(user_globals, ALLOWED_IMPORTS)
        sandbox.deepcopy_state(user_globals, skip={"df", "result"})
        try:
            await sandbox.run_bounded(
                lambda: self._exec_sync(code_obj, user_globals, buf),
                timeout_seconds=p.timeout_seconds,
                label="python_transform",
            )
        except sandbox.SandboxTimeout:
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
    def _exec_sync(code_obj, user_globals: dict[str, Any], buf: io.StringIO) -> None:
        import contextlib

        try:
            with contextlib.redirect_stdout(buf):
                exec(code_obj, user_globals)  # noqa: S102 - sandboxed, see sandbox.py
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
# v46: the curated algorithm zoo - 4 classifiers + 5 regressors
CLASSIFIERS = ("random_forest_classifier", "gradient_boosting_classifier", "decision_tree_classifier", "logistic_regression")
REGRESSORS = ("random_forest_regressor", "gradient_boosting_regressor", "decision_tree_regressor", "linear_regression", "ridge_regression")

# whitelisted hyperparameter passthrough (validated + type-coerced)
_HYPERPARAM_TYPES = {
    "n_estimators": int, "max_depth": int, "min_samples_split": int,
    "min_samples_leaf": int, "max_iter": int, "n_neighbors": int,
    "learning_rate": float, "subsample": float, "C": float, "alpha": float,
    "class_weight": str,
}

MAX_CATEGORICAL_CARDINALITY = 50  # auto-selection skips text cols beyond this


def _make_estimator(algorithm: str, hyperparams: dict):
    from sklearn.ensemble import (
        GradientBoostingClassifier,
        GradientBoostingRegressor,
        RandomForestClassifier,
        RandomForestRegressor,
    )
    from sklearn.linear_model import LinearRegression, LogisticRegression, Ridge
    from sklearn.tree import DecisionTreeClassifier, DecisionTreeRegressor

    factories = {
        "random_forest_classifier": (RandomForestClassifier, {"n_estimators": 100, "random_state": 42}),
        "random_forest_regressor": (RandomForestRegressor, {"n_estimators": 100, "random_state": 42}),
        "gradient_boosting_classifier": (GradientBoostingClassifier, {"random_state": 42}),
        "gradient_boosting_regressor": (GradientBoostingRegressor, {"random_state": 42}),
        "decision_tree_classifier": (DecisionTreeClassifier, {"random_state": 42}),
        "decision_tree_regressor": (DecisionTreeRegressor, {"random_state": 42}),
        "logistic_regression": (LogisticRegression, {"max_iter": 1000}),
        "linear_regression": (LinearRegression, {}),
        "ridge_regression": (Ridge, {"random_state": 42}),
    }
    cls, defaults = factories[algorithm]
    kwargs = {**defaults, **hyperparams}
    try:
        return cls(**kwargs)
    except (TypeError, ValueError) as exc:
        raise NodeExecutionError(f"Model Train: bad hyperparameters for {algorithm}: {exc}") from exc


def _build_pipeline(algorithm: str, hyperparams: dict, numeric_cols: list[str], categorical_cols: list[str], scale: str):
    """Impute + (scale) + one-hot preprocessing in front of the estimator -
    the WHOLE pipeline is pickled so model_predict reproduces training-time
    preprocessing exactly (a serving-time correctness guarantee)."""
    from sklearn.compose import ColumnTransformer
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import MinMaxScaler, OneHotEncoder, StandardScaler

    num_steps = [("impute", SimpleImputer(strategy="median"))]
    if scale == "standard":
        num_steps.append(("scale", StandardScaler()))
    elif scale == "minmax":
        num_steps.append(("scale", MinMaxScaler()))
    transformers = []
    if numeric_cols:
        transformers.append(("num", Pipeline(num_steps), numeric_cols))
    if categorical_cols:
        transformers.append((
            "cat",
            Pipeline([
                ("impute", SimpleImputer(strategy="most_frequent")),
                ("onehot", OneHotEncoder(handle_unknown="ignore", sparse_output=False)),
            ]),
            categorical_cols,
        ))
    ct = ColumnTransformer(transformers, remainder="drop")
    return Pipeline([("prep", ct), ("model", _make_estimator(algorithm, hyperparams))])


class ModelTrainNode(BaseNode):
    """v46: production-grade training - 9 algorithms, preprocessing pipeline,
    cross-validation, stratified splits, rich metrics and a versioned model
    registry."""

    type = "model_train"
    name = "Model Train"
    description = (
        "Trains an sklearn model on the input items - 9 algorithms (random forest / "
        "gradient boosting / decision tree / logistic / linear / ridge), automatic "
        "impute+scale+one-hot preprocessing (persisted with the model), optional "
        "cross-validation, rich metrics and versioned model registry registration."
    )
    category = "ai"
    icon = "network"
    color = "#818cf8"

    class ParamsModel(BaseModel):
        model: str = Field(
            default="random_forest_classifier",
            json_schema_extra={"widget": "select", "options": [
                "random_forest_classifier", "gradient_boosting_classifier",
                "decision_tree_classifier", "logistic_regression",
                "random_forest_regressor", "gradient_boosting_regressor",
                "decision_tree_regressor", "linear_regression", "ridge_regression",
            ]},
        )
        task: str = Field(
            default="auto",
            description="auto infers from the target column dtype",
            json_schema_extra={"widget": "select", "options": ["auto", "classification", "regression"]},
        )
        target: str = Field(default="", description="Target column to predict")
        features: str = Field(default="", description="Comma-separated feature columns (empty = all other usable columns)")
        test_size: float = Field(default=0.2, ge=0.1, le=0.5, description="Held-out fraction for evaluation")
        random_state: int = Field(default=42, description="Seed for split + estimators")
        scale: str = Field(
            default="none",
            description="Numeric-feature scaling (persisted with the model)",
            json_schema_extra={"widget": "select", "options": ["none", "standard", "minmax"]},
        )
        cross_validation: int = Field(default=0, ge=0, le=10, description="K-fold CV on the training data (0 = off, 2-10)")
        hyperparams: dict = Field(
            default_factory=dict,
            description='Algorithm kwargs, e.g. {"n_estimators": 200, "max_depth": 6}',
            json_schema_extra={"widget": "code", "rows": 4, "language": "json", "hint": '{"n_estimators": 200}'},
        )
        model_name: str = Field(default="", description="Registry name (empty = the algorithm name)")
        register: bool = Field(default=True, description="Register the fitted model as a new version")

    def _resolve_hyperparams(self) -> dict:
        out: dict[str, Any] = {}
        for key, value in (self.params.hyperparams or {}).items():  # type: ModelTrainNode.ParamsModel
            if key not in _HYPERPARAM_TYPES:
                raise NodeExecutionError(
                    f"Model Train: hyperparameter {key!r} is not supported (allowed: {sorted(_HYPERPARAM_TYPES)})"
                )
            try:
                out[key] = _HYPERPARAM_TYPES[key](value)
            except (TypeError, ValueError):
                raise NodeExecutionError(f"Model Train: hyperparameter {key!r} must be {_HYPERPARAM_TYPES[key].__name__}") from None
        return out

    async def execute(self, context: ExecutionContext) -> NodeResult:
        import pickle

        import sklearn
        from sklearn.metrics import (
            accuracy_score,
            confusion_matrix,
            f1_score,
            mean_absolute_error,
            mean_squared_error,
            precision_score,
            r2_score,
            recall_score,
        )
        from sklearn.model_selection import cross_val_score, train_test_split
        from sklearn.preprocessing import LabelEncoder

        p = self.params  # type: ModelTrainNode.ParamsModel
        if not p.target:
            raise NodeExecutionError("A target column is required")
        if p.model not in CLASSIFIERS and p.model not in REGRESSORS:
            raise NodeExecutionError(f"Unknown model {p.model!r}")
        df = _input_df(context)
        if len(df) < 10:
            raise NodeExecutionError(f"Model training needs at least 10 rows (got {len(df)})")
        if p.target not in df.columns:
            raise NodeExecutionError(f"Target column {p.target!r} not found - available: {[str(c) for c in df.columns]}")

        # ---- task resolution (auto = object/bool target or explicit pick)
        y_raw = df[p.target].dropna()
        target_is_text = bool(len(y_raw)) and (y_raw.dtype == object or str(y_raw.dtype) in ("object", "bool", "boolean", "string"))
        task = p.task if p.task != "auto" else ("classification" if target_is_text else "regression")
        if task == "classification" and p.model in REGRESSORS:
            raise NodeExecutionError(f"Model Train: {p.model} is a regressor but the task is classification")
        if task == "regression" and p.model in CLASSIFIERS:
            raise NodeExecutionError(f"Model Train: {p.model} is a classifier but the task is regression")

        # ---- feature selection (categoricals welcome; high-cardinality text skipped)
        if p.features.strip():
            feats = [f.strip() for f in p.features.split(",") if f.strip()]
            _require_columns(df, feats)
        else:
            feats = []
            for c in df.columns:
                if c == p.target:
                    continue
                col = df[c]
                if str(col.dtype) in ("datetime64[ns]", "datetime64[ns, UTC]") or "datetime" in str(col.dtype):
                    continue  # datetimes are excluded from auto-selection
                if col.dtype == object or str(col.dtype) == "string":
                    uniq = int(col.nunique(dropna=True))
                    if uniq > MAX_CATEGORICAL_CARDINALITY:
                        continue  # likely an id/name column - one-hot would explode
                feats.append(c)
        if not feats:
            raise NodeExecutionError("No usable feature columns available - pass `features` explicitly")

        data = df[feats + [p.target]].dropna(subset=[p.target])
        # keep rows with valid target; feature NaNs are imputed by the pipeline
        if len(data) < 10:
            raise NodeExecutionError(f"Only {len(data)} rows with a valid target - need 10+")

        numeric_cols = [c for c in feats if c in data.select_dtypes(include=["number"]).columns]
        categorical_cols = [c for c in feats if c not in numeric_cols]
        X = data[feats]

        labeler = None
        y = data[p.target]
        if task == "classification":
            if y.dtype == object or str(y.dtype) in ("bool", "boolean", "string"):
                labeler = LabelEncoder()
                y = pd.Series(labeler.fit_transform(y), index=y.index)
            if y.nunique() < 2:
                raise NodeExecutionError("Classification needs at least 2 distinct target classes")

        hyperparams = self._resolve_hyperparams()
        pipeline = _build_pipeline(p.model, hyperparams, numeric_cols, categorical_cols, p.scale)

        # ---- split (stratified when possible) + fit
        stratify_arg = None
        if task == "classification" and y.nunique() > 1:
            counts = y.value_counts()
            if counts.min() >= 2:  # stratify needs >= 2 per class
                stratify_arg = y
            else:
                stratify_arg = None  # tiny class - fall back to plain split
        X_tr, X_te, y_tr, y_te = train_test_split(
            X, y, test_size=p.test_size, random_state=p.random_state, stratify=stratify_arg,
        )
        pipeline.fit(X_tr, y_tr)
        pred = pipeline.predict(X_te)

        def _readable(series: Any) -> list[Any]:
            if labeler is not None:
                return [str(v) for v in labeler.inverse_transform(pd.Series(series).astype(int))]
            return [json.loads(json.dumps(float(v))) for v in series]

        metrics: dict[str, Any] = {}
        if task == "classification":
            metrics = {
                "accuracy": round(float(accuracy_score(y_te, pred)), 4),
                "f1_weighted": round(float(f1_score(y_te, pred, average="weighted", zero_division=0)), 4),
                "precision_weighted": round(float(precision_score(y_te, pred, average="weighted", zero_division=0)), 4),
                "recall_weighted": round(float(recall_score(y_te, pred, average="weighted", zero_division=0)), 4),
            }
            if y.nunique() == 2 and hasattr(pipeline, "predict_proba"):
                try:
                    proba = pipeline.predict_proba(X_te)[:, 1]
                    from sklearn.metrics import roc_auc_score

                    metrics["roc_auc"] = round(float(roc_auc_score(y_te, proba)), 4)
                except Exception:  # noqa: BLE001 - proba/auc are best-effort
                    pass
            labels = sorted(y.unique().tolist())
            cm = confusion_matrix(y_te, pred, labels=labels)
            readable_labels = _readable(labels) if labeler is not None else [str(v) for v in labels]
            metrics["confusion_matrix"] = {"labels": readable_labels, "matrix": cm.astype(int).tolist()}
        else:
            mse = float(mean_squared_error(y_te, pred))
            metrics = {
                "r2": round(float(r2_score(y_te, pred)), 4),
                "mae": round(float(mean_absolute_error(y_te, pred)), 4),
                "mse": round(mse, 4),
                "rmse": round(mse ** 0.5, 4),
            }

        # ---- cross-validation on the FULL data (pipeline included)
        if p.cross_validation >= 2:
            scoring = "f1_weighted" if task == "classification" else "r2"
            try:
                scores = cross_val_score(pipeline, X, y, cv=p.cross_validation, scoring=scoring)
                metrics["cv_mean"] = round(float(scores.mean()), 4)
                metrics["cv_std"] = round(float(scores.std()), 4)
                metrics["cv_folds"] = int(p.cross_validation)
                metrics["cv_scoring"] = scoring
            except Exception as exc:  # noqa: BLE001 - CV is optional, never fatal
                metrics["cv_error"] = str(exc)[:200]

        # ---- feature attribution (best effort, algorithm-dependent)
        try:
            estimator = pipeline.named_steps["model"]
            names_out = list(pipeline.named_steps["prep"].get_feature_names_out())
            pretty = [n.split("__", 1)[-1] for n in names_out]
            if hasattr(estimator, "feature_importances_"):
                metrics["feature_importances"] = {
                    f: round(float(v), 4) for f, v in sorted(zip(pretty, estimator.feature_importances_), key=lambda kv: -kv[1])[:20]
                }
            elif hasattr(estimator, "coef_"):
                import numpy as _np

                coefs = _np.abs(estimator.coef_)
                if coefs.ndim > 1:
                    coefs = coefs.mean(axis=0)
                metrics["coefficients"] = {f: round(float(v), 4) for f, v in sorted(zip(pretty, coefs), key=lambda kv: -kv[1])[:20]}
        except Exception:  # noqa: BLE001 - attribution is best-effort
            pass

        sample = pd.DataFrame({"actual": y_te, "predicted": pred}).head(20)
        predictions = [
            {"actual": a, "predicted": b}
            for a, b in zip(_readable(sample["actual"]), _readable(sample["predicted"]))
        ]

        # ---- persist: the WHOLE pipeline + labeler as one pickle payload
        payload = {
            "pipeline": pipeline,
            "labeler": labeler,
            "task": task,
            "algorithm": p.model,
            "target": p.target,
            "features": feats,
            "numeric_features": numeric_cols,
            "categorical_features": categorical_cols,
            "sklearn_version": sklearn.__version__,
        }
        saved = await _save_artifact_row(
            context,
            kind="model",
            data=pickle.dumps(payload),
            content_type="application/octet-stream",
            meta={"model": p.model, "target": p.target, "features": feats, "metrics": metrics, "node": self.name, "task": task},
            filename="model.pkl",
        )

        # ---- model registry (v46)
        registry_row = None
        if p.register:
            from ...db import AsyncSessionLocal
            from ...services import models as model_svc

            name = (p.model_name or "").strip() or p.model
            input_data = context.current_input
            ds_name = None
            if isinstance(input_data, dict):
                ds_name = input_data.get("dataset")
                if not isinstance(ds_name, str):
                    ds_name = None
            async with AsyncSessionLocal() as session:
                row = await model_svc.register_model(
                    session,
                    name=name,
                    algorithm=p.model,
                    task=task,
                    target=p.target,
                    features=feats,
                    metrics=metrics,
                    artifact_id=saved["id"],
                    owner_id=getattr(context, "owner_id", None),
                    dataset_name=ds_name,
                    row_count=int(len(data)),
                    activate=True,
                )
                await session.commit()
                registry_row = model_svc.model_out(row)

        out: dict[str, Any] = {
            "items": predictions,
            "metrics": metrics,
            "model_id": saved["id"],
            "artifact_url": f"/api/v1/artifacts/{saved['id']}/content",
            "model": p.model,
            "task": task,
            "target": p.target,
            "features": feats,
            "rows_used": int(len(data)),
        }
        if registry_row is not None:
            out["registry"] = {"id": registry_row["id"], "name": registry_row["name"], "version": registry_row["version"], "active": registry_row["active"]}
        return self._single(out)


class ModelPredictNode(BaseNode):
    """v46: batch scoring against the model registry - the missing half of
    model_train. Loads the active (or specified) version's pickled pipeline,
    reproduces training-time preprocessing exactly, and appends a
    ``prediction`` (and optionally a class-probability) column to every item."""

    type = "model_predict"
    name = "Model Predict"
    description = (
        "Scores the input items with a registered model (by id, or by name → its ACTIVE "
        "version): adds a prediction column, and class probabilities when available. "
        "Training-time preprocessing (impute/scale/one-hot) is reproduced exactly."
    )
    category = "ai"
    icon = "target"
    color = "#f472b6"

    class ParamsModel(BaseModel):
        model: str = Field(default="", description="Registry name (uses the ACTIVE version) or a registry row id")
        probability_column: str = Field(
            default="prediction_proba",
            description="Column name for the predicted-class probability (classification only; empty = off)",
        )

    async def execute(self, context: ExecutionContext) -> NodeResult:
        import pickle

        from ...db import AsyncSessionLocal
        from ...models import Artifact
        from ...services import artifacts as art_svc
        from ...services import models as model_svc

        p = self.params  # type: ModelPredictNode.ParamsModel
        if not p.model or not p.model.strip():
            raise NodeExecutionError("A model name or id is required")
        async with AsyncSessionLocal() as session:
            row = await model_svc.resolve_model(session, p.model.strip(), owner_id=getattr(context, "owner_id", None))
            if row is None:
                raise NodeExecutionError(f"Model {p.model!r} not found in the registry (or not owned by you)")
            info = model_svc.model_out(row)
            artifact_row = await session.get(Artifact, row.artifact_id) if row.artifact_id else None
        if artifact_row is None:
            raise NodeExecutionError(f"Model {info['name']} v{info['version']} has no loadable artifact")
        raw = art_svc.read_bytes(artifact_row)
        try:
            payload = pickle.loads(raw)
        except Exception as exc:  # noqa: BLE001
            raise NodeExecutionError(f"Model artifact is corrupted: {exc}") from exc

        # legacy v28 pickles are bare estimators - normalize to the v46 payload
        if not isinstance(payload, dict) or "pipeline" not in payload:
            payload = {
                "pipeline": payload, "labeler": None, "task": info.get("task") or "regression",
                "algorithm": info.get("algorithm") or "unknown", "target": info.get("target") or "",
                "features": info.get("features") or [], "numeric_features": None, "categorical_features": None,
            }

        items = _items(_working_data(context.current_input))
        rows = [r for r in items if isinstance(r, dict)]
        if not rows:
            raise NodeExecutionError("Model Predict needs object items to score")
        import pandas as pd

        df = pd.DataFrame(rows)
        feats = payload.get("features") or []
        missing = [c for c in feats if c not in df.columns]
        if missing:
            raise NodeExecutionError(
                f"Model {info['name']} v{info['version']} needs feature column(s) {missing} - available: {[str(c) for c in df.columns]}"
            )
        X = df[feats] if feats else df

        pipeline = payload["pipeline"]
        labeler = payload.get("labeler")
        try:
            pred = pipeline.predict(X)
            probas = pipeline.predict_proba(X) if p.probability_column and hasattr(pipeline, "predict_proba") else None
        except Exception as exc:  # noqa: BLE001
            raise NodeExecutionError(f"Scoring failed: {type(exc).__name__}: {exc}") from exc

        def _label(value: Any) -> Any:
            if labeler is not None:
                return str(labeler.inverse_transform([int(value)])[0])
            try:
                num = float(value)
                return int(num) if num.is_integer() else round(num, 6)
            except (TypeError, ValueError, AttributeError):
                return value

        labels = [_label(v) for v in pred]
        proba_col: list[Any] = [None] * len(rows)
        if probas is not None:
            best = probas.max(axis=1)
            for i in range(len(labels)):
                proba_col[i] = round(float(best[i]), 4)

        out_items: list[dict] = []
        for i, item in enumerate(rows):
            rec = dict(item)
            rec["prediction"] = labels[i]
            if probas is not None:
                rec[p.probability_column or "prediction_proba"] = proba_col[i]
            out_items.append(rec)

        return self._single({
            "items": out_items,
            "predicted": len(out_items),
            "rows_in": len(rows),
            "model": {
                "id": info["id"], "name": info["name"], "version": info["version"],
                "algorithm": info["algorithm"], "task": info["task"],
            },
        })
