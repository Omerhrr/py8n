"""Standalone sanity for v63 nodes: MLP convergence + featurizers."""
import base64
import io
import sys

import numpy as np

sys.path.insert(0, "/home/z/my-project/py8n/mini-services/api-backend")

from app.engine.nodes.modal import _MLP  # noqa: E402

# --- 1) MLP learns a nonlinear classification from scratch -----------------
rng = np.random.default_rng(0)
X = rng.normal(size=(400, 4))
y = ((2.0 * X[:, 0] - X[:, 1] + 0.5 * X[:, 2] ** 2) > 0).astype(int)
net = _MLP([4, 24, 12, 2], activation="relu", seed=42)
hist = net.fit(X[:300], y[:300], task="classification", epochs=120, batch_size=16,
               lr=0.02, optimizer="adam", l2=1e-4, val_data=(X[300:], y[300:]), patience=30)
pred = net.predict(X[300:]).argmax(axis=1)
acc = float((pred == y[300:]).mean())
print(f"[1] MLP classification: val acc={acc:.3f} epochs={hist['epochs_run']} "
      f"final_loss={hist['train_loss'][-1]:.4f} params={sum(W.size for W in net.weights)}")
assert acc > 0.9, acc

# regression
yr = X[:, 0] * 3.0 + 1.0
net2 = _MLP([4, 16, 1], seed=7)
h2 = net2.fit(X[:150], yr[:150], task="regression", epochs=80, batch_size=16, lr=0.02, optimizer="adam")
r2 = 1 - np.mean((net2.predict(X[150:]) - yr[150:]) ** 2) / np.var(yr[150:])
print(f"[2] MLP regression: r2={r2:.3f}")
assert r2 > 0.95, r2

# fine-tune: continue from net2's weights on shifted data
yshift = yr + 5.0
net3 = _MLP.from_state(net2.state())
h3 = net3.fit(X[:150], yshift[:150], task="regression", epochs=60, batch_size=16, lr=0.01, optimizer="adam")
r3 = 1 - np.mean((net3.predict(X[150:]) - yshift[150:]) ** 2) / np.var(yshift[150:])
print(f"[3] fine-tune continues from base weights: r2={r3:.3f} epochs={h3['epochs_run']}")
assert r3 > 0.9, r3

# --- 2) image features stateless ---------------------------------------
from PIL import Image  # noqa: E402
from app.engine.schema import NodeSpec  # noqa: E402


def img_bytes(color):
    img = Image.new("RGB", (64, 48), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


b1, b2 = img_bytes((255, 0, 0)), img_bytes((0, 0, 255))
from app.engine.nodes.modal import ImageFeaturesNode  # noqa: E402

node = ImageFeaturesNode(NodeSpec(id="n1", type="image_features",
                                  parameters={"image_field": "img", "size": 32, "prefix": "img"}))
node.params = node.ParamsModel(image_field="img", size=32, prefix="img")


class Ctx:
    current_input = {"items": [{"img": b1}, {"img": b2}]}
    workflow_id = None
    execution_id = None
    owner_id = None


import asyncio  # noqa: E402

res = asyncio.run(node.execute(Ctx()))
payload = res.raw_output
r0, r1 = payload["items"][0], payload["items"][1]
assert r0["img_r_mean"] > 0.9 and r1["img_b_mean"] > 0.9
res2 = asyncio.run(node.execute(Ctx()))
assert res2.raw_output["items"] == payload["items"], "image features must be stateless"
print(f"[4] image features: r_mean={r0['img_r_mean']} vs b_mean={r1['img_b_mean']}, stateless OK, dims={payload['dims']}")

# --- 3) audio features --------------------------------------------------
import wave  # noqa: E402

t = np.linspace(0, 0.5, int(8000 * 0.5), endpoint=False)
sine = (np.sin(2 * np.pi * 440 * t) * 20000).astype(np.int16)
buf = io.BytesIO()
with wave.open(buf, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(8000)
    w.writeframes(sine.tobytes())
aud_b64 = base64.b64encode(buf.getvalue()).decode()

from app.engine.nodes.modal import AudioFeaturesNode  # noqa: E402

anode = AudioFeaturesNode(NodeSpec(id="n2", type="audio_features", parameters={"audio_field": "aud", "prefix": "aud"}))
anode.params = anode.ParamsModel(audio_field="aud", prefix="aud")
actx = Ctx()
actx.current_input = {"items": [{"aud": aud_b64}]}
ares = asyncio.run(anode.execute(actx))
feat = ares.raw_output["items"][0]
assert 0.45 < feat["aud_duration_s"] < 0.55 and feat["aud_rms"] > 0.3
print(f"[5] audio features: duration={feat['aud_duration_s']}s rms={feat['aud_rms']} centroid={feat['aud_centroid']}Hz")

# --- 4) text featurizer fit/transform parity (no DB; artifact IO is
# exercised by the API tests) --------------------------------------------
from sklearn.decomposition import TruncatedSVD  # noqa: E402
from sklearn.feature_extraction.text import TfidfVectorizer  # noqa: E402

texts = ["the quick brown fox jumps", "lazy dogs sleep deeply",
         "the fox is quick and brown", "payment failed for customer",
         "refund requested immediately", "customer asked for refund"]
tfidf = TfidfVectorizer(max_features=100, stop_words="english")
Xt = tfidf.fit_transform(texts)
svd = TruncatedSVD(n_components=4, random_state=42)
Z = svd.fit_transform(Xt)
Zt = svd.transform(tfidf.transform(texts))
assert np.allclose(Z, Zt, atol=1e-10), "transform must reproduce fit embeddings"
print(f"[6] text featurizer: vocab={len(tfidf.vocabulary_)} dims={Z.shape[1]} fit/transform parity OK")

print("ALL v63 SANITY CHECKS GREEN")
