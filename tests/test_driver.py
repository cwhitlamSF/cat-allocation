"""Chunked driver vs the in-memory pipeline on a synthetic book written as Parquet chunks."""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_os.chdir(_os.path.dirname(_os.path.abspath(__file__)))

import os
import tempfile

import numpy as np
import polars as pl

from catalloc.driver import run_allocation
from catalloc.inuring import (apply_inuring, apply_inuring_to_policies, apply_per_risk, make_subject,
                     per_risk_ratios, split_policies_by_region, stages_from_table,
                     subject_event_elt, subject_policies)
from catalloc.distortion import allocate_to_policies, layer_event_weights, premium_by_policy

rng = np.random.default_rng(5)
regions = ["US_FL", "US_GA", "US_SC", "US_NC", "US_TX", "US_LA"]
lobs = ["HO", "CO"]

# ---- synthetic book ---------------------------------------------------------
exp_rows, pol_lob = [], {}
for p in range(400):
    pol_lob[p] = str(rng.choice(lobs))
    for r in rng.choice(regions, rng.integers(1, 4), replace=False):
        exp_rows.append((p, str(r), rng.lognormal(16, 1)))
exposure = pl.DataFrame(exp_rows, schema=["POLICYID", "REGION", "TIV"], orient="row")

n_ev = 500
rate = rng.gamma(0.5, 0.004, n_ev)
true_rows = []
for e in range(n_ev):
    start = rng.integers(0, len(regions))
    hit = {regions[(start + k) % len(regions)]: rng.lognormal(-5.5, 1.2)
           for k in range(rng.integers(1, 4))}
    for p, r, tiv in exp_rows:
        if r in hit and rng.random() < 0.8:
            m = tiv * hit[r] * rng.lognormal(0, 0.5)
            true_rows.append((e, p, pol_lob[p], r, m, m * rng.uniform(.3, .9), m * rng.uniform(.2, .6)))
truth = pl.DataFrame(true_rows, orient="row", schema=[
    "EVENTID", "POLICYID", "LOBNAME", "REGION", "PERSPVALUE", "STDDEVI", "STDDEVC"])

rms = [pl.col("PERSPVALUE").sum(), (pl.col("STDDEVI") ** 2).sum().sqrt().alias("STDDEVI"),
       pl.col("STDDEVC").sum()]
policies = truth.group_by(["EVENTID", "POLICYID", "LOBNAME"]).agg(rms)
rate_df = pl.DataFrame({"EVENTID": np.arange(n_ev), "RATE": rate})
gross_cells = (truth.group_by(["EVENTID", "LOBNAME", "REGION"]).agg(rms)
               .with_columns((pl.col("PERSPVALUE") * 8).alias("EXPVALUE"))
               .join(rate_df, on="EVENTID"))
# net of per-risk: a random factor per cell (so BU-level ratios are an approximation)
f = rng.uniform(0.7, 1.0, gross_cells.height)
cells = gross_cells.with_columns(
    (pl.col("PERSPVALUE") * f).alias("PERSPVALUE"), (pl.col("STDDEVC") * f).alias("STDDEVC"),
    (pl.col("STDDEVI") * f).alias("STDDEVI"), (pl.col("EXPVALUE") * f).alias("EXPVALUE"))

# ---- programme -------------------------------------------------------------
subj = {"FHCF": make_subject([("HO", ["US_FL"])]),
        "HO": make_subject([("HO", [])]),
        "ALL": make_subject([]),
        "CO": make_subject([("CO", [])]),
        "HO_FLGA": make_subject([("HO", ["US_FL", "US_GA"])])}
inuring = pl.DataFrame({"Stage": [1, 2, 2, 2], "Layer": ["FHCF", "HO 1", "HO 2", "CO 1"],
                        "Limit": [20e6, 5e6, 10e6, 8e6], "Retention": [5e6, 3e6, 8e6, 4e6],
                        "Placement %": [0.9, 1.0, 0.95, 1.0], "Subject": ["FHCF", "HO", "HO", "CO"]})
stages = stages_from_table(inuring, subj)
upper = pl.DataFrame({"Layer": ["U1", "U2", "U3"], "Retention": [10e6, 25e6, 4e6],
                      "Limit": [15e6, 25e6, 6e6], "Subject": ["ALL", "ALL", "HO_FLGA"],
                      "Premium": [5e6, 1.5e6, 1.0666667e6], "Placement %": [0.6, 1.0, 0.75]})
placed = {"U1": 0.6, "U2": 1.0, "U3": 0.75}

# ---- write chunks in shuffled order ----------------------------------------
folder = tempfile.mkdtemp()
shuffled = policies.sample(fraction=1.0, shuffle=True, seed=1)
for i, part in enumerate(shuffled.iter_slices(n_rows=20_000)):
    part.write_parquet(os.path.join(folder, f"pol_part_{i:03d}.parquet"))
print(f"{policies.height:,} policy-event rows in "
      f"{len([x for x in os.listdir(folder) if x.startswith('pol_part')])} chunks")

results = {}
for sharing, method, dist in [("conditional", "conditional", "wang"),
                              ("conditional", "mean", "wang"),
                              ("prorata", "conditional", "wang"),
                              ("conditional", "conditional", "ph"),
                              ("conditional", "conditional", "identity")]:
    first = (sharing, method, dist) == ("conditional", "conditional", "wang")
    by_pl, by_pol, info, diag = run_allocation(
        folder, exposure, gross_cells, cells, stages, upper, subj, method=method,
        sharing=sharing, distortion=dist, verbose=first)
    results[(sharing, method, dist)] = by_pol
    if first:
        print(info)
        print("diagnostics:", diag)

    # in-memory reference
    pieces, _ = split_policies_by_region(policies, exposure, gross_cells, lobs=["HO"])
    net_cells, inur = apply_inuring(cells, stages, sharing=sharing)
    pol = apply_inuring_to_policies(apply_per_risk(pieces, per_risk_ratios(gross_cells, cells)),
                                    inur)
    worst = 0.0
    for r in upper.iter_rows(named=True):
        s = subj[r["Subject"]]
        ew, _ = layer_event_weights(subject_event_elt(net_cells, s), r["Retention"], r["Limit"],
                                    premium=r["Premium"], distortion=dist)
        ref = premium_by_policy(
            allocate_to_policies(
                subject_policies(pol, s).group_by(["EVENTID", "POLICYID"]).agg(
                    pl.col("PERSPVALUE").sum(), pl.col("STDDEVC").sum(),
                    (pl.col("STDDEVI") ** 2).sum().sqrt()),
                ew, method, bounded=True),
            r["Premium"] * placed[r["Layer"]])
        got = by_pl.filter(pl.col("Layer") == r["Layer"])
        cmp = ref.join(got, on="POLICYID", how="full", coalesce=True, suffix="_D").fill_null(0)
        worst = max(worst, (cmp["PREMIUM"] - cmp["PREMIUM_D"]).abs().max())
    print(f"{dist:<8} sharing={sharing:<11} split={method:<11} max |driver - in-memory| premium per "
          f"policy: {worst:.2e};  total allocated {by_pol['PREMIUM'].sum():,.2f}")

# effect of the sharing rule on the FHCF business unit
lob = pl.DataFrame(list(pol_lob.items()), schema=["POLICYID", "LOBNAME"], orient="row")
fl = (exposure.group_by("POLICYID").agg(
          (pl.col("TIV").filter(pl.col("REGION") == "US_FL").sum() / pl.col("TIV").sum())
          .alias("FL_SHARE")))
a = results[("prorata", "conditional", "wang")].rename({"PREMIUM": "PRORATA"})
b = results[("conditional", "conditional", "wang")].rename({"PREMIUM": "CORECOVERY"})
cmp = (a.join(b, on="POLICYID").join(lob, on="POLICYID").join(fl, on="POLICYID")
       .with_columns(pl.when(pl.col("FL_SHARE") == 0).then(pl.lit("no FL"))
                     .when(pl.col("FL_SHARE") < 1).then(pl.lit("part FL"))
                     .otherwise(pl.lit("all FL")).alias("SEGMENT")))
print("\nAllocated premium by segment, pro rata vs co-recovery sharing of inuring recoveries:")
print(cmp.group_by(["LOBNAME", "SEGMENT"]).agg(pl.col("PRORATA").sum(), pl.col("CORECOVERY").sum())
      .with_columns(((pl.col("CORECOVERY") / pl.col("PRORATA") - 1) * 100).round(2).alias("CHANGE_%"))
      .sort(["LOBNAME", "SEGMENT"]))
chg = (cmp["CORECOVERY"] / cmp["PRORATA"] - 1).abs()
print(f"policy-level change: median {chg.median():.2%}, 90th pct {chg.quantile(0.9):.2%}, "
      f"max {chg.max():.2%}")


# effect of the distortion: Wang vs PH vs expected-loss key, by segment
w = results[("conditional", "conditional", "wang")].rename({"PREMIUM": "WANG"})
p_ = results[("conditional", "conditional", "ph")].rename({"PREMIUM": "PH"})
i_ = results[("conditional", "conditional", "identity")].rename({"PREMIUM": "EL_KEY"})
seg = (w.join(p_, on="POLICYID").join(i_, on="POLICYID").join(lob, on="POLICYID")
       .join(fl, on="POLICYID")
       .with_columns(pl.when(pl.col("FL_SHARE") == 0).then(pl.lit("no FL"))
                     .when(pl.col("FL_SHARE") < 1).then(pl.lit("part FL"))
                     .otherwise(pl.lit("all FL")).alias("SEGMENT")))
print("\nAllocated premium by segment: expected-loss key vs Wang vs PH:")
print(seg.group_by(["LOBNAME", "SEGMENT"]).agg(pl.col("EL_KEY").sum(), pl.col("WANG").sum(),
                                               pl.col("PH").sum()).sort(["LOBNAME", "SEGMENT"]))
chg = (seg["WANG"] / seg["EL_KEY"] - 1)
print(f"policy-level Wang vs EL key: median {chg.median():+.1%}, 10th pct {chg.quantile(0.1):+.1%}, "
      f"90th pct {chg.quantile(0.9):+.1%}")
chg = (seg["PH"] / seg["WANG"] - 1).abs()
print(f"policy-level |PH vs Wang|: median {chg.median():.2%}, 90th pct {chg.quantile(0.9):.2%}, "
      f"max {chg.max():.2%}")


# ---- selective split: same answer as splitting every LOB ---------------------
print("\nSelective split (auto: HO only) vs splitting every LOB:")
for upper_case, up in [("all upper subjects", upper)]:
    a_, b_, ia, da = run_allocation(folder, exposure, gross_cells, cells, stages, up, subj,
                                    split_lobs="auto", verbose=True)
    c_, d_, ic, dc = run_allocation(folder, exposure, gross_cells, cells, stages, up, subj,
                                    split_lobs="all", verbose=False)
    cmp = b_.join(d_, on="POLICYID", suffix="_ALL")
    print(f"   max |auto - all| premium per policy: {(cmp['PREMIUM'] - cmp['PREMIUM_ALL']).abs().max():.2e}")
    print(f"   rows looked up in exposure: auto {da['rows_split_lobs'] + da['rows_label_lobs']:,} of {da['rows_in']:,};  "
          f"multi-region rows split: auto {da['rows_split_multi_region']:,}, all {dc['rows_split_multi_region']:,}")

# a program with no region-restricted subject needs no exposure data at all
up2 = upper.filter(pl.col("Subject") != "HO_FLGA")
stages_nofl = stages_from_table(inuring.filter(pl.col("Subject") != "FHCF"), subj)
_, b2, _, d2 = run_allocation(folder, None, gross_cells, cells, stages_nofl, up2, subj, verbose=False)
_, b3, _, _ = run_allocation(folder, exposure, gross_cells, cells, stages_nofl, up2, subj,
                             split_lobs="all", verbose=False)
cmp = b2.join(b3, on="POLICYID", suffix="_ALL")
print(f"No regional subjects, exposure=None: max |none - all| {(cmp['PREMIUM'] - cmp['PREMIUM_ALL']).abs().max():.2e}; "
      f"rows looked up {d2['rows_split_lobs'] + d2['rows_label_lobs']}")
