"""The live bot's fair models: refit on EVERY archived window of the asset (what the walk-forward validates:
"fit on everything before the day you trade"), with the entry-cost components measured the same way.

    nice -n 19 python scripts/paired_exit/fit_live.py BTC   -> data/replay/paired_exit/live/fair_btc.json
"""
import json, sys, time
import numpy as np
sys.path.insert(0, "/root/troll-poly-bot/scripts/paired_exit")
import pe_lib as L, walkforward as WF

for asset in (sys.argv[1:] or ["BTC"]):
    wins = L.prepare(L.load_asset(asset))
    model = WF.fit(wins)
    for w in wins:
        w.fair_up = L.predict(model, L.design(w.F))
    ex = L.Exec()
    sl, ff, wf, fa, wa = [], [], [], [], []
    for w in wins:
        for i, d in WF.S.candidates(w, 2.0, 60, 290):
            side = "UP" if d > 0 else "DOWN"
            ask = (w.ua if d > 0 else w.da)[i]
            if not (0.15 <= ask <= 0.85):
                continue
            fair = w.fair_up[i] if d > 0 else 1 - w.fair_up[i]
            won = 1.0 if w.outcome == side else 0.0
            fa.append(fair); wa.append(won)
            px, _ = ex.taker_buy(w, side, w.T[i], round(ask + 0.02, 2))
            if px is not None:
                sl.append(px - ask); ff.append(fair); wf.append(won)
    adv = (np.mean(ff) - np.mean(wf)) - (np.mean(fa) - np.mean(wa))
    model.update({"asset": asset, "windows": len(wins), "fitted_to": time.strftime("%Y-%m-%d %H:%M", time.gmtime(wins[-1].close / 1000)),
                  "fitted_from": time.strftime("%Y-%m-%d %H:%M", time.gmtime(wins[0].open / 1000)),
                  "cost_slippage": round(float(np.mean(sl)), 4), "cost_adverse": round(float(max(adv, 0.0)), 4),
                  "fill_rate": round(len(sl) / max(len(fa), 1), 3)})
    json.dump(model, open(f"{L.CACHE}/live/fair_{asset.lower()}.json", "w"), indent=1)
    print(asset, model["windows"], model["fitted_from"], "->", model["fitted_to"], "slippage", model["cost_slippage"],
          "adverse", model["cost_adverse"], "fill", model["fill_rate"])
