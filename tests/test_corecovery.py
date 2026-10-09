"""Checks for co-recovery (conditional) sharing of inuring recoveries."""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_os.chdir(_os.path.dirname(_os.path.abspath(__file__)))

import numpy as np
import polars as pl
from scipy import stats

from catalloc.inuring import (InuringLayer, apply_inuring, apply_inuring_to_policies, apply_tower,
                     make_subject, subject_event_elt, subject_integrals)
from catalloc.distortion import beta_params

rng = np.random.default_rng(21)
layers = [(4e6, 6e6, 0.9), (10e6, 10e6, 0.75)]

# ---- 1. integrals vs Monte Carlo ------------------------------------------
print("1. Integrals E[N], SD[N], E[N/S], Cov(N/S, N): exact vs 4M-draw Monte Carlo")
for mean, sd, E in [(3e6, 4e6, 40e6), (8e6, 6e6, 30e6), (12e6, 5e6, 25e6)]:
    it = subject_integrals([mean], [sd], [E], layers)
    a, b = beta_params(np.array([mean]), np.array([sd]), np.array([E]))
    s = stats.beta.rvs(a[0], b[0], size=4_000_000, random_state=rng) * E
    n = s.copy()
    for att, lim, p in layers:
        n -= p * np.clip(s - att, 0, lim)
    g = n / s
    print(f"   mean {mean/1e6:>4.0f}m:  E[N/S] {it['EG'][0]:.5f} vs {g.mean():.5f}   "
          f"Cov {it['COV_GN'][0]:>11,.0f} vs {np.cov(g, n)[0, 1]:>11,.0f}")

# ---- 2. policy-level formulas vs a joint simulation ------------------------
# Units follow the RMS structure: S ~ scaled beta; X_i = mu_i + beta_i (S - mu_S) + eta_i,
# eta independent of S with sum zero. Then Y_i = X_i * N(S)/S.
print("\n2. Policy net mean and regression on net subject: formula vs simulation")
n_pol = 6
mu = rng.uniform(0.5e6, 3e6, n_pol)
sdc = mu * rng.uniform(0.2, 0.8, n_pol)
sdi = mu * rng.uniform(0.1, 1.2, n_pol)
MU, SDC, SDI = mu.sum(), sdc.sum(), np.sqrt((sdi ** 2).sum())
E = MU * 5
units = pl.DataFrame({"EVENTID": [0] * n_pol, "LOBNAME": ["HO"] * n_pol,
                      "REGION": ["US_FL"] * n_pol, "PERSPVALUE": mu,
                      "STDDEVC": sdc, "STDDEVI": sdi})
fl_layers = [(MU * 0.9, MU * 1.5, 0.9)]
it = subject_integrals([MU], [SDC + SDI], [E], fl_layers)
table = pl.DataFrame({"EVENTID": [0], "MU_S": [MU], "SDC_S": [SDC], "SDI_S": [SDI],
                      "EN": it["EN"], "EG": it["EG"], "K": it["COV_GN"] / it["SDN"] ** 2,
                      "SDN": it["SDN"], "R_SD": it["SDN"] / (SDC + SDI), "R_EXP": it["NE"] / E})
net = apply_tower(units, make_subject([]), table, "conditional")
pr = apply_tower(units, make_subject([]), table, "prorata")

a, b = beta_params(np.array([MU]), np.array([SDC + SDI]), np.array([E]))
nsim = 3_000_000
S = stats.beta.rvs(a[0], b[0], size=nsim, random_state=rng) * E
N = S - fl_layers[0][2] * np.clip(S - fl_layers[0][0], 0, fl_layers[0][1])
beta = (sdc + sdi ** 2 / SDI) / (SDC + SDI)
e = rng.normal(0, 1, (nsim, n_pol)) * sdi * 0.5
eta = e - np.outer(e.sum(1), sdi ** 2 / (sdi ** 2).sum())
X = mu + np.outer(S - MU, beta) + eta
Y = X * (N / S)[:, None]
sim_mean = Y.mean(0)
sim_beta = np.array([np.cov(Y[:, i], N)[0, 1] for i in range(n_pol)]) / N.var()
f_beta = (net["STDDEVC"].to_numpy() + net["STDDEVI"].to_numpy() ** 2
          / np.sqrt((net["STDDEVI"].to_numpy() ** 2).sum())) / it["SDN"][0]
print("   policy   gross mean   net (formula)   net (sim)   prorata net   beta' (formula)  beta' (sim)")
for i in range(n_pol):
    print(f"   {i:>6} {mu[i]:>12,.0f} {net['PERSPVALUE'][i]:>15,.0f} {sim_mean[i]:>11,.0f}"
          f" {pr['PERSPVALUE'][i]:>13,.0f} {f_beta[i]:>16.4f} {sim_beta[i]:>12.4f}")
print(f"   totals: net {net['PERSPVALUE'].sum():,.0f}  E[N] {it['EN'][0]:,.0f}  "
      f"sum STDDEVC {net['STDDEVC'].sum():,.0f} vs {it['SDN'][0] * SDC / (SDC + SDI):,.0f}  "
      f"quad STDDEVI {np.sqrt((net['STDDEVI'] ** 2).sum()):,.0f} vs {it['SDN'][0] * SDI / (SDC + SDI):,.0f}")

# ---- 3. two-stage cell-level ties ------------------------------------------
print("\n3. Cell-level ties through two stages (FHCF on HO FL, then HO tower)")
regions = ["US_FL", "US_GA", "US_SC"]
rows = []
for e_ in range(200):
    for r in regions:
        for lob in ["HO", "CO"]:
            m = rng.lognormal(14.5, 1.2)
            rows.append((e_, lob, r, m, m * rng.uniform(.3, .9), m * rng.uniform(.2, .6),
                         m * 8, 0.001))
cells = pl.DataFrame(rows, orient="row", schema=["EVENTID", "LOBNAME", "REGION", "PERSPVALUE",
                                                  "STDDEVI", "STDDEVC", "EXPVALUE", "RATE"])
stages = [InuringLayer(2e6, 6e6, 0.9, make_subject([("HO", ["US_FL"])])),
          [InuringLayer(3e6, 4e6, 1.0, make_subject([("HO", [])])),
           InuringLayer(7e6, 8e6, 0.95, make_subject([("HO", [])]))]]
for mode in ["prorata", "conditional"]:
    nc, inur = apply_inuring(cells, stages, sharing=mode)
    ho_c = subject_event_elt(nc, make_subject([("HO", [])])).sort("EVENTID")
    ho_s1 = subject_event_elt(apply_inuring(cells, stages[:1], sharing=mode)[0],
                              make_subject([("HO", [])])).sort("EVENTID")
    it2 = subject_integrals(ho_s1["PERSPVALUE"], ho_s1["STDDEVI"] + ho_s1["STDDEVC"],
                            ho_s1["EXPVALUE"], [(3e6, 4e6, 1.0), (7e6, 8e6, 0.95)])
    err_m = np.max(np.abs(ho_c["PERSPVALUE"].to_numpy() / it2["EN"] - 1))
    err_s = np.max(np.abs((ho_c["STDDEVI"] + ho_c["STDDEVC"]).to_numpy() / it2["SDN"] - 1))
    fl = nc.filter((pl.col("LOBNAME") == "HO")).group_by("REGION").agg(
        (pl.col("PERSPVALUE") * pl.col("RATE")).sum().alias("AAL")).sort("REGION")
    print(f"   {mode:<11} HO subject net mean max rel err {err_m:.1e}, SD {err_s:.1e};  "
          f"HO net AAL by region: " + ", ".join(f"{r} {v:,.0f}" for r, v in fl.iter_rows()))
    neg = (nc["STDDEVC"] < 0).sum()
    print(f"               cells with negative net STDDEVC: {neg}")
