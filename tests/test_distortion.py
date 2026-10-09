"""Checks for the distortion-calibrated layer allocation (occurrence basis)."""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_os.chdir(_os.path.dirname(_os.path.abspath(__file__)))

import numpy as np
import polars as pl
from scipy import stats

from catalloc.inuring import subject_integrals
from catalloc.distortion import (allocate_to_policies, beta_params, g_over_u, layer_event_weights,
                        premium_by_policy)

rng = np.random.default_rng(7)

# ---- synthetic policy-level ELT --------------------------------------------
n_ev, n_pol = 400, 30
rate = rng.gamma(0.5, 0.004, n_ev)
pol_rows = []
for e in range(n_ev):
    hit = rng.choice(n_pol, rng.integers(3, n_pol), replace=False)
    sev = rng.lognormal(14, 1.4)
    w = rng.dirichlet(np.ones(len(hit)))
    for p, wi in zip(hit, w):
        m = sev * wi
        pol_rows.append((e, p, m, m * rng.uniform(0.3, 0.9), m * rng.uniform(0.2, 0.6)))
policies = pl.DataFrame(
    pol_rows, schema=["EVENTID", "POLICYID", "PERSPVALUE", "STDDEVI", "STDDEVC"], orient="row")
events = (
    policies.group_by("EVENTID").agg(
        pl.col("PERSPVALUE").sum(),
        (pl.col("STDDEVI") ** 2).sum().sqrt().alias("STDDEVI"),
        pl.col("STDDEVC").sum())
    .with_columns((pl.col("PERSPVALUE") * 8).alias("EXPVALUE"))
    .join(pl.DataFrame({"EVENTID": np.arange(n_ev), "RATE": rate}), on="EVENTID")
    .sort("EVENTID"))

layers = {"working 5m xs 2m": (2e6, 5e6), "middle 10m xs 5m": (5e6, 10e6),
          "upper 20m xs 15m": (15e6, 20e6)}

# ---- 1. identity reproduces expected loss ----------------------------------
print("1. g = identity: event weights vs exact expected layer loss")
for name, (att, lim) in layers.items():
    ew, info = layer_event_weights(events, att, lim, distortion="identity", drop_rel=0)
    ev = events.join(ew.select("EVENTID", "A", "B"), on="EVENTID").sort("EVENTID")
    it = subject_integrals(ev["PERSPVALUE"], ev["STDDEVI"] + ev["STDDEVC"], ev["EXPVALUE"],
                           [(att, lim, 1.0)])
    el_exact = ev["PERSPVALUE"].to_numpy() - it["EN"]          # E[L] = E[S] - E[N]
    ratio_exact = 1.0 - it["EG"]                               # E[L/S] = 1 - E[N/S]
    rB = ev["B"].to_numpy() / np.where(el_exact > 0, el_exact, 1)
    rA = ev["A"].to_numpy() / np.where(ratio_exact > 0, ratio_exact, 1)
    sig = el_exact > 1e-6 * el_exact.max()
    aal = (ev["RATE"].to_numpy() * el_exact).sum()
    print(f"   {name:<18} AAL exact {aal:>11,.0f}  grid {info['expected_loss']:>11,.0f}   "
          f"max rel err B {np.abs(rB[sig] - 1).max():.1e}  A {np.abs(rA[sig] - 1).max():.1e}")

# policy level: identity + mean split = event EL split by policy mean share
att, lim = layers["middle 10m xs 5m"]
ew_id, _ = layer_event_weights(events, att, lim, distortion="identity", drop_rel=0)
pe = allocate_to_policies(policies, ew_id, "mean")
chk = (pe.join(ew_id.select("EVENTID", pl.col("ALLOC").alias("EV")), on="EVENTID")
       .with_columns((pl.col("EV") * pl.col("PERSPVALUE")
                      / pl.col("PERSPVALUE").sum().over("EVENTID")).alias("EXPECT")))
print(f"   policy EL split (mean): max abs diff {(chk['ALLOC'] - chk['EXPECT']).abs().max():.1e}")

# ---- 2. calibration ---------------------------------------------------------
print("\n2. Calibration to the 100% premium")
premiums = {"working 5m xs 2m": 1.5e6, "middle 10m xs 5m": 1.5e6, "upper 20m xs 15m": 1.0e6}
results = {}
for name, (att, lim) in layers.items():
    for dist in ["wang", "ph"]:
        ew, info = layer_event_weights(events, att, lim, premium=premiums[name],
                                       distortion=dist)
        results[(name, dist)] = (ew, info)
        print(f"   {name:<18} {dist:<5} theta {info['theta']:>7.4f}  target "
              f"{info['target_100pct']:>11,.0f}  sum of event weights "
              f"{info['distorted_total']:>11,.0f}  EL {info['expected_loss']:>10,.0f}  "
              f"load {info['load_ratio']:.2f}x  P(att) {info['P_attach']:.3f} "
              f"P(exh) {info['P_exhaust']:.3f}")


# ---- 3. Monte Carlo with an empirical OEP ----------------------------------
def simulate(att, lim, n_years=2_000_000):
    ev = events.filter(pl.col("PERSPVALUE") > 0)
    lam = ev["RATE"].to_numpy()
    mu = ev["PERSPVALUE"].to_numpy()
    E = ev["EXPVALUE"].to_numpy()
    a, b = beta_params(mu, (ev["STDDEVI"] + ev["STDDEVC"]).to_numpy(), E)
    n = rng.poisson(lam.sum() * n_years)
    year = rng.integers(0, n_years, n)
    eid = rng.choice(len(lam), n, p=lam / lam.sum())
    S = stats.beta.rvs(a[eid], b[eid], random_state=rng) * E[eid]
    L = np.clip(S - att, 0, lim)
    M = np.zeros(n_years)
    np.maximum.at(M, year, L)
    return ev["EVENTID"].to_numpy(), eid, S, L, M, n_years


print("\n3. Monte Carlo (2M years): empirical OEP and Lambda, calibrated independently")
for name in ["working 5m xs 2m", "upper 20m xs 15m"]:
    att, lim = layers[name]
    ids, eid, S, L, M, Y = simulate(att, lim)
    for dist in ["wang", "ph"]:
        ew, info = results[(name, dist)]
        x = info["oep_x"]
        dx = x[1] - x[0]
        Ls, Ms = np.sort(L), np.sort(M)
        lam_emp = (len(Ls) - np.searchsorted(Ls, x, side="right")) / Y
        oep_emp = (len(Ms) - np.searchsorted(Ms, x, side="right")) / Y
        target = info["target_100pct"]

        def tot(t):
            return float((np.where(oep_emp > 0, g_over_u(oep_emp, dist, t), 0) * lam_emp).sum() * dx)
        from scipy.optimize import brentq
        lo, hi = (-5, 5) if dist == "wang" else (0.05, 50)
        th = brentq(lambda t: tot(t) - target, lo, hi)
        h_emp = np.where(oep_emp > 0, g_over_u(oep_emp, dist, th), 0.0)
        H = np.concatenate([[0.0], np.cumsum(h_emp) * dx])
        grid = np.concatenate([[0.0], x + dx / 2])
        psi = np.interp(L, grid, H)
        mc = np.bincount(eid, weights=psi, minlength=len(ids)) / Y
        an = (events.select("EVENTID").join(ew.select("EVENTID", "ALLOC"), on="EVENTID",
                                            how="left").fill_null(0)["ALLOC"].to_numpy())
        top = np.argsort(-an)[:4]
        rel = ", ".join(f"{mc[t] / an[t] - 1:+.1%}" for t in top)
        print(f"   {name:<18} {dist:<5} theta analytic {info['theta']:.4f} vs MC {th:.4f};  "
              f"top-4 event weights MC/analytic - 1: {rel}")

    # A_e check: E[psi(S)/S] for the largest events
    ew, info = results[(name, "wang")]
    x = info["oep_x"]; dx = x[1] - x[0]
    H = np.concatenate([[0.0], np.cumsum(info["h"]) * dx])
    grid = np.concatenate([[0.0], x + dx / 2])
    ratio = np.interp(L, grid, H) / S
    mcA = np.bincount(eid, weights=ratio, minlength=len(ids)) / np.maximum(
        np.bincount(eid, minlength=len(ids)), 1)
    ewd = dict(zip(ew["EVENTID"].to_list(), ew["A"].to_list()))
    big = np.argsort(-np.bincount(eid, weights=psi, minlength=len(ids)))[:4]
    print(f"   {'':<18} A_e (wang) MC/analytic - 1 for top events: "
          + ", ".join(f"{mcA[t] / ewd[ids[t]] - 1:+.2%}" for t in big))

# ---- 4. policy split within an event vs simulation -------------------------
print("\n4. Policy split within one event: E[X_i psi(S)/S] formula vs simulation")
att, lim = layers["middle 10m xs 5m"]
ew, info = results[("middle 10m xs 5m", "wang")]
e_big = ew.sort("ALLOC", descending=True)["EVENTID"][0]
pe = allocate_to_policies(policies, ew, "conditional").filter(pl.col("EVENTID") == e_big)
p = policies.filter(pl.col("EVENTID") == e_big).sort("POLICYID")
pe = pe.sort("POLICYID")
mu, sdc, sdi = (p[c].to_numpy() for c in ["PERSPVALUE", "STDDEVC", "STDDEVI"])
MU, SDC, SDI = mu.sum(), sdc.sum(), np.sqrt((sdi ** 2).sum())
Ev = events.filter(pl.col("EVENTID") == e_big)["EXPVALUE"][0]
a, b = beta_params(np.array([MU]), np.array([SDC + SDI]), np.array([Ev]))
n = 2_000_000
S = stats.beta.rvs(a[0], b[0], size=n, random_state=rng) * Ev
x = info["oep_x"]; dx = x[1] - x[0]
H = np.concatenate([[0.0], np.cumsum(info["h"]) * dx])
psi = np.interp(np.clip(S - att, 0, lim), np.concatenate([[0.0], x + dx / 2]), H)
beta = (sdc + sdi ** 2 / SDI) / (SDC + SDI)
eta = rng.normal(0, 1, (n, len(mu))) * sdi * 0.5
eta -= np.outer(eta.sum(1), sdi ** 2 / (sdi ** 2).sum())
X = mu + np.outer(S - MU, beta) + eta
sim = (X * (psi / S)[:, None]).mean(0) * ew.filter(pl.col("EVENTID") == e_big)["RATE"][0]
rel = pe["ALLOC"].to_numpy() / sim - 1
print(f"   event {e_big}, {len(mu)} policies: max |formula/sim - 1| = {np.abs(rel).max():.2%}; "
      f"event total formula {pe['ALLOC'].sum():,.0f} vs {ew.filter(pl.col('EVENTID') == e_big)['ALLOC'][0]:,.0f}")

# ---- 5. how the key shifts with the distortion -----------------------------
print("\n5. Share of the upper layer's allocation from events with P(S > 25m) above 50%")
att, lim = layers["upper 20m xs 15m"]
ev_id, _ = layer_event_weights(events, att, lim, distortion="identity")
evs = events.filter(pl.col("PERSPVALUE") > 0)
a, b = beta_params(evs["PERSPVALUE"].to_numpy(), (evs["STDDEVI"] + evs["STDDEVC"]).to_numpy(),
                   evs["EXPVALUE"].to_numpy())
severe = pl.DataFrame({"EVENTID": evs["EVENTID"],
                       "SEVERE": stats.beta.sf(25e6 / evs["EXPVALUE"].to_numpy(), a, b) > 0.5})
for label, ew in [("identity (EL)", ev_id), ("wang", results[("upper 20m xs 15m", "wang")][0]),
                  ("ph", results[("upper 20m xs 15m", "ph")][0])]:
    j = ew.join(severe, on="EVENTID")
    print(f"   {label:<14} {j.filter(pl.col('SEVERE'))['ALLOC'].sum() / j['ALLOC'].sum():.1%}")

prem = premium_by_policy(allocate_to_policies(policies, results[("upper 20m xs 15m", "wang")][0],
                                              "conditional"), premiums["upper 20m xs 15m"] * 0.6)
print(f"\nAllocated placed premium (60% placement): {prem['PREMIUM'].sum():,.2f} "
      f"(100% premium {premiums['upper 20m xs 15m']:,.0f})")
