"""Bounded conditional split: comonotone RMS-style event (beta marginals, CVs 0.3-6),
exact E[X_i psi(S)/S] by simulation vs mean, linear, bounded and clamped splits."""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_os.chdir(_os.path.dirname(_os.path.abspath(__file__)))

import numpy as np, polars as pl
from scipy import stats
from catalloc.distortion import allocate_to_policies, beta_params

rng = np.random.default_rng(3)
n_pol, n = 200, 400_000
U = rng.random(n)
m = rng.lognormal(0, 1, n_pol); m *= 100e6 / m.sum()
cv = np.where(rng.random(n_pol) < 0.1, rng.uniform(2, 6, n_pol), rng.uniform(0.3, 0.8, n_pol))
Ei = m * np.maximum(10, 2 * cv ** 2 + 2)
a, b = beta_params(m, cv * m, Ei)
X = stats.beta.ppf(U[:, None], a[None, :], b[None, :]) * Ei[None, :]
S = X.sum(1); mu = X.mean(0)
pols = pl.DataFrame({"EVENTID": 0, "POLICYID": np.arange(n_pol), "PERSPVALUE": mu,
                     "STDDEVC": X.std(0), "STDDEVI": 0.0})
for att, lim in [(20e6, 30e6), (50e6, 50e6), (80e6, 100e6), (150e6, 150e6)]:
    psi = np.clip(S - att, 0, lim)
    truth = (X * (psi / S)[:, None]).mean(0)
    ew = pl.DataFrame({"EVENTID": [0], "RATE": [1.0], "A": [(psi / S).mean()], "B": [psi.mean()]})
    out = {}
    for name, kw in [("mean", dict(method="mean")), ("linear", dict(method="conditional")),
                     ("bounded", dict(method="conditional", bounded=True)),
                     ("clamped", dict(method="conditional", clamp_negative=True))]:
        out[name] = allocate_to_policies(pols, ew, **kw).sort("POLICYID")["ALLOC"].to_numpy()
    lin = out["linear"]
    print(f"layer {lim/1e6:>3.0f}m xs {att/1e6:<3.0f}m P(exhaust) {(S > att + lim).mean():4.0%} "
          f"linear negatives {int((lin < 0).sum()):>2} ({-lin[lin < 0].sum() / lin.sum():.1%}) | "
          + "  ".join(f"{k}: err {np.abs(v - truth).sum() / truth.sum():5.1%}" for k, v in out.items())
          + f" | bounded negatives {int((out['bounded'] < -1e-9).sum())}, "
            f"total tie {abs(out['bounded'].sum() / lin.sum() - 1):.0e}")
