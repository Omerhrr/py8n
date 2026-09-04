"""V66 correctness gate: conditioned-LM gradients vs finite differences,
and numpy <-> torch forward parity on a shared state.

Run: /home/z/.venv/bin/python scripts/gradient_check_v66.py
"""
import sys

sys.path.insert(0, "mini-services/api-backend")

import numpy as np

from app.engine.nodes.lm import _TinyLM
from app.engine.torch_backend import _TorchLM


def rel_err(a: float, b: float) -> float:
    denom = max(abs(a), abs(b), 1e-12)
    return abs(a - b) / denom


def numeric_loss(net: _TinyLM, x, y, cond, leaf, pert) -> float:
    orig = leaf.copy()  # a REAL copy - a[...] is a view, not a snapshot
    leaf[...] = orig + pert
    cache = {}
    net._forward(x, cache, cond=cond)
    logits = cache["logits"]
    T_full = logits.shape[1]
    shifted = logits - logits.max(axis=-1, keepdims=True)
    p = np.exp(shifted)
    p /= p.sum(axis=-1, keepdims=True)
    rows = np.arange(x.shape[0])[:, None]
    pos = np.arange(1, T_full)[None, :]
    loss = float(-np.mean(np.log(p[rows, pos, y] + 1e-12)))
    leaf[...] = orig
    return loss


def main() -> int:
    rng = np.random.default_rng(11)
    V, B, T, d, cond_dim = 23, 3, 8, 12, 3
    net = _TinyLM(vocab_size=V, d_model=d, n_heads=3, n_ctx=T, n_blocks=2, seed=3, cond_dim=cond_dim)
    x = rng.integers(2, V, size=(B, T))
    y = rng.integers(2, V, size=(B, T))
    cond = rng.normal(size=(B, cond_dim))

    cache = {}
    net._forward(x, cache, cond=cond)
    loss, grads = net._backward(y, cache)

    leaves = [("tok_emb", net.tok_emb, grads["tok_emb"]),
              ("pos_emb", net.pos_emb, grads["pos_emb"]),
              ("lnf_g", net.lnf_g, grads["lnf_g"]),
              ("lnf_b", net.lnf_b, grads["lnf_b"]),
              ("Wh", net.Wh, grads["Wh"]),
              ("bh", net.bh, grads["bh"]),
              ("W_cond", net.W_cond, grads["W_cond"])]
    for bi, blk in enumerate(net.blocks):
        for k, v in blk.items():
            leaves.append((f"block{bi}.{k}", v, grads["blocks"][bi][k]))

    worst = 0.0
    checked = 0
    # float64 roundoff dominates below eps~1e-5 for O(1) losses (measured:
    # worst err 1.8e-05 at eps=1e-5 vs 1e-03 at eps=1e-6) - real bugs show
    # up at O(0.1..1), so eps=1e-5 with a 1e-4 gate separates them cleanly
    eps = 1e-5
    for name, leaf, grad in leaves:
        flat_view = leaf.reshape(-1)
        for idx in rng.choice(flat_view.size, size=min(2, flat_view.size), replace=False):
            pert = np.zeros(flat_view.size)
            pert[idx] = eps
            lp = numeric_loss(net, x, y, cond, flat_view, pert)
            lm = numeric_loss(net, x, y, cond, flat_view, -pert)
            num = (lp - lm) / (2 * eps)
            ana = grad.reshape(-1)[idx]
            err = rel_err(float(num), float(ana))
            worst = max(worst, err)
            checked += 1
    print(f"gradient check (conditioned, {checked} leaves sampled): max rel err = {worst:.2e}")
    assert worst < 1e-4, f"gradient check FAILED: {worst}"

    # ---- numpy <-> torch parity on the same state ------------------------
    tnet = _TorchLM.from_state(net.state(), device="cpu")
    # build identical windows from real ids
    windows = [[int(v) for v in w] for w in
               np.concatenate([x, y[:, -1:]], axis=1)]  # (T+1)-token windows
    l_np = net.eval_loss(windows, cond.tolist()[: len(windows)] if net.cond_dim else None)
    l_t = tnet.eval_loss(windows, cond.tolist()[: len(windows)] if net.cond_dim else None)
    print(f"eval_loss parity: numpy={l_np:.10f} torch={l_t:.10f}")
    assert abs(l_np - l_t) < 1e-9, "numpy/torch eval_loss mismatch"

    g_np = net.generate([int(x[0, 0])], 5, temperature=0.9, top_k=8, seed=9,
                        cond=cond[0].tolist() if net.cond_dim else None)
    g_t = tnet.generate([int(x[0, 0])], 5, temperature=0.9, top_k=8, seed=9,
                        cond=cond[0].tolist() if net.cond_dim else None)
    print(f"generate parity: numpy={g_np} torch={g_t}")
    assert g_np == g_t, "numpy/torch sampling diverged on the same state"

    print("GRADIENT CHECK v66 GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
