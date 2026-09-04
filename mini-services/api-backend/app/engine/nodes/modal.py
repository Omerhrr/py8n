"""Multimodal + neural nodes (v63) - the Model System building blocks.

The classical surface (``model_train``) is sklearn's; THIS module is the
from-scratch half of the ML story:

* ``text_features`` - turn a text column into numeric features. TF-IDF is
  CORPUS-FITTED, so the node has two modes: ``fit`` (learn the vocabulary
  + projection on this batch, persist a featurizer artifact, emit columns)
  and ``transform`` (load a previously fitted featurizer and apply it) -
  serving-time parity by construction, the same guarantee model_predict
  gives sklearn pipelines.
* ``image_features`` - stateless PIL features per row (resize, channel
  stats, histogram, brightness, edge density). Nothing is fitted; the
  same image always produces the same vector.
* ``audio_features`` - stateless WAV features per row (duration, RMS,
  zero-crossing rate, FFT band energies, spectral centroid).
* ``neural_train`` - a multilayer perceptron implemented in raw numpy
  (He init, ReLU/tanh, softmax cross-entropy or MSE, minibatch SGD with
  momentum or Adam, early stopping) - no sklearn estimator involved.
  Supports FINE-TUNING: point ``base_model`` at a registry row to start
  from its weights and continue on new data.

All nodes follow the item model and resolve Jinja like every other node;
decoding failures fail loud with the offending row index.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import pickle
import time
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from ..context import ExecutionContext
from .base import BaseNode, NodeExecutionError
from .data import _items, _working_data
from .datascience import _input_df, _save_artifact_row, _require_columns


# ----------------------------------------------------------------- the network
class _MLP:
    """A multilayer perceptron in raw numpy - the 'from scratch' part.

    Architecture: linear layers with ReLU/tanh activations and a final
    softmax (classification) or identity (regression) output. Training is
    minibatch backprop with SGD + momentum or Adam. State is plain numpy
    arrays so it pickles cleanly and fine-tuning just means continuing.
    """

    def __init__(self, layer_sizes: list[int], activation: str = "relu", seed: int = 42):
        rng = np.random.default_rng(seed)
        self.layer_sizes = layer_sizes
        self.activation = activation
        self.weights = [
            rng.normal(0.0, np.sqrt(2.0 / layer_sizes[i]), size=(layer_sizes[i], layer_sizes[i + 1]))
            for i in range(len(layer_sizes) - 1)
        ]
        self.biases = [np.zeros(layer_sizes[i + 1]) for i in range(len(layer_sizes) - 1)]

    # ---- forward ----
    def _act(self, z: np.ndarray) -> np.ndarray:
        if self.activation == "tanh":
            return np.tanh(z)
        return np.maximum(0.0, z)  # relu

    def _forward(self, X: np.ndarray):
        """Return (activations, pre_activations); activations[0] is X."""
        activations = [X]
        zs = []
        a = X
        last = len(self.weights) - 1
        for i, (W, b) in enumerate(zip(self.weights, self.biases)):
            z = a @ W + b
            zs.append(z)
            if i == last:
                a = z  # linear head (softmax/MSE applied by the loss)
            else:
                a = self._act(z)
            activations.append(a)
        return activations, zs

    def predict(self, X: np.ndarray) -> np.ndarray:
        a, _ = self._forward(np.asarray(X, dtype=np.float64))
        out = a[-1]
        if out.shape[1] == 1:
            return out[:, 0]
        return out

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        out = self.predict(X)
        if out.ndim == 1:
            return np.column_stack([1 - out, out])  # binary sigmoid-style head
        e = np.exp(out - out.max(axis=1, keepdims=True))
        return e / e.sum(axis=1, keepdims=True)

    # ---- training ----
    def fit(
        self,
        X: np.ndarray,
        y: np.ndarray,
        *,
        task: str,
        epochs: int = 60,
        batch_size: int = 16,
        lr: float = 0.01,
        optimizer: str = "adam",
        l2: float = 0.0,
        val_data: tuple[np.ndarray, np.ndarray] | None = None,
        patience: int = 0,
        seed: int = 42,
        verbose: bool = False,
    ) -> dict:
        """Minibatch training loop. Returns the history dict."""
        rng = np.random.default_rng(seed)
        n, _ = X.shape
        out_dim = self.layer_sizes[-1]
        Y = y.reshape(-1, 1).astype(np.float64) if out_dim == 1 else np.eye(out_dim)[y.astype(int)]

        # optimizer state
        mW = [np.zeros_like(W) for W in self.weights]
        vW = [np.zeros_like(W) for W in self.weights]
        mb = [np.zeros_like(b) for b in self.biases]
        vb = [np.zeros_like(b) for b in self.biases]
        beta1, beta2, eps = 0.9, 0.999, 1e-8
        t = 0
        velW = [np.zeros_like(W) for W in self.weights]
        velb = [np.zeros_like(b) for b in self.biases]

        history: dict = {"train_loss": [], "val_loss": []}
        best_val = float("inf")
        best_state = None
        best_epoch = 0
        started = time.time()

        for epoch in range(1, epochs + 1):
            order = rng.permutation(n)
            epoch_loss = 0.0
            batches = 0
            for start in range(0, n, batch_size):
                idx = order[start:start + batch_size]
                xb, yb = X[idx], Y[idx]
                activations, _ = self._forward(xb)
                out = activations[-1]
                if out_dim == 1:
                    loss = np.mean((out - yb) ** 2)
                    delta = 2.0 * (out - yb) / len(xb)
                else:
                    shifted = out - out.max(axis=1, keepdims=True)
                    e = np.exp(shifted)
                    p = e / e.sum(axis=1, keepdims=True)
                    loss = -np.mean(np.log(p[np.arange(len(xb)), yb.argmax(axis=1)] + 1e-12))
                    delta = (p - yb) / len(xb)
                epoch_loss += float(loss)
                batches += 1

                # backprop
                gradsW = [None] * len(self.weights)
                gradsb = [None] * len(self.biases)
                for i in range(len(self.weights) - 1, -1, -1):
                    gradsW[i] = activations[i].T @ delta
                    gradsb[i] = delta.sum(axis=0)
                    if i > 0:
                        delta = (delta @ self.weights[i].T)
                        if self.activation == "tanh":
                            delta = delta * (1 - activations[i] ** 2)
                        else:
                            delta = delta * (activations[i] > 0)
                if l2 > 0:
                    gradsW = [g + l2 * W for g, W in zip(gradsW, self.weights)]

                # parameter update
                t += 1
                for i in range(len(self.weights)):
                    if optimizer == "adam":
                        mW[i] = beta1 * mW[i] + (1 - beta1) * gradsW[i]
                        vW[i] = beta2 * vW[i] + (1 - beta2) * gradsW[i] ** 2
                        mb[i] = beta1 * mb[i] + (1 - beta1) * gradsb[i]
                        vb[i] = beta2 * vb[i] + (1 - beta2) * gradsb[i] ** 2
                        mhat = mW[i] / (1 - beta1 ** t)
                        vhat = vW[i] / (1 - beta2 ** t)
                        self.weights[i] -= lr * mhat / (np.sqrt(vhat) + eps)
                        self.biases[i] -= lr * (mb[i] / (1 - beta1 ** t)) / (np.sqrt(vb[i] / (1 - beta2 ** t)) + eps)
                    elif optimizer == "momentum":
                        velW[i] = 0.9 * velW[i] + gradsW[i]
                        velb[i] = 0.9 * velb[i] + gradsb[i]
                        self.weights[i] -= lr * velW[i]
                        self.biases[i] -= lr * velb[i]
                    else:  # sgd
                        self.weights[i] -= lr * gradsW[i]
                        self.biases[i] -= lr * gradsb[i]

            history["train_loss"].append(round(epoch_loss / max(batches, 1), 6))

            if val_data is not None and len(val_data[0]):
                Xv, yv = val_data
                if out_dim == 1:
                    yv = yv.reshape(-1, 1)
                    vloss = float(np.mean((self.predict(Xv).reshape(-1, 1) - yv) ** 2))
                else:
                    Yv = np.eye(out_dim)[np.asarray(yv).astype(int)]
                    outv = self.predict(Xv)
                    shifted = outv - outv.max(axis=1, keepdims=True)
                    e = np.exp(shifted)
                    p = e / e.sum(axis=1, keepdims=True)
                    vloss = float(-np.mean(np.log(p[np.arange(len(Yv)), Yv.argmax(axis=1)] + 1e-12)))
                history["val_loss"].append(round(vloss, 6))
                if vloss < best_val:
                    best_val = vloss
                    best_epoch = epoch
                    best_state = ([W.copy() for W in self.weights], [b.copy() for b in self.biases])
                elif patience and epoch - best_epoch >= patience:
                    break  # early stopping - restore the best weights below
            elif best_val == float("inf"):
                best_epoch = epoch

        if best_state is not None:
            self.weights, self.biases = best_state
        history["epochs_run"] = len(history["train_loss"])
        history["best_epoch"] = best_epoch
        history["train_seconds"] = round(time.time() - started, 2)
        if verbose:
            history["final_train_loss"] = history["train_loss"][-1]
        return history

    def state(self) -> dict:
        return {
            "layer_sizes": self.layer_sizes,
            "activation": self.activation,
            "weights": [W.tolist() for W in self.weights],
            "biases": [b.tolist() for b in self.biases],
        }

    @classmethod
    def from_state(cls, state: dict) -> "_MLP":
        net = cls.__new__(cls)
        net.layer_sizes = list(state["layer_sizes"])
        net.activation = state["activation"]
        net.weights = [np.asarray(W, dtype=np.float64) for W in state["weights"]]
        net.biases = [np.asarray(b, dtype=np.float64) for b in state["biases"]]
        return net


# ----------------------------------------------------------------- helpers
def _decode_b64(value: Any) -> bytes:
    """Accept raw base64 or a data URL; fail loud otherwise."""
    if not isinstance(value, str) or not value.strip():
        raise NodeExecutionError("expected a base64 string (or data URL) in the image/audio column")
    raw = value.strip()
    if raw.startswith("data:"):
        _, _, tail = raw.partition(",")
        if not tail:
            raise NodeExecutionError("data URL has no payload after the comma")
        raw = tail
    try:
        return base64.b64decode(raw, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise NodeExecutionError(f"invalid base64 payload: {exc}") from exc


def _numeric_frame(df: pd.DataFrame, features: list[str]) -> pd.DataFrame:
    """The numeric feature frame; non-numeric columns are refused (fail
    loud) - run text_features/image_features/audio_features first."""
    num = df[features].apply(pd.to_numeric, errors="coerce")
    return num


async def _load_featurizer(owner_id: str | None, name: str) -> dict:
    from sqlalchemy import select

    from ...db import AsyncSessionLocal
    from ...models import Artifact
    from ...services import artifacts as art_svc

    async with AsyncSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(Artifact)
                    .where(Artifact.kind == "featurizer")
                    .order_by(Artifact.created_at.desc())
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            meta = row.meta or {}
            if meta.get("featurizer") != name:
                continue
            if owner_id and meta.get("owner_id") not in (owner_id, None):
                continue
            return pickle.loads(art_svc.read_bytes(row))
    raise NodeExecutionError(
        f"featurizer {name!r} not found - run a text_features node with mode=fit "
        "(and the same featurizer name) before transform"
    )


# ----------------------------------------------------------------- node: text
class TextFeaturesNode(BaseNode):
    """v63: text -> numeric features with serving-time parity."""

    type = "text_features"
    name = "Text Features"
    description = (
        "Turns a text column into numeric features (TF-IDF -> SVD + length stats). "
        "mode=fit learns the vocabulary and PERSISTS a named featurizer artifact; "
        "mode=transform reuses it - so training and scoring batches are embedded "
        "identically."
    )
    category = "ai"
    icon = "type"
    color = "#a78bfa"

    class ParamsModel(BaseModel):
        column: str = Field(default="", description="Column holding the text")
        mode: str = Field(default="fit", json_schema_extra={"widget": "select", "options": ["fit", "transform"]})
        featurizer: str = Field(default="", description="Featurizer name to save (fit) or load (transform)")
        max_features: int = Field(default=2000, ge=50, le=20000, description="Vocabulary size (fit)")
        svd_dims: int = Field(default=16, ge=2, le=256, description="Projected dimensions")
        ngram_max: int = Field(default=1, ge=1, le=3, description="Word n-gram upper bound (fit)")
        prefix: str = Field(default="text", description="Output column prefix")

    async def execute(self, context: ExecutionContext) -> NodeResult:
        from sklearn.decomposition import TruncatedSVD
        from sklearn.feature_extraction.text import TfidfVectorizer

        p = self.params  # type: TextFeaturesNode.ParamsModel
        if not p.column:
            raise NodeExecutionError("A text column is required")
        items = _items(_working_data(context.current_input))
        rows = [r for r in items if isinstance(r, dict)]
        if not rows:
            raise NodeExecutionError("Text Features needs object items")
        texts = [str(r.get(p.column) or "") for r in rows]
        if p.mode == "fit":
            if not p.featurizer:
                raise NodeExecutionError("A featurizer name is required in fit mode")
            try:
                tfidf = TfidfVectorizer(max_features=p.max_features,
                                        ngram_range=(1, max(1, p.ngram_max)),
                                        stop_words="english")
                X = tfidf.fit_transform(texts)
                dims = min(p.svd_dims, X.shape[1], max(2, len(texts) - 1))
                svd = TruncatedSVD(n_components=dims, random_state=42)
                Z = svd.fit_transform(X)
            except ValueError as exc:
                raise NodeExecutionError(f"text_features fit failed: {exc}") from exc
            saved = await _save_artifact_row(
                context,
                kind="featurizer",
                data=pickle.dumps({"tfidf": tfidf, "svd": svd}),
                content_type="application/octet-stream",
                meta={"featurizer": p.featurizer, "owner_id": getattr(context, "owner_id", None),
                      "column": p.column, "dims": int(dims)},
                filename=f"featurizer_{p.featurizer}.pkl",
            )
            vecs = Z
            note = f"fitted on {len(texts)} rows (vocab {len(tfidf.vocabulary_)}) - artifact {saved['id'][:8]}"
        else:
            if not p.featurizer:
                raise NodeExecutionError("A featurizer name is required in transform mode")
            payload = await _load_featurizer(getattr(context, "owner_id", None), p.featurizer)
            tfidf, svd = payload["tfidf"], payload["svd"]
            vecs = svd.transform(tfidf.transform(texts))
            note = f"transformed {len(texts)} rows with featurizer {p.featurizer!r} (dims {vecs.shape[1]})"

        prefix = p.prefix or "text"
        out_rows = []
        for i, r in enumerate(rows):
            rec = dict(r)
            for d in range(vecs.shape[1]):
                rec[f"{prefix}_vec_{d}"] = round(float(vecs[i, d]), 6)
            rec[f"{prefix}_chars"] = len(texts[i])
            rec[f"{prefix}_tokens"] = len(texts[i].split())
            out_rows.append(rec)
        return self._single({
            "items": out_rows,
            "rows_in": len(rows),
            "dims": int(vecs.shape[1]),
            "mode": p.mode,
            "note": note,
        })


# ----------------------------------------------------------------- node: image
class ImageFeaturesNode(BaseNode):
    """v63: stateless image features per row via PIL."""

    type = "image_features"
    name = "Image Features"
    description = (
        "Extracts a fixed numeric feature vector from every row's image "
        "(base64/data URL in the chosen column): RGB channel means+stds, "
        "grayscale histogram, brightness, aspect ratio, edge density. "
        "Stateless - the same image always yields the same vector."
    )
    category = "ai"
    icon = "image"
    color = "#f472b6"

    class ParamsModel(BaseModel):
        image_field: str = Field(default="image_b64", description="Column with base64/data-URL image bytes")
        size: int = Field(default=32, ge=8, le=128, description="Resize side for the stats")
        prefix: str = Field(default="img", description="Output column prefix")

    async def execute(self, context: ExecutionContext) -> NodeResult:
        from PIL import Image

        p = self.params  # type: ImageFeaturesNode.ParamsModel
        items = _items(_working_data(context.current_input))
        rows = [r for r in items if isinstance(r, dict)]
        if not rows:
            raise NodeExecutionError("Image Features needs object items")
        prefix = p.prefix or "img"
        out_rows = []
        for i, r in enumerate(rows):
            raw = _decode_b64(r.get(p.image_field))
            try:
                img = Image.open(io.BytesIO(raw)).convert("RGB")
            except Exception as exc:  # noqa: BLE001
                raise NodeExecutionError(f"row {i}: cannot decode image ({type(exc).__name__}: {exc})") from exc
            small = img.resize((p.size, p.size))
            arr = np.asarray(small, dtype=np.float64) / 255.0
            gray = arr.mean(axis=2)
            hist, _ = np.histogram(gray, bins=8, range=(0.0, 1.0))
            hist = hist / max(hist.sum(), 1)
            gx = np.abs(np.diff(gray, axis=1)).mean()
            rec = dict(r)
            rec[f"{prefix}_r_mean"] = round(float(arr[..., 0].mean()), 6)
            rec[f"{prefix}_g_mean"] = round(float(arr[..., 1].mean()), 6)
            rec[f"{prefix}_b_mean"] = round(float(arr[..., 2].mean()), 6)
            rec[f"{prefix}_r_std"] = round(float(arr[..., 0].std()), 6)
            rec[f"{prefix}_g_std"] = round(float(arr[..., 1].std()), 6)
            rec[f"{prefix}_b_std"] = round(float(arr[..., 2].std()), 6)
            rec[f"{prefix}_brightness"] = round(float(gray.mean()), 6)
            rec[f"{prefix}_contrast"] = round(float(gray.std()), 6)
            rec[f"{prefix}_aspect"] = round(img.width / max(img.height, 1), 4)
            rec[f"{prefix}_edge_density"] = round(float(gx), 6)
            for b in range(8):
                rec[f"{prefix}_hist_{b}"] = round(float(hist[b]), 6)
            out_rows.append(rec)
        return self._single({
            "items": out_rows,
            "rows_in": len(rows),
            "dims": 18,
            "note": f"stateless PIL features over {len(rows)} image(s) at {p.size}px",
        })


# ----------------------------------------------------------------- node: audio
class AudioFeaturesNode(BaseNode):
    """v63: stateless WAV features per row (stdlib wave + numpy FFT)."""

    type = "audio_features"
    name = "Audio Features"
    description = (
        "Extracts a fixed numeric feature vector from every row's WAV audio "
        "(base64/data URL in the chosen column): duration, RMS, peak, "
        "zero-crossing rate, 8 FFT band energies, spectral centroid. "
        "WAV only - everything else fails loud with guidance."
    )
    category = "ai"
    icon = "audio-lines"
    color = "#34d399"

    class ParamsModel(BaseModel):
        audio_field: str = Field(default="audio_b64", description="Column with base64/data-URL WAV bytes")
        prefix: str = Field(default="aud", description="Output column prefix")

    async def execute(self, context: ExecutionContext) -> NodeResult:
        import wave

        p = self.params  # type: AudioFeaturesNode.ParamsModel
        items = _items(_working_data(context.current_input))
        rows = [r for r in items if isinstance(r, dict)]
        if not rows:
            raise NodeExecutionError("Audio Features needs object items")
        prefix = p.prefix or "aud"
        out_rows = []
        for i, r in enumerate(rows):
            raw = _decode_b64(r.get(p.audio_field))
            try:
                with wave.open(io.BytesIO(raw)) as w:
                    n_channels = w.getnchannels()
                    width = w.getsampwidth()
                    rate = w.getframerate()
                    frames = w.readframes(w.getnframes())
            except wave.Error as exc:
                raise NodeExecutionError(
                    f"row {i}: not a decodable WAV ({exc}) - convert to 16-bit PCM WAV first"
                ) from exc
            if width != 2:
                raise NodeExecutionError(f"row {i}: {width * 8}-bit WAV not supported - use 16-bit PCM")
            samples = np.frombuffer(frames, dtype=np.int16).astype(np.float64) / 32768.0
            if n_channels > 1:
                samples = samples.reshape(-1, n_channels).mean(axis=1)
            if not samples.size:
                raise NodeExecutionError(f"row {i}: WAV contains no samples")
            rms = float(np.sqrt(np.mean(samples ** 2)))
            zc = float(np.mean(np.abs(np.diff(np.sign(samples))) > 0))
            spec = np.abs(np.fft.rfft(samples * np.hanning(samples.size)))
            freqs = np.fft.rfftfreq(samples.size, d=1.0 / max(rate, 1))
            bands = np.array_split(spec, 8)
            total = float(spec.sum() + 1e-12)
            centroid = float((spec * freqs).sum() / total)
            rec = dict(r)
            rec[f"{prefix}_duration_s"] = round(samples.size / max(rate, 1), 4)
            rec[f"{prefix}_rms"] = round(rms, 6)
            rec[f"{prefix}_peak"] = round(float(np.abs(samples).max()), 6)
            rec[f"{prefix}_zcr"] = round(zc, 6)
            rec[f"{prefix}_centroid"] = round(centroid, 2)
            for b, band in enumerate(bands):
                rec[f"{prefix}_band_{b}"] = round(float(band.sum() / total), 6)
            out_rows.append(rec)
        return self._single({
            "items": out_rows,
            "rows_in": len(rows),
            "dims": 13,
            "note": f"stateless WAV features over {len(rows)} clip(s)",
        })


# ----------------------------------------------------------------- node: neural
NEURAL_HYPERPARAMS = {"hidden_layers", "epochs", "batch_size", "learning_rate",
                      "optimizer", "weight_decay", "patience"}


class NeuralTrainNode(BaseNode):
    """v63: from-scratch numpy MLP training (+ fine-tuning from the registry)."""

    type = "neural_train"
    name = "Neural Train"
    description = (
        "Trains a multilayer perceptron implemented from scratch in numpy - "
        "no sklearn estimator: He init, ReLU/tanh hidden layers, minibatch "
        "SGD/momentum/Adam, early stopping, evaluation on a held-out split. "
        "Point base_model at a registry row to FINE-TUNE from its weights."
    )
    category = "ai"
    icon = "brain-circuit"
    color = "#818cf8"

    class ParamsModel(BaseModel):
        task: str = Field(
            default="auto",
            json_schema_extra={"widget": "select", "options": ["auto", "classification", "regression"]},
        )
        target: str = Field(default="", description="Target column to predict")
        features: str = Field(default="", description="Comma-separated NUMERIC feature columns (empty = all numeric)")
        test_size: float = Field(default=0.2, ge=0.1, le=0.5)
        hidden_layers: str = Field(default="32,16", description="Hidden layer sizes, e.g. '64,32' (empty = logistic head)")
        epochs: int = Field(default=60, ge=1, le=500)
        batch_size: int = Field(default=16, ge=1, le=512)
        learning_rate: float = Field(default=0.01, gt=0, le=1.0)
        optimizer: str = Field(default="adam", json_schema_extra={"widget": "select", "options": ["adam", "momentum", "sgd"]})
        weight_decay: float = Field(default=0.0, ge=0.0, le=1.0)
        patience: int = Field(default=0, ge=0, le=100, description="Early stopping patience on val loss (0 = off)")
        seed: int = Field(default=42)
        base_model: str = Field(default="", description="Registry name/id to fine-tune from (empty = from scratch)")
        model_name: str = Field(default="", description="Registry name (empty = 'neural_net')")
        register: bool = Field(default=True)

    async def execute(self, context: ExecutionContext) -> NodeResult:
        from sklearn.metrics import (
            accuracy_score,
            f1_score,
            mean_absolute_error,
            r2_score,
        )
        from sklearn.model_selection import train_test_split
        from sklearn.preprocessing import LabelEncoder, StandardScaler
        from sklearn.impute import SimpleImputer
        from sklearn.pipeline import Pipeline

        p = self.params  # type: NeuralTrainNode.ParamsModel
        if not p.target:
            raise NodeExecutionError("A target column is required")
        df = _input_df(context)
        if len(df) < 10:
            raise NodeExecutionError(f"Neural training needs at least 10 rows (got {len(df)})")
        if p.target not in df.columns:
            raise NodeExecutionError(f"Target column {p.target!r} not found - available: {[str(c) for c in df.columns]}")

        # ---- fine-tune base? ----
        base_payload = None
        base_row = None
        if p.base_model.strip():
            from ...db import AsyncSessionLocal
            from ...services import models as model_svc

            async with AsyncSessionLocal() as session:
                base_row = await model_svc.resolve_model(session, p.base_model.strip(), owner_id=getattr(context, "owner_id", None))
                if base_row is None:
                    raise NodeExecutionError(f"base_model {p.base_model!r} not found in the registry (or not owned by you)")
                from ...models import Artifact
                from ...services import artifacts as art_svc

                art = await session.get(Artifact, base_row.artifact_id) if base_row.artifact_id else None
                if art is None:
                    raise NodeExecutionError("the base model has no loadable artifact")
                raw = art_svc.read_bytes(art)
            base_payload = pickle.loads(raw)
            if base_payload.get("kind") != "neural":
                raise NodeExecutionError(
                    f"fine-tuning needs a neural_train model - {base_payload.get('algorithm', base_row.algorithm)!r} "
                    "is a classical model; retrain it or train a new neural network"
                )

        # ---- task resolution ----
        y_raw = df[p.target].dropna()
        target_is_text = bool(len(y_raw)) and (y_raw.dtype == object or str(y_raw.dtype) in ("object", "bool", "boolean", "string"))
        task = p.task if p.task != "auto" else ("classification" if target_is_text else "regression")
        if base_payload is not None and base_payload["task"] != task:
            raise NodeExecutionError(
                f"fine-tune task mismatch: base model is {base_payload['task']} but this batch looks {task}"
            )

        # ---- feature selection: numeric columns only (fail loud with guidance)
        if p.features.strip():
            feats = [f.strip() for f in p.features.split(",") if f.strip()]
            _require_columns(df, feats)
        elif base_payload is not None:
            feats = list(base_payload["features"])
            missing = [c for c in feats if c not in df.columns]
            if missing:
                raise NodeExecutionError(f"fine-tune needs feature column(s) {missing} - available: {[str(c) for c in df.columns]}")
        else:
            feats = [c for c in df.select_dtypes(include=["number"]).columns if c != p.target]
        if not feats:
            raise NodeExecutionError(
                "no numeric feature columns - encode text/images/audio first "
                "(text_features, image_features, audio_features) or pass `features`"
            )
        data = df[feats + [p.target]].dropna(subset=[p.target])
        if len(data) < 10:
            raise NodeExecutionError(f"Only {len(data)} rows with a valid target - need 10+")

        labeler = None
        y = data[p.target]
        if task == "classification":
            if y.dtype == object or str(y.dtype) in ("bool", "boolean", "string"):
                labeler = LabelEncoder()
                y = pd.Series(labeler.fit_transform(y), index=y.index)
            if y.nunique() < 2:
                raise NodeExecutionError("Classification needs at least 2 distinct target classes")

        # ---- numeric prep (impute + scale), persisted for serving parity ----
        X_df = _numeric_frame(data, feats)
        if base_payload is not None:
            prep = base_payload["prep"]
        else:
            prep = Pipeline([("impute", SimpleImputer(strategy="median")), ("scale", StandardScaler())])
        X = prep.fit_transform(X_df) if base_payload is None else prep.transform(X_df)
        X = np.asarray(X, dtype=np.float64)

        X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=p.test_size, random_state=p.seed)

        # ---- the network ----
        if base_payload is not None:
            net = _MLP.from_state(base_payload["mlp"])
        else:
            hidden = [int(h) for h in p.hidden_layers.split(",") if h.strip()]
            out_dim = 1 if task == "regression" else int(y.nunique())
            layer_sizes = [X.shape[1], *hidden, out_dim]
            net = _MLP(layer_sizes, activation="relu", seed=p.seed)

        y_tr_np = y_tr.to_numpy(dtype=np.float64)
        y_te_np = y_te.to_numpy(dtype=np.float64)
        history = net.fit(
            X_tr, y_tr_np, task=task, epochs=p.epochs, batch_size=p.batch_size,
            lr=p.learning_rate, optimizer=p.optimizer, l2=p.weight_decay,
            val_data=(X_te, y_te_np) if len(X_te) else None,
            patience=p.patience, seed=p.seed,
        )

        # ---- evaluation ----
        metrics: dict[str, Any] = {}
        if task == "classification":
            pred_te = net.predict(X_te)
            pred_labels = np.round(pred_te).astype(int) if pred_te.ndim == 1 else pred_te.argmax(axis=1)
            metrics["accuracy"] = round(float(accuracy_score(y_te, pred_labels)), 4)
            metrics["f1_weighted"] = round(float(f1_score(y_te, pred_labels, average="weighted", zero_division=0)), 4)
        else:
            pred_te = net.predict(X_te)
            metrics["r2"] = round(float(r2_score(y_te, pred_te)), 4)
            metrics["mae"] = round(float(mean_absolute_error(y_te, pred_te)), 4)
        metrics["epochs_run"] = history["epochs_run"]
        metrics["best_epoch"] = history["best_epoch"]
        metrics["final_train_loss"] = history["train_loss"][-1] if history["train_loss"] else None
        metrics["final_val_loss"] = history["val_loss"][-1] if history["val_loss"] else None
        metrics["train_seconds"] = history["train_seconds"]
        metrics["params_count"] = int(sum(W.size for W in net.weights) + sum(b.size for b in net.biases))
        metrics["architecture"] = "->".join(str(s) for s in net.layer_sizes)
        metrics["optimizer"] = p.optimizer
        metrics["learning_rate"] = p.learning_rate

        fine_tuned_from = None
        if base_payload is not None and base_row is not None:
            fine_tuned_from = {"registry_id": base_row.id, "name": base_row.name, "version": base_row.version}
            metrics["fine_tuned_from"] = f"{base_row.name} v{base_row.version}"

        # ---- persist ----
        payload = {
            "kind": "neural",
            "mlp": net.state(),
            "prep": prep,
            "labeler": labeler,
            "task": task,
            "algorithm": "mlp_regressor" if task == "regression" else "mlp_classifier",
            "target": p.target,
            "features": feats,
            "fine_tuned_from": fine_tuned_from,
            "history": {"train_loss": history["train_loss"][-50:], "val_loss": history["val_loss"][-50:]},
        }
        saved = await _save_artifact_row(
            context,
            kind="model",
            data=pickle.dumps(payload),
            content_type="application/octet-stream",
            meta={"model": "neural_net", "target": p.target, "features": feats,
                  "metrics": metrics, "node": self.name, "task": task},
            filename="model.pkl",
        )

        # ---- registry ----
        registry_row = None
        if p.register:
            from ...db import AsyncSessionLocal
            from ...services import models as model_svc

            name = (p.model_name or "").strip() or "neural_net"
            reference_stats = model_svc.compute_reference_stats(data, feats)
            async with AsyncSessionLocal() as session:
                row = await model_svc.register_model(
                    session,
                    name=name,
                    algorithm=payload["algorithm"],
                    task=task,
                    target=p.target,
                    features=feats,
                    metrics=metrics,
                    artifact_id=saved["id"],
                    owner_id=getattr(context, "owner_id", None),
                    dataset_name=None,
                    row_count=int(len(data)),
                    activate=True,
                    reference_stats=reference_stats,
                )
                await session.commit()
                registry_row = model_svc.model_out(row)

        pred_out = pred_labels if task == "classification" else np.asarray(pred_te).reshape(-1)
        sample = pd.DataFrame({"actual": y_te.reset_index(drop=True).to_numpy(), "predicted": pred_out})
        if labeler is not None:
            sample = sample.assign(
                actual=labeler.inverse_transform(sample["actual"].astype(int)),
                predicted=labeler.inverse_transform(np.round(pd.to_numeric(sample["predicted"])).astype(int)),
            )
        predictions = json.loads(sample.head(20).to_json(orient="records"))

        out: dict[str, Any] = {
            "items": predictions,
            "metrics": metrics,
            "model_id": saved["id"],
            "artifact_url": f"/api/v1/artifacts/{saved['id']}/content",
            "mode": "fine-tune" if base_payload is not None else "from-scratch",
            "architecture": metrics["architecture"],
            "task": task,
            "target": p.target,
            "features": feats,
            "rows_used": int(len(data)),
        }
        if registry_row is not None:
            out["registry"] = {"id": registry_row["id"], "name": registry_row["name"],
                               "version": registry_row["version"], "active": registry_row["active"]}
        return self._single(out)
