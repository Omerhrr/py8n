"""Torch training backend (v66) - the compute half of GPU execution mode.

v65 built the honest device POLICY (detect accelerators, resolve intent,
refuse to fake GPU placement). v66 builds the compute BRIDGE it was
refusing to fake: torch mirrors of the two from-scratch training cores,

* ``_TorchMLP``  - the multilayer perceptron (mirror of nodes/modal._MLP)
* ``_TorchLM``   - the causal transformer LM (mirror of nodes/lm._TinyLM)

Both are drop-in duck-type compatible with their numpy twins AND share
the exact same state() format - so a model pretrained on the numpy CPU
core can be continued/fine-tuned on torch (and back), served by either
backend, and pickled identically. When the resolved device is CUDA or
MPS the tensors live on the accelerator; on ``device=torch`` without an
accelerator the torch CPU backend runs (an explicit opt-in).

Design notes:
* forward/backward use plain torch ops + autograd (no nn.Module tree) -
  the math mirrors the numpy implementations one line at a time.
* init does NOT match numpy RNG (different generators); continued
  training does not care because weights are loaded from state.
* state() always converts to CPU numpy lists so artifacts stay
  backend-agnostic.
"""

from __future__ import annotations

import importlib.util
import math
import time
from typing import Any


def torch_available() -> bool:
    return importlib.util.find_spec("torch") is not None


def _torch():
    import torch  # type: ignore

    return torch


# --------------------------------------------------------------------- MLP
class _TorchMLP:
    """Duck-type mirror of nodes.modal._MLP on torch tensors."""

    def __init__(self, layer_sizes: list[int], activation: str = "relu",
                 seed: int = 42, device: str = "cpu"):
        torch = _torch()
        self.layer_sizes = list(layer_sizes)
        self.activation = activation
        self.torch_device = device
        gen = torch.Generator().manual_seed(int(seed))

        def _w(i: int) -> Any:
            fan_in = self.layer_sizes[i]
            w = torch.randn(self.layer_sizes[i], self.layer_sizes[i + 1], generator=gen, dtype=torch.float64)
            w = w * (2.0 / math.sqrt(fan_in))
            return w.to(device).requires_grad_(True)

        self.weights = [_w(i) for i in range(len(self.layer_sizes) - 1)]
        self.biases = [
            torch.zeros(self.layer_sizes[i + 1], dtype=torch.float64, device=device).requires_grad_(True)
            for i in range(len(self.layer_sizes) - 1)
        ]

    # ---- forward ----
    def _forward_t(self, X: Any) -> Any:
        torch = _torch()
        a = X
        last = len(self.weights) - 1
        for i, (W, b) in enumerate(zip(self.weights, self.biases)):
            z = a @ W + b
            if i == last:
                a = z  # linear head (softmax/MSE applied by the loss)
            elif self.activation == "tanh":
                a = torch.tanh(z)
            else:
                a = torch.relu(z)
        return a

    def predict(self, X) -> "Any":
        torch = _torch()
        xt = torch.as_tensor(X, dtype=torch.float64, device=self.torch_device)
        with torch.no_grad():
            out = self._forward_t(xt)
        out = out.detach().cpu().numpy()
        if out.shape[1] == 1:
            return out[:, 0]
        return out

    def predict_proba(self, X) -> Any:
        out = self.predict(X)
        import numpy as np

        if out.ndim == 1:
            return np.column_stack([1 - out, out])
        e = np.exp(out - out.max(axis=1, keepdims=True))
        return e / e.sum(axis=1, keepdims=True)

    # ---- training ----
    def fit(self, X, y, *, task: str = "", epochs: int = 60, batch_size: int = 16,
            lr: float = 0.01, optimizer: str = "adam", l2: float = 0.0,
            val_data: tuple | None = None, patience: int = 0, seed: int = 42,
            verbose: bool = False) -> dict:
        torch = _torch()
        import numpy as np

        Xa = np.asarray(X, dtype=np.float64)
        ya = np.asarray(y)
        n = Xa.shape[0]
        out_dim = self.layer_sizes[-1]
        if out_dim == 1:
            Ya = ya.reshape(-1, 1).astype(np.float64)
        else:
            Ya = np.eye(out_dim)[ya.astype(int)]
        Xt = torch.as_tensor(Xa, dtype=torch.float64, device=self.torch_device)
        Yt = torch.as_tensor(Ya, dtype=torch.float64, device=self.torch_device)

        params = list(self.weights) + list(self.biases)
        if optimizer == "adam":
            opt = torch.optim.Adam(params, lr=lr)
        elif optimizer == "momentum":
            opt = torch.optim.SGD(params, lr=lr, momentum=0.9)
        else:
            opt = torch.optim.SGD(params, lr=lr)

        rng = np.random.default_rng(seed)
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
                it = torch.as_tensor(idx, dtype=torch.long, device=self.torch_device)
                xb, yb = Xt[it], Yt[it]
                opt.zero_grad()
                out = self._forward_t(xb)
                if out_dim == 1:
                    base = torch.mean((out - yb) ** 2)
                else:
                    shifted = out - out.max(dim=1, keepdim=True).values
                    e = torch.exp(shifted)
                    p = e / e.sum(dim=1, keepdim=True)
                    base = -torch.mean(torch.log(p[torch.arange(len(idx)), yb.argmax(dim=1)] + 1e-12))
                if l2 > 0:
                    # mirror numpy: L2 on the WEIGHTS only
                    loss = base + 0.5 * l2 * sum((W ** 2).sum() for W in self.weights)
                else:
                    loss = base
                loss.backward()
                opt.step()
                epoch_loss += float(base.detach())
                batches += 1
            history["train_loss"].append(round(epoch_loss / max(batches, 1), 6))

            if val_data is not None and len(val_data[0]):
                Xv, yv = val_data
                with torch.no_grad():
                    outv = self._forward_t(
                        torch.as_tensor(np.asarray(Xv, dtype=np.float64), dtype=torch.float64,
                                        device=self.torch_device))
                if out_dim == 1:
                    yvt = torch.as_tensor(np.asarray(yv, dtype=np.float64).reshape(-1, 1),
                                          dtype=torch.float64, device=self.torch_device)
                    vloss = float(torch.mean((outv - yvt) ** 2))
                else:
                    Yvt = torch.as_tensor(np.eye(out_dim)[np.asarray(yv).astype(int)],
                                          dtype=torch.float64, device=self.torch_device)
                    shifted = outv - outv.max(dim=1, keepdim=True).values
                    e = torch.exp(shifted)
                    pv = e / e.sum(dim=1, keepdim=True)
                    vloss = float(-torch.mean(torch.log(pv[torch.arange(len(Yvt)), Yvt.argmax(dim=1)] + 1e-12)))
                history["val_loss"].append(round(vloss, 6))
                if vloss < best_val:
                    best_val = vloss
                    best_epoch = epoch
                    best_state = ([w.detach().clone() for w in self.weights],
                                  [b.detach().clone() for b in self.biases])
                elif patience and epoch - best_epoch >= patience:
                    break
            elif best_val == float("inf"):
                best_epoch = epoch

        if best_state is not None:
            for p, snap in list(zip(self.weights, best_state[0])) + list(zip(self.biases, best_state[1])):
                p.data.copy_(snap)
        history["epochs_run"] = len(history["train_loss"])
        history["best_epoch"] = best_epoch
        history["train_seconds"] = round(time.time() - started, 2)
        return history

    def params_count(self) -> int:
        return int(sum(p.numel() for p in list(self.weights) + list(self.biases)))

    def state(self) -> dict:
        return {
            "layer_sizes": self.layer_sizes,
            "activation": self.activation,
            "weights": [w.detach().cpu().numpy().tolist() for w in self.weights],
            "biases": [b.detach().cpu().numpy().tolist() for b in self.biases],
        }

    @classmethod
    def from_state(cls, state: dict, device: str = "cpu") -> "_TorchMLP":
        torch = _torch()
        net = cls.__new__(cls)
        net.layer_sizes = list(state["layer_sizes"])
        net.activation = state["activation"]
        net.torch_device = device
        net.weights = [
            torch.as_tensor(w, dtype=torch.float64, device=device).requires_grad_(True)
            for w in state["weights"]
        ]
        net.biases = [
            torch.as_tensor(b, dtype=torch.float64, device=device).requires_grad_(True)
            for b in state["biases"]
        ]
        return net


# ---------------------------------------------------------------------- LM
class _TorchLM:
    """Duck-type mirror of nodes.lm._TinyLM (causal transformer) on torch.

    Same architecture, same state() layout (numpy lists), same fit/generate
    API - plus the v66 condition-prefix adapter (``cond_dim`` > 0 adds a
    learned projection whose output is prepended to every sequence).
    """

    def __init__(self, vocab_size: int, d_model: int = 32, n_heads: int = 2,
                 n_ctx: int = 16, n_blocks: int = 1, seed: int = 42,
                 cond_dim: int = 0, device: str = "cpu"):
        torch = _torch()
        if d_model % n_heads != 0:
            raise ValueError("d_model must be divisible by n_heads")
        gen = torch.Generator().manual_seed(int(seed))

        def _w(*shape: int) -> Any:
            return (torch.randn(*shape, generator=gen, dtype=torch.float64) * 0.02).to(device).requires_grad_(True)

        def _z(*shape: int) -> Any:
            return torch.zeros(*shape, dtype=torch.float64, device=device).requires_grad_(True)

        def _o(*shape: int) -> Any:
            return torch.ones(*shape, dtype=torch.float64, device=device).requires_grad_(True)

        self.torch_device = device
        self.vocab_size = int(vocab_size)
        self.d_model = int(d_model)
        self.n_heads = int(n_heads)
        self.n_ctx = int(n_ctx)
        self.n_blocks = int(n_blocks)
        self.cond_dim = int(cond_dim)

        self.tok_emb = _w(vocab_size, d_model)
        self.pos_emb = _w(n_ctx, d_model)
        self.blocks: list[dict] = []
        for _ in range(n_blocks):
            self.blocks.append({
                "ln1_g": _o(d_model), "ln1_b": _z(d_model),
                "Wq": _w(d_model, d_model), "Wk": _w(d_model, d_model),
                "Wv": _w(d_model, d_model), "Wo": _w(d_model, d_model),
                "ln2_g": _o(d_model), "ln2_b": _z(d_model),
                "W1": _w(d_model, 4 * d_model), "b1": _z(4 * d_model),
                "W2": _w(4 * d_model, d_model), "b2": _z(d_model),
            })
        self.lnf_g = _o(d_model)
        self.lnf_b = _z(d_model)
        self.Wh = _w(d_model, vocab_size)
        self.bh = _z(vocab_size)
        self.W_cond = _w(cond_dim, d_model) if cond_dim > 0 else None

    # ---- bookkeeping ----
    def _params(self) -> list:
        ps = [self.tok_emb, self.pos_emb, self.lnf_g, self.lnf_b, self.Wh, self.bh]
        for blk in self.blocks:
            ps.extend(blk.values())
        if self.W_cond is not None:
            ps.append(self.W_cond)
        return ps

    def params_count(self) -> int:
        return int(sum(p.numel() for p in self._params()))

    def architecture(self) -> str:
        base = (f"lm d{self.d_model} h{self.n_heads} b{self.n_blocks} "
                f"ctx{self.n_ctx} V{self.vocab_size}")
        return base + f" cond{self.cond_dim}" if self.cond_dim > 0 else base

    def state(self) -> dict:
        def _l(t: Any) -> list:
            return t.detach().cpu().numpy().tolist()

        st: dict = {
            "config": {"vocab_size": self.vocab_size, "d_model": self.d_model,
                       "n_heads": self.n_heads, "n_ctx": self.n_ctx,
                       "n_blocks": self.n_blocks, "cond_dim": self.cond_dim},
            "tok_emb": _l(self.tok_emb), "pos_emb": _l(self.pos_emb),
            "blocks": [{k: _l(v) for k, v in blk.items()} for blk in self.blocks],
            "lnf_g": _l(self.lnf_g), "lnf_b": _l(self.lnf_b),
            "Wh": _l(self.Wh), "bh": _l(self.bh),
        }
        if self.W_cond is not None:
            st["W_cond"] = _l(self.W_cond)
        return st

    @classmethod
    def from_state(cls, st: dict, device: str = "cpu") -> "_TorchLM":
        torch = _torch()
        cfg = st["config"]
        net = cls.__new__(cls)
        net.torch_device = device
        net.vocab_size = int(cfg["vocab_size"])
        net.d_model = int(cfg["d_model"])
        net.n_heads = int(cfg["n_heads"])
        net.n_ctx = int(cfg["n_ctx"])
        net.n_blocks = int(cfg["n_blocks"])
        net.cond_dim = int(cfg.get("cond_dim") or 0)

        def arr(x: list) -> Any:
            return torch.as_tensor(x, dtype=torch.float64, device=device)

        net.tok_emb = arr(st["tok_emb"]).requires_grad_(True)
        net.pos_emb = arr(st["pos_emb"]).requires_grad_(True)
        net.blocks = [{k: arr(v).requires_grad_(True) for k, v in blk.items()} for blk in st["blocks"]]
        net.lnf_g = arr(st["lnf_g"]).requires_grad_(True)
        net.lnf_b = arr(st["lnf_b"]).requires_grad_(True)
        net.Wh = arr(st["Wh"]).requires_grad_(True)
        net.bh = arr(st["bh"]).requires_grad_(True)
        net.W_cond = arr(st["W_cond"]).requires_grad_(True) if net.cond_dim > 0 else None
        return net

    def add_condition_adapter(self, cond_dim: int, seed: int = 42) -> None:
        """v66 multimodal fine-tuning: give a text-only LM a condition
        adapter. The backbone (and the positional embeddings) carry over
        UNCHANGED; only the fresh projection starts random."""
        torch = _torch()
        if self.cond_dim > 0:
            raise ValueError("this LM already has a condition adapter")
        gen = torch.Generator().manual_seed(int(seed))
        self.W_cond = (torch.randn(int(cond_dim), self.d_model, generator=gen, dtype=torch.float64) * 0.02) \
            .to(self.torch_device).requires_grad_(True)
        self.cond_dim = int(cond_dim)

    # ---- forward ----
    @staticmethod
    def _ln(x: Any, g: Any, b: Any, eps: float = 1e-5) -> Any:
        torch = _torch()
        mu = x.mean(dim=-1, keepdim=True)
        var = x.var(dim=-1, keepdim=True, unbiased=False)
        return g * (x - mu) / torch.sqrt(var + eps) + b

    def _forward(self, tok: Any, cond: Any | None = None) -> Any:
        torch = _torch()
        B, T = tok.shape
        Dh = self.d_model // self.n_heads
        E = self.tok_emb[tok] + self.pos_emb[:T][None, :, :]
        if self.W_cond is not None and cond is not None:
            cond_tok = (cond @ self.W_cond)[:, None, :]  # (B, 1, d) - positionless prefix
            E = torch.cat([cond_tok, E], dim=1)
        T_full = E.shape[1]
        mask = torch.triu(torch.ones(T_full, T_full, dtype=torch.bool, device=E.device), diagonal=1)
        for blk in self.blocks:
            H = self._ln(E, blk["ln1_g"], blk["ln1_b"])
            Q = (H @ blk["Wq"]).reshape(B, T_full, self.n_heads, Dh).transpose(1, 2)
            K = (H @ blk["Wk"]).reshape(B, T_full, self.n_heads, Dh).transpose(1, 2)
            V = (H @ blk["Wv"]).reshape(B, T_full, self.n_heads, Dh).transpose(1, 2)
            scores = Q @ K.transpose(2, 3) / math.sqrt(Dh)
            scores = torch.where(mask[None, None], torch.tensor(-1e9, device=E.device, dtype=E.dtype), scores)
            A = torch.softmax(scores, dim=-1)
            ctx = A @ V
            ctx2d = ctx.transpose(1, 2).reshape(B, T_full, self.d_model)
            E = E + ctx2d @ blk["Wo"]
            H2 = self._ln(E, blk["ln2_g"], blk["ln2_b"])
            Ff = torch.relu(H2 @ blk["W1"] + blk["b1"])
            E = E + Ff @ blk["W2"] + blk["b2"]
        Ef = self._ln(E, self.lnf_g, self.lnf_b)
        return Ef @ self.Wh + self.bh  # (B, T_full, V)

    def _loss(self, logits: Any, y: Any) -> Any:
        """Next-token CE. Unconditioned: position i predicts token i+1 over all
        T positions. Conditioned: the sequence is [cond, w_0..w_{T-1}] so text
        token w_i sits at position i+1 and predicts y_i - predictions come
        from positions 1..T (the cond token is a pure context prefix)."""
        torch = _torch()
        preds = logits[:, 1:, :] if self.W_cond is not None else logits
        shifted = preds - preds.max(dim=-1, keepdim=True).values
        e = torch.exp(shifted)
        p = e / e.sum(dim=-1, keepdim=True)
        B, Tp, _ = preds.shape
        flat = p.reshape(B * Tp, -1)
        tgt = y.reshape(B * Tp)
        return -torch.mean(torch.log(flat[torch.arange(B * Tp), tgt] + 1e-12))

    # ---- training ----
    def eval_window_losses(self, windows: list[list[int]], conds: list | None = None,
                           batch_size: int = 64) -> list[float]:
        """Per-window mean cross-entropy (nats) - the LM drift signal."""
        torch = _torch()
        out: list[float] = []
        for start in range(0, len(windows), batch_size):
            chunk = windows[start:start + batch_size]
            x = torch.tensor([w[:-1] for w in chunk], dtype=torch.long, device=self.torch_device)
            y = torch.tensor([w[1:] for w in chunk], dtype=torch.long, device=self.torch_device)
            cond = None
            if self.W_cond is not None:
                if conds is None:
                    raise ValueError("this LM is conditioned - conds are required")
                cond = torch.tensor(conds[start:start + batch_size], dtype=torch.float64,
                                    device=self.torch_device)
            with torch.no_grad():
                logits = self._forward(x, cond)
                # per-row CE: conditioned predictions start at position 1
                preds = logits[:, 1:, :] if self.W_cond is not None else logits
                shifted = preds - preds.max(dim=-1, keepdim=True).values
                e = torch.exp(shifted)
                p = e / e.sum(dim=-1, keepdim=True)
                B, Tp, _ = preds.shape
                ce = -torch.log(p[torch.arange(B)[:, None], torch.arange(Tp)[None, :], y] + 1e-12)
                out.extend(ce.mean(dim=1).detach().cpu().numpy().tolist())
        return [float(v) for v in out]

    def eval_loss(self, windows: list[list[int]], conds: list | None = None,
                  batch_size: int = 64) -> float:
        losses = self.eval_window_losses(windows, conds, batch_size)
        if not losses:
            return float("nan")
        return sum(losses) / len(losses)

    def fit(self, train_windows: list[list[int]], val_windows: list[list[int]], *,
            epochs: int = 20, batch_size: int = 8, lr: float = 0.003,
            patience: int = 0, seed: int = 42,
            conds_train: list | None = None, conds_val: list | None = None,
            grad_accum: int = 1) -> dict:
        torch = _torch()
        import numpy as np

        if self.W_cond is not None:
            if conds_train is None:
                raise ValueError("this LM is conditioned - conds_train is required")
        grad_accum = max(1, int(grad_accum))
        rng = np.random.default_rng(seed)
        opt = torch.optim.Adam(self._params(), lr=lr)
        history: dict = {"train_loss": [], "val_loss": []}
        best_val = float("inf")
        best_state = None
        best_epoch = 0
        started = time.time()
        n = len(train_windows)
        for epoch in range(1, epochs + 1):
            order = rng.permutation(n)
            losses = []
            micro = 0
            opt.zero_grad()
            for start in range(0, n, batch_size):
                idx = order[start:start + batch_size]
                batch = [train_windows[i] for i in idx]
                x = torch.tensor([w[:-1] for w in batch], dtype=torch.long, device=self.torch_device)
                y = torch.tensor([w[1:] for w in batch], dtype=torch.long, device=self.torch_device)
                cond = None
                if self.W_cond is not None:
                    cond = torch.tensor([conds_train[i] for i in idx], dtype=torch.float64,
                                        device=self.torch_device)
                logits = self._forward(x, cond)
                # v67 gradient accumulation: scale each micro-batch loss so the
                # summed gradients average over grad_accum micro-batches, then
                # step once per accumulation window
                loss = self._loss(logits, y) / grad_accum
                loss.backward()
                losses.append(float(loss.detach()) * grad_accum)
                micro += 1
                if micro % grad_accum == 0 or start + batch_size >= n:
                    opt.step()
                    opt.zero_grad()
            history["train_loss"].append(round(sum(losses) / max(len(losses), 1), 6))
            if val_windows:
                vloss = self.eval_loss(val_windows, conds_val)
                history["val_loss"].append(round(vloss, 6))
                if vloss < best_val:
                    best_val = vloss
                    best_epoch = epoch
                    best_state = [p.detach().clone() for p in self._params()]
                elif patience and epoch - best_epoch >= patience:
                    break
            elif best_epoch == 0:
                best_epoch = epoch
        if best_state is not None:
            for p, snap in zip(self._params(), best_state):
                p.data.copy_(snap)
        history["epochs_run"] = len(history["train_loss"])
        history["best_epoch"] = best_epoch
        history["train_seconds"] = round(time.time() - started, 2)
        return history

    # ---- sampling ----
    def generate(self, prompt_ids: list[int], max_new: int, temperature: float = 0.8,
                 top_k: int = 0, seed: int = 42, cond: list | None = None) -> list[int]:
        torch = _torch()
        import numpy as np

        if self.W_cond is not None and cond is None:
            raise ValueError("this LM is conditioned - a condition vector is required")
        rng = np.random.default_rng(seed)
        ct = None
        if self.W_cond is not None and cond is not None:
            ct = torch.tensor([cond], dtype=torch.float64, device=self.torch_device)
        ids = [1] + list(prompt_ids)  # <bos> prefix keeps empty prompts well-defined
        out: list[int] = []
        for _ in range(max_new):
            x = torch.tensor([ids[-self.n_ctx:]], dtype=torch.long, device=self.torch_device)
            with torch.no_grad():
                logits = self._forward(x, ct)
            row = logits[0, -1].detach().cpu().numpy().astype(np.float64)
            row /= max(float(temperature), 1e-3)
            if top_k and 0 < top_k < self.vocab_size:
                kth = np.sort(row)[-top_k]
                row = np.where(row < kth, -1e9, row)
            shifted = row - row.max()
            p = np.exp(shifted)
            p /= p.sum()
            nxt = int(rng.choice(self.vocab_size, p=p))
            ids.append(nxt)
            out.append(nxt)
        return out
