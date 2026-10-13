"""The LIVE book model: the ridge of bs_lib fitted on every archived BTC window (the study's TRAIN fit is
data/replay/book_signal/book_model_train.json). Written to data/replay/paired_exit/live/book_btc.json, which the
triangle engine reads when TPB_TRIANGLE_PX_SIGNAL_SOURCE is book / both / either.

    nice -n 19 .venv/bin/python scripts/book_signal/fit_live_book.py
"""
import json
import sys
import time

import numpy as np

sys.path.insert(0, "/root/troll-poly-bot/scripts/book_signal")
import bs_lib as B   # noqa: E402

wins = B.load_btc()
X, y = [], []
for w in wins:
    bf = B.book_features(w, w.F["mid"])
    yy = B.future_move(w, w.F["mid"])
    idx = np.flatnonzero(~np.isnan(yy) & (w.left >= 5) & (w.left <= 295) & ~np.isnan(bf["vel5"]))
    X.append(B.design(bf, idx)); y.append(yy[idx])
X, y = np.vstack(X), np.concatenate(y)
model = B.fit_ridge(X, y)
pred = B.predict(model, X)
model.update({"asset": "BTC", "train_windows": len(wins), "fitted_to": max(w.close for w in wins),
              "fitted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "in_sample_corr": float(np.corrcoef(pred, y)[0, 1])})
out = f"{B.L.ROOT}/data/replay/paired_exit/live/book_btc.json"
json.dump(model, open(out, "w"), indent=1)
print(f"{out}: {len(wins)} windows, {len(y)} samples, in-sample corr {model['in_sample_corr']:+.3f}")
