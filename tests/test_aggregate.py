"""Aggregate (annual) basis: exact checks and Monte Carlo over simulated years."""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_os.chdir(_os.path.dirname(_os.path.abspath(__file__)))

import numpy as np, polars as pl, warnings
from scipy import stats
from scipy.optimize import brentq
from catalloc.distortion import beta_params, g_over_u, layer_event_weights, allocate_to_policies
warnings.filterwarnings("ignore")

src = open("test_distortion.py").read()
exec(src[:src.index("# ---- 1. identity")])            # synthetic events + policies, layers

print("1. identity, unlimited aggregate: annual key = occurrence expected loss")
for name, (att, lim) in layers.items():
    ew_o, _ = layer_event_weights(events, att, lim, distortion="identity", drop_rel=0)
    ew_a, ia = layer_event_weights(events, att, lim, distortion="identity", drop_rel=0,
                                   basis="aggregate", agg_limit=np.inf)
    j = ew_o.join(ew_a, on="EVENTID", suffix="_A")
    rel = (j["ALLOC_A"] / j["ALLOC"] - 1).abs().max()
    print(f"   {name:<18} max |annual / occurrence - 1| per event {rel:.1e};  "
          f"h range {ia['h'].min():.6f}..{ia['h'].max():.6f}")

print("\n2. event weights tie to the annual distorted value")
prem = {"working 5m xs 2m": 1.5e6, "middle 10m xs 5m": 1.5e6, "upper 20m xs 15m": 1.0e6}
res = {}
for name, (att, lim) in layers.items():
    for dist in ["wang", "ph"]:
        ew, info = layer_event_weights(events, att, lim, premium=prem[name], distortion=dist,
                                       basis="aggregate", reinstatements=1, drop_rel=0)
        res[(name, dist)] = (ew, info)
        print(f"   {name:<18} {dist:<5} theta {info['theta']:7.4f}  target {info['target_100pct']:>11,.0f} "
              f"(incl. exp. RP {info['expected_reinstatement_premium_100pct']:>9,.0f})  "
              f"sum events {info['distorted_total']:>11,.0f}  annual value {info['annual_value']:>11,.0f}  "
              f"E[R] {info['expected_loss']:>10,.0f}  tail {info['grid_tail_mass']:.0e}")

print("\n3. Monte Carlo (1M years), 1 reinstatement: empirical AEP, theta and event weights")
ev = events.filter(pl.col("PERSPVALUE") > 0)
lam = ev["RATE"].to_numpy(); mu = ev["PERSPVALUE"].to_numpy(); E = ev["EXPVALUE"].to_numpy()
a, b = beta_params(mu, (ev["STDDEVI"] + ev["STDDEVC"]).to_numpy(), E)
Y_N = 1_000_000
n = rng.poisson(lam.sum() * Y_N)
year = rng.integers(0, Y_N, n)
eid = rng.choice(len(lam), n, p=lam / lam.sum())
S = stats.beta.rvs(a[eid], b[eid], random_state=rng) * E[eid]
for name in ["working 5m xs 2m", "upper 20m xs 15m"]:
    att, lim = layers[name]
    L = np.clip(S - att, 0, lim)
    Y = np.bincount(year, weights=L, minlength=Y_N)
    AL = 2 * lim
    R = np.minimum(Y, AL)
    for dist in ["wang", "ph"]:
        ew, info = res[(name, dist)]
        target = info["target_100pct"]
        Rs = np.sort(R)
        r = (np.arange(4000) + 0.5) * AL / 4000
        aep = (len(Rs) - np.searchsorted(Rs, r, side="right")) / Y_N
        tot = lambda t: float((np.where(aep > 0, g_over_u(aep, dist, t), 0) * aep).sum() * AL / 4000)
        lo, hi = (-5, 5) if dist == "wang" else (0.05, 50)
        th = brentq(lambda t: tot(t) - target, lo, hi)
        hR = np.where(aep > 0, g_over_u(aep, dist, th), 0)
        psiR = np.interp(R, np.concatenate([[0], r + AL / 8000]), np.concatenate([[0], np.cumsum(hR) * AL / 4000]))
        share = np.where(Y[year] > 0, L / np.where(Y[year] > 0, Y[year], 1), 0) * psiR[year]
        mc = np.bincount(eid, weights=share, minlength=len(lam)) / Y_N
        an = (ev.select("EVENTID").join(ew.select("EVENTID", "ALLOC"), on="EVENTID", how="left")
              .fill_null(0)["ALLOC"].to_numpy())
        top = np.argsort(-an)[:5]
        print(f"   {name:<18} {dist:<5} theta analytic {info['theta']:.4f} vs MC {th:.4f};  "
              f"MC total {mc.sum():>11,.0f} vs {an.sum():>11,.0f};  top-5 events MC/analytic - 1: "
              + ", ".join(f"{mc[t] / an[t] - 1:+.1%}" for t in top))

print("\n4. occurrence vs aggregate key, upper layer, share from the 20 largest events")
for basis in ["occurrence", "aggregate"]:
    ew, _ = layer_event_weights(events, *layers["upper 20m xs 15m"], premium=1.0e6, basis=basis,
                                reinstatements=1)
    print(f"   {basis:<10} {ew.sort('ALLOC', descending=True)['ALLOC'][:20].sum() / ew['ALLOC'].sum():.1%}")

pe = allocate_to_policies(policies, res[("middle 10m xs 5m", "wang")][0], "conditional", bounded=True)
print(f"\npolicy split on aggregate weights: negatives {int((pe['ALLOC'] < 0).sum())}, "
      f"total {pe['ALLOC'].sum():,.0f} vs {res[('middle 10m xs 5m', 'wang')][0]['ALLOC'].sum():,.0f}")

print("\n5. aggregate binding: working layer, frequent events (rates x 8), 0 reinstatements")
ev8 = events.with_columns(pl.col("RATE") * 8)
att, lim = 2e6, 5e6
lam8 = lam * 8
n = rng.poisson(lam8.sum() * Y_N)
year = rng.integers(0, Y_N, n)
eid = rng.choice(len(lam8), n, p=lam8 / lam8.sum())
S = stats.beta.rvs(a[eid], b[eid], random_state=rng) * E[eid]
L = np.clip(S - att, 0, lim)
Y = np.bincount(year, weights=L, minlength=Y_N)
for reinst in [0, 1, 3]:
    AL = lim * (1 + reinst)
    ew, info = layer_event_weights(ev8, att, lim, premium=4.0e6, basis="aggregate",
                                   reinstatements=reinst, drop_rel=0)
    R = np.minimum(Y, AL)
    target = info["target_100pct"]
    r = (np.arange(4000) + 0.5) * AL / 4000
    Rs = np.sort(R)
    aep = (len(Rs) - np.searchsorted(Rs, r, side="right")) / Y_N
    tot = lambda t: float((np.where(aep > 0, g_over_u(aep, "wang", t), 0) * aep).sum() * AL / 4000)
    th = brentq(lambda t: tot(t) - target, -5, 5)
    hR = np.where(aep > 0, g_over_u(aep, "wang", th), 0)
    psiR = np.interp(R, np.concatenate([[0], r + AL / 8000]), np.concatenate([[0], np.cumsum(hR) * AL / 4000]))
    share = np.where(Y[year] > 0, L / np.where(Y[year] > 0, Y[year], 1), 0) * psiR[year]
    mc = np.bincount(eid, weights=share, minlength=len(lam8)) / Y_N
    an = (ev.select("EVENTID").join(ew.select("EVENTID", "ALLOC"), on="EVENTID", how="left")
          .fill_null(0)["ALLOC"].to_numpy())
    ew_o, _ = layer_event_weights(ev8, att, lim, premium=target, drop_rel=0)
    occ = (ev.select("EVENTID").join(ew_o.select("EVENTID", "ALLOC"), on="EVENTID", how="left")
           .fill_null(0)["ALLOC"].to_numpy())
    big = np.argsort(-mu)[:10]                       # 10 most severe events
    print(f"   reinstatements {reinst}: P(agg exhausted) {info['P_exhaust']:.1%} (MC {(Y >= AL).mean():.1%});  "
          f"theta {info['theta']:.4f} vs MC {th:.4f};  sum |MC - analytic| / total {np.abs(mc - an).sum() / an.sum():.2%};  "
          f"10 most severe events' share: occurrence key {occ[big].sum() / occ.sum():.1%}, aggregate key {an[big].sum() / an.sum():.1%}")
