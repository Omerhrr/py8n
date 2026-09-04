"""Language-model nodes (v64) - text continued-pretraining, from scratch.

The v63 surface trains MLPs over NUMERIC features (neural_train) and
classical estimators over tables (model_train); THIS module trains a
LANGUAGE MODEL over RAW TEXT:

* ``lm_train`` - a causal transformer implemented from scratch in raw
  numpy (token + learned position embeddings, multi-head causal
  self-attention, ReLU feedforward blocks, pre-layer-norm) trained on
  next-token prediction over a text corpus. Point ``base_model`` at a
  registry row to CONTINUE PRETRAINING that model on a new corpus
  (domain adaptation): the weights AND the fitted tokenizer carry over,
  and the lineage is recorded as ``continued_pretrained_from``.
* ``lm_generate`` - loads a registered language model and samples text
  autoregressively (temperature, top-k) - the deployment half of the
  language-model story, the mirror of model_predict for the
  sklearn/neural surface.

Both follow the item model, resolve Jinja like every other node, and
fail loud with guidance when handed the wrong kind of model.
"""

from __future__ import annotations

import pickle
import re
import time
from collections import Counter
from typing import Any

import numpy as np
from pydantic import BaseModel, Field

from ..context import ExecutionContext
from .base import BaseNode, NodeExecutionError
from .data import _items, _working_data
from .datascience import _save_artifact_row

_UNK, _BOS = "<unk>", "<bos>"
_TOKEN_RE = re.compile(r"[a-z0-9']+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


# ----------------------------------------------------------------- the net
class _TinyLM:
    """A causal transformer in raw numpy - the continued-pretraining engine.

    Architecture: learned token + position embeddings, N blocks of
    (pre-LN multi-head causal self-attention -> residual) + (pre-LN
    ReLU feedforward -> residual), a final layer norm and an untied
    output head. Training is next-token cross-entropy with minibatch
    Adam; the backward pass is hand-written (attention softmax
    Jacobian, layer-norm backward, embedding scatter). State is plain
    numpy arrays so it pickles cleanly and continued pretraining means
    loading the state and calling ``fit`` again on new text.
    """

    def __init__(self, vocab_size: int, d_model: int = 32, n_heads: int = 2,
                 n_ctx: int = 16, n_blocks: int = 1, seed: int = 42):
        if d_model % n_heads != 0:
            raise ValueError("d_model must be divisible by n_heads")
        rng = np.random.default_rng(seed)
        self.vocab_size = int(vocab_size)
        self.d_model = int(d_model)
        self.n_heads = int(n_heads)
        self.n_ctx = int(n_ctx)
        self.n_blocks = int(n_blocks)

        def _w(*shape: int) -> np.ndarray:
            return rng.normal(0.0, 0.02, size=shape)

        self.tok_emb = _w(vocab_size, d_model)
        self.pos_emb = _w(n_ctx, d_model)
        self.blocks: list[dict] = []
        for _ in range(n_blocks):
            self.blocks.append({
                "ln1_g": np.ones(d_model), "ln1_b": np.zeros(d_model),
                "Wq": _w(d_model, d_model), "Wk": _w(d_model, d_model),
                "Wv": _w(d_model, d_model), "Wo": _w(d_model, d_model),
                "ln2_g": np.ones(d_model), "ln2_b": np.zeros(d_model),
                "W1": _w(d_model, 4 * d_model), "b1": np.zeros(4 * d_model),
                "W2": _w(4 * d_model, d_model), "b2": np.zeros(d_model),
            })
        self.lnf_g = np.ones(d_model)
        self.lnf_b = np.zeros(d_model)
        self.Wh = _w(d_model, vocab_size)
        self.bh = np.zeros(vocab_size)

    # ---- bookkeeping ----
    def _params_tree(self) -> dict:
        return {"tok_emb": self.tok_emb, "pos_emb": self.pos_emb,
                "blocks": self.blocks, "lnf_g": self.lnf_g, "lnf_b": self.lnf_b,
                "Wh": self.Wh, "bh": self.bh}

    def params_count(self) -> int:
        total = 0
        for arr in (self.tok_emb, self.pos_emb, self.lnf_g, self.lnf_b, self.Wh, self.bh):
            total += int(arr.size)
        for blk in self.blocks:
            for v in blk.values():
                total += int(v.size)
        return total

    def architecture(self) -> str:
        return (f"lm d{self.d_model} h{self.n_heads} b{self.n_blocks} "
                f"ctx{self.n_ctx} V{self.vocab_size}")

    def state(self) -> dict:
        return {
            "config": {"vocab_size": self.vocab_size, "d_model": self.d_model,
                       "n_heads": self.n_heads, "n_ctx": self.n_ctx,
                       "n_blocks": self.n_blocks},
            "tok_emb": self.tok_emb.tolist(), "pos_emb": self.pos_emb.tolist(),
            "blocks": [{k: v.tolist() for k, v in blk.items()} for blk in self.blocks],
            "lnf_g": self.lnf_g.tolist(), "lnf_b": self.lnf_b.tolist(),
            "Wh": self.Wh.tolist(), "bh": self.bh.tolist(),
        }

    @classmethod
    def from_state(cls, st: dict) -> "_TinyLM":
        cfg = st["config"]
        net = cls.__new__(cls)
        net.vocab_size = int(cfg["vocab_size"])
        net.d_model = int(cfg["d_model"])
        net.n_heads = int(cfg["n_heads"])
        net.n_ctx = int(cfg["n_ctx"])
        net.n_blocks = int(cfg["n_blocks"])

        def arr(x: list) -> np.ndarray:
            return np.asarray(x, dtype=np.float64)

        net.tok_emb = arr(st["tok_emb"])
        net.pos_emb = arr(st["pos_emb"])
        net.blocks = [{k: arr(v) for k, v in blk.items()} for blk in st["blocks"]]
        net.lnf_g = arr(st["lnf_g"])
        net.lnf_b = arr(st["lnf_b"])
        net.Wh = arr(st["Wh"])
        net.bh = arr(st["bh"])
        return net

    # ---- forward ----
    @staticmethod
    def _ln(x: np.ndarray, g: np.ndarray, b: np.ndarray, eps: float = 1e-5):
        mu = x.mean(axis=-1, keepdims=True)
        sigma = np.sqrt(x.var(axis=-1, keepdims=True) + eps)
        x_hat = (x - mu) / sigma
        return g * x_hat + b, x_hat, 1.0 / sigma

    def _forward(self, tok: np.ndarray, cache: dict | None = None) -> np.ndarray:
        B, T = tok.shape
        Dh = self.d_model // self.n_heads
        E = self.tok_emb[tok] + self.pos_emb[:T][None, :, :]
        if cache is not None:
            cache["tok"] = tok
        for blk in self.blocks:
            H, x_hat1, inv1 = self._ln(E, blk["ln1_g"], blk["ln1_b"])
            Q = (H @ blk["Wq"]).reshape(B, T, self.n_heads, Dh).transpose(0, 2, 1, 3)
            K = (H @ blk["Wk"]).reshape(B, T, self.n_heads, Dh).transpose(0, 2, 1, 3)
            V = (H @ blk["Wv"]).reshape(B, T, self.n_heads, Dh).transpose(0, 2, 1, 3)
            scores = Q @ K.transpose(0, 1, 3, 2) / np.sqrt(Dh)
            mask = np.triu(np.ones((T, T), dtype=bool), k=1)
            scores = np.where(mask[None, None], -1e9, scores)
            scores -= scores.max(axis=-1, keepdims=True)
            e = np.exp(scores)
            A = e / e.sum(axis=-1, keepdims=True)
            ctx = A @ V
            ctx2d = ctx.transpose(0, 2, 1, 3).reshape(B, T, self.d_model)
            attn_out = ctx2d @ blk["Wo"]
            E2 = E + attn_out
            H2, x_hat2, inv2 = self._ln(E2, blk["ln2_g"], blk["ln2_b"])
            Fpre = H2 @ blk["W1"] + blk["b1"]
            Fmask = Fpre > 0
            F = np.where(Fmask, Fpre, 0.0)
            E = E2 + F @ blk["W2"] + blk["b2"]
            if cache is not None:
                cache.setdefault("blocks", []).append({
                    "x_hat1": x_hat1, "inv1": inv1, "H": H, "A": A,
                    "Q": Q, "K": K, "V": V, "ctx2d": ctx2d, "x_hat2": x_hat2, "inv2": inv2,
                    "H2": H2, "Fmask": Fmask, "F": F,
                })
        Ef, x_hatf, invf = self._ln(E, self.lnf_g, self.lnf_b)
        logits = Ef @ self.Wh + self.bh
        if cache is not None:
            cache["x_hatf"] = x_hatf
            cache["invf"] = invf
            cache["Ef"] = Ef
            cache["logits"] = logits
        return logits

    # ---- backward ----
    @staticmethod
    def _ln_backward(dy: np.ndarray, x_hat: np.ndarray, g_w: np.ndarray,
                     inv: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        dxhat = dy * g_w
        axes = tuple(range(dy.ndim - 1))
        dg = (dy * x_hat).sum(axis=axes)
        db = dy.sum(axis=axes)
        dx = inv * (dxhat - dxhat.mean(-1, keepdims=True)
                    - x_hat * (dxhat * x_hat).mean(-1, keepdims=True))
        return dx, dg, db

    def _backward(self, y: np.ndarray, cache: dict) -> tuple[float, dict]:
        tok = cache["tok"]
        B, T = tok.shape
        logits = cache["logits"]
        shifted = logits - logits.max(axis=-1, keepdims=True)
        p = np.exp(shifted)
        p /= p.sum(axis=-1, keepdims=True)
        rows = np.arange(B)[:, None]
        cols = np.arange(T)[None, :]
        loss = float(-np.mean(np.log(p[rows, cols, y] + 1e-12)))
        dlogits = p.copy()
        dlogits[rows, cols, y] -= 1.0
        dlogits /= (B * T)

        def _zero_like(v: np.ndarray) -> np.ndarray:
            return np.zeros_like(v)

        g: dict = {"tok_emb": _zero_like(self.tok_emb), "pos_emb": _zero_like(self.pos_emb),
                   "blocks": [{k: _zero_like(v) for k, v in blk.items()} for blk in self.blocks],
                   "lnf_g": _zero_like(self.lnf_g), "lnf_b": _zero_like(self.lnf_b),
                   "Wh": _zero_like(self.Wh), "bh": _zero_like(self.bh)}

        dE = dlogits @ self.Wh.T
        g["Wh"] += cache["Ef"].reshape(-1, self.d_model).T @ dlogits.reshape(-1, self.vocab_size)
        g["bh"] += dlogits.sum(axis=(0, 1))
        dE, dg, db = self._ln_backward(dE, cache["x_hatf"], self.lnf_g, cache["invf"])
        g["lnf_g"] += dg
        g["lnf_b"] += db

        Dh = self.d_model // self.n_heads
        for bi in range(self.n_blocks - 1, -1, -1):
            blk = self.blocks[bi]
            c = cache["blocks"][bi]
            gb = g["blocks"][bi]

            dE2 = dE  # residual through the FF sum
            dFF = dE
            gb["W2"] += c["F"].reshape(-1, 4 * self.d_model).T @ dFF.reshape(-1, self.d_model)
            gb["b2"] += dFF.sum(axis=(0, 1))
            dFpre = np.where(c["Fmask"], dFF @ blk["W2"].T, 0.0)
            gb["W1"] += c["H2"].reshape(-1, self.d_model).T @ dFpre.reshape(-1, 4 * self.d_model)
            gb["b1"] += dFpre.sum(axis=(0, 1))
            dH2 = dFpre @ blk["W1"].T
            dE2_ln, dg2, db2 = self._ln_backward(dH2, c["x_hat2"], blk["ln2_g"], c["inv2"])
            gb["ln2_g"] += dg2
            gb["ln2_b"] += db2
            dE2 = dE2 + dE2_ln

            gb["Wo"] += c["ctx2d"].reshape(-1, self.d_model).T @ dE2.reshape(-1, self.d_model)
            dctx = (dE2 @ blk["Wo"].T).reshape(B, T, self.n_heads, Dh).transpose(0, 2, 1, 3)
            dV = c["A"].transpose(0, 1, 3, 2) @ dctx
            dA = dctx @ c["V"].transpose(0, 1, 3, 2)
            dZ = c["A"] * (dA - (dA * c["A"]).sum(axis=-1, keepdims=True))
            dQ = dZ @ c["K"] / np.sqrt(Dh)
            dK = dZ.transpose(0, 1, 3, 2) @ c["Q"] / np.sqrt(Dh)
            dQ2d = dQ.transpose(0, 2, 1, 3).reshape(B, T, self.d_model)
            dK2d = dK.transpose(0, 2, 1, 3).reshape(B, T, self.d_model)
            dV2d = dV.transpose(0, 2, 1, 3).reshape(B, T, self.d_model)
            Hf = c["H"].reshape(-1, self.d_model)
            gb["Wq"] += Hf.T @ dQ2d.reshape(-1, self.d_model)
            gb["Wk"] += Hf.T @ dK2d.reshape(-1, self.d_model)
            gb["Wv"] += Hf.T @ dV2d.reshape(-1, self.d_model)
            dH = dQ2d @ blk["Wq"].T + dK2d @ blk["Wk"].T + dV2d @ blk["Wv"].T
            dE_in, dg1, db1 = self._ln_backward(dH, c["x_hat1"], blk["ln1_g"], c["inv1"])
            gb["ln1_g"] += dg1
            gb["ln1_b"] += db1
            dE = dE2 + dE_in

        np.add.at(g["tok_emb"], tok, dE)
        g["pos_emb"][:T] += dE.sum(axis=0)
        return loss, g

    # ---- training ----
    def eval_loss(self, windows: list[list[int]], batch_size: int = 32) -> float:
        if not windows:
            return float("nan")
        total, count = 0.0, 0
        for start in range(0, len(windows), batch_size):
            chunk = windows[start:start + batch_size]
            x = np.array([w[:-1] for w in chunk])
            y = np.array([w[1:] for w in chunk])
            logits = self._forward(x)
            shifted = logits - logits.max(axis=-1, keepdims=True)
            e = np.exp(shifted)
            p = e / e.sum(axis=-1, keepdims=True)
            ce = -np.log(p[np.arange(len(chunk))[:, None], np.arange(x.shape[1]), y] + 1e-12)
            total += float(ce.sum())
            count += ce.size
        return total / max(count, 1)

    def fit(self, train_windows: list[list[int]], val_windows: list[list[int]], *,
            epochs: int = 20, batch_size: int = 8, lr: float = 0.003,
            patience: int = 0, seed: int = 42) -> dict:
        rng = np.random.default_rng(seed)
        opt = _Adam(self._params_tree(), lr)
        history: dict = {"train_loss": [], "val_loss": []}
        best_val = float("inf")
        best_state: dict | None = None
        best_epoch = 0
        started = time.time()
        n = len(train_windows)
        for epoch in range(1, epochs + 1):
            order = rng.permutation(n)
            losses = []
            for start in range(0, n, batch_size):
                batch = [train_windows[i] for i in order[start:start + batch_size]]
                x = np.array([w[:-1] for w in batch])
                y = np.array([w[1:] for w in batch])
                cache: dict = {}
                self._forward(x, cache)
                loss, grads = self._backward(y, cache)
                opt.step(self._params_tree(), grads)
                losses.append(loss)
            history["train_loss"].append(round(sum(losses) / max(len(losses), 1), 6))
            if val_windows:
                vloss = self.eval_loss(val_windows)
                history["val_loss"].append(round(vloss, 6))
                if vloss < best_val:
                    best_val = vloss
                    best_epoch = epoch
                    best_state = _snapshot(self._params_tree())
                elif patience and epoch - best_epoch >= patience:
                    break
            elif best_epoch == 0:
                best_epoch = epoch
        if best_state is not None:
            _restore(self._params_tree(), best_state)
        history["epochs_run"] = len(history["train_loss"])
        history["best_epoch"] = best_epoch
        history["train_seconds"] = round(time.time() - started, 2)
        return history

    # ---- sampling ----
    def generate(self, prompt_ids: list[int], max_new: int, temperature: float = 0.8,
                 top_k: int = 0, seed: int = 42) -> list[int]:
        rng = np.random.default_rng(seed)
        ids = [1] + list(prompt_ids)  # <bos> prefix keeps empty prompts well-defined
        out: list[int] = []
        for _ in range(max_new):
            x = np.array([ids[-self.n_ctx:]])
            logits = self._forward(x)[0, -1].copy()
            logits /= max(float(temperature), 1e-3)
            if top_k and 0 < top_k < self.vocab_size:
                kth = np.sort(logits)[-top_k]
                logits = np.where(logits < kth, -1e9, logits)
            shifted = logits - logits.max()
            p = np.exp(shifted)
            p /= p.sum()
            nxt = int(rng.choice(self.vocab_size, p=p))
            ids.append(nxt)
            out.append(nxt)
        return out


class _Adam:
    """Adam over the plain-numpy parameter tree (in place)."""

    def __init__(self, params: dict, lr: float):
        self.lr = lr
        self.t = 0
        self.m = _zeros_tree(params)
        self.v = _zeros_tree(params)

    def step(self, params: dict, grads: dict) -> None:
        self.t += 1
        b1, b2, eps = 0.9, 0.999, 1e-8
        bc1 = 1 - b1 ** self.t
        bc2 = 1 - b2 ** self.t

        def _walk(p, g, m, v):
            if isinstance(p, dict):
                for k in p:
                    _walk(p[k], g[k], m[k], v[k])
            elif isinstance(p, list):
                for i in range(len(p)):
                    _walk(p[i], g[i], m[i], v[i])
            else:
                m += (1 - b1) * (g - m)
                v += (1 - b2) * (g * g - v)
                p -= self.lr * (m / bc1) / (np.sqrt(v / bc2) + eps)

        _walk(params, grads, self.m, self.v)


def _zeros_tree(params: dict) -> dict:
    def _build(p):
        if isinstance(p, dict):
            return {k: _build(v) for k, v in p.items()}
        if isinstance(p, list):
            return [_build(v) for v in p]
        return np.zeros_like(p)

    return _build(params)


def _snapshot(tree: dict) -> dict:
    def _build(p):
        if isinstance(p, dict):
            return {k: _build(v) for k, v in p.items()}
        if isinstance(p, list):
            return [_build(v) for v in p]
        return p.copy()

    return _build(tree)


def _restore(tree: dict, snap: dict) -> None:
    def _walk(p, s):
        if isinstance(p, dict):
            for k in p:
                _walk(p[k], s[k])
        elif isinstance(p, list):
            for i in range(len(p)):
                _walk(p[i], s[i])
        else:
            p[...] = s

    _walk(tree, snap)


# ----------------------------------------------------------------- tokenizer
def _fit_vocab(texts: list[str], vocab_size: int) -> dict[str, int]:
    counts = Counter(t for text in texts for t in _tokenize(text))
    vocab: dict[str, int] = {_UNK: 0, _BOS: 1}
    for word, _n in counts.most_common(max(0, vocab_size - 2)):
        vocab[word] = len(vocab)
    return vocab


def _encode(text: str, vocab: dict[str, int]) -> list[int]:
    unk = vocab[_UNK]
    return [vocab.get(t, unk) for t in _tokenize(text)]


def _decode(ids: list[int], id2tok: dict[int, str]) -> str:
    return " ".join(id2tok.get(i, _UNK) for i in ids if i != 1)


# ------------------------------------------------------- byte-level BPE (v65)
# A self-contained byte-level Byte-Pair-Encoding tokenizer (GPT-style, minus
# the regex pre-tokenizer): every one of the 256 bytes starts as its own
# token and the trainer repeatedly merges the most frequent adjacent pair
# until the vocabulary cap is reached.  Because the base alphabet covers all
# bytes, encoding is LOSSLESS - decode(encode(text)) == text for ANY input,
# including characters never seen during training (they fall back to raw
# bytes instead of an <unk> hole).
_BPE_MIN_VOCAB = 258  # 1 <unk> + 1 <bos> + 256 byte tokens


class _BPE:
    """Trained byte-level BPE: vocab maps token string -> id; merges is the
    ordered list of (left, right) pairs.  Token strings are single characters
    for the 256 base bytes (chr(byte)) and concatenated characters for
    merges, so id -> string -> bytes roundtrips exactly."""

    def __init__(self, vocab: dict[str, int], merges: list[tuple[str, str]]):
        self.vocab = vocab
        self.merges = merges
        self._ranks: dict[tuple[str, str], int] = {pair: i for i, pair in enumerate(merges)}
        self._cache: dict[str, list[int]] = {}

    @classmethod
    def train(cls, texts: list[str], vocab_size: int) -> "_BPE":
        vocab_size = max(int(vocab_size), _BPE_MIN_VOCAB)
        vocab: dict[str, int] = {_UNK: 0, _BOS: 1}
        for b in range(256):
            vocab[chr(b)] = 2 + b
        seqs = [[chr(b) for b in (text or "").encode("utf-8")] for text in texts]
        seqs = [s for s in seqs if s]
        merges: list[tuple[str, str]] = []
        while len(vocab) < vocab_size:
            counts: Counter = Counter()
            for s in seqs:
                for a, c in zip(s, s[1:]):
                    counts[(a, c)] += 1
            if not counts:
                break
            # most frequent pair (ties break lexicographically for determinism)
            # whose concatenation is not already a token - two different pairs
            # can concatenate to the same string ("a"+"bc" vs "ab"+"c"); the
            # duplicate is skipped and training continues with the next pair.
            ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            best = next((p for p, c in ranked if c >= 2 and p[0] + p[1] not in vocab), None)
            if best is None:
                break
            new_tok = best[0] + best[1]
            vocab[new_tok] = len(vocab)
            merges.append(best)
            seqs = [_merge_seq(s, best, new_tok) for s in seqs]
        return cls(vocab, merges)

    def encode(self, text: str) -> list[int]:
        cached = self._cache.get(text)
        if cached is not None:
            return list(cached)
        symbols = [chr(b) for b in (text or "").encode("utf-8")]
        for a, b in self.merges:
            symbols = _merge_seq(symbols, (a, b), a + b)
        ids = [self.vocab[s] for s in symbols]
        self._cache[text] = list(ids)
        return ids

    def decode(self, ids: list[int]) -> str:
        id2tok = {i: t for t, i in self.vocab.items()}
        raw = "".join(id2tok.get(int(i), "") for i in ids if int(i) != _BOS_ID)
        return raw.encode("latin-1").decode("utf-8", errors="replace")

    def state(self) -> dict:
        return {"type": "bpe", "vocab": dict(self.vocab), "merges": [list(p) for p in self.merges]}

    @classmethod
    def from_state(cls, st: dict) -> "_BPE":
        return cls(dict(st["vocab"]), [(a, b) for a, b in st["merges"]])


_BOS_ID = 1


def _merge_seq(symbols: list[str], pair: tuple[str, str], new_tok: str) -> list[str]:
    """Replace every non-overlapping occurrence of ``pair`` inside ``symbols``."""
    out: list[str] = []
    i = 0
    n = len(symbols)
    a, b = pair
    while i < n:
        if i < n - 1 and symbols[i] == a and symbols[i + 1] == b:
            out.append(new_tok)
            i += 2
        else:
            out.append(symbols[i])
            i += 1
    return out


def _fit_tokenizer(texts: list[str], kind: str, vocab_size: int) -> tuple[dict[str, int], dict]:
    """Returns (vocab, tokenizer_state) for the requested tokenizer kind."""
    if kind == "bpe":
        tok = _BPE.train(texts, vocab_size)
        return dict(tok.vocab), tok.state()
    return _fit_vocab(texts, vocab_size), {"type": "word"}


def _encode_with(text: str, vocab: dict[str, int], tokenizer: dict) -> list[int]:
    if (tokenizer or {}).get("type") == "bpe":
        return _BPE.from_state(tokenizer).encode(text)
    return _encode(text, vocab)


def _decode_with(ids: list[int], vocab: dict[str, int], tokenizer: dict) -> str:
    if (tokenizer or {}).get("type") == "bpe":
        return _BPE.from_state(tokenizer).decode(ids)
    id2tok = {i: t for t, i in vocab.items()}
    return _decode(ids, id2tok)


def _windows(doc_ids: list[int], n_ctx: int) -> list[list[int]]:
    """Slide a (n_ctx + 1)-token window over one document; short documents
    are left-padded with <bos> so every position keeps a real target."""
    w: list[list[int]] = []
    if len(doc_ids) < 2:
        return w
    if len(doc_ids) >= n_ctx + 1:
        stride = max(1, n_ctx // 2)
        for start in range(0, len(doc_ids) - n_ctx, stride):
            w.append(doc_ids[start:start + n_ctx + 1])
        if not w or w[-1] != doc_ids[-(n_ctx + 1):]:
            w.append(doc_ids[-(n_ctx + 1):])
    else:
        w.append([1] * (n_ctx + 1 - len(doc_ids)) + doc_ids)
    return w


async def _resolve_registry_artifact(model_ref: str, owner_id: str | None) -> tuple[dict, dict]:
    """Registry name/id -> (info, pickled payload) with honest failures."""
    from ...db import AsyncSessionLocal
    from ...models import Artifact
    from ...services import artifacts as art_svc
    from ...services import models as model_svc

    async with AsyncSessionLocal() as session:
        row = await model_svc.resolve_model(session, model_ref.strip(), owner_id=owner_id)
        if row is None:
            raise NodeExecutionError(f"Model {model_ref!r} not found in the registry (or not owned by you)")
        info = model_svc.model_out(row)
        art = await session.get(Artifact, row.artifact_id) if row.artifact_id else None
        if art is None:
            raise NodeExecutionError(f"Model {info['name']} v{info['version']} has no loadable artifact")
        raw = art_svc.read_bytes(art)
    try:
        payload = pickle.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise NodeExecutionError(f"Model artifact is corrupted: {exc}") from exc
    return info, payload


# ----------------------------------------------------------------- node: train
class LMTrainNode(BaseNode):
    """v64: from-scratch transformer language-model training + continued pretraining."""

    type = "lm_train"
    name = "Language Model Train"
    description = (
        "Trains a causal transformer language model FROM SCRATCH in raw numpy "
        "on a text corpus (next-token prediction; held-out perplexity). Point "
        "base_model at a registered lm_train model to CONTINUE PRETRAINING it "
        "on a new corpus - weights AND tokenizer carry over, lineage recorded."
    )
    category = "ai"
    icon = "languages"
    color = "#c084fc"

    class ParamsModel(BaseModel):
        text_column: str = Field(default="", description="Column holding the raw text")
        base_model: str = Field(default="", description="Registry name/id of an lm_train model to continue pretraining from (empty = from scratch)")
        tokenizer: str = Field(default="word", json_schema_extra={
            "widget": "select", "options": ["word", "bpe"]},
            description="word = regex word vocabulary; bpe = byte-level Byte Pair Encoding (v65, lossless roundtrip)")
        vocab_size: int = Field(default=600, ge=50, le=8000, description="Vocabulary cap (BPE counts the 256 byte tokens + 2 specials)")
        d_model: int = Field(default=32, ge=8, le=256)
        n_heads: int = Field(default=2, ge=1, le=8)
        n_ctx: int = Field(default=16, ge=4, le=64, description="Context window in tokens")
        n_blocks: int = Field(default=1, ge=1, le=3)
        epochs: int = Field(default=20, ge=1, le=300)
        batch_size: int = Field(default=8, ge=1, le=128)
        learning_rate: float = Field(default=0.003, gt=0, le=0.1)
        patience: int = Field(default=0, ge=0, le=50, description="Early stopping patience on val loss (0 = off)")
        seed: int = Field(default=42)
        device: str = Field(default="cpu", json_schema_extra={
            "widget": "select", "options": ["cpu", "auto", "gpu"]},
            description="Device policy - py8n refuses to fake GPU compute (v65)")
        model_name: str = Field(default="", description="Registry name (empty = 'lm_base')")
        register: bool = Field(default=True)

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: LMTrainNode.ParamsModel
        if not p.text_column:
            raise NodeExecutionError("A text column is required")
        items = _items(_working_data(context.current_input))
        rows = [r for r in items if isinstance(r, dict)]
        texts = [str(r.get(p.text_column) or "").strip() for r in rows]
        texts = [t for t in texts if t]
        if len(texts) < 8:
            raise NodeExecutionError(
                f"Language-model training needs at least 8 non-empty text rows (got {len(texts)})")

        if p.tokenizer not in ("word", "bpe"):
            raise NodeExecutionError(f"unknown tokenizer {p.tokenizer!r} (allowed: word, bpe)")
        if p.tokenizer == "bpe" and not p.base_model.strip() and p.vocab_size < _BPE_MIN_VOCAB:
            raise NodeExecutionError(
                f"the BPE tokenizer always carries all 256 byte tokens + 2 specials, "
                f"so vocab_size must be >= {_BPE_MIN_VOCAB} (got {p.vocab_size})")
        from ...services.devices import resolve_device

        try:
            dev = resolve_device(p.device)
        except ValueError as exc:
            raise NodeExecutionError(str(exc)) from exc

        # ---- continue pretraining from the registry? ----
        base_payload = None
        if p.base_model.strip():
            base_info, base_payload = await _resolve_registry_artifact(
                p.base_model, getattr(context, "owner_id", None))
            if base_payload.get("kind") != "lm":
                raise NodeExecutionError(
                    f"continued pretraining needs an lm_train model - "
                    f"{(base_payload.get('algorithm') if isinstance(base_payload, dict) else None) or base_info['algorithm']!r} "
                    "is not a language model; pretrain one first with lm_train")
            net = _TinyLM.from_state(base_payload["net"])
            vocab = dict(base_payload["vocab"])
            tokenizer_state = dict(base_payload.get("tokenizer") or {"type": "word"})
            cfg = dict(base_payload["config"])

        if base_payload is None:
            if p.d_model % p.n_heads != 0:
                raise NodeExecutionError(f"d_model {p.d_model} is not divisible by n_heads {p.n_heads}")
            vocab, tokenizer_state = _fit_tokenizer(texts, p.tokenizer, p.vocab_size)
            cfg = {"vocab_size": len(vocab), "d_model": p.d_model, "n_heads": p.n_heads,
                   "n_ctx": p.n_ctx, "n_blocks": p.n_blocks}
            net = _TinyLM(seed=p.seed, **cfg)

        doc_ids = [_encode_with(t, vocab, tokenizer_state) for t in texts]
        total_tokens = sum(len(d) for d in doc_ids)
        if total_tokens < 40:
            raise NodeExecutionError(
                f"Corpus is too small to pretrain on - {total_tokens} tokens across {len(doc_ids)} rows (need 40+)")

        val_n = max(1, int(len(doc_ids) * 0.2))
        train_docs = doc_ids[:-val_n] or doc_ids[:1]
        val_docs = doc_ids[-val_n:]
        train_windows = [w for d in train_docs for w in _windows(d, int(cfg["n_ctx"]))]
        val_windows = [w for d in val_docs for w in _windows(d, int(cfg["n_ctx"]))]
        if not train_windows:
            raise NodeExecutionError("No trainable windows - the corpus rows are too short after tokenization")

        history = net.fit(train_windows, val_windows, epochs=p.epochs, batch_size=p.batch_size,
                          lr=p.learning_rate, patience=p.patience, seed=p.seed)
        val_loss = net.eval_loss(val_windows) if val_windows else history["train_loss"][-1]
        perplexity = round(float(min(np.exp(min(val_loss, 20.0)), 5e8)), 2)

        metrics: dict[str, Any] = {
            "final_loss": history["train_loss"][-1] if history["train_loss"] else None,
            "val_loss": round(float(val_loss), 4),
            "perplexity": perplexity,
            "tokens_total": total_tokens,
            "vocabulary": len(vocab),
            "params_count": net.params_count(),
            "architecture": net.architecture(),
            "epochs_run": history["epochs_run"],
            "best_epoch": history["best_epoch"],
            "train_seconds": history["train_seconds"],
            "optimizer": "adam",
            "learning_rate": p.learning_rate,
            "device": dev["resolved"],
            "device_backend": dev["backend"],
            "tokenizer": (tokenizer_state.get("type") or "word") + (
                f" ({len(tokenizer_state.get('merges') or [])} merges)"
                if tokenizer_state.get("type") == "bpe" else ""),
            "chars_per_token": round(sum(len(t) for t in texts) / max(total_tokens, 1), 3),
        }
        continued_from = None
        if base_payload is not None:
            continued_from = {"registry_id": base_info["id"], "name": base_info["name"],
                              "version": base_info["version"]}
            metrics["continued_pretrained_from"] = f"{base_info['name']} v{base_info['version']}"
        if dev.get("note"):
            metrics["device_note"] = dev["note"]

        payload = {
            "kind": "lm",
            "net": net.state(),
            "vocab": vocab,
            "tokenizer": tokenizer_state,
            "config": cfg,
            "task": "language_modeling",
            "algorithm": "lm_transformer",
            "text_column": p.text_column,
            "continued_from": continued_from,
            "history": {"train_loss": history["train_loss"][-50:], "val_loss": history["val_loss"][-50:]},
        }
        saved = await _save_artifact_row(
            context,
            kind="model",
            data=pickle.dumps(payload),
            content_type="application/octet-stream",
            meta={"model": "lm_base", "task": "language_modeling", "metrics": metrics,
                  "node": self.name, "text_column": p.text_column},
            filename="model.pkl",
        )

        registry_row = None
        if p.register:
            from ...db import AsyncSessionLocal
            from ...services import models as model_svc

            name = (p.model_name or "").strip() or "lm_base"
            async with AsyncSessionLocal() as session:
                row = await model_svc.register_model(
                    session,
                    name=name,
                    algorithm="lm_transformer",
                    task="language_modeling",
                    target=p.text_column,
                    features=[],
                    metrics=metrics,
                    artifact_id=saved["id"],
                    owner_id=getattr(context, "owner_id", None),
                    dataset_name=None,
                    row_count=int(len(texts)),
                    activate=True,
                    reference_stats=None,  # honest: PSI drift does not apply to an LM
                )
                await session.commit()
                registry_row = model_svc.model_out(row)

        sample_items = []
        for doc in val_docs[:10]:
            wins = _windows(doc, int(cfg["n_ctx"]))
            dl = net.eval_loss(wins) if wins else None
            sample_items.append({
                "text": _decode_with(doc, vocab, tokenizer_state)[:160],
                "tokens": len(doc),
                "ce_loss": round(float(dl), 4) if dl is not None and dl == dl else None,
            })

        out: dict[str, Any] = {
            "items": sample_items,
            "mode": "continued pretrain" if base_payload is not None else "from-scratch pretrain",
            "metrics": metrics,
            "perplexity": perplexity,
            "tokens_total": total_tokens,
            "vocabulary": len(vocab),
            "model_id": saved["id"],
            "artifact_url": f"/api/v1/artifacts/{saved['id']}/content",
        }
        if registry_row is not None:
            out["registry"] = {"id": registry_row["id"], "name": registry_row["name"],
                               "version": registry_row["version"], "active": registry_row["active"]}
        return self._single(out)


# ----------------------------------------------------------------- node: generate
class LMGenerateNode(BaseNode):
    """v64: autoregressive sampling from a registered language model."""

    type = "lm_generate"
    name = "Language Model Generate"
    description = (
        "Loads a registered lm_train model (by name -> ACTIVE version, or id), "
        "tokenizes the prompt with ITS fitted vocabulary and continues it "
        "autoregressively with temperature / top-k sampling."
    )
    category = "ai"
    icon = "sparkles"
    color = "#f0abfc"

    class ParamsModel(BaseModel):
        model: str = Field(default="", description="Registry name (ACTIVE version) or registry row id")
        prompt: str = Field(default="", description="Prompt text - supports {{ expressions }}",
                            json_schema_extra={"widget": "textarea", "rows": 3})
        max_tokens: int = Field(default=16, ge=1, le=64, description="Tokens to sample (clipped to the model's context window)")
        temperature: float = Field(default=0.8, gt=0, le=2)
        top_k: int = Field(default=40, ge=0, description="Keep only the k most likely tokens (0 = off)")
        seed: int = Field(default=42)

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: LMGenerateNode.ParamsModel
        if not p.model or not p.model.strip():
            raise NodeExecutionError("A model name or id is required")
        info, payload = await _resolve_registry_artifact(
            p.model, getattr(context, "owner_id", None))
        if payload.get("kind") != "lm":
            raise NodeExecutionError(
                f"lm_generate needs an lm_train model - "
                f"{payload.get('algorithm') or info['algorithm']!r} "
                "is not a language model; train one with lm_train first")
        net = _TinyLM.from_state(payload["net"])
        vocab = payload["vocab"]
        tokenizer_state = dict(payload.get("tokenizer") or {"type": "word"})
        cfg = payload["config"]
        prompt = str(p.prompt or "").strip()
        ids = _encode_with(prompt, vocab, tokenizer_state) if prompt else []
        max_new = min(p.max_tokens, int(cfg["n_ctx"]))
        gen_ids = net.generate(ids, max_new, temperature=p.temperature,
                               top_k=p.top_k, seed=p.seed)
        text = _decode_with(gen_ids, vocab, tokenizer_state)
        return self._single({
            "items": [{"prompt": prompt, "generated": text}],
            "text": text,
            "tokens_generated": len(gen_ids),
            "tokenizer": tokenizer_state.get("type") or "word",
            "model": {"id": info["id"], "name": info["name"], "version": info["version"],
                      "algorithm": info["algorithm"]},
        })
