"""Checks for inuring.py: subject filters, exact net moments, ties, end-to-end."""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_os.chdir(_os.path.dirname(_os.path.abspath(__file__)))

import numpy as np
import polars as pl
from scipy import stats

from catalloc.inuring import (
    InuringLayer, apply_inuring, apply_inuring_to_policies, make_subject, net_moments,
    subject_event_elt, subject_expr, subject_policies,
)
from catalloc.distortion import allocate_to_policies, beta_params, layer_event_weights, premium_by_policy

rng = np.random.default_rng(11)

# ---- 1. subject filter semantics ------------------------------------------
cells_demo = pl.DataFrame({
    "LOBNAME": ["HO", "HO", "CO", "CO", "AU"],
    "REGION": ["US_FL", "US_TX", "US_FL", "US_LA", "US_TX"],
})
cases = {
    "empty df -> all": make_subject([]),
    "HO, all regions": make_subject([("HO", [])]),
    "all LOBs, FL": make_subject([("", ["US_FL"])]),
    "null LOB, FL/LA": pl.DataFrame({"LOBNAME": [None], "REGIONS": [["US_FL", "US_LA"]]},
                                     schema={"LOBNAME": pl.Utf8, "REGIONS": pl.List(pl.Utf8)}),
    "HO FL or CO all": make_subject([("HO", ["US_FL"]), ("CO", [])]),
}
print("1. Subject filters")
for name, subj in cases.items():
    sel = cells_demo.filter(subject_expr(subj))
    print(f"   {name:<18} -> {list(zip(sel['LOBNAME'], sel['REGION']))}")

# ---- 2. exact net moments vs Monte Carlo ----------------------------------
print("\n2. Net moments: exact vs 4M-draw Monte Carlo")
layers = [(4e6, 6e6, 0.9), (10e6, 10e6, 0.75)]       # a two-layer tower
for mean, sd, E in [(3e6, 4e6, 40e6), (8e6, 6e6, 30e6), (12e6, 5e6, 25e6)]:
    m1, s1, nE = net_moments([mean], [sd], [E], layers)
    a, b = beta_params(np.array([mean]), np.array([sd]), np.array([E]))
    s = stats.beta.rvs(a[0], b[0], size=4_000_000, random_state=rng) * E
    n = s.copy()
    for att, lim, p in layers:
        n -= p * np.clip(s - att, 0, lim)
    print(f"   mean {mean/1e6:>4.0f}m sd {sd/1e6:>2.0f}m:  E[N] {m1[0]:>12,.0f} vs {n.mean():>12,.0f}"
          f"   SD[N] {s1[0]:>12,.0f} vs {n.std():>12,.0f}")

# ---- 3. synthetic policy ELT with LOB/region ------------------------------
lobs = ["HO", "CO", "AU"]
regions = ["US_FL", "US_TX", "US_LA", "US_SC"]
n_ev = 300
pol_meta = [(p, rng.choice(lobs), rng.choice(regions)) for p in range(60)]
rate = rng.gamma(0.5, 0.004, n_ev)
rows = []
for e in range(n_ev):
    sev = rng.lognormal(14.5, 1.3)
    hit = rng.choice(len(pol_meta), rng.integers(5, 40), replace=False)
    w = rng.dirichlet(np.ones(len(hit)))
    for h, wi in zip(hit, w):
        p, lob, reg = pol_meta[h]
        m = sev * wi
        rows.append((e, p, lob, reg, m, m * rng.uniform(0.3, 0.9), m * rng.uniform(0.2, 0.6), m * 10))
policies = pl.DataFrame(rows, orient="row", schema=[
    "EVENTID", "POLICYID", "LOBNAME", "REGION", "PERSPVALUE", "STDDEVI", "STDDEVC", "EXPVALUE"])

rate_df = pl.DataFrame({"EVENTID": np.arange(n_ev), "RATE": rate})
cells = (policies.group_by(["EVENTID", "LOBNAME", "REGION"]).agg(
            pl.col("PERSPVALUE").sum(),
            (pl.col("STDDEVI") ** 2).sum().sqrt().alias("STDDEVI"),
            pl.col("STDDEVC").sum(), pl.col("EXPVALUE").sum())
         .join(rate_df, on="EVENTID"))

# Stage 1: FHCF proxy on HO x FL.  Stage 2: HO lower tower (two layers), all regions.
fhcf = InuringLayer(3e6, 8e6, 0.90, make_subject([("HO", ["US_FL"])]), "FHCF proxy")
ho_1 = InuringLayer(2e6, 3e6, 1.00, make_subject([("HO", [])]), "HO 3x2")
ho_2 = InuringLayer(5e6, 10e6, 0.80, make_subject([("HO", [])]), "HO 10x5")
net_cells, ratios = apply_inuring(cells, [fhcf, [ho_1, ho_2]])

print("\n3. Ties")
# (a) re-aggregated HO subject reproduces the tower's net moments
ho_before = subject_event_elt(apply_inuring(cells, [fhcf])[0], ho_1.subject)
m1, s1, _ = net_moments(ho_before["PERSPVALUE"], ho_before["STDDEVI"] + ho_before["STDDEVC"],
                        ho_before["EXPVALUE"],
                        [(l.attachment, l.limit, l.placement) for l in (ho_1, ho_2)])
ho_after = subject_event_elt(net_cells, ho_1.subject).sort("EVENTID")
print(f"   HO subject net mean max rel err "
      f"{np.max(np.abs(ho_after['PERSPVALUE'].to_numpy() / m1 - 1)):.1e}, "
      f"SD max rel err "
      f"{np.max(np.abs((ho_after['STDDEVI'] + ho_after['STDDEVC']).to_numpy() / s1 - 1)):.1e}")

# (b) scaled policies re-aggregate to the net cells
pol_net = apply_inuring_to_policies(policies, ratios)
chk = (pol_net.group_by(["EVENTID", "LOBNAME", "REGION"]).agg(
            pl.col("PERSPVALUE").sum().alias("P_MEAN"),
            pl.col("STDDEVC").sum().alias("P_SDC"),
            (pl.col("STDDEVI") ** 2).sum().sqrt().alias("P_SDI"))
       .join(net_cells, on=["EVENTID", "LOBNAME", "REGION"]))
for a_, b_ in [("P_MEAN", "PERSPVALUE"), ("P_SDC", "STDDEVC"), ("P_SDI", "STDDEVI")]:
    print(f"   policies vs net cells, {b_:<10} max rel err "
          f"{(chk[a_] / chk[b_] - 1).abs().max():.1e}")

# (c) non-subject cells untouched
untouched = (net_cells.filter(pl.col("LOBNAME") != "HO")
             .join(cells, on=["EVENTID", "LOBNAME", "REGION"], suffix="_G"))
print(f"   non-HO cells changed: {(untouched['PERSPVALUE'] != untouched['PERSPVALUE_G']).sum()}")

ho_aal_g = cells.filter(pl.col("LOBNAME") == "HO").select((pl.col("PERSPVALUE") * pl.col("RATE")).sum()).item()
ho_aal_n = net_cells.filter(pl.col("LOBNAME") == "HO").select((pl.col("PERSPVALUE") * pl.col("RATE")).sum()).item()
print(f"   HO AAL {ho_aal_g:,.0f} -> {ho_aal_n:,.0f} after FHCF + lower tower")

# ---- 4. upper tower, end to end --------------------------------------------
print("\n4. Upper tower 20m xs 10m, all LOBs/regions, Wang key calibrated to 2m premium")
upper_subject = make_subject([])
upper_elt = subject_event_elt(net_cells, upper_subject)
ew, info = layer_event_weights(upper_elt, 10e6, 20e6, premium=2_000_000)
print(f"   P(attach) {info['P_attach']:.4f}  EL {info['expected_loss']:,.0f}  "
      f"Wang theta {info['theta']:.4f}  load {info['load_ratio']:.2f}x")
pe = allocate_to_policies(subject_policies(pol_net, upper_subject), ew, "conditional")
prem = premium_by_policy(pe, 2_000_000)
by_lob = (prem.join(pl.DataFrame(pol_meta, schema=["POLICYID", "LOBNAME", "REGION"], orient="row"),
                    on="POLICYID")
          .group_by("LOBNAME").agg(pl.col("PREMIUM").sum()).sort("LOBNAME"))
print(by_lob)
print("   total:", round(prem["PREMIUM"].sum(), 2))
