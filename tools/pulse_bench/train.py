"""the kick model: logistic regression over beat_map._kick_rows, fit on
candidates labelled by drum truth (the stem reference for real songs, exact
truth for rendered ones). each song weighs the same however long it is.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import minimize

import common
from tide import beat_map as bm
from tide import kick_model

L2 = 10.0
LABEL_TOL = 0.035
MODEL_PATH = common.ROOT / "src" / "tide" / "kick_model.py"


def candidates(pcm):
    """tide's own candidates and model rows for a song, and their times."""
    an = bm.Analysis()
    an.push(pcm)
    wide, mel = an._wide_now()
    cand, _kick, _ref, _local, rows, _ring = bm._kick_candidates(wide, mel, an.frames)
    times = (cand * bm.HOP + bm.N_FFT / 2.0) / bm.SR + bm.ONSET_LEAD_S
    return times, rows


def labels(times, truth):
    """1 on a kick, 0 elsewhere; a ghost kick (soft) teaches nothing."""
    pairs = sorted((float(t), float(v)) for t, v in truth["kicks"])
    kt = np.array([t for t, _v in pairs])
    kv = np.array([v for _t, v in pairs])
    med = np.median(kv) if kv.size else 1.0
    y = np.zeros(times.size)
    w = np.ones(times.size)
    if kt.size:
        for j, t in enumerate(times):
            k = int(np.argmin(np.abs(kt - t)))
            if abs(kt[k] - t) <= LABEL_TOL:
                if kv[k] >= 0.55 * med:
                    y[j] = 1
                else:
                    w[j] = 0
    return y, w


def fit(items, l2=L2):
    """items: (rows, labels, weights) per song. returns weights and bias
    with the standardization folded in."""
    X = np.vstack([r for r, _y, _w in items])
    y = np.concatenate([yy for _r, yy, _w in items])
    w = np.concatenate([ww / max(1.0, ww.sum()) * 1000.0 for _r, _y, ww in items])
    mu = X.mean(0)
    sd = X.std(0) + 1e-6
    Z = np.hstack(((X - mu) / sd, np.ones((X.shape[0], 1))))

    def loss(beta):
        p = 1.0 / (1.0 + np.exp(-(Z @ beta)))
        ll = -(w * (y * np.log(p + 1e-12) + (1 - y) * np.log(1 - p + 1e-12))).sum()
        g = Z.T @ (w * (p - y))
        g[:-1] += 2 * l2 * beta[:-1]
        return ll + l2 * (beta[:-1] ** 2).sum(), g
    beta = minimize(loss, np.zeros(Z.shape[1]), jac=True, method="L-BFGS-B",
                    options=dict(maxiter=2000)).x
    weights = beta[:-1] / sd
    return weights, float(beta[-1] - (mu * beta[:-1] / sd).sum())


def use(weights, bias):
    """swap the running analyzer's model (for cross-fitting)."""
    bm._KICK_WEIGHTS = np.asarray(weights, dtype=float)
    kick_model.BIAS = bias


def module_text(weights, bias):
    rows = ["    " + " ".join(f"{w: .6e}," for w in weights[k:k + 6]) for k in range(0, len(weights), 6)]
    return ('"""the kick model: logistic regression weights over beat_map._kick_rows,\n'
            'standardization folded in. written by tools/pulse_bench/train.py; do\n'
            'not edit by hand."""\n'
            "WEIGHTS = (\n" + "\n".join(rows) + "\n)\n"
            f"BIAS = {bias:.6e}\n")


def write(weights, bias):
    tmp = MODEL_PATH.with_suffix(".py.new")
    tmp.write_text(module_text(weights, bias))
    tmp.replace(MODEL_PATH)
