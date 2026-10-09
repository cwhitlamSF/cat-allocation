"""Checks for the "Multiple Regions Per Policy" flag."""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_os.chdir(_os.path.dirname(_os.path.abspath(__file__)))

import os
import tempfile
import warnings

import numpy as np
import polars as pl

from catalloc.driver import run_allocation
from catalloc.inuring import make_subject, region_requirements, stages_from_table

warnings.filterwarnings("ignore")
rng = np.random.default_rng(9)
regions = ["US_FL", "US_GA", "US_SC", "US_NC", "US_TX", "US_LA"]

# ---- 1. flag logic ----------------------------------------------------------
print("1. region_requirements: (split LOBs, region-only LOBs)")
fl, all_ = make_subject([("HO", ["US_FL"])]), make_subject([("HO", [])])
blank = make_subject([("", ["US_FL"])])
for name, pairs in [("FHCF flagged False", [(fl, False), (all_, True)]),
                    ("FHCF flagged True", [(fl, True)]),
                    ("False on one layer, True on another", [(fl, False), (fl, True)]),
                    ("blank LOB with regions, False", [(blank, False)]),
                    ("blank LOB with regions, True", [(blank, True)]),
                    ("no regional subjects", [(all_, True)])]:
    print(f"   {name:<38} -> {region_requirements(pairs)}")

# ---- 2. synthetic book: HO single-region, CO multi-region --------------------
def book(ho_multi_policies=0):
    exp_rows, pol_lob = [], {}
    for p in range(300):
        lob = "HO" if p < 150 else "CO"
        pol_lob[p] = lob
        k = 1 if (lob == "HO" and p >= ho_multi_policies) else rng.integers(1, 4)
        if lob == "HO" and p < ho_multi_policies:
            k = 2
        for r in rng.choice(regions, k, replace=False):
            exp_rows.append((p, str(r), rng.lognormal(16, 1)))
    exposure = pl.DataFrame(exp_rows, schema=["POLICYID", "REGION", "TIV"], orient="row")
    n_ev = 300
    rate = rng.gamma(0.5, 0.004, n_ev)
    rows = []
    for e in range(n_ev):
        start = rng.integers(0, len(regions))
        hit = {regions[(start + k) % len(regions)]: rng.lognormal(-5.5, 1.2)
               for k in range(rng.integers(1, 4))}
        for p, r, tiv in exp_rows:
            if r in hit and rng.random() < 0.8:
                m = tiv * hit[r] * rng.lognormal(0, 0.5)
                rows.append((e, p, pol_lob[p], r, m, m * rng.uniform(.3, .9), m * rng.uniform(.2, .6)))
    truth = pl.DataFrame(rows, orient="row", schema=["EVENTID", "POLICYID", "LOBNAME", "REGION",
                                                     "PERSPVALUE", "STDDEVI", "STDDEVC"])
    rms = [pl.col("PERSPVALUE").sum(), (pl.col("STDDEVI") ** 2).sum().sqrt().alias("STDDEVI"),
           pl.col("STDDEVC").sum()]
    policies = truth.group_by(["EVENTID", "POLICYID", "LOBNAME"]).agg(rms)
    gross = (truth.group_by(["EVENTID", "LOBNAME", "REGION"]).agg(rms)
             .with_columns((pl.col("PERSPVALUE") * 8).alias("EXPVALUE"))
             .join(pl.DataFrame({"EVENTID": np.arange(n_ev), "RATE": rate}), on="EVENTID"))
    f = rng.uniform(0.7, 1.0, gross.height)
    net = gross.with_columns(*[(pl.col(c) * f).alias(c)
                               for c in ["PERSPVALUE", "STDDEVC", "STDDEVI", "EXPVALUE"]])
    folder = tempfile.mkdtemp()
    for i, part in enumerate(policies.sample(fraction=1.0, shuffle=True, seed=2).iter_slices(10_000)):
        part.write_parquet(os.path.join(folder, f"pol_part_{i:03d}.parquet"))
    return folder, exposure, gross, net


subj = {"FHCF": make_subject([("HO", ["US_FL"])]), "HO": make_subject([("HO", [])]),
        "CO": make_subject([("CO", [])]), "ALL": make_subject([])}
upper = pl.DataFrame({"Layer": ["U1", "U2"], "Retention": [6e6, 15e6], "Limit": [9e6, 15e6],
                      "Subject": ["ALL", "ALL"], "Premium": [2e6, 1e6]})


def inuring(flag):
    return pl.DataFrame({"Stage": [1, 2, 2], "Layer": ["FHCF", "HO 1", "CO 1"],
                         "Limit": [12e6, 5e6, 6e6], "Retention": [3e6, 2e6, 3e6],
                         "Placement %": [0.9, 1.0, 1.0], "Subject": ["FHCF", "HO", "CO"],
                         "Multiple Regions Per Policy": [flag, None, None]})


print("\n2. HO single-region book: FHCF flagged False vs True")
folder, exposure, gross, net = book()
st_f = stages_from_table(inuring("FALSE"), subj)
st_t = stages_from_table(inuring(True), subj)
print(f"   parsed flags: {[l.multi_region for s in st_f for l in s]}")
_, a, _, da = run_allocation(folder, exposure, gross, net, st_f, upper, subj, verbose=True)
_, b, _, db = run_allocation(folder, exposure, gross, net, st_t, upper, subj, verbose=True)
cmp = a.join(b, on="POLICYID", suffix="_T")
print(f"   max |False - True| premium per policy: {(cmp['PREMIUM'] - cmp['PREMIUM_T']).abs().max():.2e}")
print(f"   False: rows split-path {da['rows_split_lobs']:,}, region-only {da['rows_label_lobs']:,}, "
      f"multi-region rows split {da['rows_split_multi_region']}")
print(f"   True:  rows split-path {db['rows_split_lobs']:,}, region-only {db['rows_label_lobs']:,}, "
      f"multi-region rows split {db['rows_split_multi_region']}")

print("\n3. Dirty data: 5 HO policies with two regions, FHCF flagged False")
folder, exposure, gross, net = book(ho_multi_policies=5)
_, a, _, da = run_allocation(folder, exposure, gross, net, st_f, upper, subj, verbose=True)
print(f"   diag label_policies_multi_region = {da['label_policies_multi_region']}; "
      f"total allocated {a['PREMIUM'].sum():,.2f}")
