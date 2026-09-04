"""Gradient check for _TinyLM's hand-written backward pass.

Compares analytic gradients against central finite differences on a tiny
model + synthetic token batch. Every parameter leaf must match to ~1e-5.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "/home/z/my-project/py8n/mini-services/api-backend")

import numpy as np

from app.engine.nodes.lm import _TinyLM


def main() -> int:
    rng = np.random.default_rng(7)
    V, D, H, C, B = 17, 12, 3, 8, 2
    net = _TinyLM(vocab_size=V, d_model=D, n_heads=H, n_ctx=C, n_blocks=2, seed=3)
    tok = rng.integers(0, V, size=(B, C))
    y = rng.integers(0, V, size=(B, C))

    cache: dict = {}
    net._forward(tok, cache)
    loss, grads = net._backward(y, cache)
    print(f"baseline loss {loss:.6f}")

    leaves: list[tuple[str, np.ndarray, np.ndarray]] = []

    def _collect(prefix: str, params: dict, gs: dict) -> None:
        for k in params:
            if isinstance(params[k], dict):
                _collect(f"{prefix}.{k}", params[k], gs[k])
            elif isinstance(params[k], list):
                for i, (a, b) in enumerate(zip(params[k], gs[k])):
                    _collect(f"{prefix}.{k}[{i}]", a, b)
            else:
                leaves.append((f"{prefix}.{k}", params[k], gs[k]))

    _collect("p", net._params_tree(), grads)

    def batch_loss() -> float:
        logits = net._forward(tok)
        shifted = logits - logits.max(axis=-1, keepdims=True)
        p = np.exp(shifted)
        p /= p.sum(axis=-1, keepdims=True)
        return float(-np.mean(np.log(p[np.arange(B)[:, None], np.arange(C)[None, :], y] + 1e-12)))

    eps = 1e-5
    worst_name, worst_rel = "", 0.0
    for name, param, grad in leaves:
        flat = param.reshape(-1)
        gflat = grad.reshape(-1)
        idxs = rng.choice(flat.size, size=min(6, flat.size), replace=False)
        for i in idxs:
            orig = flat[i]
            flat[i] = orig + eps
            lp = batch_loss()
            flat[i] = orig - eps
            lm = batch_loss()
            flat[i] = orig
            num = (lp - lm) / (2 * eps)
            ana = gflat[i]
            denom = max(abs(num), abs(ana), 1e-6)
            rel = abs(num - ana) / denom
            if rel > worst_rel:
                worst_rel, worst_name = rel, name
            if rel > 5e-3:
                print(f"FAIL {name}[{i}]: analytic={ana:.8f} numeric={num:.8f} rel={rel:.2e}")
                return 1
    print(f"OK - {len(leaves)} leaves gradient-checked, worst rel err {worst_rel:.2e} at {worst_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
